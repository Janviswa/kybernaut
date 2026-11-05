import importlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

import app.database as database
from app.services import action_claim_repository as claims
from app.services import audit_repository as audit


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "claims.db")


def workflow_claim(operator_id, key, incident_ref="INC-1"):
    return claims.create_workflow_claim(
        operator_id=operator_id,
        service="kubernetes-lab",
        idempotency_key=key,
        incident_ref=incident_ref,
        request_correlation=incident_ref,
    )


def reserve(workflow_id, *, logical_key="logical-A", deployment="demo-web",
            attempt_no=0, limit=3):
    return claims.reserve_action_claim(
        workflow_claim_id=workflow_id,
        attempt_no=attempt_no,
        logical_key=logical_key,
        deployment=deployment,
        action={"action": "restart_pod", "deployment": deployment},
        policy={"allowed": True, "requires_approval": True},
        max_actions=limit,
        window_seconds=3600,
        auto_authorized=False,
    )


def test_workflow_idempotency_is_durable_and_request_bound():
    first, created, conflict = workflow_claim("operator", "same-key")
    duplicate, duplicate_created, duplicate_conflict = workflow_claim(
        "operator", "same-key"
    )

    assert created and not conflict
    assert not duplicate_created and not duplicate_conflict
    assert duplicate["claim_id"] == first["claim_id"]
    assert duplicate["status"] == "RUNNING"

    different_request, different_created, different_conflict = workflow_claim(
        "operator", "same-key", "INC-other"
    )
    assert not different_created and different_conflict
    assert different_request["claim_id"] == first["claim_id"]


def test_concurrent_same_idempotency_key_creates_one_persisted_claim():
    barrier = Barrier(2)

    def create(_):
        barrier.wait()
        return workflow_claim("operator", "concurrent-key")

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(create, range(2)))

    assert sum(created for _, created, _ in results) == 1
    assert len({row["claim_id"] for row, _, _ in results}) == 1
    assert not any(conflict for _, _, conflict in results)


def test_sqlite_write_lock_conflict_does_not_create_partial_claim(monkeypatch):
    claims.ensure_schema()
    lock_holder = database.get_connection()
    lock_holder.execute("BEGIN IMMEDIATE")
    original_get_connection = claims.get_connection

    def short_timeout_connection():
        connection = original_get_connection()
        connection.execute("PRAGMA busy_timeout = 50")
        return connection

    monkeypatch.setattr(claims, "get_connection", short_timeout_connection)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            workflow_claim("operator", "locked-key")
    finally:
        lock_holder.rollback()
        lock_holder.close()

    with database.get_connection() as conn:
        count = conn.execute(
            """SELECT COUNT(*) AS n FROM workflow_claims
               WHERE operator_id = ? AND service = ?""",
            ("operator", "kubernetes-lab"),
        ).fetchone()["n"]
    assert count == 0


def test_same_logical_action_concurrent_claim_is_exclusive():
    first, _, _ = workflow_claim("operator-a", "key-a")
    second, _, _ = workflow_claim("operator-b", "key-b", "INC-2")
    barrier = Barrier(2)

    def attempt(workflow_id):
        barrier.wait()
        return reserve(workflow_id)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, (first["claim_id"], second["claim_id"])))

    assert sum(result["allowed"] for result in results) == 1
    rejected = next(result for result in results if not result["allowed"])
    assert rejected["duplicate_claim_id"]


def test_different_incidents_compete_atomically_for_deployment_capacity():
    first, _, _ = workflow_claim("operator-a", "rate-a", "INC-A")
    second, _, _ = workflow_claim("operator-b", "rate-b", "INC-B")
    barrier = Barrier(2)

    def attempt(workflow_id, logical_key):
        barrier.wait()
        return reserve(
            workflow_id, logical_key=logical_key, limit=1
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(
            lambda pair: attempt(*pair),
            ((first["claim_id"], "logical-A"), (second["claim_id"], "logical-B")),
        ))

    assert sum(result["allowed"] for result in results) == 1
    assert all(result.get("attempt_count") == 1 for result in results)
    assert any("limit reached" in result.get("reason", "") for result in results)


def test_blocked_claim_does_not_consume_action_capacity():
    workflow, _, _ = workflow_claim("operator", "persist")
    action = reserve(workflow["claim_id"], limit=1)
    action_id = action["action_claim_id"]

    assert claims.transition_action_claim(action_id, "RESERVED", "BLOCKED")
    assert claims.get_action_claim(action_id)["status"] == "BLOCKED"
    assert claims.count_recent_action_claims("demo-web", 3600) == 0

    second, _, _ = workflow_claim("operator-2", "second")
    denied = reserve(second["claim_id"], logical_key="logical-B", limit=1)
    assert denied["allowed"] is True
    assert denied["attempt_count"] == 1


def test_dry_run_claim_does_not_consume_action_capacity():
    workflow, _, _ = workflow_claim("operator", "dry-run")
    action = reserve(workflow["claim_id"], limit=1)
    action_id = action["action_claim_id"]
    for current, following in (
        ("RESERVED", "AWAITING_APPROVAL"),
        ("AWAITING_APPROVAL", "APPROVED"),
        ("APPROVED", "EXECUTING"),
        ("EXECUTING", "DRY_RUN_COMPLETE"),
    ):
        assert claims.transition_action_claim(
            action_id,
            current,
            following,
            execution=(
                {"mode": "dry_run", "executed": False, "ok": True}
                if following == "DRY_RUN_COMPLETE" else None
            ),
        )

    assert claims.count_recent_action_claims("demo-web", 3600) == 0
    second, _, _ = workflow_claim("operator-2", "after-dry-run")
    assert reserve(second["claim_id"], logical_key="logical-B", limit=1)["allowed"]


def test_execution_failure_consumes_action_capacity():
    workflow, _, _ = workflow_claim("operator", "execution-failure")
    action = reserve(workflow["claim_id"], limit=1)
    action_id = action["action_claim_id"]
    for current, following in (
        ("RESERVED", "AWAITING_APPROVAL"),
        ("AWAITING_APPROVAL", "APPROVED"),
        ("APPROVED", "EXECUTING"),
        ("EXECUTING", "EXECUTION_FAILED"),
    ):
        assert claims.transition_action_claim(action_id, current, following)

    assert claims.count_recent_action_claims("demo-web", 3600) == 1
    second, _, _ = workflow_claim("operator-2", "after-failed-execution")
    denied = reserve(second["claim_id"], logical_key="logical-B", limit=1)
    assert denied["allowed"] is False
    assert "limit reached" in denied["reason"]


def test_workflow_claim_survives_repository_reload():
    workflow, created, _ = workflow_claim("operator", "restart")
    assert created

    importlib.reload(claims)
    resumed = claims.get_workflow_claim(workflow["claim_id"])

    assert resumed is not None
    assert resumed["status"] == "RUNNING"
    assert resumed["idempotency_hash"] == workflow["idempotency_hash"]


def test_failed_reservation_transaction_rolls_back_for_retry():
    workflow, _, _ = workflow_claim("operator", "rollback")
    with pytest.raises(TypeError):
        reserve(workflow["claim_id"], limit=None)

    result = reserve(workflow["claim_id"], limit=2)
    assert result["allowed"] is True
    assert claims.get_action_claim(result["action_claim_id"]) is not None


def test_claim_transition_rejects_illegal_state_change():
    workflow, _, _ = workflow_claim("operator", "transition")
    action = reserve(workflow["claim_id"])

    with pytest.raises(claims.ClaimError, match="Illegal"):
        claims.transition_action_claim(
            action["action_claim_id"], "RESERVED", "COMPLETED"
        )


def test_simulation_claim_is_terminal_nonverified_and_does_not_count_as_execution():
    workflow, _, _ = workflow_claim("operator", "simulation")
    action = reserve(workflow["claim_id"], logical_key="simulated-logical")
    action_id = action["action_claim_id"]
    assert claims.transition_action_claim(
        action_id, "RESERVED", "AWAITING_APPROVAL"
    )
    assert claims.transition_action_claim(
        action_id, "AWAITING_APPROVAL", "APPROVED"
    )
    assert claims.transition_action_claim(action_id, "APPROVED", "EXECUTING")
    assert claims.transition_action_claim(
        action_id,
        "EXECUTING",
        "SIMULATED",
        execution={
            "mode": "simulated",
            "executed": False,
            "ok": True,
        },
    )

    assert claims.get_action_claim(action_id)["status"] == "SIMULATED"
    assert claims.count_recent_action_claims("demo-web", 3600) == 0
    assert claims.record_action_verification(
        action_id, {"recovered": True}
    ) is False
    with pytest.raises(claims.ClaimError, match="Illegal"):
        claims.transition_action_claim(action_id, "SIMULATED", "VERIFIED")

    next_workflow, _, _ = workflow_claim("operator-2", "after-simulation")
    second = reserve(
        next_workflow["claim_id"],
        logical_key="simulated-logical",
        attempt_no=1,
        limit=2,
    )
    assert second["allowed"] is True
    assert second["attempt_count"] == 1


def test_approval_is_bound_to_claim_action_and_policy_atomically():
    workflow, _, _ = workflow_claim("operator", "approval")
    proposal = {"action": "restart_pod", "deployment": "demo-web"}
    policy = {"allowed": True, "requires_approval": True}
    action = claims.reserve_action_claim(
        workflow_claim_id=workflow["claim_id"],
        attempt_no=0,
        logical_key="approval-logical",
        deployment="demo-web",
        action=proposal,
        policy=policy,
        max_actions=2,
        window_seconds=3600,
        auto_authorized=False,
    )
    approval = audit.create_approval(
        incident_ref="INC-1",
        proposed_action=proposal,
        context={"policy": policy},
        requested_by="operator",
        action_claim_id=action["action_claim_id"],
    )

    assert approval["action_claim_id"] == action["action_claim_id"]
    assert claims.get_action_claim(action["action_claim_id"])["approval_id"] == (
        approval["approval_id"]
    )
    decided = audit.decide_approval(
        approval["approval_id"], "approved", "reviewer"
    )
    assert decided["status"] == "APPROVED"
    assert claims.get_action_claim(action["action_claim_id"])["status"] == (
        "APPROVED"
    )
    events = audit.list_events("INC-1")
    assert [event["event_type"] for event in events].count(
        "approval_requested"
    ) == 1
    assert [event["event_type"] for event in events].count(
        "approval_approved"
    ) == 1


def test_approval_request_audit_failure_rolls_back_request_and_claim(monkeypatch):
    workflow, _, _ = workflow_claim("operator", "approval-audit-request")
    action = reserve(workflow["claim_id"], logical_key="approval-audit-request")

    def fail_audit(*_args, **_kwargs):
        raise sqlite3.OperationalError("audit unavailable")

    monkeypatch.setattr(audit, "record_event_in_transaction", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="audit unavailable"):
        audit.create_approval(
            incident_ref="INC-1",
            proposed_action={
                "action": "restart_pod",
                "deployment": "demo-web",
            },
            context={
                "policy": {"allowed": True, "requires_approval": True},
            },
            requested_by="operator",
            action_claim_id=action["action_claim_id"],
        )

    assert audit.list_approvals() == []
    assert claims.get_action_claim(action["action_claim_id"])["status"] == (
        "RESERVED"
    )


def test_approval_decision_audit_failure_rolls_back_decision_and_claim(
    monkeypatch,
):
    workflow, _, _ = workflow_claim("operator", "approval-audit-decision")
    action = reserve(workflow["claim_id"], logical_key="approval-audit-decision")
    approval = audit.create_approval(
        incident_ref="INC-1",
        proposed_action={"action": "restart_pod", "deployment": "demo-web"},
        context={"policy": {"allowed": True, "requires_approval": True}},
        requested_by="operator",
        action_claim_id=action["action_claim_id"],
    )

    def fail_audit(*_args, **_kwargs):
        raise sqlite3.OperationalError("audit unavailable")

    monkeypatch.setattr(audit, "record_event_in_transaction", fail_audit)
    with pytest.raises(sqlite3.OperationalError, match="audit unavailable"):
        audit.decide_approval(approval["approval_id"], "approved", "reviewer")

    assert audit.get_approval(approval["approval_id"])["status"] == "PENDING"
    assert claims.get_action_claim(action["action_claim_id"])["status"] == (
        "AWAITING_APPROVAL"
    )
    assert claims.get_workflow_claim(workflow["claim_id"])["status"] == "RUNNING"


def test_non_boolean_recovery_evidence_is_not_recorded_as_verified():
    workflow, _, _ = workflow_claim("operator", "verification-type")
    action = reserve(workflow["claim_id"], logical_key="verification-type")
    action_id = action["action_claim_id"]
    assert claims.transition_action_claim(
        action_id, "RESERVED", "AWAITING_APPROVAL"
    )
    assert claims.transition_action_claim(
        action_id, "AWAITING_APPROVAL", "APPROVED"
    )
    assert claims.transition_action_claim(action_id, "APPROVED", "EXECUTING")
    assert claims.transition_action_claim(
        action_id,
        "EXECUTING",
        "COMPLETED",
        execution={"mode": "live", "executed": True, "ok": True},
    )

    assert claims.record_action_verification(
        action_id,
        {"recovered": "yes", "reason": "malformed evidence"},
        audit_event={
            "incident_ref": "INC-1",
            "event_type": "verification_completed",
            "actor": "workflow",
            "summary": "Verification failed",
            "outcome": "not_recovered",
            "details": {"recovered": "yes"},
        },
    )

    assert claims.get_action_claim(action_id)["status"] == "VERIFICATION_FAILED"
    verification_event = next(
        event for event in audit.list_events("INC-1")
        if event["event_type"] == "verification_completed"
    )
    assert verification_event["outcome"] == "not_recovered"


def test_approval_and_claim_roll_back_if_workflow_cannot_resume():
    workflow, _, _ = workflow_claim("operator", "approval-resume-failure")
    action = reserve(workflow["claim_id"], logical_key="approval-resume")
    approval = audit.create_approval(
        incident_ref="INC-1",
        proposed_action={"action": "restart_pod", "deployment": "demo-web"},
        context={"policy": {"allowed": True, "requires_approval": True}},
        requested_by="operator",
        action_claim_id=action["action_claim_id"],
    )
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE workflow_claims SET status = 'COMPLETED' WHERE claim_id = ?",
            (workflow["claim_id"],),
        )

    with pytest.raises(audit.ApprovalError, match="could not be resumed"):
        audit.decide_approval(approval["approval_id"], "approved", "reviewer")

    assert audit.get_approval(approval["approval_id"])["status"] == "PENDING"
    assert claims.get_action_claim(action["action_claim_id"])["status"] == (
        "AWAITING_APPROVAL"
    )
    assert claims.get_workflow_claim(workflow["claim_id"])["status"] == "COMPLETED"


def test_mismatched_approval_cannot_be_created_or_decided():
    workflow, _, _ = workflow_claim("operator", "mismatch")
    proposal = {"action": "restart_pod", "deployment": "demo-web"}
    policy = {"allowed": True, "requires_approval": True}
    action = claims.reserve_action_claim(
        workflow_claim_id=workflow["claim_id"],
        attempt_no=0,
        logical_key="mismatch-logical",
        deployment="demo-web",
        action=proposal,
        policy=policy,
        max_actions=2,
        window_seconds=3600,
        auto_authorized=False,
    )

    with pytest.raises(audit.ApprovalError, match="does not match"):
        audit.create_approval(
            incident_ref="INC-1",
            proposed_action={"action": "rollback_deployment"},
            context={"policy": policy},
            requested_by="operator",
            action_claim_id=action["action_claim_id"],
        )

    approval = audit.create_approval(
        incident_ref="INC-1",
        proposed_action=proposal,
        context={"policy": policy},
        requested_by="operator",
        action_claim_id=action["action_claim_id"],
    )
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE approval_requests SET proposed_action = ? WHERE approval_id = ?",
            ('{"action":"rollback_deployment"}', approval["approval_id"]),
        )

    with pytest.raises(audit.ApprovalError, match="no longer matches"):
        audit.decide_approval(approval["approval_id"], "approved", "reviewer")
    assert audit.get_approval(approval["approval_id"])["status"] == "PENDING"
    assert claims.get_action_claim(action["action_claim_id"])["status"] == (
        "AWAITING_APPROVAL"
    )


def test_approval_cannot_bind_action_claim_to_different_incident():
    workflow, _, _ = workflow_claim("operator", "incident-binding", "INC-real")
    proposal = {"action": "restart_pod", "deployment": "demo-web"}
    policy = {"allowed": True, "requires_approval": True}
    action = claims.reserve_action_claim(
        workflow_claim_id=workflow["claim_id"],
        attempt_no=0,
        logical_key="incident-binding-logical",
        deployment="demo-web",
        action=proposal,
        policy=policy,
        max_actions=2,
        window_seconds=3600,
        auto_authorized=False,
    )

    with pytest.raises(audit.ApprovalError, match="does not match"):
        audit.create_approval(
            incident_ref="INC-spoofed",
            proposed_action=proposal,
            context={"policy": policy},
            requested_by="operator",
            action_claim_id=action["action_claim_id"],
        )

    assert audit.list_approvals() == []
    assert claims.get_action_claim(action["action_claim_id"])["status"] == (
        "RESERVED"
    )


def test_expired_approval_expires_its_claim():
    workflow, _, _ = workflow_claim("operator", "expired")
    proposal = {"action": "restart_pod", "deployment": "demo-web"}
    policy = {"allowed": True, "requires_approval": True}
    action = claims.reserve_action_claim(
        workflow_claim_id=workflow["claim_id"],
        attempt_no=0,
        logical_key="expired-logical",
        deployment="demo-web",
        action=proposal,
        policy=policy,
        max_actions=2,
        window_seconds=3600,
        auto_authorized=False,
    )
    approval = audit.create_approval(
        incident_ref="INC-1",
        proposed_action=proposal,
        context={"policy": policy},
        requested_by="operator",
        action_claim_id=action["action_claim_id"],
        ttl_seconds=-1,
    )

    with pytest.raises(audit.ApprovalError) as error:
        audit.decide_approval(approval["approval_id"], "approved", "reviewer")

    assert error.value.code == "expired"
    assert claims.get_action_claim(action["action_claim_id"])["status"] == "EXPIRED"
