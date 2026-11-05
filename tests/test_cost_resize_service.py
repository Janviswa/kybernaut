import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

import app.services.cost_resize_service as resize_service
from app.services.cost_resize_service import (
    CostResizeError,
    build_resize_plan,
    execute_approved_resize,
    preview_resize,
)


def report(*, containers=None, cpu_peak=0.1, memory_peak=0.2, now=None):
    now = now or datetime.now(timezone.utc).replace(microsecond=0)
    period_start = now - timedelta(days=7)
    latest_observation = now - timedelta(hours=1)
    containers = containers or [{
        "pod": "demo-web-abc",
        "container": "web",
        "cpu_request": "1",
        "memory_request": "1Gi",
        "cpu_limit": "2",
        "memory_limit": "2Gi",
        "cpu_request_cores": 1.0,
        "memory_request_bytes": 1024 ** 3,
        "cpu_limit_cores": 2.0,
        "memory_limit_bytes": 2 * 1024 ** 3,
    }]
    return {
        "run_id": "test-run",
        "generated_at": now.isoformat(),
        "reporting_period": {
            "start": period_start.isoformat(),
            "end_exclusive": now.isoformat(),
            "sample_interval_minutes": 60,
            "minimum_coverage": 0.8,
        },
        "pricing": {
            "model": "kubernetes_requests",
            "cost_model_status": "configured",
            "example": False,
            "source": "approved test rate card",
            "currency": "USD",
            "effective_date": "2026-01-01",
        },
        "thresholds": {"minimum_peak_headroom": 1.25},
        "recommendations": [{
            "resource": "kybernaut-lab/demo-web",
            "status": "request_downsize_candidate",
            "coverage": 1.0,
            "sample_count": 168,
            "first_observed_at": period_start.isoformat(),
            "last_observed_at": latest_observation.isoformat(),
            "replicas": 1,
            "container_identity": containers,
            "cpu_request_recommended_cores": 0.2,
            "memory_request_recommended_bytes": 512 * 1024 ** 2,
            "cpu_avg": 0.05,
            "cpu_peak": cpu_peak,
            "memory_avg": 0.1,
            "memory_peak": memory_peak,
            "deployment_uid": "uid-demo",
            "resource_version": "11",
        }],
    }


def deployment(version="11", *, cpu="1", memory="1Gi"):
    return {
        "metadata": {
            "namespace": "kybernaut-lab",
            "uid": "uid-demo",
            "resourceVersion": version,
        },
        "spec": {
            "replicas": 1,
            "generation": 2 if version == "12" else 1,
            "template": {
                "spec": {
                    "containers": [{
                        "name": "web",
                        "resources": {
                            "requests": {"cpu": cpu, "memory": memory},
                            "limits": {"cpu": "2", "memory": "2Gi"},
                        },
                    }],
                },
            },
        },
        "status": {
            "observedGeneration": 2,
            "availableReplicas": 1,
        },
    }


class ResizeRunner:
    def __init__(self):
        self.calls = []
        self.timeouts = []
        self.current = deployment()

    def __call__(self, args, *, timeout):
        self.calls.append(args)
        self.timeouts.append(timeout)
        if args[0] == "get":
            return subprocess.CompletedProcess(args, 0, json.dumps(self.current), "")
        if args[0] == "patch":
            patch = json.loads(args[args.index("-p") + 1])
            self.current = deployment(version="12", cpu="200m", memory="512Mi")
            return subprocess.CompletedProcess(args, 0, json.dumps(patch), "")
        if args[0] == "rollout":
            return subprocess.CompletedProcess(args, 0, "success", "")
        raise AssertionError(f"unexpected kubectl call {args}")


def test_preview_is_dry_run_and_rejects_ambiguous_containers():
    plan = build_resize_plan(report(), "demo-web")

    assert preview_resize(plan)["executed"] is False
    with pytest.raises(CostResizeError, match="single-container"):
        build_resize_plan(report(containers=[
            {
                "pod": "demo-web-abc", "container": "web",
                "cpu_request": "500m", "memory_request": "512Mi",
                "cpu_limit": None, "memory_limit": None,
                "cpu_request_cores": 0.5,
                "memory_request_bytes": 512 * 1024 ** 2,
                "cpu_limit_cores": None, "memory_limit_bytes": None,
            },
            {
                "pod": "demo-web-abc", "container": "sidecar",
                "cpu_request": "500m", "memory_request": "512Mi",
                "cpu_limit": None, "memory_limit": None,
                "cpu_request_cores": 0.5,
                "memory_request_bytes": 512 * 1024 ** 2,
                "cpu_limit_cores": None, "memory_limit_bytes": None,
            },
        ]), "demo-web")


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"workload": "other"}, "allowlisted"),
        ({"pricing": {"model": "vm"}}, "compatible Kubernetes"),
        ({"status": "capacity_pressure"}, "safe downsize"),
        ({"coverage": 0.2}, "evidence is incomplete"),
        ({"cpu_peak": 0.9}, "peak headroom"),
    ],
)
def test_resize_plan_fails_closed(overrides, error):
    data = report()
    row = data["recommendations"][0]
    workload = "demo-web"
    if "workload" in overrides:
        workload = overrides["workload"]
    for key, value in overrides.items():
        if key != "workload":
            if key == "pricing":
                data[key] = value
            else:
                row[key] = value
    with pytest.raises(CostResizeError, match=error):
        build_resize_plan(data, workload)


def test_approved_execution_only_patches_requests_and_verifies_rollout():
    plan = build_resize_plan(report(), "demo-web")
    runner = ResizeRunner()
    stages = []

    result = execute_approved_resize(
        plan,
        operator_id="approver-2",
        runner=runner,
        stage_callback=stages.append,
    )

    assert result["status"] == "VERIFIED"
    assert result["execution_stage"] == "EXECUTION_VERIFIED"
    assert stages == [
        "PREFLIGHT_VALIDATION",
        "PATCH_ATTEMPT_STARTED",
        "PATCH_ACCEPTED_VERIFICATION_PENDING",
        "EXECUTION_VERIFIED",
    ]
    assert result["executed"] is True
    assert result["requests_verified"] is True
    assert result["limits_unchanged"] is True
    assert result["realized_savings"] == 0.0
    patch_call = next(call for call in runner.calls if call[0] == "patch")
    patch = json.loads(patch_call[patch_call.index("-p") + 1])
    assert all("limits" not in operation["path"] for operation in patch)
    assert all("spec/replicas" not in operation["path"]
               for operation in patch if operation["op"] != "test")


def test_approved_execution_rejects_stale_version_before_patch():
    plan = build_resize_plan(report(), "demo-web")
    runner = ResizeRunner()
    runner.current["metadata"]["resourceVersion"] = "99"

    with pytest.raises(CostResizeError, match="version"):
        execute_approved_resize(plan, operator_id="operator", runner=runner)

    assert not any(call[0] == "patch" for call in runner.calls)


def test_resize_execution_uses_configured_bounded_timeout(monkeypatch):
    plan = build_resize_plan(report(), "demo-web")
    runner = ResizeRunner()
    monkeypatch.setenv("KYBERNAUT_COST_RESIZE_TIMEOUT_SECONDS", "15")

    result = execute_approved_resize(
        plan,
        operator_id="operator",
        runner=runner,
    )

    assert result["status"] == "VERIFIED"
    assert runner.timeouts == [15, 15, 15, 15]
    rollout = next(call for call in runner.calls if call[0] == "rollout")
    assert "--timeout=15s" in rollout


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("generated_at", "2026-10-28T22:59:59+00:00", "too old"),
        ("generated_at", "2026-10-29T00:00:01+00:00", "future"),
        ("generated_at", "2026-10-28T23:59:59+00:00", "do not match"),
        ("first_observed_at", "2026-10-21T23:59:59+00:00", "do not match"),
        ("last_observed_at", "2026-10-29T00:00:00+00:00", "do not match"),
        ("last_observed_at", "2026-10-28T21:59:59+00:00", "observation is too old"),
        ("end_exclusive", "2026-10-28T21:59:59+00:00", "period ended too long"),
        ("generated_at", "2026-10-29T00:00:00", "timezone"),
    ],
)
def test_resize_plan_rejects_stale_future_or_malformed_evidence(
    monkeypatch, field, value, error
):
    now = datetime(2026, 10, 29, tzinfo=timezone.utc)
    monkeypatch.setattr(resize_service, "_utc_now", lambda: now)
    data = report(now=now)
    if field == "end_exclusive":
        data["reporting_period"][field] = value
        data["recommendations"][0]["last_observed_at"] = (
            datetime.fromisoformat(value) - timedelta(minutes=1)
        ).isoformat()
    elif field in {"first_observed_at", "last_observed_at"}:
        data["recommendations"][0][field] = value
    else:
        data[field] = value
        if field == "generated_at" and value.startswith("2026-10-28T22:"):
            end = now - timedelta(hours=1, seconds=2)
            data["reporting_period"]["end_exclusive"] = end.isoformat()
            data["recommendations"][0]["last_observed_at"] = (
                end - timedelta(minutes=1)
            ).isoformat()

    with pytest.raises(CostResizeError, match=error):
        build_resize_plan(data, "demo-web")


def test_resize_plan_rejects_invalid_freshness_configuration(monkeypatch):
    now = datetime(2026, 10, 29, tzinfo=timezone.utc)
    monkeypatch.setattr(resize_service, "_utc_now", lambda: now)
    monkeypatch.setenv("KYBERNAUT_COST_RESIZE_MAX_REPORT_AGE_SECONDS", "invalid")

    with pytest.raises(CostResizeError, match="freshness limits"):
        build_resize_plan(report(now=now), "demo-web")


def test_resize_execution_rechecks_freshness_before_kubernetes_reads(monkeypatch):
    now = datetime(2026, 10, 29, tzinfo=timezone.utc)
    monkeypatch.setattr(resize_service, "_utc_now", lambda: now)
    plan = build_resize_plan(report(now=now), "demo-web")
    monkeypatch.setattr(
        resize_service,
        "_utc_now",
        lambda: now + timedelta(hours=2),
    )
    runner = ResizeRunner()
    stages = []

    with pytest.raises(CostResizeError, match="too old") as failure:
        execute_approved_resize(
            plan,
            operator_id="operator",
            runner=runner,
            stage_callback=stages.append,
        )

    assert failure.value.execution_stage == "PREFLIGHT_FAILED"
    assert runner.calls == []
    assert stages == ["PREFLIGHT_VALIDATION", "PREFLIGHT_FAILED"]


def test_stage_persistence_failure_before_patch_blocks_mutation():
    plan = build_resize_plan(report(), "demo-web")
    runner = ResizeRunner()

    def fail_before_patch(stage):
        if stage == "PATCH_ATTEMPT_STARTED":
            raise RuntimeError("offline persistence failure")

    with pytest.raises(CostResizeError) as failure:
        execute_approved_resize(
            plan,
            operator_id="operator",
            runner=runner,
            stage_callback=fail_before_patch,
        )

    assert failure.value.execution_stage == "PREFLIGHT_FAILED"
    assert [call[0] for call in runner.calls] == ["get"]


def test_patch_timeout_is_unknown_and_never_retried():
    plan = build_resize_plan(report(), "demo-web")
    stages = []

    class TimeoutRunner(ResizeRunner):
        def __call__(self, args, *, timeout):
            self.calls.append(args)
            if args[0] == "patch":
                raise subprocess.TimeoutExpired("kubectl", timeout)
            return super().__call__(args, timeout=timeout)

    runner = TimeoutRunner()
    with pytest.raises(CostResizeError) as failure:
        execute_approved_resize(
            plan,
            operator_id="operator",
            runner=runner,
            stage_callback=stages.append,
        )

    assert failure.value.execution_stage == "PATCH_OUTCOME_UNKNOWN"
    assert [call[0] for call in runner.calls].count("patch") == 1
    assert stages[-1] == "PATCH_OUTCOME_UNKNOWN"


def test_read_only_reconciliation_never_claims_execution_attribution():
    from app.services.cost_resize_service import inspect_resize_plan

    plan = build_resize_plan(report(), "demo-web")
    original_runner = ResizeRunner()
    original = inspect_resize_plan(plan, runner=original_runner)
    assert original["status"] == "RECONCILED_ORIGINAL_STATE"
    assert [call[0] for call in original_runner.calls] == ["get"]
    assert original["executed"] is None

    proposed_runner = ResizeRunner()
    proposed_runner.current = deployment(
        version="12", cpu="200m", memory="512Mi"
    )
    proposed = inspect_resize_plan(plan, runner=proposed_runner)
    assert proposed["status"] == "RECONCILED_PROPOSED_STATE"
    assert proposed["executed"] is None
    assert "attribution_not_proven" in proposed["execution_outcome"]
    assert [call[0] for call in proposed_runner.calls] == ["get"]
