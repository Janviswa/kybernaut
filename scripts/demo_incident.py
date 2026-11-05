#!/usr/bin/env python3
"""Run the authenticated response workflow with synthetic, local-only inputs."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import secrets
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.anomaly_detector import FEATURE_NAMES  # noqa: E402
from app.services.anomaly_policy import DirectionAwareAnomalyPolicy  # noqa: E402
from app.services.anomaly_runtime import POLICY_THRESHOLD  # noqa: E402


SYNTHETIC_OBSERVED_AT = "2026-10-22T14:00:00+00:00"


def synthetic_telemetry(mode: str = "detected") -> dict:
    """Evaluate synthetic memory telemetry with the active MAD policy."""
    if mode not in {"detected", "missed"}:
        raise ValueError("mode must be 'detected' or 'missed'")
    baseline_path = PROJECT_ROOT / "data" / "telemetry_baseline_v6.csv"
    with baseline_path.open(newline="", encoding="utf-8") as handle:
        rows = [
            row for row in csv.DictReader(handle)
            if row.get("service") == "order-service"
        ]
    if len(rows) < 20:
        raise RuntimeError("Synthetic demo needs 20 runtime baseline rows")
    rows.sort(key=lambda row: row["timestamp"])
    baseline = [
        [float(row[name]) for name in FEATURE_NAMES]
        for row in rows
    ]
    policy = DirectionAwareAnomalyPolicy(threshold=POLICY_THRESHOLD)
    policy.fit(baseline)
    values = np.asarray(baseline, dtype=float)
    median = np.median(values, axis=0)
    mad = np.median(np.abs(values - median), axis=0)
    scale = 1.4826 * mad
    scale = np.where(
        scale > 1e-12,
        scale,
        np.maximum(np.std(values, axis=0), 1e-12),
    )
    memory_index = FEATURE_NAMES.index("memory_bytes")
    multiplier = 8.0 if mode == "detected" else 4.0
    observed = median.copy()
    observed[memory_index] += multiplier * scale[memory_index]
    output = policy.evaluate([observed.tolist()])[0]
    feature_result = output["features"]["memory_bytes"]
    detected = bool(
        output["actionable_candidates"] or output["informational_deviations"]
    )
    return {
        "status": "evaluated",
        "detected": detected,
        "expected_anomaly": True,
        "scenario": f"synthetic_memory_anomaly_{mode}",
        "metric": "memory_bytes",
        "timestamp": SYNTHETIC_OBSERVED_AT,
        "value": feature_result["value"],
        "baseline_median": feature_result["baseline_median"],
        "deviation_score": feature_result["deviation_score"],
        "threshold": policy.threshold,
        "baseline_samples": len(baseline),
        "baseline_source": baseline_path.name,
        "baseline_sha256": hashlib.sha256(
            baseline_path.read_bytes()
        ).hexdigest(),
        "classification": feature_result["classification"],
        "actionable_candidates": output["actionable_candidates"],
        "informational_deviations": output["informational_deviations"],
        "detector_identity": (
            "app.services.anomaly_policy.DirectionAwareAnomalyPolicy"
        ),
        "evidence": (
            "Synthetic time-series point scored by the active direction-aware "
            "MAD policy using the checked-in telemetry baseline."
        ),
    }


def synthetic_observation(incident_id: str = "DEMO") -> dict:
    observed_at = SYNTHETIC_OBSERVED_AT
    resource_suffix = incident_id.removeprefix("DEMO-")
    return {
        "namespace": "kybernaut-lab",
        "observed_at": observed_at,
        "incidents": [{
            "type": "pod_container_failure",
            "severity": "high",
            "resource": f"demo-web-{resource_suffix}",
            "resource_kind": "Pod",
            "resource_uid": f"synthetic-{resource_suffix}",
            "source_timestamp": observed_at,
            "evidence": [
                "Container nginx terminated with reason OOMKilled after the "
                "synthetic memory anomaly."
            ],
        }],
        "incident_count": 1,
        "deployments": [{
            "name": "demo-web",
            "desired_replicas": 2,
            "available_replicas": 2,
            "ready_replicas": 2,
        }],
    }


def synthetic_diagnosis() -> dict:
    return {
        "service": "order-service",
        "status": "down",
        "evidence_status": "available",
        "observed_at": SYNTHETIC_OBSERVED_AT,
        "missing_evidence": [],
        "evidence": {
            "synthetic_health_check": "failed",
            "source": "local demo fixture",
        },
        "findings": [{
            "signal": "service_health",
            "evidence": "Synthetic health check reports unavailable.",
        }],
    }


def _token(jwt, *, secret: str, issuer: str, audience: str,
           operator_id: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": operator_id,
            "iss": issuer,
            "aud": audience,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "permissions": ["read", "remediation"],
        },
        secret,
        algorithm="HS256",
    )


def _failure(stage: str, reason: str, **details) -> dict:
    return {
        "success": False,
        "failed_stage": stage,
        "reason": reason,
        **details,
    }


def _synthetic_optimization() -> dict:
    from app.services.cost_optimizer import analyze_resource, load_catalog

    catalog, metadata = load_catalog(
        PROJECT_ROOT / "config" / "pricing.example.json"
    )
    recommendation = analyze_resource(
        {
            "name": "synthetic-order-service",
            "instance_type": "example.xlarge",
            "cpu_utilization": [0.12] * 24,
            "memory_utilization": [0.24] * 24,
        },
        catalog,
    )
    return {
        "status": recommendation["status"],
        "recommendation": recommendation,
        "pricing_is_example": metadata.get("example", False),
        "realized_savings": recommendation["realized_savings"],
        "source": "24 synthetic utilization samples and example pricing catalog",
    }


def run_demo(
    *,
    observation_factory=synthetic_observation,
    diagnosis_factory=synthetic_diagnosis,
    detection_mode: str = "detected",
) -> dict:
    """Exercise the real API with isolated credentials and synthetic inputs."""
    jwt_secret = secrets.token_urlsafe(32)
    environment = {
        "PYTHON_DOTENV_DISABLED": "true",
        "KYBERNAUT_JWT_SECRET": jwt_secret,
        "KYBERNAUT_JWT_ISSUER": "kybernaut-local-demo",
        "KYBERNAUT_JWT_AUDIENCE": "kybernaut-local-demo-api",
        "KYBERNAUT_REMEDIATION_MODE": "dry_run",
        "KYBERNAUT_POLICY_AUTO_REMEDIATION": "false",
        "KYBERNAUT_POLICY_MIN_CONFIDENCE": "0.6",
        "KYBERNAUT_POLICY_AUTO_CONFIDENCE": "0.9",
        "KYBERNAUT_POLICY_MAX_REPLICAS": "5",
        "KYBERNAUT_POLICY_MAX_ACTIONS": "3",
        "KYBERNAUT_POLICY_WINDOW_SECONDS": "3600",
    }
    with patch.dict(os.environ, environment, clear=False):
        return _run_demo(
            observation_factory=observation_factory,
            diagnosis_factory=diagnosis_factory,
            telemetry_factory=lambda: synthetic_telemetry(detection_mode),
            environment=environment,
            jwt_secret=jwt_secret,
        )


def _run_demo(
    *,
    observation_factory,
    diagnosis_factory,
    telemetry_factory,
    environment: dict[str, str],
    jwt_secret: str,
) -> dict:
    import jwt
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.database as database
    import app.core.jwt_auth as jwt_auth
    import app.api.operations as operations
    from app.services import incident_repository
    from app.services import demo_results
    import app.services.incident_response_graph as graph
    import app.services.k8s_remediation as remediation

    original_database_path = database.DATABASE_PATH
    issuer = "kybernaut-local-demo"
    audience = "kybernaut-local-demo-api"
    previous_jwt = (jwt_auth.JWT_SECRET, jwt_auth.JWT_ISSUER,
                    jwt_auth.JWT_AUDIENCE)
    incident_id = f"DEMO-{uuid.uuid4().hex[:12]}"
    request_key = f"demo-{uuid.uuid4().hex}"
    operator_id = "demo-operator"
    approver_id = "demo-approver"
    demo_root = demo_results.DEMO_DATABASE_DIR
    demo_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    synthetic_telemetry_result = telemetry_factory()
    database.DATABASE_PATH = demo_root / f"{incident_id.lower()}.sqlite3"
    app = FastAPI()
    app.include_router(operations.router)

    async def collect_synthetic_evidence(_state):
        diagnosis = diagnosis_factory()
        observation = (
            observation_factory(incident_id)
            if observation_factory is synthetic_observation
            else observation_factory()
        )
        return {
            "diagnosis": diagnosis,
            "telemetry_anomaly": synthetic_telemetry_result,
            "kubernetes": observation,
            "kubernetes_status": "completed",
        }

    async def rca_unavailable(_state):
        return {
            "rca": None,
            "rca_status": "unavailable",
            "rca_error": "external RCA provider intentionally not used",
        }

    try:
        database.initialize_database()
        with patch.dict(os.environ, environment, clear=False):
            jwt_auth.JWT_SECRET = jwt_secret
            jwt_auth.JWT_ISSUER = issuer
            jwt_auth.JWT_AUDIENCE = audience
            with (
                patch.object(graph, "detect_and_diagnose",
                             collect_synthetic_evidence),
                patch.object(graph, "analyze_root_cause", rca_unavailable),
                patch.object(graph, "response_graph",
                             graph.build_response_graph()),
                patch.object(
                    graph,
                    "observe_namespace",
                    lambda: (
                        observation_factory(incident_id)
                        if observation_factory is synthetic_observation
                        else observation_factory()
                    ),
                ),
                patch.object(remediation, "default_runner",
                             side_effect=AssertionError(
                                 "Live kubectl runner must not be called."
                             )) as live_runner,
                TestClient(app) as client,
            ):
                from app.services import audit_repository as audit

                try:
                    audit_event = audit.record_event(
                        incident_ref=incident_id,
                        event_type=(
                            "anomaly_detected"
                            if synthetic_telemetry_result["detected"]
                            else "anomaly_detection_missed"
                        ),
                        actor="kybernaut-synthetic-monitor",
                        summary=(
                            "Synthetic memory anomaly detected by active MAD policy"
                            if synthetic_telemetry_result["detected"]
                            else (
                                "Synthetic memory anomaly was below the "
                                "active MAD threshold"
                            )
                        ),
                        resource="order-service",
                        outcome=(
                            "detected"
                            if synthetic_telemetry_result["detected"]
                            else "missed"
                        ),
                        details=synthetic_telemetry_result,
                    )
                except Exception as exc:
                    return _failure(
                        "audit_persistence",
                        "initial anomaly event could not be persisted; workflow was not started",
                        error_type=type(exc).__name__,
                        detection=synthetic_telemetry_result,
                        skipped_stages=[
                            "diagnosis", "decision", "approval", "dry_run",
                            "verification", "optimization",
                        ],
                        live_runner_calls=0,
                    )
                if not synthetic_telemetry_result["detected"]:
                    with sqlite3.connect(database.DATABASE_PATH) as connection:
                        persisted = connection.execute(
                            "SELECT event_id FROM audit_events WHERE event_id = ?",
                            (audit_event["event_id"],),
                        ).fetchone()
                    return _failure(
                        "detection",
                        (
                            "synthetic anomaly was not detected; downstream "
                            "incident workflow was not started"
                        ),
                        incident_id=incident_id,
                        detection=synthetic_telemetry_result,
                        diagnosis={"status": "not_run"},
                        policy_decision={"status": "not_run"},
                        execution={"status": "not_run"},
                        verification={"status": "not_run"},
                        audit={
                            "persisted": persisted is not None,
                            "event_types": [audit_event["event_type"]],
                            "sqlite_path": str(database.DATABASE_PATH),
                        },
                        skipped_stages=[
                            "diagnosis", "decision", "approval", "dry_run",
                            "verification", "optimization",
                        ],
                        live_runner_calls=0,
                    )
                incident_repository.record_incident({
                    "incident_id": incident_id,
                    "service": "order-service",
                    "type": "memory_leak",
                    "severity": "high",
                    "message": (
                        "Synthetic memory anomaly detected; diagnosis and "
                        "response evidence are recorded in this isolated demo."
                    ),
                })
                operator_auth = {
                    "Authorization": "Bearer " + _token(
                        jwt, secret=jwt_secret, issuer=issuer,
                        audience=audience, operator_id=operator_id,
                    ),
                }
                approver_auth = {
                    "Authorization": "Bearer " + _token(
                        jwt, secret=jwt_secret, issuer=issuer,
                        audience=audience, operator_id=approver_id,
                    ),
                }
                started = client.post(
                    "/workflow/order-service/respond",
                    headers={
                        **operator_auth,
                        "Idempotency-Key": request_key,
                    },
                    json={"incident_id": incident_id},
                )
                if started.status_code != 200:
                    return _failure(
                        "workflow_start",
                        f"response API returned HTTP {started.status_code}",
                    )
                pending = started.json()
                contract = pending.get("contract") or {}
                prometheus = next(
                    (
                        item for item in contract.get("evidence_sources", [])
                        if item.get("source") == "prometheus"
                    ),
                    {},
                )
                if prometheus.get("status") != "available":
                    audit_response = client.get(
                        f"/audit?incident_id={incident_id}",
                        headers=operator_auth,
                    )
                    audit_events = (
                        audit_response.json().get("events", [])
                        if audit_response.status_code == 200 else []
                    )
                    return _failure(
                        "diagnosis",
                        "synthetic diagnostic evidence was unavailable",
                        incident_id=incident_id,
                        detection=synthetic_telemetry_result,
                        workflow_status=pending.get("status"),
                        diagnostic_evidence={"status": prometheus.get("status")},
                        prometheus_status=prometheus.get("status"),
                        audit_event_types=[
                            item.get("event_type") for item in audit_events
                        ],
                        policy_decision={"status": "not_run"},
                        approval={"status": "not_run"},
                        execution={"status": "not_run"},
                        verification={"status": "not_run"},
                        skipped_stages=[
                            "policy", "approval", "dry_run",
                            "verification", "optimization",
                        ],
                        sqlite_path=str(database.DATABASE_PATH),
                    )
                if pending.get("classification", {}).get("incident_type") != (
                    "memory_leak"
                ):
                    return _failure(
                        "classification",
                        "synthetic memory failure was not classified as memory_leak",
                        classification=pending.get("classification"),
                        detection=synthetic_telemetry_result,
                        skipped_stages=[
                            "action_proposal", "policy", "approval",
                            "dry_run", "verification", "optimization",
                        ],
                    )
                policy = pending.get("policy_decision") or {}
                if pending.get("status") == "blocked" or not policy.get("allowed"):
                    events_response = client.get(
                        f"/audit?incident_id={incident_id}",
                        headers=operator_auth,
                    )
                    return _failure(
                        "policy",
                        "deterministic policy denied the synthetic action",
                        detection=synthetic_telemetry_result,
                        classification=pending.get("classification"),
                        diagnostic_evidence={"status": prometheus.get("status")},
                        policy_decision=policy,
                        approval={"status": "not_requested"},
                        execution={"status": "not_run"},
                        verification={"status": "not_run"},
                        skipped_stages=[
                            "approval", "dry_run", "verification",
                            "optimization",
                        ],
                        audit_events=(
                            events_response.json().get("events", [])
                            if events_response.status_code == 200 else []
                        ),
                    )
                if not (
                    pending.get("status") == "awaiting_approval"
                    and policy.get("requires_approval") is True
                    and policy.get("auto_execute") is False
                    and pending.get("approval_id")
                ):
                    return _failure(
                        "approval_gate",
                        "workflow did not stop at the required approval gate",
                        workflow_status=pending.get("status"),
                        policy_decision=policy,
                        approval={"status": "not_run"},
                        execution={"status": "not_run"},
                        verification={"status": "not_run"},
                        skipped_stages=[
                            "dry_run", "verification", "optimization",
                        ],
                    )

                decided = client.post(
                    f"/approvals/{pending['approval_id']}/decision",
                    headers=approver_auth,
                    json={
                        "decision": "approved",
                        "reason": "Approved for synthetic dry-run demonstration.",
                    },
                )
                if decided.status_code != 200:
                    return _failure(
                        "approval",
                        f"approval API returned HTTP {decided.status_code}",
                        approval={"status": "failed"},
                        execution={"status": "not_run"},
                        verification={"status": "not_run"},
                        skipped_stages=[
                            "dry_run", "verification", "optimization",
                        ],
                    )
                completed = decided.json()
                workflow_result = completed
                execution = workflow_result.get("execution")
                execution = execution if isinstance(execution, dict) else {}
                verification = workflow_result.get("verification")
                verification = (
                    verification if isinstance(verification, dict) else {}
                )
                contract = workflow_result.get("contract")
                contract = contract if isinstance(contract, dict) else {}
                dry_run_checks = {
                    "workflow_completed_as_dry_run": (
                        workflow_result.get("status") == "dry_run_complete"
                    ),
                    "execution_mode_is_dry_run": (
                        execution.get("mode") == "dry_run"
                    ),
                    "execution_was_not_performed": (
                        execution.get("executed") is False
                    ),
                    "dry_run_executor_succeeded": execution.get("ok") is True,
                    "recovery_was_not_claimed": (
                        verification.get("recovered") is False
                    ),
                    "verification_records_not_executed": (
                        verification.get("reason") == "not_executed"
                    ),
                    "contract_verification_is_not_run": (
                        contract.get("verification_status") == "not_run"
                    ),
                }
                if not all(dry_run_checks.values()):
                    return _failure(
                        "dry_run_validation",
                        "workflow did not preserve dry-run and verification semantics",
                        workflow_status=workflow_result.get("status"),
                        execution=execution,
                        verification=verification,
                        failed_checks=[
                            check for check, passed in dry_run_checks.items()
                            if not passed
                        ],
                        skipped_stages=["optimization"],
                        live_runner_calls=live_runner.call_count,
                    )

                audit_response = client.get(
                    f"/audit?incident_id={incident_id}",
                    headers=operator_auth,
                )
                if audit_response.status_code != 200:
                    return _failure(
                        "audit",
                        f"audit API returned HTTP {audit_response.status_code}",
                    )
                audit_events = audit_response.json().get("events", [])
                event_types = [item.get("event_type") for item in audit_events]
                required_events = {
                    "evidence_collected",
                    "incident_classified",
                    "action_proposed",
                    "policy_evaluated",
                    "approval_requested",
                    "approval_approved",
                    "remediation_started",
                    "remediation_dry_run",
                    "dry_run_complete",
                }
                if not required_events.issubset(set(event_types)):
                    return _failure(
                        "audit",
                        "one or more expected workflow audit events are missing",
                        detection=synthetic_telemetry_result,
                        missing_events=sorted(required_events - set(event_types)),
                        skipped_stages=["optimization"],
                    )
                with sqlite3.connect(database.DATABASE_PATH) as connection:
                    persisted_ids = {
                        row[0] for row in connection.execute(
                            "SELECT event_id FROM audit_events "
                            "WHERE incident_ref = ?",
                            (incident_id,),
                        )
                    }
                api_ids = {
                    item.get("event_id") for item in audit_events
                    if isinstance(item.get("event_id"), str)
                }
                audit_persisted = (
                    len(persisted_ids) == len(audit_events)
                    and persisted_ids == api_ids
                    and audit_event["event_id"] in persisted_ids
                )
                if not audit_persisted:
                    return _failure(
                        "audit_persistence",
                        "audit API results do not match rows persisted in the isolated SQLite database",
                        detection=synthetic_telemetry_result,
                        api_event_count=len(audit_events),
                        persisted_event_count=len(persisted_ids),
                    )
                if live_runner.call_count:
                    return _failure(
                        "safety",
                        "live remediation runner was invoked",
                        live_runner_calls=live_runner.call_count,
                    )

                try:
                    optimization = _synthetic_optimization()
                except Exception as exc:
                    return _failure(
                        "optimization",
                        "synthetic cost analysis could not complete",
                        error_type=type(exc).__name__,
                        skipped_stages=[],
                        detection=synthetic_telemetry_result,
                    )
                return {
                    "success": True,
                    "incident_id": incident_id,
                    "workflow_claim_id": workflow_result.get("workflow_claim_id"),
                    "classification": workflow_result.get("classification"),
                    "monitoring": {
                        "source": "synthetic time series",
                        "service": "order-service",
                        "observed_at": synthetic_telemetry_result["timestamp"],
                    },
                    "detection": synthetic_telemetry_result,
                    "diagnostic_evidence": {
                        "source": "synthetic",
                        "status": prometheus.get("status"),
                        "incident_evidence": "synthetic OOMKilled event",
                    },
                    "root_cause_analysis": {
                        "status": "unavailable",
                        "reason": "external RCA provider intentionally not used",
                    },
                    "runbook_resolution": workflow_result.get(
                        "runbook_resolution"
                    ),
                    "proposal": workflow_result.get("proposal"),
                    "policy_decision": workflow_result.get("policy_decision"),
                    "approval": {
                        "status": "approved",
                        "requested_by": operator_id,
                        "decided_by": approver_id,
                        "approval_id": completed.get("approval_id"),
                    },
                    "execution": {
                        "mode": execution.get("mode"),
                        "executed": execution.get("executed"),
                        "ok": execution.get("ok"),
                    },
                    "verification": {
                        "status": "not_run",
                        "recovered": False,
                        "reason": (
                            "No action was executed; dry-run cannot establish "
                            "workload recovery."
                        ),
                    },
                    "optimization": optimization,
                    "audit": {
                        "event_count": len(audit_events),
                        "persisted": audit_persisted,
                        "event_types": event_types,
                        "event_ids": [
                            item["event_id"] for item in audit_events
                            if isinstance(item.get("event_id"), str)
                        ],
                        "sqlite_path": str(database.DATABASE_PATH),
                    },
                    "live_runner_calls": live_runner.call_count,
                    "skipped_stages": [
                        "live recovery verification (dry-run does not execute)"
                    ],
                }
    finally:
        jwt_auth.JWT_SECRET, jwt_auth.JWT_ISSUER, jwt_auth.JWT_AUDIENCE = (
            previous_jwt
        )
        database.DATABASE_PATH = original_database_path


def main() -> int:
    report = run_demo()
    print(json.dumps(report, indent=2))
    return 0 if report.get("success") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
