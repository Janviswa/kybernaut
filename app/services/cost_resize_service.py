"""Approval-gated, lab-only Deployment request resizing."""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Callable

from app.services.cost_telemetry import (
    CostTelemetryError,
    parse_cpu_quantity,
    parse_memory_quantity,
)
from app.services.k8s_observer import LAB_CONTEXT, LAB_NAMESPACE
from app.services.policy_engine import PolicyConfig

MIN_CPU_CORES = 0.01
MIN_MEMORY_BYTES = 16 * 1024 ** 2
MAX_CPU_CORES = 8.0
MAX_MEMORY_BYTES = 32 * 1024 ** 3
COMMAND_TIMEOUT_SECONDS = 120
MIB = 1024 ** 2
DEFAULT_MAX_REPORT_AGE_SECONDS = 3600
EXECUTION_STAGES = {
    "AUTHORIZED_NOT_ATTEMPTED",
    "PREFLIGHT_VALIDATION",
    "PREFLIGHT_FAILED",
    "PATCH_ATTEMPT_STARTED",
    "PATCH_REJECTED",
    "PATCH_OUTCOME_UNKNOWN",
    "PATCH_ACCEPTED_VERIFICATION_PENDING",
    "EXECUTION_VERIFIED",
    "VERIFICATION_FAILED",
    "RECONCILIATION_STARTED",
    "RECONCILED_ORIGINAL_STATE",
    "RECONCILED_PROPOSED_STATE",
    "RECONCILIATION_UNRESOLVED",
}
DEFINITIVE_REJECTION = re.compile(
    r"(?i)Error from server \((?:BadRequest|Forbidden|Invalid|NotFound|"
    r"Unauthorized|Conflict)\):"
)


class CostResizeError(ValueError):
    def __init__(
        self, code: str, message: str, *, execution_stage: str | None = None
    ):
        super().__init__(message)
        self.code = code
        self.execution_stage = execution_stage


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _freshness_limits(sample_interval_minutes: object) -> tuple[int, int]:
    if (
        isinstance(sample_interval_minutes, bool)
        or not isinstance(sample_interval_minutes, int)
        or sample_interval_minutes < 1
    ):
        raise CostResizeError(
            "invalid_freshness_config",
            "Resize evidence sample interval is invalid.",
            execution_stage="PREFLIGHT_FAILED",
        )
    try:
        report_age = int(os.getenv(
            "KYBERNAUT_COST_RESIZE_MAX_REPORT_AGE_SECONDS",
            str(DEFAULT_MAX_REPORT_AGE_SECONDS),
        ))
        evidence_age = int(os.getenv(
            "KYBERNAUT_COST_RESIZE_MAX_EVIDENCE_AGE_SECONDS",
            str(sample_interval_minutes * 120),
        ))
    except ValueError as exc:
        raise CostResizeError(
            "invalid_freshness_config",
            "Resize evidence freshness limits must be integer seconds.",
            execution_stage="PREFLIGHT_FAILED",
        ) from exc
    if not 1 <= report_age <= 86400 or not 1 <= evidence_age <= 604800:
        raise CostResizeError(
            "invalid_freshness_config",
            "Resize freshness limits are outside their supported bounds.",
            execution_stage="PREFLIGHT_FAILED",
        )
    return report_age, evidence_age


def _parse_aware_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise CostResizeError(
            "stale_evidence",
            f"Resize evidence {field} timestamp is missing or malformed.",
            execution_stage="PREFLIGHT_FAILED",
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CostResizeError(
            "stale_evidence",
            f"Resize evidence {field} timestamp is missing or malformed.",
            execution_stage="PREFLIGHT_FAILED",
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CostResizeError(
            "stale_evidence",
            f"Resize evidence {field} timestamp must include a timezone.",
            execution_stage="PREFLIGHT_FAILED",
        )
    return parsed.astimezone(timezone.utc)


def _validate_evidence_freshness(
    *,
    generated_at: object,
    period_start: object,
    period_end: object,
    first_observation: object,
    latest_observation: object,
    sample_interval_minutes: object,
    now: datetime | None = None,
) -> dict:
    report_age_limit, evidence_age_limit = _freshness_limits(
        sample_interval_minutes
    )
    generated = _parse_aware_timestamp(generated_at, "generation")
    started = _parse_aware_timestamp(period_start, "period-start")
    ended = _parse_aware_timestamp(period_end, "period-end")
    first = _parse_aware_timestamp(first_observation, "first-observation")
    observed = _parse_aware_timestamp(latest_observation, "latest-observation")
    current = now or _utc_now()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    current = current.astimezone(timezone.utc)
    if any(timestamp > current for timestamp in (
        generated, started, ended, first, observed
    )):
        raise CostResizeError(
            "stale_evidence",
            "Resize evidence contains a future timestamp.",
            execution_stage="PREFLIGHT_FAILED",
        )
    if started >= ended or generated < ended or not started <= first <= observed < ended:
        raise CostResizeError(
            "stale_evidence",
            "Resize observation timestamps do not match the report period.",
            execution_stage="PREFLIGHT_FAILED",
        )
    if (current - generated).total_seconds() > report_age_limit:
        raise CostResizeError(
            "stale_evidence",
            "Cost report is too old to authorize a resize.",
            execution_stage="PREFLIGHT_FAILED",
        )
    if (current - ended).total_seconds() > evidence_age_limit:
        raise CostResizeError(
            "stale_evidence",
            "Cost reporting period ended too long ago to authorize a resize.",
            execution_stage="PREFLIGHT_FAILED",
        )
    if (current - observed).total_seconds() > evidence_age_limit:
        raise CostResizeError(
            "stale_evidence",
            "Latest utilization observation is too old to authorize a resize.",
            execution_stage="PREFLIGHT_FAILED",
        )
    return {
        "max_report_age_seconds": report_age_limit,
        "max_evidence_age_seconds": evidence_age_limit,
    }


def validate_resize_plan_freshness(
    plan: dict, *, now: datetime | None = None
) -> None:
    period = plan.get("reporting_period")
    evidence = plan.get("evidence")
    if not isinstance(period, dict) or not isinstance(evidence, dict):
        raise CostResizeError(
            "stale_evidence",
            "Resize plan is missing timestamped evidence.",
            execution_stage="PREFLIGHT_FAILED",
        )
    _validate_evidence_freshness(
        generated_at=plan.get("report_generated_at"),
        period_start=period.get("start"),
        period_end=period.get("end_exclusive"),
        first_observation=evidence.get("first_observed_at"),
        latest_observation=evidence.get("last_observed_at"),
        sample_interval_minutes=period.get("sample_interval_minutes"),
        now=now,
    )


def _emit_stage(
    callback: Callable[[str], None] | None,
    stage: str,
    *,
    failure_stage: str,
    mutation_may_have_started: bool = False,
) -> None:
    if callback is None:
        return
    try:
        callback(stage)
    except Exception as exc:
        raise CostResizeError(
            "stage_persistence_failed",
            "Resize execution stage could not be durably recorded.",
            execution_stage=(
                "PATCH_OUTCOME_UNKNOWN"
                if mutation_may_have_started else failure_stage
            ),
        ) from exc


class ResizeRunner:
    def __call__(self, args: list[str], *, timeout: int) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["kubectl", "--context", LAB_CONTEXT, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )


def _quantity_limits() -> tuple[float, float, float, float]:
    try:
        minimum_cpu = float(os.getenv(
            "KYBERNAUT_COST_RESIZE_MIN_CPU_CORES", str(MIN_CPU_CORES)
        ))
        maximum_cpu = float(os.getenv(
            "KYBERNAUT_COST_RESIZE_MAX_CPU_CORES", str(MAX_CPU_CORES)
        ))
        minimum_memory = float(os.getenv(
            "KYBERNAUT_COST_RESIZE_MIN_MEMORY_BYTES", str(MIN_MEMORY_BYTES)
        ))
        maximum_memory = float(os.getenv(
            "KYBERNAUT_COST_RESIZE_MAX_MEMORY_BYTES", str(MAX_MEMORY_BYTES)
        ))
    except ValueError as exc:
        raise CostResizeError(
            "invalid_limits", "Kubernetes resize bounds are invalid"
        ) from exc
    bounds = (minimum_cpu, maximum_cpu, minimum_memory, maximum_memory)
    if any(not math.isfinite(value) or value <= 0 for value in bounds):
        raise CostResizeError(
            "invalid_limits", "Kubernetes resize bounds must be finite and positive"
        )
    if minimum_cpu > maximum_cpu or minimum_memory > maximum_memory:
        raise CostResizeError(
            "invalid_limits", "Kubernetes resize minimum exceeds maximum"
        )
    return bounds


def _command_timeout() -> int:
    try:
        timeout = int(os.getenv(
            "KYBERNAUT_COST_RESIZE_TIMEOUT_SECONDS",
            str(COMMAND_TIMEOUT_SECONDS),
        ))
    except ValueError as exc:
        raise CostResizeError(
            "invalid_timeout", "Kubernetes resize timeout is invalid"
        ) from exc
    if not 1 <= timeout <= 600:
        raise CostResizeError(
            "invalid_timeout",
            "Kubernetes resize timeout must be between 1 and 600 seconds",
        )
    return timeout


def _millicores(value: float) -> str:
    return f"{math.ceil(value * 1000 - 1e-9)}m"


def _mebibytes(value: float) -> str:
    return f"{math.ceil(value / MIB - 1e-9)}Mi"


def build_resize_plan(report: dict, workload: str) -> dict:
    """Derive a single-container-per-pod plan from a current report."""
    allowed = PolicyConfig().allowed_deployments
    if workload not in allowed:
        raise CostResizeError("out_of_scope", "Workload is not policy-allowlisted")
    if report.get("pricing", {}).get("model") != "kubernetes_requests":
        raise CostResizeError(
            "pricing_required", "A compatible Kubernetes request pricing model is required"
        )
    if (
        report.get("pricing", {}).get("cost_model_status") != "configured"
        or report.get("pricing", {}).get("example")
    ):
        raise CostResizeError(
            "pricing_required",
            "Resize approval requires a configured non-example pricing model",
        )
    rows = report.get("recommendations")
    if not isinstance(rows, list):
        raise CostResizeError("invalid_report", "Cost recommendations are unavailable")
    resource_name = f"{LAB_NAMESPACE}/{workload}"
    matches = [
        row for row in rows
        if isinstance(row, dict) and row.get("resource") == resource_name
    ]
    if len(matches) != 1:
        raise CostResizeError(
            "invalid_report", "Exactly one matching workload recommendation is required"
        )
    row = matches[0]
    if row.get("status") != "request_downsize_candidate":
        raise CostResizeError(
            "not_recommended", "Workload does not have a safe downsize recommendation"
        )
    period = report.get("reporting_period") or {}
    if not isinstance(period, dict):
        raise CostResizeError(
            "stale_evidence",
            "Cost report is missing a valid reporting period.",
            execution_stage="PREFLIGHT_FAILED",
        )
    freshness = _validate_evidence_freshness(
        generated_at=report.get("generated_at"),
        period_start=period.get("start"),
        period_end=period.get("end_exclusive"),
        first_observation=row.get("first_observed_at"),
        latest_observation=row.get("last_observed_at"),
        sample_interval_minutes=period.get("sample_interval_minutes"),
    )
    if (
        row.get("coverage", 0) < period.get("minimum_coverage", 1)
        or row.get("sample_count", 0) < 24
        or not row.get("first_observed_at")
        or not row.get("last_observed_at")
    ):
        raise CostResizeError(
            "insufficient_evidence", "Recommendation evidence is incomplete"
        )
    replicas = row.get("replicas")
    identities = row.get("container_identity")
    if (
        isinstance(replicas, bool)
        or not isinstance(replicas, int)
        or replicas < 1
        or not isinstance(identities, list)
        or len(identities) != replicas
        or not identities
    ):
        raise CostResizeError(
            "ambiguous_target",
            "A homogeneous single-container-per-pod Deployment is required",
        )
    names = {identity.get("container") for identity in identities}
    if len(names) != 1 or any(not identity.get("pod") for identity in identities):
        raise CostResizeError(
            "ambiguous_target",
            "Container identities do not map one-to-one to all replicas",
        )
    container_name = next(iter(names))
    if not isinstance(container_name, str) or not container_name:
        raise CostResizeError("ambiguous_target", "Container identity is invalid")
    cpu_requests = {identity.get("cpu_request_cores") for identity in identities}
    memory_requests = {identity.get("memory_request_bytes") for identity in identities}
    cpu_limits = {identity.get("cpu_limit_cores") for identity in identities}
    memory_limits = {identity.get("memory_limit_bytes") for identity in identities}
    raw_cpu_requests = {identity.get("cpu_request") for identity in identities}
    raw_memory_requests = {identity.get("memory_request") for identity in identities}
    raw_cpu_limits = {identity.get("cpu_limit") for identity in identities}
    raw_memory_limits = {identity.get("memory_limit") for identity in identities}
    if (
        len(cpu_requests) != 1 or len(memory_requests) != 1
        or len(cpu_limits) != 1 or len(memory_limits) != 1
        or len(raw_cpu_requests) != 1 or len(raw_memory_requests) != 1
        or len(raw_cpu_limits) != 1 or len(raw_memory_limits) != 1
    ):
        raise CostResizeError(
            "ambiguous_target", "Per-replica container resource configuration differs"
        )
    current_cpu = cpu_requests.pop()
    current_memory = memory_requests.pop()
    cpu_limit = cpu_limits.pop()
    memory_limit = memory_limits.pop()
    raw_cpu_request = raw_cpu_requests.pop()
    raw_memory_request = raw_memory_requests.pop()
    raw_cpu_limit = raw_cpu_limits.pop()
    raw_memory_limit = raw_memory_limits.pop()
    aggregate_cpu = row.get("cpu_request_recommended_cores")
    aggregate_memory = row.get("memory_request_recommended_bytes")
    if not all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
        for value in (
            current_cpu, current_memory, aggregate_cpu, aggregate_memory
        )
    ):
        raise CostResizeError(
            "invalid_recommendation", "Request recommendation quantities are invalid"
        )
    target_cpu = math.ceil(aggregate_cpu / replicas * 1000 - 1e-9) / 1000
    target_memory = math.ceil(
        aggregate_memory / replicas / MIB - 1e-9
    ) * MIB
    minimum_cpu, maximum_cpu, minimum_memory, maximum_memory = _quantity_limits()
    if not (
        minimum_cpu <= target_cpu <= maximum_cpu
        and minimum_memory <= target_memory <= maximum_memory
    ):
        raise CostResizeError(
            "unsafe_quantity", "Recommended requests exceed configured resize bounds"
        )
    if target_cpu > current_cpu or target_memory > current_memory:
        raise CostResizeError(
            "not_downsize", "Resize workflow permits request reductions only"
        )
    if target_cpu == current_cpu and target_memory == current_memory:
        raise CostResizeError(
            "no_change", "Recommendation does not change CPU or memory requests"
        )
    if cpu_limit is not None and target_cpu > cpu_limit:
        raise CostResizeError("unsafe_quantity", "CPU request exceeds its existing limit")
    if memory_limit is not None and target_memory > memory_limit:
        raise CostResizeError(
            "unsafe_quantity", "Memory request exceeds its existing limit"
        )
    safety_margin = report.get("thresholds", {}).get(
        "minimum_peak_headroom", 1.25
    )
    cpu_peak = row.get("cpu_peak")
    memory_peak = row.get("memory_peak")
    if (
        not isinstance(safety_margin, (int, float))
        or safety_margin < 1
        or not isinstance(cpu_peak, (int, float))
        or not isinstance(memory_peak, (int, float))
    ):
        raise CostResizeError(
            "insufficient_evidence", "Peak usage safety evidence is unavailable"
        )
    if (
        target_cpu + 1e-9 < current_cpu * cpu_peak * safety_margin
        or target_memory + 1e-9
        < current_memory * memory_peak * safety_margin
    ):
        raise CostResizeError(
            "peak_safety", "Proposed requests do not preserve required peak headroom"
        )
    uid = row.get("deployment_uid")
    resource_version = row.get("resource_version")
    if not all(isinstance(value, str) and value for value in (uid, resource_version)):
        raise CostResizeError(
            "stale_evidence", "Deployment UID and resourceVersion are required"
        )
    return {
        "report_run_id": report.get("run_id"),
        "report_generated_at": report.get("generated_at"),
        "reporting_period": period,
        "freshness_limits": freshness,
        "workload": workload,
        "namespace": LAB_NAMESPACE,
        "context": LAB_CONTEXT,
        "deployment_uid": uid,
        "resource_version": resource_version,
        "replicas": replicas,
        "container": container_name,
        "current": {
            "cpu_request": raw_cpu_request,
            "memory_request": raw_memory_request,
            "cpu_limit": raw_cpu_limit,
            "memory_limit": raw_memory_limit,
        },
        "proposed": {
            "cpu_request": _millicores(target_cpu),
            "memory_request": _mebibytes(target_memory),
        },
        "evidence": {
            "cpu_average": row["cpu_avg"],
            "cpu_peak": cpu_peak,
            "memory_average": row["memory_avg"],
            "memory_peak": memory_peak,
            "sample_count": row["sample_count"],
            "coverage": row["coverage"],
            "first_observed_at": row["first_observed_at"],
            "last_observed_at": row["last_observed_at"],
            "first_observed_at": row["first_observed_at"],
            "minimum_peak_headroom": safety_margin,
        },
        "pricing": {
            "source": report["pricing"]["source"],
            "currency": report["pricing"]["currency"],
            "effective_date": report["pricing"]["effective_date"],
            "model": report["pricing"]["model"],
            "estimated_potential_savings": (
                row.get("request_cost_savings") or {}
            ).get("potential_savings"),
        },
        "limits_are_unchanged": True,
        "dry_run": True,
    }


def _run(runner: Callable, args: list[str]) -> subprocess.CompletedProcess:
    try:
        result = runner(args, timeout=_command_timeout())
    except subprocess.TimeoutExpired as exc:
        raise CostResizeError(
            "command_timeout", "Kubernetes command timed out"
        ) from exc
    except OSError as exc:
        raise CostResizeError(
            "command_unavailable", "Kubernetes command could not be started"
        ) from exc
    if result.returncode != 0:
        raise CostResizeError(
            "kubernetes_command_failed",
            "Kubernetes read or update command failed",
        )
    return result


def _invoke(
    runner: Callable, args: list[str]
) -> subprocess.CompletedProcess:
    try:
        return runner(args, timeout=_command_timeout())
    except subprocess.TimeoutExpired as exc:
        raise CostResizeError(
            "command_timeout", "Kubernetes command outcome is uncertain"
        ) from exc
    except OSError as exc:
        raise CostResizeError(
            "command_unavailable", "Kubernetes command could not be started"
        ) from exc


def _read_deployment(workload: str, runner: Callable) -> dict:
    result = _run(
        runner,
        ["get", f"deployment/{workload}", "-n", LAB_NAMESPACE, "-o", "json"],
    )
    try:
        deployment = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise CostResizeError(
            "invalid_kubernetes_response", "Deployment response is invalid"
        ) from exc
    if (
        not isinstance(deployment, dict)
        or (deployment.get("metadata") or {}).get("namespace") != LAB_NAMESPACE
    ):
        raise CostResizeError(
            "out_of_scope", "Kubernetes returned a deployment outside the lab namespace"
        )
    return deployment


def _container_spec(deployment: dict, plan: dict) -> tuple[dict, str]:
    spec = deployment.get("spec") or {}
    metadata = deployment.get("metadata") or {}
    template = (spec.get("template") or {}).get("spec") or {}
    containers = template.get("containers")
    if (
        metadata.get("uid") != plan["deployment_uid"]
        or metadata.get("resourceVersion") != plan["resource_version"]
        or spec.get("replicas") != plan["replicas"]
        or not isinstance(containers, list)
        or len(containers) != 1
        or containers[0].get("name") != plan["container"]
    ):
        raise CostResizeError(
            "stale_evidence",
            "Deployment identity, version, replicas, or container set changed",
        )
    resources = containers[0].get("resources") or {}
    requests = resources.get("requests") or {}
    if (
        requests.get("cpu") != plan["current"]["cpu_request"]
        or requests.get("memory") != plan["current"]["memory_request"]
    ):
        raise CostResizeError(
            "stale_evidence", "Current requests differ from the approved proposal"
        )
    limits = resources.get("limits") or {}
    for metric, expected in (
        ("cpu", plan["current"]["cpu_limit"]),
        ("memory", plan["current"]["memory_limit"]),
    ):
        actual = limits.get(metric)
        if actual != expected:
            raise CostResizeError(
                "stale_evidence", "Existing limits changed since recommendation"
            )
    return containers[0], metadata["resourceVersion"]


def preview_resize(plan: dict) -> dict:
    return {
        "status": "DRY_RUN",
        "executed": False,
        "plan": plan,
        "required_approvals": [
            "distinct operator with owner permission",
            "explicit execution request with remediation permission",
        ],
    }


def execute_approved_resize(
    plan: dict,
    *,
    operator_id: str,
    runner: Callable = ResizeRunner(),
    stage_callback: Callable[[str], None] | None = None,
) -> dict:
    """Apply one approved patch, then verify rollout; never auto-roll back."""
    allowed = PolicyConfig().allowed_deployments
    if (
        plan.get("context") != LAB_CONTEXT
        or plan.get("namespace") != LAB_NAMESPACE
        or plan.get("workload") not in allowed
    ):
        raise CostResizeError("out_of_scope", "Resize plan is outside the lab allowlist")
    if plan.get("dry_run") is not True:
        raise CostResizeError("invalid_plan", "Resize plan is not marked as previewed")
    if operator_id == "":
        raise CostResizeError("invalid_operator", "Operator identity is required")
    _emit_stage(
        stage_callback,
        "PREFLIGHT_VALIDATION",
        failure_stage="PREFLIGHT_FAILED",
    )
    try:
        validate_resize_plan_freshness(plan)
        deployment = _read_deployment(plan["workload"], runner)
        container, resource_version = _container_spec(deployment, plan)
    except CostResizeError as exc:
        _emit_stage(
            stage_callback,
            "PREFLIGHT_FAILED",
            failure_stage="PREFLIGHT_FAILED",
        )
        if exc.execution_stage is None:
            exc.execution_stage = "PREFLIGHT_FAILED"
        raise
    containers = (deployment.get("spec") or {}).get("template", {}).get(
        "spec", {}
    ).get("containers", [])
    requests = (container.get("resources") or {}).get("requests") or {}
    patch = [
        {"op": "test", "path": "/metadata/resourceVersion",
         "value": resource_version},
        {"op": "test", "path": "/spec/replicas", "value": plan["replicas"]},
        {"op": "test", "path": "/spec/template/spec/containers/0/name",
         "value": plan["container"]},
        {"op": "test", "path": "/spec/template/spec/containers/0/resources/requests/cpu",
         "value": requests["cpu"]},
        {"op": "test", "path": "/spec/template/spec/containers/0/resources/requests/memory",
         "value": requests["memory"]},
        {"op": "replace",
         "path": "/spec/template/spec/containers/0/resources/requests/cpu",
         "value": plan["proposed"]["cpu_request"]},
        {"op": "replace",
         "path": "/spec/template/spec/containers/0/resources/requests/memory",
         "value": plan["proposed"]["memory_request"]},
    ]
    started_at = datetime.now(timezone.utc).isoformat()
    _emit_stage(
        stage_callback,
        "PATCH_ATTEMPT_STARTED",
        failure_stage="PREFLIGHT_FAILED",
    )
    try:
        patch_result = runner(
            [
                "patch", f"deployment/{plan['workload']}", "-n", LAB_NAMESPACE,
                "--type=json", "-p", json.dumps(patch, separators=(",", ":")),
            ],
            timeout=_command_timeout(),
        )
    except subprocess.TimeoutExpired as exc:
        _emit_stage(
            stage_callback,
            "PATCH_OUTCOME_UNKNOWN",
            failure_stage="PATCH_OUTCOME_UNKNOWN",
            mutation_may_have_started=True,
        )
        raise CostResizeError(
            "patch_outcome_unknown",
            "Kubernetes patch outcome is uncertain; do not retry automatically.",
            execution_stage="PATCH_OUTCOME_UNKNOWN",
        ) from exc
    except OSError as exc:
        _emit_stage(
            stage_callback,
            "PATCH_OUTCOME_UNKNOWN",
            failure_stage="PATCH_OUTCOME_UNKNOWN",
            mutation_may_have_started=True,
        )
        raise CostResizeError(
            "patch_outcome_unknown",
            "Kubernetes patch command outcome is uncertain; inspect the Deployment.",
            execution_stage="PATCH_OUTCOME_UNKNOWN",
        ) from exc
    if patch_result.returncode != 0:
        error_text = "\n".join(
            value for value in (patch_result.stderr, patch_result.stdout)
            if isinstance(value, str)
        )
        if DEFINITIVE_REJECTION.search(error_text):
            stage = "PATCH_REJECTED"
            code = "patch_rejected"
            message = "Kubernetes definitively rejected the resize patch."
        else:
            stage = "PATCH_OUTCOME_UNKNOWN"
            code = "patch_outcome_unknown"
            message = (
                "Kubernetes patch outcome is uncertain; do not retry automatically."
            )
        _emit_stage(
            stage_callback,
            stage,
            failure_stage="PATCH_OUTCOME_UNKNOWN",
            mutation_may_have_started=(stage == "PATCH_OUTCOME_UNKNOWN"),
        )
        raise CostResizeError(code, message, execution_stage=stage)
    _emit_stage(
        stage_callback,
        "PATCH_ACCEPTED_VERIFICATION_PENDING",
        failure_stage="PATCH_OUTCOME_UNKNOWN",
        mutation_may_have_started=True,
    )
    try:
        rollout = _invoke(
            runner,
            [
                "rollout", "status", f"deployment/{plan['workload']}",
                "-n", LAB_NAMESPACE, f"--timeout={_command_timeout()}s",
            ],
        )
        applied_deployment = _read_deployment(plan["workload"], runner)
        applied_spec, _ = _container_spec_after_update(applied_deployment, plan)
    except CostResizeError as exc:
        exc.execution_stage = "VERIFICATION_FAILED"
        _emit_stage(
            stage_callback,
            "VERIFICATION_FAILED",
            failure_stage="VERIFICATION_FAILED",
            mutation_may_have_started=True,
        )
        raise
    applied_requests = (applied_spec.get("resources") or {}).get("requests") or {}
    applied_limits = (applied_spec.get("resources") or {}).get("limits") or {}
    try:
        requests_match = (
            parse_cpu_quantity(applied_requests.get("cpu"))
            == parse_cpu_quantity(plan["proposed"]["cpu_request"])
            and parse_memory_quantity(applied_requests.get("memory"))
            == parse_memory_quantity(plan["proposed"]["memory_request"])
        )
    except CostTelemetryError:
        requests_match = False
    limits_match = all(
        applied_limits.get(metric) == plan["current"][field]
        for metric, field in (
            ("cpu", "cpu_limit"),
            ("memory", "memory_limit"),
        )
    )
    spec = applied_deployment.get("spec") or {}
    status = applied_deployment.get("status") or {}
    healthy = (
        requests_match
        and limits_match
        and rollout.returncode == 0
        and status.get("observedGeneration", 0) >= spec.get("generation", 1)
        and status.get("availableReplicas", 0) >= plan["replicas"]
    )
    final_stage = "EXECUTION_VERIFIED" if healthy else "VERIFICATION_FAILED"
    _emit_stage(
        stage_callback,
        final_stage,
        failure_stage="PATCH_OUTCOME_UNKNOWN",
        mutation_may_have_started=True,
    )
    finished_at = datetime.now(timezone.utc).isoformat()
    return {
        "status": "VERIFIED" if healthy else "VERIFICATION_FAILED",
        "execution_stage": final_stage,
        "executed": True,
        "mutation_attempted": True,
        "operator_id": operator_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "before": plan["current"],
        "approved_proposal": plan["proposed"],
        "applied_requests": applied_requests,
        "rollout_verified": rollout.returncode == 0,
        "requests_verified": requests_match,
        "limits_unchanged": limits_match,
        "available_replicas": status.get("availableReplicas"),
        "desired_replicas": plan["replicas"],
        "resource_version_after": (
            applied_deployment.get("metadata") or {}
        ).get("resourceVersion"),
        "audit_plan": plan,
        "kubectl_patch_result": "succeeded",
        "realized_savings": 0.0,
        "recovery_action": (
            None if healthy else
            "Stop further changes and have an operator inspect the Deployment; "
            "no automatic rollback was attempted."
        ),
    }


def inspect_resize_plan(
    plan: dict, *, runner: Callable = ResizeRunner()
) -> dict:
    """Read-only reconciliation of an interrupted resize against its plan."""
    allowed = PolicyConfig().allowed_deployments
    if (
        plan.get("context") != LAB_CONTEXT
        or plan.get("namespace") != LAB_NAMESPACE
        or plan.get("workload") not in allowed
    ):
        raise CostResizeError(
            "out_of_scope",
            "Resize plan is outside the lab allowlist.",
            execution_stage="RECONCILIATION_UNRESOLVED",
        )
    deployment = _read_deployment(plan["workload"], runner)
    metadata = deployment.get("metadata") or {}
    spec = deployment.get("spec") or {}
    containers = ((spec.get("template") or {}).get("spec") or {}).get("containers")
    if (
        metadata.get("uid") != plan.get("deployment_uid")
        or spec.get("replicas") != plan.get("replicas")
        or not isinstance(containers, list)
        or len(containers) != 1
        or containers[0].get("name") != plan.get("container")
    ):
        return {
            "status": "RECONCILIATION_UNRESOLVED",
            "execution_stage": "RECONCILIATION_UNRESOLVED",
            "execution_outcome": "deployment_identity_or_replica_mismatch",
            "executed": None,
            "mutation_attempted": True,
            "manual_review_required": True,
        }
    container = containers[0]
    resources = container.get("resources") or {}
    requests = resources.get("requests") or {}
    limits = resources.get("limits") or {}
    original_limits_match = all(
        limits.get(metric) == plan["current"][field]
        for metric, field in (
            ("cpu", "cpu_limit"),
            ("memory", "memory_limit"),
        )
    )
    original_requests_match = all(
        requests.get(metric) == plan["current"][field]
        for metric, field in (
            ("cpu", "cpu_request"),
            ("memory", "memory_request"),
        )
    )
    proposed_requests_match = False
    try:
        proposed_requests_match = (
            parse_cpu_quantity(requests.get("cpu"))
            == parse_cpu_quantity(plan["proposed"]["cpu_request"])
            and parse_memory_quantity(requests.get("memory"))
            == parse_memory_quantity(plan["proposed"]["memory_request"])
        )
    except (CostTelemetryError, KeyError, TypeError):
        pass

    if (
        metadata.get("resourceVersion") == plan.get("resource_version")
        and original_requests_match
        and original_limits_match
    ):
        result_status = "RECONCILED_ORIGINAL_STATE"
        outcome = "current_deployment_matches_original_plan_state"
    elif (
        proposed_requests_match
        and original_limits_match
        and status_is_healthy(deployment, int(plan["replicas"]))
    ):
        result_status = "RECONCILED_PROPOSED_STATE"
        outcome = (
            "current_deployment_matches_approved_proposal; "
            "execution_attribution_not_proven"
        )
    else:
        result_status = "RECONCILIATION_UNRESOLVED"
        outcome = "current_deployment_matches_neither_verified_plan_state"

    return {
        "status": result_status,
        "execution_stage": result_status,
        "execution_outcome": outcome,
        "executed": None,
        "mutation_attempted": True,
        "manual_review_required": result_status == "RECONCILIATION_UNRESOLVED",
        "deployment_uid": metadata.get("uid"),
        "resource_version": metadata.get("resourceVersion"),
        "generation": spec.get("generation"),
        "observed_generation": (deployment.get("status") or {}).get(
            "observedGeneration"
        ),
        "available_replicas": (deployment.get("status") or {}).get(
            "availableReplicas"
        ),
        "desired_replicas": plan["replicas"],
        "current_requests": requests,
        "current_limits": limits,
        "limits_unchanged": original_limits_match,
        "realized_savings": 0.0,
    }


def status_is_healthy(deployment: dict, replicas: int) -> bool:
    spec = deployment.get("spec") or {}
    status = deployment.get("status") or {}
    generation = spec.get("generation")
    observed_generation = status.get("observedGeneration")
    available = status.get("availableReplicas")
    return (
        isinstance(generation, int)
        and isinstance(observed_generation, int)
        and observed_generation >= generation
        and isinstance(available, int)
        and available >= replicas
    )


def _container_spec_after_update(deployment: dict, plan: dict) -> tuple[dict, str]:
    metadata = deployment.get("metadata") or {}
    spec = deployment.get("spec") or {}
    containers = ((spec.get("template") or {}).get("spec") or {}).get("containers")
    if (
        metadata.get("uid") != plan["deployment_uid"]
        or not isinstance(containers, list)
        or len(containers) != 1
        or containers[0].get("name") != plan["container"]
        or spec.get("replicas") != plan["replicas"]
    ):
        raise CostResizeError(
            "verification_failed", "Applied Deployment identity changed unexpectedly"
        )
    return containers[0], metadata.get("resourceVersion", "")
