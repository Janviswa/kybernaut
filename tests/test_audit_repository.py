from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import sqlite3

import pytest

import app.database as database
from app.services import audit_repository as audit
from app.services import incident_repository


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "a.db")


def test_events_round_trip_in_order():
    audit.record_event(incident_ref="I1", event_type="a", actor="x",
                       summary="one", details={"k": 1})
    audit.record_event(incident_ref="I1", event_type="b", actor="x",
                       summary="two", policy_decision={"allowed": True})
    audit.record_event(incident_ref="I2", event_type="c", actor="x",
                       summary="other")
    events = audit.list_events("I1")
    assert [e["event_type"] for e in events] == ["a", "b"]
    assert events[0]["details"] == {"k": 1}
    assert events[1]["policy_decision"] == {"allowed": True}


def test_newest_first_uses_event_timestamp_and_limit():
    for event_type in ("old", "newest", "middle"):
        audit.record_event(
            incident_ref="I1",
            event_type=event_type,
            actor="x",
            summary=event_type,
        )
    with database.get_connection() as conn:
        conn.executemany(
            "UPDATE audit_events SET timestamp = ? WHERE event_type = ?",
            [
                ("2026-10-27T10:00:00+00:00", "old"),
                ("2026-10-27T13:00:00+00:00", "newest"),
                ("2026-10-27T12:00:00+00:00", "middle"),
            ],
        )

    events = audit.list_events("I1", 2, newest_first=True)

    assert [event["event_type"] for event in events] == ["newest", "middle"]


def test_approval_decided_once():
    a = audit.create_approval(incident_ref="I1", proposed_action={"x": 1},
                              context={}, requested_by="op")
    assert a["status"] == "PENDING"
    done = audit.decide_approval(a["approval_id"], "approved", "boss", "ok")
    assert done["status"] == "APPROVED" and done["decided_by"] == "boss"
    with pytest.raises(audit.ApprovalError) as e:
        audit.decide_approval(a["approval_id"], "rejected", "other")
    assert e.value.code == "already_decided"


def test_duplicate_pending_incident_action_is_rejected():
    first = audit.create_approval(
        incident_ref="I-DUPLICATE",
        proposed_action={"action": "restart_pod", "resource": "demo-web-1"},
        context={},
        requested_by="operator-a",
    )

    with pytest.raises(audit.ApprovalError) as error:
        audit.create_approval(
            incident_ref="I-DUPLICATE",
            proposed_action={
                "resource": "demo-web-1",
                "action": "restart_pod",
            },
            context={},
            requested_by="operator-b",
        )

    assert error.value.code == "duplicate_pending"
    assert audit.get_approval(first["approval_id"])["status"] == "PENDING"
    assert len(audit.list_approvals()) == 1


def test_production_approval_requires_active_incident_and_matching_service():
    database.initialize_database()

    with pytest.raises(audit.ApprovalError) as missing:
        audit.create_approval(
            incident_ref="INC-NOT-PERSISTED",
            proposed_action={"action": "restart_pod"},
            context={},
            requested_by="operator-a",
            require_active_incident=True,
            expected_service="kubernetes-lab",
        )
    assert missing.value.code == "incident_not_active"

    incident = incident_repository.record_incident({
        "incident_id": "INC-SERVICE-MISMATCH",
        "service": "order-service",
        "type": "cpu_spike",
        "severity": "medium",
        "message": "Synthetic test occurrence",
        "fingerprint": "test:service-mismatch",
    })
    with pytest.raises(audit.ApprovalError) as wrong_service:
        audit.create_approval(
            incident_ref=incident["incident_id"],
            proposed_action={"action": "restart_pod"},
            context={},
            requested_by="operator-a",
            require_active_incident=True,
            expected_service="kubernetes-lab",
        )
    assert wrong_service.value.code == "incident_not_active"

    approval = audit.create_approval(
        incident_ref=incident["incident_id"],
        proposed_action={"action": "restart_pod"},
        context={},
        requested_by="operator-a",
        require_active_incident=True,
        expected_service="order-service",
    )
    assert approval["status"] == "PENDING"


def test_reconcile_preserves_and_audits_approval_for_resolved_incident():
    database.initialize_database()
    occurrence = incident_repository.record_incident({
        "incident_id": "INC-RESOLVED-APPROVAL",
        "service": "inventory-service",
        "type": "memory_leak",
        "severity": "medium",
        "message": "Synthetic test occurrence",
        "fingerprint": "test:resolved-approval",
    })
    with database.get_connection() as conn:
        conn.execute(
            "UPDATE incidents SET status = 'RESOLVED' WHERE id = ?",
            (occurrence["id"],),
        )
    approval = audit.create_approval(
        incident_ref=occurrence["incident_id"],
        proposed_action={"action": "restart_pod"},
        context={},
        requested_by="operator-a",
    )

    assert audit.reconcile_pending_approvals() == 1

    persisted = audit.get_approval(approval["approval_id"])
    assert persisted["status"] == "CANCELLED"
    assert persisted["decision_reason"] == "incident_resolved"
    assert [
        event["event_type"]
        for event in audit.list_events(occurrence["incident_id"])
    ] == ["approval_requested", "approval_cancelled"]


def test_requester_can_approve_own_action():
    approval = audit.create_approval(
        incident_ref="I-SELF-APPROVAL",
        proposed_action={"action": "restart_pod"},
        context={},
        requested_by="operator-a",
    )

    result = audit.decide_approval(
        approval["approval_id"], "approved", "operator-a"
    )

    assert result["status"] == "APPROVED"
    assert result["decided_by"] == "operator-a"


def test_concurrent_approval_decisions_commit_at_most_once():
    approval = audit.create_approval(
        incident_ref="I-CONCURRENT",
        proposed_action={"action": "restart_pod"},
        context={},
        requested_by="op",
    )
    barrier = Barrier(2)

    def decide(decision):
        barrier.wait()
        try:
            result = audit.decide_approval(
                approval["approval_id"], decision, decision
            )
            return result["status"]
        except audit.ApprovalError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(decide, ("approved", "rejected")))

    assert sum(outcome in {"APPROVED", "REJECTED"} for outcome in outcomes) == 1
    assert sum(outcome == "already_decided" for outcome in outcomes) == 1
    assert audit.get_approval(approval["approval_id"])["status"] in {
        "APPROVED", "REJECTED"
    }


def test_expired_and_unknown_approval():
    a = audit.create_approval(incident_ref="I1", proposed_action={},
                              context={}, requested_by="op", ttl_seconds=-5)
    with pytest.raises(audit.ApprovalError) as e:
        audit.decide_approval(a["approval_id"], "approved", "boss")
    assert e.value.code == "expired"
    assert audit.get_approval(a["approval_id"])["status"] == "EXPIRED"
    with pytest.raises(audit.ApprovalError) as e:
        audit.decide_approval("apr-missing", "approved", "boss")
    assert e.value.code == "not_found"


def test_invalid_decision_rejected():
    with pytest.raises(audit.ApprovalError):
        audit.decide_approval("x", "maybe", "boss")


def test_recent_action_count_only_counts_executions():
    audit.record_event(incident_ref="I", event_type="remediation_executed",
                       actor="x", summary="s", resource="demo-web")
    audit.record_event(incident_ref="I", event_type="remediation_dry_run",
                       actor="x", summary="s", resource="demo-web")
    assert audit.count_recent_actions("demo-web", 3600) == 1
    assert audit.count_recent_actions("other", 3600) == 0


def test_claim_link_schema_migrates_existing_audit_and_approval_rows():
    with sqlite3.connect(database.DATABASE_PATH) as conn:
        conn.execute(
            """CREATE TABLE audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                incident_ref TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                actor TEXT NOT NULL,
                event_type TEXT NOT NULL,
                summary TEXT NOT NULL,
                resource TEXT,
                policy_decision TEXT,
                approval_id TEXT,
                outcome TEXT,
                details TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE approval_requests (
                approval_id TEXT PRIMARY KEY,
                incident_ref TEXT NOT NULL,
                status TEXT NOT NULL,
                proposed_action TEXT NOT NULL,
                context TEXT,
                requested_by TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                decided_by TEXT,
                decided_at TEXT,
                decision_reason TEXT
            )"""
        )
        conn.execute(
            """INSERT INTO audit_events (
                event_id, incident_ref, timestamp, actor, event_type, summary
            ) VALUES ('legacy-event', 'INC-OLD', '2026-01-01T00:00:00+00:00',
                'operator', 'legacy', 'preserve')"""
        )
        conn.execute(
            """INSERT INTO approval_requests (
                approval_id, incident_ref, status, proposed_action, requested_by,
                requested_at, expires_at
            ) VALUES ('legacy-approval', 'INC-OLD', 'PENDING', '{}', 'operator',
                '2026-01-01T00:00:00+00:00', '2027-01-01T00:00:00+00:00')"""
        )

    audit.ensure_schema()

    with database.get_connection() as conn:
        event_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(audit_events)")
        }
        approval_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(approval_requests)")
        }
        event = conn.execute(
            "SELECT summary, action_claim_id, workflow_claim_id "
            "FROM audit_events WHERE event_id = 'legacy-event'"
        ).fetchone()
        approval = conn.execute(
            "SELECT status, action_claim_id FROM approval_requests "
            "WHERE approval_id = 'legacy-approval'"
        ).fetchone()

    assert {"action_claim_id", "workflow_claim_id"} <= event_columns
    assert "action_claim_id" in approval_columns
    assert event["summary"] == "preserve"
    assert event["action_claim_id"] is None
    assert event["workflow_claim_id"] is None
    assert approval["status"] == "PENDING"
    assert approval["action_claim_id"] is None
