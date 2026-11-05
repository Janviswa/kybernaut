import sqlite3

import pytest

from app.services import demo_results
from scripts.demo_incident import (
    run_demo,
    synthetic_diagnosis,
    synthetic_observation,
)


@pytest.fixture(autouse=True)
def isolate_demo_result_store(tmp_path, monkeypatch):
    monkeypatch.setattr(demo_results, "DEMO_DATABASE_DIR", tmp_path / "demos")


def test_demo_runs_authenticated_synthetic_dry_run_and_persists_audit():
    result = run_demo()

    assert result["success"] is True
    assert result["classification"]["incident_type"] == "memory_leak"
    assert result["monitoring"]["source"] == "synthetic time series"
    assert result["detection"]["detected"] is True
    assert result["detection"]["metric"] == "memory_bytes"
    assert result["detection"]["deviation_score"] > result["detection"]["threshold"]
    assert result["detection"]["baseline_samples"] >= 20
    assert len(result["detection"]["baseline_sha256"]) == 64
    assert result["proposal"]["action"] == "restart_pod"
    assert result["runbook_resolution"]["status"] == "applicable"
    assert result["runbook_resolution"]["runbook_id"] == (
        "oom-killed-pod-restart"
    )
    assert result["runbook_resolution"]["supporting_evidence"] == [
        "Container nginx terminated with reason OOMKilled after the "
        "synthetic memory anomaly."
    ]
    assert result["runbook_resolution"]["prerequisites"]["permissions"] == [
        "remediation"
    ]
    assert result["runbook_resolution"]["approval_required"] is True
    assert result["policy_decision"]["allowed"] is True
    assert result["policy_decision"]["requires_approval"] is True
    assert result["approval"]["requested_by"] != result["approval"]["decided_by"]
    assert result["execution"] == {
        "mode": "dry_run",
        "executed": False,
        "ok": True,
    }
    assert result["verification"]["status"] == "not_run"
    assert result["verification"]["recovered"] is False
    assert result["root_cause_analysis"]["status"] == "unavailable"
    assert result["optimization"]["status"] == "downsize_candidate"
    assert result["optimization"]["pricing_is_example"] is True
    assert result["optimization"]["realized_savings"] == 0.0
    assert result["audit"]["persisted"] is True
    assert result["live_runner_calls"] == 0
    assert {
        "evidence_collected",
        "anomaly_detected",
        "incident_classified",
        "action_proposed",
        "runbook_resolution_matched",
        "policy_evaluated",
        "approval_requested",
        "approval_approved",
        "remediation_dry_run",
        "dry_run_complete",
    } <= set(result["audit"]["event_types"])

    with sqlite3.connect(result["audit"]["sqlite_path"]) as connection:
        rows = connection.execute(
            """SELECT event_type, outcome FROM audit_events
               WHERE incident_ref = ?""",
            (result["incident_id"],),
        ).fetchall()
        incident = connection.execute(
            """SELECT incident_id, service, incident_type, severity, status
               FROM incidents WHERE incident_id = ?""",
            (result["incident_id"],),
        ).fetchone()
    assert len(rows) == result["audit"]["event_count"]
    assert incident == (
        result["incident_id"], "order-service", "memory_leak", "high", "OPEN"
    )
    assert ("remediation_dry_run", "ok") in rows
    assert ("dry_run_complete", "dry_run") in rows
    assert "incident_recovery_verified" not in {event for event, _ in rows}


def test_missed_synthetic_anomaly_does_not_create_downstream_incident():
    result = run_demo(detection_mode="missed")

    assert result["success"] is False
    assert result["failed_stage"] == "detection"
    assert result["detection"]["detected"] is False
    assert result["detection"]["expected_anomaly"] is True
    assert result["diagnosis"]["status"] == "not_run"
    assert result["policy_decision"]["status"] == "not_run"
    assert result["execution"]["status"] == "not_run"
    assert result["audit"]["persisted"] is True
    assert result["audit"]["event_types"] == ["anomaly_detection_missed"]
    assert result["live_runner_calls"] == 0


def test_demo_policy_denial_does_not_request_approval_or_execute():
    def denied_observation():
        observation = synthetic_observation()
        observation["incidents"][0]["resource"] = "unapproved-app-pod-1"
        observation["deployments"][0]["name"] = "unapproved-app"
        return observation

    result = run_demo(observation_factory=denied_observation)

    assert result["success"] is False
    assert result["failed_stage"] == "policy"
    assert result["policy_decision"]["allowed"] is False
    assert "policy_evaluated" in {
        event["event_type"] for event in result["audit_events"]
    }
    event_types = {event["event_type"] for event in result["audit_events"]}
    assert "approval_requested" not in event_types
    assert "remediation_started" not in event_types
    assert "remediation_dry_run" not in event_types


def test_demo_fails_clearly_when_synthetic_diagnosis_is_unavailable():
    def unavailable_diagnosis():
        return {
            **synthetic_diagnosis(),
            "status": "unknown",
            "evidence_status": "unavailable",
            "missing_evidence": ["synthetic health check"],
        }

    result = run_demo(diagnosis_factory=unavailable_diagnosis)

    assert result["success"] is False
    assert result["failed_stage"] == "diagnosis"
    assert result["prometheus_status"] == "unavailable"
    event_types = set(result["audit_event_types"])
    assert "approval_requested" in event_types
    assert "remediation_started" not in event_types
    assert "remediation_dry_run" not in event_types
    assert result["sqlite_path"]


def test_audit_failure_stops_workflow_before_approval_or_execution(monkeypatch):
    from app.services import audit_repository

    def fail_audit(**_kwargs):
        raise OSError("synthetic audit unavailable")

    monkeypatch.setattr(audit_repository, "record_event", fail_audit)
    result = run_demo()

    assert result["success"] is False
    assert result["failed_stage"] == "audit_persistence"
    assert result["detection"]["detected"] is True
    assert result["live_runner_calls"] == 0
    assert "approval" in result["skipped_stages"]


def test_demo_policy_configuration_is_independent_of_operator_environment(
    monkeypatch,
):
    monkeypatch.setenv("KYBERNAUT_POLICY_MIN_CONFIDENCE", "0.95")
    monkeypatch.setenv("KYBERNAUT_POLICY_AUTO_REMEDIATION", "true")

    result = run_demo()

    assert result["success"] is True
    assert result["policy_decision"]["allowed"] is True
    assert result["policy_decision"]["requires_approval"] is True
    assert result["policy_decision"]["auto_execute"] is False
    assert result["live_runner_calls"] == 0


def test_demo_reports_specific_dry_run_contract_failure(monkeypatch):
    import app.api.operations as operations

    async def invalid_dry_run_result(
        _approval_id, _decision, _operator_id, _reason
    ):
        return {
            "status": "dry_run_complete",
            "execution": {
                "mode": "live",
                "executed": False,
                "ok": True,
            },
            "verification": {
                "recovered": False,
                "reason": "not_executed",
            },
            "contract": {"verification_status": "not_run"},
        }

    monkeypatch.setattr(
        operations,
        "resume_after_decision",
        invalid_dry_run_result,
    )

    result = run_demo()

    assert result["success"] is False
    assert result["failed_stage"] == "dry_run_validation"
    assert result["failed_checks"] == ["execution_mode_is_dry_run"]
    assert result["live_runner_calls"] == 0
