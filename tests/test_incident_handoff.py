import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

import app.database as database
import app.services.application_anomaly_monitor as monitor
import app.services.incident_workflow as incident_workflow
import app.services.incident_response_graph as graph
import app.services.k8s_remediation as remediation
from app.services import action_claim_repository as claims
from app.services import audit_repository as audit
from app.services import incident_handoff_repository as handoffs
from app.services import incident_repository
from app.services.incident_workflow import _persist_telemetry_incidents
from app.services.incident_response_graph import (
    WorkflowExecutionAmbiguous,
    WorkflowNotStarted,
)


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "handoff.db")
    database.initialize_database()
    monkeypatch.setenv("KYBERNAUT_POLICY_AUTO_REMEDIATION", "true")

    live_runner_calls = []

    def forbidden_runner(proposal):
        live_runner_calls.append(proposal)
        raise AssertionError("The real remediation runner must not be called")

    monkeypatch.setattr(
        graph, "_test_live_runner_calls", live_runner_calls, raising=False
    )
    monkeypatch.setattr(graph, "execute_action", forbidden_runner)
    monkeypatch.setattr(
        remediation,
        "default_runner",
        forbidden_runner,
    )
    monkeypatch.setattr(graph, "response_graph", graph.build_response_graph())


def _telemetry(*, anomaly=True, observed_at=None):
    timestamp = observed_at or datetime.now(timezone.utc).isoformat()
    return {
        "status": "evaluated",
        "threshold": 5.0,
        "observed_at": timestamp,
        "sample_timestamps": {"cpu_cores_used": timestamp},
        "features": {
            "cpu_cores_used": {
                "value": 0.5 if anomaly else 0.02,
                "baseline_median": 0.02,
                "deviation_score": 8.0 if anomaly else 0.0,
                "direction": "HIGH" if anomaly else "NORMAL",
                "classification": (
                    "actionable_candidate" if anomaly else "normal"
                ),
            },
        },
        "actionable_candidates": ["cpu_cores_used"] if anomaly else [],
    }


def _create_occurrence():
    persisted = _persist_telemetry_incidents(
        "order-service", _telemetry()
    )
    assert persisted["status"] == "persisted"
    return incident_repository.get_incident_occurrence(
        persisted["occurrences"][0]["incident_row_id"]
    )


def _monitor_incident_observation(resource="demo-web-pod-1"):
    observed_at = datetime.now(timezone.utc).isoformat()
    return {
        "namespace": "kybernaut-lab",
        "observed_at": observed_at,
        "incidents": [{
            "type": "pod_container_failure",
            "severity": "high",
            "namespace": "kybernaut-lab",
            "resource": resource,
            "resource_kind": "Pod",
            "resource_uid": "synthetic-pod-uid",
            "source_timestamp": observed_at,
            "evidence": ["Container terminated with reason OOMKilled"],
        }],
        "deployments": [{
            "name": "demo-web",
            "desired_replicas": 2,
            "available_replicas": 2,
            "ready_replicas": 2,
        }],
    }


def _use_real_handoff_evidence_helper(monkeypatch, observations):
    helper_calls = []
    helper = incident_workflow.detect_and_diagnose

    async def diagnose(service):
        return {
            "service": service,
            "status": "up",
            "evidence_status": "available",
            "missing_evidence": [],
            "findings": [],
        }

    async def evaluate_anomaly(_service):
        return {"status": "evaluated", "actionable_candidates": []}

    def observe():
        observation = observations.pop(0)
        if isinstance(observation, Exception):
            raise observation
        return observation

    async def check_argument_and_call_helper(state):
        assert isinstance(state, dict)
        assert state.get("service") == "order-service"
        helper_calls.append(state)
        return await helper(state)

    monkeypatch.setattr(incident_workflow, "diagnose_service", diagnose)
    monkeypatch.setattr(
        incident_workflow, "evaluate_service_anomaly", evaluate_anomaly
    )
    monkeypatch.setattr(incident_workflow, "observe_namespace", observe)
    monkeypatch.setattr(
        graph, "detect_and_diagnose", check_argument_and_call_helper
    )
    return helper_calls


def _reserve_occurrence_action(occurrence, workflow_claim_id):
    proposal = {
        "action": "restart_pod",
        "namespace": "kybernaut-lab",
        "resource": "demo-web-pod-1",
        "deployment": "demo-web",
        "confidence": 0.95,
        "severity": "high",
        "rationale": "synthetic test",
        "source": "rules",
    }
    return claims.reserve_action_claim(
        workflow_claim_id=workflow_claim_id,
        attempt_no=0,
        logical_key=f"occurrence-{occurrence['id']}",
        deployment="demo-web",
        action=proposal,
        policy={"allowed": True, "requires_approval": True, "auto_execute": False},
        max_actions=3,
        window_seconds=3600,
        auto_authorized=False,
        incident_occurrence_id=occurrence["id"],
        expected_service=occurrence["service"],
        expected_incident_type=occurrence["incident_type"],
        expected_fingerprint=occurrence["fingerprint"],
    )


def _approve_occurrence_action(occurrence, workflow_claim_id):
    reservation = _reserve_occurrence_action(occurrence, workflow_claim_id)
    assert reservation["allowed"]
    proposal = json.loads(
        claims.get_action_claim(reservation["action_claim_id"])["action_json"]
    )
    policy = {
        "allowed": True,
        "requires_approval": True,
        "auto_execute": False,
    }
    approval = audit.create_approval(
        incident_ref=occurrence["incident_id"],
        proposed_action=proposal,
        context={
            "service": occurrence["service"],
            "policy": policy,
            "monitor_handoff": True,
            "occurrence_id": occurrence["id"],
            "expected_occurrence_service": occurrence["service"],
            "expected_incident_type": occurrence["incident_type"],
            "expected_fingerprint": occurrence["fingerprint"],
        },
        requested_by="kybernaut-monitor",
        action_claim_id=reservation["action_claim_id"],
    )
    audit.decide_approval(
        approval["approval_id"], "approved", "human-reviewer"
    )
    return reservation["action_claim_id"], approval["approval_id"]


def _interrupt_handoff(occurrence_id, workflow_claim_id):
    with database.get_connection() as conn:
        conn.execute(
            """UPDATE incident_handoffs
               SET status = 'RECONCILIATION_REQUIRED', lease_owner = NULL,
                   lease_token = NULL, lease_expires_at = NULL
               WHERE incident_row_id = ?""",
            (occurrence_id,),
        )
        conn.execute(
            """UPDATE workflow_claims
               SET status = 'RECONCILIATION_REQUIRED'
               WHERE claim_id = ?""",
            (workflow_claim_id,),
        )


def _action_to_outcome(occurrence, workflow_claim_id, outcome):
    reservation = _reserve_occurrence_action(occurrence, workflow_claim_id)
    action_id = reservation["action_claim_id"]
    if outcome == "BLOCKED":
        assert claims.transition_action_claim(action_id, "RESERVED", outcome)
        return action_id
    if outcome == "RESERVED":
        return action_id
    assert claims.transition_action_claim(
        action_id, "RESERVED", "AWAITING_APPROVAL"
    )
    if outcome in {"REJECTED", "EXPIRED"}:
        assert claims.transition_action_claim(
            action_id, "AWAITING_APPROVAL", outcome
        )
        return action_id
    assert claims.transition_action_claim(
        action_id, "AWAITING_APPROVAL", "APPROVED"
    )
    if outcome == "APPROVED":
        return action_id
    if outcome == "AUTO_AUTHORIZED":
        with database.get_connection() as conn:
            conn.execute(
                """UPDATE action_claims SET status = 'AUTO_AUTHORIZED'
                   WHERE action_claim_id = ?""",
                (action_id,),
            )
        return action_id
    if outcome == "EXECUTING":
        assert claims.transition_action_claim(
            action_id, "APPROVED", "EXECUTING"
        )
        return action_id
    if outcome == "FAILED_BEFORE_EXECUTION":
        assert claims.transition_action_claim(
            action_id, "APPROVED", outcome
        )
    else:
        assert claims.transition_action_claim(
            action_id, "APPROVED", "EXECUTING"
        )
        if outcome in {"VERIFIED", "VERIFICATION_FAILED", "COMPLETED"}:
            assert claims.transition_action_claim(
                action_id,
                "EXECUTING",
                "COMPLETED",
                execution={"mode": "live", "executed": True, "ok": True},
            )
            if outcome != "COMPLETED":
                assert claims.record_action_verification(
                    action_id, {"recovered": outcome == "VERIFIED"}
                )
                expected = (
                    "VERIFIED"
                    if outcome == "VERIFIED" else "VERIFICATION_FAILED"
                )
                assert claims.get_action_claim(action_id)["status"] == expected
        else:
            execution = {
                "SIMULATED": {
                    "mode": "simulated", "executed": False, "ok": True,
                },
                "SIMULATION_FAILED": {
                    "mode": "simulated", "executed": False, "ok": False,
                },
                "DRY_RUN_COMPLETE": {
                    "mode": "dry_run", "executed": False, "ok": True,
                },
                "EXECUTION_FAILED": {
                    "mode": "live", "executed": False, "ok": False,
                },
                "EXECUTION_UNKNOWN": {
                    "mode": "live", "executed": True, "ok": False,
                },
            }[outcome]
            assert claims.transition_action_claim(
                action_id, "EXECUTING", outcome, execution=execution
            )
    return action_id


def test_repeated_poll_and_recurrence_use_occurrence_scoped_handoffs():
    first_timestamp = datetime.now(timezone.utc)
    second_timestamp = first_timestamp + timedelta(seconds=1)
    first = _persist_telemetry_incidents(
        "order-service",
        _telemetry(observed_at=first_timestamp.isoformat()),
    )
    repeated = _persist_telemetry_incidents(
        "order-service",
        _telemetry(observed_at=second_timestamp.isoformat()),
    )
    rows = handoffs.list_handoffs()

    assert len(rows) == 1
    assert rows[0]["incident_row_id"] == first["occurrences"][0]["incident_row_id"]
    assert repeated["occurrences"][0]["incident_row_id"] == rows[0]["incident_row_id"]
    assert rows[0]["evidence_timestamp"] == second_timestamp.isoformat()

    incident_repository.resolve_incident_fingerprint(
        "prometheus:order-service:cpu_spike"
    )
    recurrence = _persist_telemetry_incidents("order-service", _telemetry())

    assert recurrence["occurrences"][0]["incident_row_id"] != (
        first["occurrences"][0]["incident_row_id"]
    )
    assert len(handoffs.list_handoffs()) == 2


def test_incident_and_outbox_write_roll_back_together(monkeypatch):
    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("synthetic audit write failure")

    monkeypatch.setattr(incident_repository, "_record_handoff_audit", fail_audit)
    incident = {
        "incident_id": "inc-atomic",
        "service": "order-service",
        "type": "cpu_spike",
        "severity": "high",
        "message": "synthetic anomaly",
        "fingerprint": "prometheus:order-service:cpu_spike",
    }

    with pytest.raises(RuntimeError, match="synthetic audit"):
        incident_repository.record_incident(incident, enqueue_handoff=True)

    assert incident_repository.list_incidents() == []
    assert handoffs.list_handoffs() == []


def test_healthy_resolution_cancels_undispatched_handoff():
    occurrence = _create_occurrence()

    incident_repository.resolve_incident_fingerprint(
        occurrence["fingerprint"]
    )

    assert handoffs.list_handoffs()[0]["status"] == "CANCELLED"
    assert handoffs.claim_next_handoff(
        "order-service", lease_owner="test"
    ) is None
    incident = incident_repository.get_incident_occurrence(occurrence["id"])
    assert incident["resolution_reason"] == "telemetry_condition_cleared"
    event = next(
        event for event in audit.list_events(occurrence["incident_id"])
        if event["event_type"] == "incident_resolved"
    )
    assert event["outcome"] == "telemetry_condition_cleared"
    assert event["details"]["evidence_type"] == "healthy_telemetry"


def test_healthy_resolution_races_action_reservation():
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="reservation-race"
    )
    barrier = Barrier(2)

    def reserve():
        barrier.wait()
        return _reserve_occurrence_action(
            occurrence, dispatch["workflow_claim_id"]
        )

    def resolve():
        barrier.wait()
        return incident_repository.resolve_incident_fingerprint(
            occurrence["fingerprint"]
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        reserve_future = executor.submit(reserve)
        resolve_future = executor.submit(resolve)
        reservation = reserve_future.result()
        resolved = resolve_future.result()

    assert resolved == 1
    assert incident_repository.get_incident_occurrence(
        occurrence["id"]
    )["status"] == "RESOLVED"
    if reservation["allowed"]:
        assert claims.get_action_claim(
            reservation["action_claim_id"]
        )["status"] == "BLOCKED"
    else:
        assert reservation["reason"] == (
            "incident_occurrence_inactive_or_changed"
        )
    assert handoffs.get_handoff(occurrence["id"])["status"] == "CANCELLED"


def test_healthy_resolution_races_final_execution_authorization():
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="authorization-race"
    )
    action_claim_id, approval_id = _approve_occurrence_action(
        occurrence, dispatch["workflow_claim_id"]
    )
    barrier = Barrier(2)

    def authorize():
        barrier.wait()
        return claims.authorize_action_execution(
            action_claim_id,
            "APPROVED",
            incident_occurrence_id=occurrence["id"],
            expected_service=occurrence["service"],
            expected_incident_type=occurrence["incident_type"],
            expected_fingerprint=occurrence["fingerprint"],
            require_handoff=True,
            actor="human-reviewer",
            approval_id=approval_id,
        )

    def resolve():
        barrier.wait()
        return incident_repository.resolve_incident_fingerprint(
            occurrence["fingerprint"]
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        authorization_future = executor.submit(authorize)
        resolution_future = executor.submit(resolve)
        authorized = authorization_future.result()
        resolved = resolution_future.result()

    action = claims.get_action_claim(action_claim_id)
    incident = incident_repository.get_incident_occurrence(occurrence["id"])
    assert not (authorized and resolved == 1)
    if authorized:
        assert action["status"] == "EXECUTING"
        assert incident["status"] == "OPEN"
        assert resolved == 0
        assert any(
            event["event_type"] == "incident_resolution_deferred"
            for event in audit.list_events(occurrence["incident_id"])
        )
    else:
        assert action["status"] == "BLOCKED"
        assert incident["status"] == "RESOLVED"
        assert resolved == 1
    assert graph._test_live_runner_calls == []


def test_concurrent_dispatch_claims_are_serialized():
    occurrence = _create_occurrence()

    def claim():
        return handoffs.claim_next_handoff(
            occurrence["service"], lease_owner="parallel-test"
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _i: claim(), range(2)))

    assert sum(result is not None for result in results) == 1
    claimed = next(result for result in results if result is not None)
    assert claimed["kind"] == "claimed"
    assert claimed["idempotency_key"] == (
        f"incident-occurrence-{occurrence['id']}"
    )
    assert claims.get_workflow_claim(claimed["workflow_claim_id"])[
        "operator_id"
    ] == "kybernaut-monitor"


def test_expired_lease_reconciles_pending_approval_without_replay():
    occurrence = _create_occurrence()
    claimed = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="worker"
    )
    workflow_id = claimed["workflow_claim_id"]
    proposal = {
        "action": "restart_pod",
        "namespace": "kybernaut-lab",
        "resource": "demo-web-pod-1",
        "deployment": "demo-web",
        "confidence": 0.95,
        "severity": "high",
        "rationale": "synthetic test",
        "source": "rules",
    }
    policy = {"allowed": True, "requires_approval": True, "auto_execute": False}
    reservation = claims.reserve_action_claim(
        workflow_claim_id=workflow_id,
        attempt_no=0,
        logical_key="stale-approval-action",
        deployment="demo-web",
        action=proposal,
        policy=policy,
        max_actions=3,
        window_seconds=3600,
        auto_authorized=False,
    )
    approval = audit.create_approval(
        incident_ref=occurrence["incident_id"],
        proposed_action=proposal,
        context={"policy": policy},
        requested_by="kybernaut-monitor",
        action_claim_id=reservation["action_claim_id"],
    )
    assert approval["status"] == "PENDING"
    with database.get_connection() as conn:
        conn.execute(
            """UPDATE incident_handoffs SET lease_expires_at = ?
               WHERE incident_row_id = ?""",
            (
                (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                occurrence["id"],
            ),
        )

    assert handoffs.reconcile_expired_leases(occurrence["service"]) == 1
    assert handoffs.list_handoffs()[0]["status"] == "AWAITING_APPROVAL"
    assert claims.get_workflow_claim(workflow_id)["status"] == "AWAITING_APPROVAL"
    event_count = len(audit.list_events(occurrence["incident_id"]))
    assert handoffs.reconcile_expired_leases(occurrence["service"]) == 0
    assert len(audit.list_events(occurrence["incident_id"])) == event_count
    assert audit.get_approval(approval["approval_id"])["requested_by"] == (
        "kybernaut-monitor"
    )
    with pytest.raises(audit.ApprovalError, match="requester"):
        audit.decide_approval(
            approval["approval_id"], "approved", "kybernaut-monitor"
        )
    audit.decide_approval(approval["approval_id"], "approved", "human-operator")


def test_expired_ambiguous_lease_is_not_replayed():
    occurrence = _create_occurrence()
    claim = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="worker"
    )
    with database.get_connection() as conn:
        conn.execute(
            """UPDATE incident_handoffs SET lease_expires_at = ?
               WHERE incident_row_id = ?""",
            (
                (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                occurrence["id"],
            ),
        )

    assert handoffs.reconcile_expired_leases(occurrence["service"]) == 1
    assert handoffs.list_handoffs()[0]["status"] == "RECONCILIATION_REQUIRED"
    assert claims.get_workflow_claim(claim["workflow_claim_id"])["status"] == (
        "RUNNING"
    )
    assert handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="replacement"
    ) is None


def test_reconciliation_state_rolls_back_when_transition_audit_fails(monkeypatch):
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="audit-rollback"
    )
    with database.get_connection() as conn:
        conn.execute(
            """UPDATE incident_handoffs SET lease_expires_at = ?
               WHERE incident_row_id = ?""",
            (
                (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                occurrence["id"],
            ),
        )

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("synthetic reconciliation audit failure")

    monkeypatch.setattr(handoffs, "_record_event_in_transaction", fail_audit)
    with pytest.raises(RuntimeError, match="reconciliation audit failure"):
        handoffs.reconcile_expired_leases(occurrence["service"])

    assert handoffs.get_handoff(occurrence["id"])["status"] == "DISPATCHING"
    assert claims.get_workflow_claim(dispatch["workflow_claim_id"])[
        "status"
    ] == "RUNNING"


def test_reconciliation_can_adopt_a_later_terminal_workflow_result():
    occurrence = _create_occurrence()
    claim = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="worker"
    )
    with database.get_connection() as conn:
        conn.execute(
            """UPDATE incident_handoffs SET lease_expires_at = ?
               WHERE incident_row_id = ?""",
            (
                (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
                occurrence["id"],
            ),
        )
    handoffs.reconcile_expired_leases(occurrence["service"])
    assert handoffs.list_handoffs()[0]["status"] == "RECONCILIATION_REQUIRED"

    with database.get_connection() as conn:
        conn.execute(
            """UPDATE workflow_claims SET status = 'COMPLETED',
               response_json = ? WHERE claim_id = ?""",
            ('{"status":"no_action"}', claim["workflow_claim_id"]),
        )

    assert handoffs.reconcile_expired_leases(occurrence["service"]) == 1
    assert handoffs.list_handoffs()[0]["status"] == "NO_ACTION"
    event_count = len(audit.list_events(occurrence["incident_id"]))
    assert handoffs.reconcile_expired_leases(occurrence["service"]) == 0
    assert len(audit.list_events(occurrence["incident_id"])) == event_count


def test_reconciliation_finalizes_persisted_verified_live_recovery():
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="verified-recovery"
    )
    action_id = _action_to_outcome(
        occurrence, dispatch["workflow_claim_id"], "VERIFIED"
    )
    _interrupt_handoff(occurrence["id"], dispatch["workflow_claim_id"])

    assert handoffs.reconcile_expired_leases(occurrence["service"]) == 1
    incident = incident_repository.get_incident_occurrence(occurrence["id"])
    handoff = handoffs.get_handoff(occurrence["id"])
    workflow = claims.get_workflow_claim(dispatch["workflow_claim_id"])
    action = claims.get_action_claim(action_id)
    assert incident["status"] == "RESOLVED"
    assert incident["resolution_reason"] == "verified_kubernetes_recovery"
    assert handoff["status"] == "COMPLETED"
    assert workflow["status"] == "COMPLETED"
    assert json.loads(workflow["response_json"])["status"] == "resolved"
    assert action["status"] == "VERIFIED"
    assert graph._test_live_runner_calls == []

    event_count = len(audit.list_events(occurrence["incident_id"]))
    assert handoffs.reconcile_expired_leases(occurrence["service"]) == 0
    assert len(audit.list_events(occurrence["incident_id"])) == event_count
    assert sum(
        event["event_type"] == "incident_recovery_verified"
        for event in audit.list_events(occurrence["incident_id"])
    ) == 1


@pytest.mark.parametrize(
    ("action_status", "handoff_status", "workflow_status"),
    [
        ("SIMULATED", "SIMULATED", "COMPLETED"),
        ("SIMULATION_FAILED", "SIMULATION_FAILED", "COMPLETED"),
        ("DRY_RUN_COMPLETE", "DRY_RUN_COMPLETE", "COMPLETED"),
        ("EXECUTION_FAILED", "FAILED", "COMPLETED"),
        ("FAILED_BEFORE_EXECUTION", "FAILED", "COMPLETED"),
        ("VERIFICATION_FAILED", "VERIFICATION_FAILED", "COMPLETED"),
        ("BLOCKED", "BLOCKED", "COMPLETED"),
        ("REJECTED", "REJECTED", "COMPLETED"),
        ("EXPIRED", "EXPIRED", "COMPLETED"),
        ("RESERVED", "RECONCILIATION_REQUIRED",
         "RECONCILIATION_REQUIRED"),
        ("APPROVED", "RECONCILIATION_REQUIRED",
         "RECONCILIATION_REQUIRED"),
        ("AUTO_AUTHORIZED", "RECONCILIATION_REQUIRED",
         "RECONCILIATION_REQUIRED"),
        ("EXECUTING", "RECONCILIATION_REQUIRED",
         "RECONCILIATION_REQUIRED"),
        ("EXECUTION_UNKNOWN", "RECONCILIATION_REQUIRED",
         "RECONCILIATION_REQUIRED"),
        ("COMPLETED", "RECONCILIATION_REQUIRED",
         "RECONCILIATION_REQUIRED"),
    ],
)
def test_reconciliation_maps_persisted_action_outcomes_safely(
    action_status, handoff_status, workflow_status
):
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner=f"outcome-{action_status}"
    )
    action_id = _action_to_outcome(
        occurrence, dispatch["workflow_claim_id"], action_status
    )
    _interrupt_handoff(occurrence["id"], dispatch["workflow_claim_id"])

    assert handoffs.reconcile_expired_leases(occurrence["service"]) == 1
    handoff = handoffs.get_handoff(occurrence["id"])
    workflow = claims.get_workflow_claim(dispatch["workflow_claim_id"])
    action = claims.get_action_claim(action_id)
    assert handoff["status"] == handoff_status
    assert workflow["status"] == workflow_status
    assert action["status"] == action_status
    assert incident_repository.get_incident_occurrence(
        occurrence["id"]
    )["status"] == "OPEN"
    assert not any(
        event["event_type"] == "incident_recovery_verified"
        for event in audit.list_events(occurrence["incident_id"])
    )
    event_count = len(audit.list_events(occurrence["incident_id"]))
    assert handoffs.reconcile_expired_leases(occurrence["service"]) == 0
    assert len(audit.list_events(occurrence["incident_id"])) == event_count
    assert graph._test_live_runner_calls == []


def test_verified_claim_without_live_recovery_evidence_stays_in_reconciliation():
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="invalid-verified-evidence"
    )
    action_id = _action_to_outcome(
        occurrence, dispatch["workflow_claim_id"], "VERIFIED"
    )
    with database.get_connection() as conn:
        conn.execute(
            """UPDATE action_claims SET execution_json = ?
               WHERE action_claim_id = ?""",
            (
                json.dumps({
                    "mode": "simulated", "executed": False, "ok": True,
                }),
                action_id,
            ),
        )
    _interrupt_handoff(occurrence["id"], dispatch["workflow_claim_id"])

    assert handoffs.reconcile_expired_leases(occurrence["service"]) == 1
    assert handoffs.get_handoff(occurrence["id"])["status"] == (
        "RECONCILIATION_REQUIRED"
    )
    assert incident_repository.get_incident_occurrence(
        occurrence["id"]
    )["status"] == "OPEN"
    assert not any(
        event["event_type"] == "incident_recovery_verified"
        for event in audit.list_events(occurrence["incident_id"])
    )
    event_count = len(audit.list_events(occurrence["incident_id"]))
    assert handoffs.reconcile_expired_leases(occurrence["service"]) == 0
    assert len(audit.list_events(occurrence["incident_id"])) == event_count
    assert graph._test_live_runner_calls == []


def test_completed_no_action_workflow_does_not_claim_successful_recovery():
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="no-action"
    )
    with database.get_connection() as conn:
        conn.execute(
            """UPDATE workflow_claims
               SET status = 'COMPLETED', response_json = ?
               WHERE claim_id = ?""",
            (
                json.dumps({"status": "no_action"}),
                dispatch["workflow_claim_id"],
            ),
        )
        conn.execute(
            """UPDATE incident_handoffs
               SET status = 'RECONCILIATION_REQUIRED'
               WHERE incident_row_id = ?""",
            (occurrence["id"],),
        )

    assert handoffs.reconcile_expired_leases(occurrence["service"]) == 1
    assert handoffs.get_handoff(occurrence["id"])["status"] == "NO_ACTION"
    assert incident_repository.get_incident_occurrence(
        occurrence["id"]
    )["status"] == "OPEN"
    assert claims.get_workflow_claim(dispatch["workflow_claim_id"])[
        "status"
    ] == "COMPLETED"
    assert graph._test_live_runner_calls == []


@pytest.mark.asyncio
async def test_confirmed_prestart_failure_retries_same_claim(monkeypatch):
    occurrence = _create_occurrence()
    monkeypatch.setattr(handoffs, "RETRY_DELAY_SECONDS", 0)
    calls = []

    async def workflow(_service, **kwargs):
        calls.append(kwargs["workflow_claim_id"])
        if len(calls) == 1:
            raise WorkflowNotStarted("synthetic preflight failure")
        assert claims.finish_workflow_claim(
            kwargs["workflow_claim_id"], {"status": "no_action"}
        )
        return {"status": "no_action"}

    monkeypatch.setattr(monitor, "run_response_workflow", workflow)
    result = await monitor.dispatch_pending_handoffs(occurrence["service"])

    assert [item["kind"] for item in result] == ["retry", "dispatched"]
    assert len(calls) == 2 and calls[0] == calls[1]
    assert handoffs.list_handoffs()[0]["status"] == "NO_ACTION"


@pytest.mark.asyncio
async def test_concurrent_dispatchers_start_only_one_workflow(monkeypatch):
    occurrence = _create_occurrence()
    entered = asyncio.Event()
    release = asyncio.Event()
    starts = []

    async def workflow(_service, **kwargs):
        starts.append(kwargs["workflow_claim_id"])
        entered.set()
        await release.wait()
        assert claims.finish_workflow_claim(
            kwargs["workflow_claim_id"], {"status": "no_action"}
        )
        return {"status": "no_action"}

    monkeypatch.setattr(monitor, "run_response_workflow", workflow)
    first = asyncio.create_task(
        monitor.dispatch_pending_handoffs(occurrence["service"])
    )
    await entered.wait()
    second = await monitor.dispatch_pending_handoffs(occurrence["service"])
    release.set()
    first_result = await first

    assert second == []
    assert len(starts) == 1
    assert first_result[0]["kind"] == "dispatched"


@pytest.mark.asyncio
async def test_ambiguous_workflow_start_is_reconciled_and_never_replayed(
    monkeypatch,
):
    occurrence = _create_occurrence()
    calls = []

    async def workflow(_service, **kwargs):
        calls.append(kwargs["workflow_claim_id"])
        claims.mark_workflow_claim_reconciliation_required(
            kwargs["workflow_claim_id"]
        )
        raise WorkflowExecutionAmbiguous("synthetic graph interruption")

    monkeypatch.setattr(monitor, "run_response_workflow", workflow)
    first = await monitor.dispatch_pending_handoffs(occurrence["service"])
    second = await monitor.dispatch_pending_handoffs(occurrence["service"])

    assert first[0]["kind"] == "reconciliation"
    assert second == []
    assert len(calls) == 1
    assert handoffs.list_handoffs()[0]["status"] == "RECONCILIATION_REQUIRED"


def test_existing_running_claim_is_not_blindly_replayed():
    occurrence = _create_occurrence()
    claims.create_workflow_claim(
        operator_id="kybernaut-monitor",
        service=occurrence["service"],
        idempotency_key=f"incident-occurrence-{occurrence['id']}",
        incident_ref=occurrence["incident_id"],
        request_correlation=occurrence["incident_id"],
    )

    result = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="test"
    )

    assert result["kind"] == "reconciliation"
    assert handoffs.list_handoffs()[0]["status"] == "RECONCILIATION_REQUIRED"


@pytest.mark.asyncio
async def test_approval_waiting_handoff_is_durable_and_correlated(monkeypatch):
    occurrence = _create_occurrence()
    seen = []

    async def awaiting(service, **kwargs):
        seen.append((service, kwargs))
        assert claims.finish_workflow_claim(
            kwargs["workflow_claim_id"],
            {"status": "awaiting_approval", "approval_id": "apr-synthetic"},
        )
        return {"status": "awaiting_approval", "approval_id": "apr-synthetic"}

    monkeypatch.setattr(monitor, "run_response_workflow", awaiting)
    results = await monitor.dispatch_pending_handoffs(occurrence["service"])
    handoff = handoffs.list_handoffs()[0]
    event = next(
        event for event in audit.list_events(occurrence["incident_id"])
        if event["event_type"] == "incident_handoff_awaiting_approval"
    )

    assert results[0]["status"] == "awaiting_approval"
    assert handoff["status"] == "AWAITING_APPROVAL"
    assert seen[0][1]["requested_by"] == "kybernaut-monitor"
    assert event["workflow_claim_id"] == handoff["workflow_claim_id"]
    assert event["details"]["incident_row_id"] == occurrence["id"]


@pytest.mark.asyncio
async def test_monitor_policy_cannot_auto_execute_and_audit_links_action_claim(
    monkeypatch,
):
    occurrence = incident_repository.record_incident(
        {
            "incident_id": "inc-monitor-policy",
            "service": "order-service",
            "type": "memory_leak",
            "severity": "high",
            "message": "synthetic OOMKilled",
            "fingerprint": "prometheus:order-service:memory_leak",
        },
        enqueue_handoff=True,
    )
    handoff = handoffs.claim_next_handoff(
        "order-service", lease_owner="policy-test"
    )
    workflow_id = handoff["workflow_claim_id"]
    target = {
        "namespace": "kybernaut-lab",
        "resource": "demo-web-pod-1",
        "resource_kind": "Pod",
        "resource_uid": "pod-uid-test",
        "deployment": "demo-web",
    }
    proposal = {
        "action": "restart_pod",
        "namespace": "kybernaut-lab",
        "resource": target["resource"],
        "deployment": "demo-web",
        "confidence": 0.95,
        "severity": "high",
        "rationale": "synthetic OOM evidence",
        "source": "rules",
    }
    state = {
        "incident_id": occurrence["incident_id"],
        "service": "order-service",
        "workflow_claim_id": workflow_id,
        "requested_by": "kybernaut-monitor",
        "monitor_handoff": True,
        "occurrence_id": occurrence["id"],
        "expected_incident_type": "memory_leak",
        "expected_fingerprint": occurrence["fingerprint"],
        "classification": {
            "incident_type": "memory_leak",
            "confidence": 0.95,
            "severity": "high",
            "target": target,
            "evidence": ["OOMKilled"],
        },
        "proposal": proposal,
        "kubernetes": {
            "namespace": "kybernaut-lab",
            "incidents": [{
                "type": "pod_container_failure",
                "severity": "high",
                "namespace": "kybernaut-lab",
                "resource": target["resource"],
                "resource_kind": "Pod",
                "resource_uid": target["resource_uid"],
                "evidence": ["Container terminated with reason OOMKilled"],
            }],
            "deployments": [{
                "name": "demo-web",
                "desired_replicas": 2,
                "available_replicas": 2,
                "ready_replicas": 2,
            }],
        },
    }

    async def fresh(_service):
        return {
            "diagnosis": {"status": "up", "findings": []},
            "telemetry_anomaly": {
                "status": "evaluated",
                "actionable_candidates": [],
            },
            "kubernetes": state["kubernetes"],
            "kubernetes_status": "completed",
        }

    monkeypatch.setattr(graph, "detect_and_diagnose", fresh)
    decision = await graph.policy(state)
    action_claim = claims.get_action_claim(decision["action_claim_id"])
    policy_event = next(
        event for event in audit.list_events(occurrence["incident_id"])
        if event["event_type"] == "policy_evaluated"
    )

    assert decision["policy_decision"]["allowed"] is True
    assert decision["policy_decision"]["auto_execute"] is False
    assert decision["policy_decision"]["requires_approval"] is True
    assert action_claim["status"] == "RESERVED"
    assert json_value(action_claim["policy_json"])["auto_execute"] is False
    assert handoffs.list_handoffs()[0]["workflow_claim_id"] == workflow_id
    assert policy_event["workflow_claim_id"] == workflow_id
    assert policy_event["action_claim_id"] == action_claim["action_claim_id"]
    assert policy_event["details"]["incident_row_id"] == occurrence["id"]


@pytest.mark.asyncio
async def test_real_graph_dispatch_waits_for_distinct_human_approval(monkeypatch):
    occurrence = incident_repository.record_incident(
        {
            "incident_id": "inc-monitor-approval",
            "service": "order-service",
            "type": "memory_leak",
            "severity": "high",
            "message": "synthetic OOMKilled evidence",
            "fingerprint": "prometheus:order-service:memory_leak",
        },
        enqueue_handoff=True,
    )
    observed_at = datetime.now(timezone.utc).isoformat()

    async def fresh_evidence(_service):
        return {
            "diagnosis": {
                "service": "order-service",
                "status": "up",
                "evidence_status": "available",
                "observed_at": observed_at,
                "findings": [],
                "missing_evidence": [],
            },
            "telemetry_anomaly": {
                "status": "evaluated",
                "observed_at": observed_at,
                "actionable_candidates": [],
            },
            "kubernetes": {
                "namespace": "kybernaut-lab",
                "observed_at": observed_at,
                "incidents": [{
                    "type": "pod_container_failure",
                    "severity": "high",
                    "namespace": "kybernaut-lab",
                    "resource": "demo-web-pod-1",
                    "resource_kind": "Pod",
                    "resource_uid": "synthetic-pod-uid",
                    "source_timestamp": observed_at,
                    "evidence": ["Container terminated with reason OOMKilled"],
                }],
                "deployments": [{
                    "name": "demo-web",
                    "desired_replicas": 2,
                    "available_replicas": 2,
                    "ready_replicas": 2,
                }],
            },
            "kubernetes_status": "completed",
        }

    async def no_rca(_state):
        return {
            "rca": None,
            "rca_status": "unavailable",
            "rca_error": "synthetic",
            "runbook_guidance": [],
        }

    monkeypatch.setattr(graph, "detect_and_diagnose", fresh_evidence)
    monkeypatch.setattr(graph, "analyze_root_cause", no_rca)
    results = await monitor.dispatch_pending_handoffs("order-service")

    assert len(results) == 1
    assert results[0]["status"] == "awaiting_approval"
    outbox = handoffs.list_handoffs()[0]
    approval = audit.list_approvals("PENDING")[0]
    assert outbox["status"] == "AWAITING_APPROVAL"
    assert approval["requested_by"] == "kybernaut-monitor"
    assert approval["action_claim_id"] is not None
    assert claims.get_action_claim(approval["action_claim_id"])[
        "workflow_claim_id"
    ] == outbox["workflow_claim_id"]

    with pytest.raises(audit.ApprovalError, match="requester"):
        audit.decide_approval(
            approval["approval_id"], "approved", "kybernaut-monitor"
        )
    audit.decide_approval(
        approval["approval_id"], "approved", "human-operator"
    )
    events = audit.list_events(occurrence["incident_id"])
    approval_event = next(
        event for event in events if event["event_type"] == "approval_requested"
    )
    assert approval_event["workflow_claim_id"] == outbox["workflow_claim_id"]
    assert approval_event["action_claim_id"] == approval["action_claim_id"]
    assert approval_event["details"]["incident_row_id"] == occurrence["id"]


def _record_memory_handoff():
    return incident_repository.record_incident(
        {
            "incident_id": "inc-real-helper-handoff",
            "service": "order-service",
            "type": "memory_leak",
            "severity": "high",
            "message": "synthetic OOMKilled evidence",
            "fingerprint": "prometheus:order-service:memory_leak",
        },
        enqueue_handoff=True,
    )


@pytest.mark.asyncio
async def test_handoff_approval_uses_real_evidence_helper_contract(monkeypatch):
    _record_memory_handoff()
    observation = _monitor_incident_observation()
    helper_calls = _use_real_handoff_evidence_helper(
        monkeypatch, [observation, observation, observation]
    )
    executions = []

    def simulated_dry_run(proposal):
        executions.append(proposal)
        return {"mode": "dry_run", "executed": False, "ok": True}

    monkeypatch.setattr(graph, "execute_action", simulated_dry_run)
    monkeypatch.setattr(graph, "response_graph", graph.build_response_graph())

    dispatch = await monitor.dispatch_pending_handoffs("order-service")
    assert dispatch[0]["status"] == "awaiting_approval"
    approval = audit.list_approvals("PENDING")[0]

    result = await graph.resume_after_decision(
        approval["approval_id"], "approved", "synthetic-human-approver"
    )

    assert len(helper_calls) == 3
    assert result["status"] == "dry_run_complete"
    assert result["execution"]["mode"] == "dry_run"
    assert result["execution"]["executed"] is False
    assert len(executions) == 1


@pytest.mark.asyncio
async def test_handoff_policy_blocks_when_real_helper_observation_is_unavailable(
    monkeypatch,
):
    _record_memory_handoff()
    helper_calls = _use_real_handoff_evidence_helper(
        monkeypatch,
        [
            _monitor_incident_observation(),
            RuntimeError("synthetic observer failure"),
        ],
    )
    executions = []
    monkeypatch.setattr(
        graph, "execute_action", lambda proposal: executions.append(proposal)
    )
    monkeypatch.setattr(graph, "response_graph", graph.build_response_graph())

    dispatch = await monitor.dispatch_pending_handoffs("order-service")

    assert dispatch[0]["status"] == "blocked"
    assert len(helper_calls) == 2
    assert audit.list_approvals("PENDING") == []
    assert executions == []


@pytest.mark.asyncio
async def test_handoff_resume_blocks_when_real_helper_observation_is_unavailable(
    monkeypatch,
):
    _record_memory_handoff()
    observation = _monitor_incident_observation()
    helper_calls = _use_real_handoff_evidence_helper(
        monkeypatch,
        [
            observation,
            observation,
            RuntimeError("synthetic observer failure"),
        ],
    )
    executions = []
    monkeypatch.setattr(
        graph, "execute_action", lambda proposal: executions.append(proposal)
    )
    monkeypatch.setattr(graph, "response_graph", graph.build_response_graph())

    dispatch = await monitor.dispatch_pending_handoffs("order-service")
    assert dispatch[0]["status"] == "awaiting_approval"
    approval = audit.list_approvals("PENDING")[0]

    result = await graph.resume_after_decision(
        approval["approval_id"], "approved", "synthetic-human-approver"
    )

    assert len(helper_calls) == 3
    assert result["status"] == "blocked"
    assert result["execution"] is None
    assert executions == []


@pytest.mark.asyncio
async def test_handoff_resume_blocks_when_real_helper_target_changes(monkeypatch):
    _record_memory_handoff()
    observation = _monitor_incident_observation()
    changed_target = _monitor_incident_observation(
        resource="different-demo-web-pod"
    )
    helper_calls = _use_real_handoff_evidence_helper(
        monkeypatch, [observation, observation, changed_target]
    )
    executions = []
    monkeypatch.setattr(
        graph, "execute_action", lambda proposal: executions.append(proposal)
    )
    monkeypatch.setattr(graph, "response_graph", graph.build_response_graph())

    dispatch = await monitor.dispatch_pending_handoffs("order-service")
    assert dispatch[0]["status"] == "awaiting_approval"
    approval = audit.list_approvals("PENDING")[0]

    result = await graph.resume_after_decision(
        approval["approval_id"], "approved", "synthetic-human-approver"
    )

    assert len(helper_calls) == 3
    assert result["status"] == "blocked"
    assert result["execution"] is None
    assert executions == []


def json_value(value):
    import json

    return json.loads(value)


@pytest.mark.asyncio
async def test_changed_fresh_evidence_closes_handoff_without_action(monkeypatch):
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="test"
    )

    async def healthy(_service):
        return {
            "diagnosis": {"status": "up", "findings": []},
            "telemetry_anomaly": {
                "status": "evaluated",
                "actionable_candidates": [],
            },
            "kubernetes": {
                "namespace": "kybernaut-lab",
                "incidents": [],
                "deployments": [],
            },
            "kubernetes_status": "completed",
        }

    monkeypatch.setattr(graph, "detect_and_diagnose", healthy)
    result = await graph.run_response_workflow(
        dispatch["service"],
        incident_id=dispatch["incident_id"],
        requested_by=dispatch["requested_by"],
        workflow_claim_id=dispatch["workflow_claim_id"],
        monitor_handoff=True,
        occurrence_id=dispatch["occurrence_id"],
        expected_incident_type=dispatch["incident_type"],
        expected_fingerprint=dispatch["fingerprint"],
    )

    assert result["status"] == "blocked"
    assert result["plan_note"] == "occurrence_or_fresh_evidence_mismatch"
    assert result["proposal"] is None
    assert result["action_claim_id"] is None
    assert claims.get_workflow_claim(dispatch["workflow_claim_id"])["status"] == (
        "COMPLETED"
    )
    assert not any(
        event["event_type"] == "remediation_started"
        for event in audit.list_events(occurrence["incident_id"])
    )


@pytest.mark.asyncio
async def test_verified_workflow_atomically_resolves_occurrence_and_handoff(
    monkeypatch,
):
    occurrence = incident_repository.record_incident(
        {
            "incident_id": "inc-verified-terminal",
            "service": "order-service",
            "type": "memory_leak",
            "severity": "high",
            "message": "synthetic OOMKilled",
            "fingerprint": "prometheus:order-service:memory_leak",
        },
        enqueue_handoff=True,
    )
    observed_at = datetime.now(timezone.utc).isoformat()
    failed_observation = {
        "namespace": "kybernaut-lab",
        "observed_at": observed_at,
        "incidents": [{
            "type": "pod_container_failure",
            "severity": "high",
            "namespace": "kybernaut-lab",
            "resource": "demo-web-abc123",
            "resource_kind": "Pod",
            "resource_uid": "synthetic-pod-uid",
            "source_timestamp": observed_at,
            "evidence": ["Container terminated with reason OOMKilled"],
        }],
        "deployments": [{
            "name": "demo-web",
            "desired_replicas": 2,
            "available_replicas": 2,
            "ready_replicas": 2,
        }],
    }
    healthy_observation = {
        "namespace": "kybernaut-lab",
        "incidents": [],
        "deployments": [{
            "name": "demo-web",
            "desired_replicas": 2,
            "available_replicas": 2,
            "ready_replicas": 2,
        }],
    }

    async def fresh_evidence(_service):
        return {
            "diagnosis": {
                "service": "order-service",
                "status": "up",
                "evidence_status": "available",
                "findings": [],
                "missing_evidence": [],
            },
            "telemetry_anomaly": {
                "status": "evaluated",
                "actionable_candidates": [],
            },
            "kubernetes": failed_observation,
            "kubernetes_status": "completed",
        }

    async def no_rca(_state):
        return {
            "rca": None,
            "rca_status": "unavailable",
            "rca_error": "synthetic",
            "runbook_guidance": [],
        }

    def simulated_live_boundary(proposal):
        return {
            "action": proposal.action,
            "namespace": proposal.namespace,
            "resource": proposal.resource,
            "deployment": proposal.deployment,
            "mode": "live",
            "executed": True,
            "ok": True,
        }

    monkeypatch.setattr(graph, "detect_and_diagnose", fresh_evidence)
    monkeypatch.setattr(graph, "analyze_root_cause", no_rca)
    monkeypatch.setattr(graph, "execute_action", simulated_live_boundary)
    monkeypatch.setattr(graph, "observe_namespace", lambda: healthy_observation)
    monkeypatch.setenv("KYBERNAUT_VERIFY_ATTEMPTS", "2")
    monkeypatch.setenv("KYBERNAUT_VERIFY_INTERVAL", "0")
    monkeypatch.setattr(graph, "response_graph", graph.build_response_graph())

    dispatch = await monitor.dispatch_pending_handoffs("order-service")
    assert dispatch[0]["status"] == "awaiting_approval"
    approval = audit.list_approvals("PENDING")[0]
    result = await graph.resume_after_decision(
        approval["approval_id"], "approved", "synthetic-human-approver"
    )

    persisted_incident = incident_repository.get_incident_occurrence(
        occurrence["id"]
    )
    handoff = handoffs.get_handoff(occurrence["id"])
    workflow = claims.get_workflow_claim(handoff["workflow_claim_id"])
    action = claims.get_action_claim(approval["action_claim_id"])
    events = audit.list_events(occurrence["incident_id"])
    recovery_event = next(
        event for event in events
        if event["event_type"] == "incident_recovery_verified"
    )

    assert result["status"] == "resolved"
    assert result["execution"]["mode"] == "live"
    assert result["verification"]["recovered"] is True
    assert persisted_incident["status"] == "RESOLVED"
    assert persisted_incident["resolution_reason"] == (
        "verified_kubernetes_recovery"
    )
    assert handoff["status"] == "COMPLETED"
    assert workflow["status"] == "COMPLETED"
    assert json.loads(workflow["response_json"])["status"] == "resolved"
    assert action["status"] == "VERIFIED"
    assert recovery_event["outcome"] == "resolved"
    assert recovery_event["actor"] == "synthetic-human-approver"
    assert recovery_event["workflow_claim_id"] == workflow["claim_id"]
    assert recovery_event["action_claim_id"] == action["action_claim_id"]
    assert recovery_event["details"]["resolution_reason"] == (
        "verified_kubernetes_recovery"
    )
    assert graph._test_live_runner_calls == []


@pytest.mark.asyncio
async def test_public_workflow_binds_and_resolves_matching_occurrence(monkeypatch):
    observed_at = datetime.now(timezone.utc).isoformat()
    resource = "demo-web-abc123"
    resource_uid = "public-synthetic-pod-uid"
    incident = incident_repository.record_incident({
        "incident_id": "inc-public-terminal",
        "service": "kubernetes-lab",
        "type": "memory_leak",
        "severity": "high",
        "message": "synthetic OOMKilled",
        "fingerprint": (
            "k8s:kybernaut-lab:Pod:demo-web-abc123:pod_container_failure"
        ),
        "source_timestamp": observed_at,
        "source_evidence": {
            "source": "kubernetes",
            "namespace": "kybernaut-lab",
            "resource": resource,
            "resource_kind": "Pod",
            "resource_uid": resource_uid,
            "source_timestamp": observed_at,
            "incident_type": "memory_leak",
        },
    })
    broken = {
        "namespace": "kybernaut-lab",
        "incidents": [{
            "type": "pod_container_failure",
            "severity": "high",
            "namespace": "kybernaut-lab",
            "resource": resource,
            "resource_kind": "Pod",
            "resource_uid": resource_uid,
            "source_timestamp": observed_at,
            "evidence": ["Container terminated with reason OOMKilled"],
        }],
        "deployments": [{
            "name": "demo-web",
            "desired_replicas": 2,
            "available_replicas": 2,
            "ready_replicas": 2,
        }],
    }
    healthy = {
        "namespace": "kybernaut-lab",
        "incidents": [],
        "deployments": [{
            "name": "demo-web",
            "desired_replicas": 2,
            "available_replicas": 2,
            "ready_replicas": 2,
        }],
    }
    observations = [broken, broken, healthy, healthy]

    def observe():
        return observations.pop(0)

    def fake_live_execution(proposal):
        return {
            "action": proposal.action,
            "namespace": proposal.namespace,
            "resource": proposal.resource,
            "deployment": proposal.deployment,
            "mode": "live",
            "executed": True,
            "ok": True,
        }

    monkeypatch.setenv("KYBERNAUT_POLICY_AUTO_REMEDIATION", "false")
    monkeypatch.setenv("KYBERNAUT_VERIFY_ATTEMPTS", "2")
    monkeypatch.setenv("KYBERNAUT_VERIFY_INTERVAL", "0")
    monkeypatch.setattr(graph, "observe_namespace", observe)
    monkeypatch.setattr(graph, "execute_action", fake_live_execution)
    monkeypatch.setattr(graph, "response_graph", graph.build_response_graph())
    pending = await graph.run_response_workflow(
        "kubernetes-lab",
        incident_id=incident["incident_id"],
        requested_by="public-requester",
    )
    assert pending["status"] == "awaiting_approval"
    workflow_id = pending["workflow_claim_id"]
    workflow = claims.get_workflow_claim(workflow_id)
    assert workflow["incident_occurrence_id"] == incident["id"]

    result = await graph.resume_after_decision(
        pending["approval_id"], "approved", "public-human-approver"
    )
    persisted = incident_repository.get_incident_occurrence(incident["id"])
    workflow = claims.get_workflow_claim(workflow_id)
    action = claims.get_action_claim(pending["action_claim_id"])
    event = next(
        item for item in audit.list_events(incident["incident_id"])
        if item["event_type"] == "incident_recovery_verified"
    )

    assert result["status"] == "resolved"
    assert persisted["resolution_reason"] == "verified_kubernetes_recovery"
    assert workflow["status"] == "COMPLETED"
    assert json.loads(workflow["response_json"])["status"] == "resolved"
    assert action["status"] == "VERIFIED"
    assert event["action_claim_id"] == action["action_claim_id"]
    assert graph._test_live_runner_calls == []


@pytest.mark.parametrize(
    ("terminal_status", "execution"),
    [
        (
            "EXECUTION_FAILED",
            {"mode": "live", "executed": True, "ok": False},
        ),
        (
            "EXECUTION_UNKNOWN",
            {"mode": "live", "executed": True, "ok": False},
        ),
        (
            "SIMULATION_FAILED",
            {"mode": "simulated", "executed": False, "ok": False},
        ),
    ],
)
def test_failed_or_ambiguous_action_cannot_finalize_verified_recovery(
    terminal_status, execution
):
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="failed-finalize"
    )
    reservation = _reserve_occurrence_action(
        occurrence, dispatch["workflow_claim_id"]
    )
    action_claim_id = reservation["action_claim_id"]
    assert claims.transition_action_claim(
        action_claim_id, "RESERVED", "AWAITING_APPROVAL"
    )
    assert claims.transition_action_claim(
        action_claim_id, "AWAITING_APPROVAL", "APPROVED"
    )
    assert claims.transition_action_claim(
        action_claim_id, "APPROVED", "EXECUTING"
    )
    assert claims.transition_action_claim(
        action_claim_id,
        "EXECUTING",
        terminal_status,
        execution=execution,
    )

    finalized = incident_repository.finalize_verified_recovery(
        occurrence_id=occurrence["id"],
        workflow_claim_id=dispatch["workflow_claim_id"],
        action_claim_id=action_claim_id,
        response={"status": "resolved"},
    )

    assert finalized is False
    assert incident_repository.get_incident_occurrence(
        occurrence["id"]
    )["status"] == "OPEN"
    assert handoffs.get_handoff(occurrence["id"])["status"] == "DISPATCHING"
    assert claims.get_workflow_claim(
        dispatch["workflow_claim_id"]
    )["status"] == "RUNNING"
    assert not any(
        event["event_type"] == "incident_recovery_verified"
        for event in audit.list_events(occurrence["incident_id"])
    )
    assert graph._test_live_runner_calls == []


def _approved_monitor_state(occurrence, dispatch):
    action_claim_id, approval_id = _approve_occurrence_action(
        occurrence, dispatch["workflow_claim_id"]
    )
    return {
        "incident_id": occurrence["incident_id"],
        "service": occurrence["service"],
        "occurrence_id": occurrence["id"],
        "expected_occurrence_service": occurrence["service"],
        "expected_incident_type": occurrence["incident_type"],
        "expected_fingerprint": occurrence["fingerprint"],
        "monitor_handoff": True,
        "workflow_claim_id": dispatch["workflow_claim_id"],
        "action_claim_id": action_claim_id,
        "approved_by": "human-reviewer",
        "approval_id": approval_id,
        "policy_decision": {"allowed": True, "requires_approval": True},
        "proposal": {
            "action": "restart_pod",
            "namespace": "kybernaut-lab",
            "resource": "demo-web-pod",
            "deployment": "demo-web",
            "confidence": 0.95,
            "severity": "high",
            "rationale": "synthetic test",
            "source": "rules",
        },
        "attempts": 0,
    }


def test_dispatch_start_audit_failure_rolls_back_handoff_and_workflow(
    monkeypatch,
):
    occurrence = _create_occurrence()

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(handoffs, "_record_event_in_transaction", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        handoffs.claim_next_handoff(
            occurrence["service"], lease_owner="audit-failure"
        )

    handoff = handoffs.get_handoff(occurrence["id"])
    assert handoff["status"] == "PENDING"
    assert handoff["attempts"] == 0
    with database.get_connection() as conn:
        workflow_count = conn.execute(
            "SELECT COUNT(*) AS n FROM workflow_claims"
        ).fetchone()["n"]
    assert workflow_count == 0


def test_handoff_retry_audit_failure_rolls_back_state(monkeypatch):
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="retry-audit"
    )

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(handoffs, "_record_event_in_transaction", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        handoffs.finish_handoff(
            occurrence["id"],
            dispatch["lease_token"],
            status="RETRY",
            workflow_claim_id=dispatch["workflow_claim_id"],
            last_error="workflow_not_started",
        )

    handoff = handoffs.get_handoff(occurrence["id"])
    assert handoff["status"] == "DISPATCHING"
    assert handoff["lease_token"] == dispatch["lease_token"]


def test_execution_authorization_audit_failure_prevents_runner(
    monkeypatch,
):
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="authorization-audit"
    )
    state = _approved_monitor_state(occurrence, dispatch)

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(audit, "record_event_in_transaction", fail_audit)
    result = graph.remediate(state)

    assert result["execution"]["error"] == "audit_unavailable"
    assert claims.get_action_claim(state["action_claim_id"])["status"] == (
        "APPROVED"
    )
    assert graph._test_live_runner_calls == []


def _authorize_monitor_claim(occurrence, action_claim_id, approval_id=None,
                             actor="human-reviewer", status="APPROVED",
                             expected_service=None):
    return claims.authorize_action_execution(
        action_claim_id,
        status,
        incident_occurrence_id=occurrence["id"],
        expected_service=expected_service or occurrence["service"],
        expected_incident_type=occurrence["incident_type"],
        expected_fingerprint=occurrence["fingerprint"],
        require_handoff=True,
        actor=actor,
        approval_id=approval_id,
    )


def test_monitor_authorization_rejects_auto_authorized_claim():
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="automatic-authorization"
    )
    proposal = {
        "action": "restart_pod",
        "namespace": "kybernaut-lab",
        "resource": "demo-web-pod-1",
        "deployment": "demo-web",
        "confidence": 0.95,
        "severity": "high",
        "rationale": "synthetic test",
        "source": "rules",
    }
    reservation = claims.reserve_action_claim(
        workflow_claim_id=dispatch["workflow_claim_id"],
        attempt_no=0,
        logical_key=f"auto-occurrence-{occurrence['id']}",
        deployment="demo-web",
        action=proposal,
        policy={"allowed": True, "auto_execute": True},
        max_actions=3,
        window_seconds=3600,
        auto_authorized=True,
        incident_occurrence_id=occurrence["id"],
        expected_service=occurrence["service"],
        expected_incident_type=occurrence["incident_type"],
        expected_fingerprint=occurrence["fingerprint"],
    )

    assert not _authorize_monitor_claim(
        occurrence,
        reservation["action_claim_id"],
        status="AUTO_AUTHORIZED",
        actor="kybernaut-monitor",
    )
    assert claims.get_action_claim(reservation["action_claim_id"])["status"] == (
        "BLOCKED"
    )
    denial_events = [
        event for event in audit.list_events(occurrence["incident_id"])
        if event["event_type"] == "action_authorization_denied"
    ]
    assert len(denial_events) == 1
    assert denial_events[0]["action_claim_id"] == reservation["action_claim_id"]
    assert denial_events[0]["outcome"] == "blocked"
    assert graph._test_live_runner_calls == []


@pytest.mark.parametrize("invalid_case", ["missing", "wrong_id", "rejected", "self"])
def test_monitor_authorization_rejects_invalid_human_approval(invalid_case):
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner=f"invalid-approval-{invalid_case}"
    )
    action_claim_id, approval_id = _approve_occurrence_action(
        occurrence, dispatch["workflow_claim_id"]
    )
    supplied_approval_id = approval_id
    actor = "human-reviewer"
    if invalid_case == "missing":
        supplied_approval_id = None
    elif invalid_case == "wrong_id":
        supplied_approval_id = "apr-not-linked"
    elif invalid_case == "rejected":
        with database.get_connection() as conn:
            conn.execute(
                "UPDATE approval_requests SET status = 'REJECTED' "
                "WHERE approval_id = ?",
                (approval_id,),
            )
    elif invalid_case == "self":
        with database.get_connection() as conn:
            conn.execute(
                "UPDATE approval_requests SET decided_by = ? "
                "WHERE approval_id = ?",
                ("kybernaut-monitor", approval_id),
            )
        actor = "kybernaut-monitor"

    assert not _authorize_monitor_claim(
        occurrence,
        action_claim_id,
        supplied_approval_id,
        actor=actor,
    )
    assert claims.get_action_claim(action_claim_id)["status"] == "BLOCKED"
    denial_events = [
        event for event in audit.list_events(occurrence["incident_id"])
        if event["event_type"] == "action_authorization_denied"
    ]
    assert len(denial_events) == 1
    assert denial_events[0]["action_claim_id"] == action_claim_id
    assert denial_events[0]["outcome"] == "blocked"
    assert graph._test_live_runner_calls == []


def test_authorization_denial_audit_failure_rolls_back_claim_transition(
    monkeypatch,
):
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="authorization-denial-audit"
    )
    state = _approved_monitor_state(occurrence, dispatch)
    original_record = audit.record_event_in_transaction

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("denial audit unavailable")

    monkeypatch.setattr(audit, "record_event_in_transaction", fail_audit)

    with pytest.raises(RuntimeError, match="denial audit unavailable"):
        _authorize_monitor_claim(
            occurrence,
            state["action_claim_id"],
            approval_id="apr-invalid",
        )

    assert claims.get_action_claim(state["action_claim_id"])["status"] == (
        "APPROVED"
    )
    assert not any(
        event["event_type"] == "action_authorization_denied"
        for event in audit.list_events(occurrence["incident_id"])
    )
    monkeypatch.setattr(audit, "record_event_in_transaction", original_record)
    assert not _authorize_monitor_claim(
        occurrence,
        state["action_claim_id"],
        approval_id="apr-invalid",
    )
    assert claims.get_action_claim(state["action_claim_id"])["status"] == (
        "BLOCKED"
    )
    assert sum(
        event["event_type"] == "action_authorization_denied"
        for event in audit.list_events(occurrence["incident_id"])
    ) == 1
    assert not _authorize_monitor_claim(
        occurrence,
        state["action_claim_id"],
        approval_id="apr-invalid",
    )
    assert sum(
        event["event_type"] == "action_authorization_denied"
        for event in audit.list_events(occurrence["incident_id"])
    ) == 1
    assert graph._test_live_runner_calls == []


def test_repeated_authorization_denial_deduplicates_only_fingerprinted_events():
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="repeated-authorization-denial"
    )
    action_claim_id, _ = _approve_occurrence_action(
        occurrence, dispatch["workflow_claim_id"]
    )

    assert not _authorize_monitor_claim(
        occurrence, action_claim_id, approval_id="apr-invalid"
    )
    first_events = [
        event for event in audit.list_events(occurrence["incident_id"])
        if event["event_type"] == "action_authorization_denied"
    ]
    assert claims.get_action_claim(action_claim_id)["status"] == "BLOCKED"
    assert len(first_events) == 1

    with database.get_connection() as conn:
        conn.execute(
            "UPDATE action_claims SET updated_at = "
            "'2030-01-01T00:00:00+00:00' WHERE action_claim_id = ?",
            (action_claim_id,),
        )
    assert not _authorize_monitor_claim(
        occurrence, action_claim_id, approval_id="apr-invalid"
    )
    assert sum(
        event["event_type"] == "action_authorization_denied"
        for event in audit.list_events(occurrence["incident_id"])
    ) == 1

    legacy_details = {
        key: value
        for key, value in first_events[0]["details"].items()
        if key != "denial_fingerprint"
    }
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE audit_events SET details = ? WHERE event_id = ?",
            (json.dumps(legacy_details), first_events[0]["event_id"]),
        )

    assert not _authorize_monitor_claim(
        occurrence, action_claim_id, approval_id="apr-invalid"
    )
    repeated_events = [
        event for event in audit.list_events(occurrence["incident_id"])
        if event["event_type"] == "action_authorization_denied"
    ]
    assert len(repeated_events) == 2


def test_distinct_authorization_denial_context_is_audited():
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="distinct-authorization-denial"
    )
    action_claim_id, _ = _approve_occurrence_action(
        occurrence, dispatch["workflow_claim_id"]
    )

    assert not _authorize_monitor_claim(
        occurrence, action_claim_id, approval_id="apr-invalid"
    )
    assert not _authorize_monitor_claim(
        occurrence,
        action_claim_id,
        approval_id="apr-invalid",
        expected_service="different-service",
    )

    denial_events = [
        event for event in audit.list_events(occurrence["incident_id"])
        if event["event_type"] == "action_authorization_denied"
    ]
    assert len(denial_events) == 2
    assert {event["actor"] for event in denial_events} == {"human-reviewer"}
    assert {event["approval_id"] for event in denial_events} == {
        "apr-invalid"
    }
    assert {json.dumps(event["policy_decision"], sort_keys=True)
            for event in denial_events} == {"null"}
    assert len({
        event["details"]["denial_fingerprint"] for event in denial_events
    }) == 2


def test_remediation_denial_is_fail_closed_and_not_audited_twice(monkeypatch):
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="authorization-denial-caller"
    )
    state = _approved_monitor_state(occurrence, dispatch)
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE approval_requests SET status = 'REJECTED' "
            "WHERE approval_id = ?",
            (state["approval_id"],),
        )
    executed = []
    monkeypatch.setattr(
        graph, "execute_action", lambda proposal: executed.append(proposal)
    )

    result = graph.remediate(state)

    events = audit.list_events(occurrence["incident_id"])
    denial_events = [
        event for event in events
        if event["event_type"] == "action_authorization_denied"
    ]
    assert result["execution"]["error"] == "action_claim_unavailable"
    assert claims.get_action_claim(state["action_claim_id"])["status"] == (
        "BLOCKED"
    )
    assert len(denial_events) == 1
    assert not any(
        event["event_type"] == "action_claim_unavailable"
        for event in events
    )
    assert executed == []


def test_valid_distinct_human_approval_authorizes_monitor_action():
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="valid-human-approval"
    )
    action_claim_id, approval_id = _approve_occurrence_action(
        occurrence, dispatch["workflow_claim_id"]
    )

    assert _authorize_monitor_claim(
        occurrence, action_claim_id, approval_id
    )
    assert claims.get_action_claim(action_claim_id)["status"] == "EXECUTING"
    assert any(
        event["event_type"] == "remediation_started"
        and event["actor"] == "human-reviewer"
        for event in audit.list_events(occurrence["incident_id"])
    )
    assert graph._test_live_runner_calls == []


@pytest.mark.parametrize(
    ("authorization_offset", "expected_authorized"),
    [
        (timedelta(seconds=1), False),
        (timedelta(0), False),
        (-timedelta(microseconds=1), True),
    ],
    ids=["after-expiry", "at-expiry", "before-expiry"],
)
def test_monitor_authorization_enforces_persisted_approval_expiry(
    monkeypatch, authorization_offset, expected_authorized
):
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="approval-expiry"
    )
    state = _approved_monitor_state(occurrence, dispatch)
    action_claim_id = state["action_claim_id"]
    approval_id = state["approval_id"]
    expiry = datetime(2030, 1, 1, tzinfo=timezone.utc)
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE approval_requests SET expires_at = ? WHERE approval_id = ?",
            (expiry.isoformat(), approval_id),
        )

    # The approval is already decided; only the authorization clock advances.
    monkeypatch.setattr(
        claims,
        "_now",
        lambda: expiry + authorization_offset,
    )
    if expected_authorized:
        authorized = _authorize_monitor_claim(
            occurrence, action_claim_id, approval_id
        )
        assert authorized
        expected_status = "EXECUTING"
    else:
        result = graph.remediate(state)
        assert result["execution"]["error"] == "action_claim_unavailable"
        expected_status = "BLOCKED"
    assert claims.get_action_claim(action_claim_id)["status"] == expected_status
    assert graph._test_live_runner_calls == []


@pytest.mark.parametrize(
    "invalid_expiry",
    ["", "not-a-timestamp", "2030-01-01T00:00:00"],
)
def test_monitor_authorization_fails_closed_for_invalid_approval_expiry(
    monkeypatch, invalid_expiry
):
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="invalid-approval-expiry"
    )
    action_claim_id, approval_id = _approve_occurrence_action(
        occurrence, dispatch["workflow_claim_id"]
    )
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE approval_requests SET expires_at = ? WHERE approval_id = ?",
            (invalid_expiry, approval_id),
        )
    monkeypatch.setattr(
        claims,
        "_now",
        lambda: datetime(2029, 1, 1, tzinfo=timezone.utc),
    )

    assert not _authorize_monitor_claim(
        occurrence, action_claim_id, approval_id
    )
    assert claims.get_action_claim(action_claim_id)["status"] == "BLOCKED"
    assert graph._test_live_runner_calls == []


def test_execution_outcome_audit_failure_keeps_claim_nonterminal(
    monkeypatch,
):
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="outcome-audit"
    )
    state = _approved_monitor_state(occurrence, dispatch)
    original_record = audit.record_event_in_transaction
    event_count = 0

    def fail_second_event(conn, **kwargs):
        nonlocal event_count
        event_count += 1
        if event_count == 2:
            raise RuntimeError("outcome audit unavailable")
        return original_record(conn, **kwargs)

    monkeypatch.setattr(audit, "record_event_in_transaction", fail_second_event)
    monkeypatch.setattr(
        graph,
        "execute_action",
        lambda _proposal: {
            "mode": "live", "executed": True, "ok": True,
        },
    )
    result = graph.remediate(state)

    assert result["execution"]["ok"] is False
    assert result["execution"]["error"] == "action_claim_outcome_unavailable"
    assert claims.get_action_claim(state["action_claim_id"])["status"] == (
        "EXECUTING"
    )
    events = audit.list_events(occurrence["incident_id"])
    assert sum(event["event_type"] == "remediation_started" for event in events) == 1
    assert not any(event["event_type"] == "remediation_executed" for event in events)


def test_runner_exception_is_unknown_and_never_replayed(monkeypatch):
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="runner-exception"
    )
    state = _approved_monitor_state(occurrence, dispatch)
    calls = []

    def uncertain_runner(_proposal):
        calls.append(True)
        raise RuntimeError("connection lost after command start")

    monkeypatch.setattr(graph, "execute_action", uncertain_runner)
    first = graph.remediate(state)
    second = graph.remediate(state)

    assert first["execution"]["error"] == "execution_outcome_unknown"
    assert second["execution"]["error"] == "action_claim_unavailable"
    assert len(calls) == 1
    assert claims.get_action_claim(state["action_claim_id"])["status"] == (
        "EXECUTION_UNKNOWN"
    )
    assert any(
        event["event_type"] == "remediation_execution_unknown"
        for event in audit.list_events(occurrence["incident_id"])
    )


def test_actual_remediation_wrapper_preserves_ambiguous_runner_exception(
    monkeypatch,
):
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="wrapper-runner-exception"
    )
    state = _approved_monitor_state(occurrence, dispatch)
    calls = []

    def runner(args):
        calls.append(args)
        if args == ["config", "current-context"]:
            return remediation.CommandResult(0, "kind-kybernaut\n")
        raise TimeoutError("command process timed out")

    monkeypatch.setenv("KYBERNAUT_REMEDIATION_MODE", "live")
    monkeypatch.setattr(graph, "execute_action", remediation.execute_action)
    monkeypatch.setattr(remediation, "default_runner", runner)
    first = graph.remediate(state)
    second = graph.remediate(state)

    action = claims.get_action_claim(state["action_claim_id"])
    unknown_event = next(
        event for event in audit.list_events(occurrence["incident_id"])
        if event["event_type"] == "remediation_execution_unknown"
    )
    assert first["execution"]["outcome_unknown"] is True
    assert first["execution"]["executed"] is True
    assert second["execution"]["error"] == "action_claim_unavailable"
    assert action["status"] == "EXECUTION_UNKNOWN"
    assert unknown_event["outcome"] == "unknown"
    assert calls == [
        ["config", "current-context"],
        [
            "delete", "pod", "demo-web-pod", "-n", "kybernaut-lab",
            "--wait=false", "--context", "kind-kybernaut",
        ],
    ]
    _interrupt_handoff(occurrence["id"], dispatch["workflow_claim_id"])
    assert handoffs.reconcile_expired_leases(occurrence["service"]) == 1
    assert handoffs.get_handoff(occurrence["id"])["status"] == (
        "RECONCILIATION_REQUIRED"
    )
    assert incident_repository.get_incident_occurrence(
        occurrence["id"]
    )["status"] == "OPEN"
    assert graph._test_live_runner_calls == []


def test_actual_remediation_wrapper_keeps_confirmed_prestart_failure_distinct(
    monkeypatch,
):
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="wrapper-prestart-failure"
    )
    state = _approved_monitor_state(occurrence, dispatch)
    calls = []

    def runner(args):
        calls.append(args)
        return remediation.CommandResult(0, "other-context\n")

    monkeypatch.setenv("KYBERNAUT_REMEDIATION_MODE", "live")
    monkeypatch.setattr(graph, "execute_action", remediation.execute_action)
    monkeypatch.setattr(remediation, "default_runner", runner)
    result = graph.remediate(state)

    action = claims.get_action_claim(state["action_claim_id"])
    assert result["execution"]["executed"] is False
    assert result["execution"].get("outcome_unknown") is not True
    assert action["status"] == "EXECUTION_FAILED"
    assert calls == [["config", "current-context"]]
    assert not any(
        event["event_type"] == "remediation_execution_unknown"
        for event in audit.list_events(occurrence["incident_id"])
    )
    assert graph._test_live_runner_calls == []


@pytest.mark.asyncio
async def test_verification_audit_failure_rolls_back_verification(monkeypatch):
    occurrence = _create_occurrence()
    dispatch = handoffs.claim_next_handoff(
        occurrence["service"], lease_owner="verification-audit"
    )
    state = _approved_monitor_state(occurrence, dispatch)
    action_id = state["action_claim_id"]
    assert claims.transition_action_claim(action_id, "APPROVED", "EXECUTING")
    assert claims.transition_action_claim(
        action_id,
        "EXECUTING",
        "COMPLETED",
        execution={"mode": "live", "executed": True, "ok": True},
    )

    async def verified(**_kwargs):
        return {"recovered": True, "reason": "healthy"}

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("verification audit unavailable")

    monkeypatch.setattr(graph, "verify_recovery", verified)
    monkeypatch.setattr(audit, "record_event_in_transaction", fail_audit)
    result = await graph.verify(state)

    assert result["verification"]["recovered"] is False
    assert result["verification"]["persistence_failed"] is True
    assert claims.get_action_claim(action_id)["status"] == "COMPLETED"
    assert claims.get_action_claim(action_id)["verification_json"] is None