"""Audit trail and approval requests (SQLite, via the existing connection).

Tables are created lazily by ``ensure_schema()`` (additive, IF NOT EXISTS),
so importing this module never touches a database.
"""

from __future__ import annotations

import json
import hashlib
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone

from app.database import get_connection
from app.services import action_claim_repository as claims


class ApprovalError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_schema() -> None:
    claims.ensure_schema()
    with closing(get_connection()) as conn, conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS audit_events (
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
                action_claim_id TEXT,
                workflow_claim_id TEXT,
                outcome TEXT,
                details TEXT
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_incident "
            "ON audit_events (incident_ref, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_resource_type "
            "ON audit_events (resource, event_type, timestamp)"
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS approval_requests (
                approval_id TEXT PRIMARY KEY,
                incident_ref TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                proposed_action TEXT NOT NULL,
                context TEXT,
                action_claim_id TEXT,
                requested_by TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                decided_by TEXT,
                decided_at TEXT,
                decision_reason TEXT
            )"""
        )
        event_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(audit_events)")
        }
        if "action_claim_id" not in event_columns:
            conn.execute("ALTER TABLE audit_events ADD COLUMN action_claim_id TEXT")
        if "workflow_claim_id" not in event_columns:
            conn.execute(
                "ALTER TABLE audit_events ADD COLUMN workflow_claim_id TEXT"
            )
        approval_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(approval_requests)")
        }
        if "action_claim_id" not in approval_columns:
            conn.execute(
                "ALTER TABLE approval_requests ADD COLUMN action_claim_id TEXT"
            )


def record_event(
    *,
    incident_ref: str,
    event_type: str,
    actor: str,
    summary: str,
    resource: str | None = None,
    policy_decision: dict | None = None,
    approval_id: str | None = None,
    action_claim_id: str | None = None,
    workflow_claim_id: str | None = None,
    outcome: str | None = None,
    details: dict | None = None,
) -> dict:
    ensure_schema()
    event = {
        "event_id": f"evt-{uuid.uuid4().hex[:16]}",
        "incident_ref": incident_ref,
        "timestamp": _now().isoformat(),
        "actor": actor,
        "event_type": event_type,
        "summary": summary,
        "resource": resource,
        "policy_decision": (
            json.dumps(policy_decision, default=str) if policy_decision else None
        ),
        "approval_id": approval_id,
        "action_claim_id": action_claim_id,
        "workflow_claim_id": workflow_claim_id,
        "outcome": outcome,
        "details": json.dumps(details, default=str) if details else None,
    }
    with closing(get_connection()) as conn, conn:
        record_event_in_transaction(conn, event=event)
    return event


def record_event_in_transaction(
    conn,
    *,
    event: dict | None = None,
    incident_ref: str | None = None,
    event_type: str | None = None,
    actor: str | None = None,
    summary: str | None = None,
    resource: str | None = None,
    policy_decision: dict | None = None,
    approval_id: str | None = None,
    action_claim_id: str | None = None,
    workflow_claim_id: str | None = None,
    outcome: str | None = None,
    details: dict | None = None,
) -> dict:
    """Insert an audit event using a caller-owned SQLite transaction."""
    if event is None:
        if not all((incident_ref, event_type, actor, summary)):
            raise ValueError("Audit event fields are incomplete")
        event = {
            "event_id": f"evt-{uuid.uuid4().hex[:16]}",
            "incident_ref": incident_ref,
            "timestamp": _now().isoformat(),
            "actor": actor,
            "event_type": event_type,
            "summary": summary,
            "resource": resource,
            "policy_decision": (
                json.dumps(policy_decision, default=str)
                if policy_decision else None
            ),
            "approval_id": approval_id,
            "action_claim_id": action_claim_id,
            "workflow_claim_id": workflow_claim_id,
            "outcome": outcome,
            "details": json.dumps(details, default=str) if details else None,
        }
    conn.execute(
        """INSERT INTO audit_events (event_id, incident_ref, timestamp,
            actor, event_type, summary, resource, policy_decision,
            approval_id, action_claim_id, workflow_claim_id, outcome, details)
           VALUES (:event_id, :incident_ref, :timestamp, :actor,
            :event_type, :summary, :resource, :policy_decision,
            :approval_id, :action_claim_id, :workflow_claim_id, :outcome,
            :details)""",
        event,
    )
    return event


def _decode(row) -> dict:
    data = dict(row)
    for key in ("policy_decision", "details", "proposed_action", "context"):
        if key in data and isinstance(data[key], str):
            try:
                data[key] = json.loads(data[key])
            except ValueError:
                pass
    return data


def list_events(
    incident_ref: str | None = None,
    limit: int = 100,
    *,
    newest_first: bool = False,
) -> list[dict]:
    ensure_schema()
    limit = max(1, min(int(limit), 1000))
    with closing(get_connection()) as conn:
        ordering = (
            "julianday(timestamp) DESC, id DESC"
            if newest_first
            else "id ASC"
        )
        if incident_ref:
            rows = conn.execute(
                "SELECT * FROM audit_events WHERE incident_ref = ? "
                f"ORDER BY {ordering} LIMIT ?",
                (incident_ref, limit),
            ).fetchall()
        else:
            global_ordering = (
                "julianday(timestamp) DESC, id DESC"
                if newest_first
                else "id DESC"
            )
            rows = conn.execute(
                f"SELECT * FROM audit_events ORDER BY {global_ordering} LIMIT ?",
                (limit,),
            ).fetchall()
    return [_decode(r) for r in rows]


def count_recent_actions(resource_deployment: str, window_seconds: int) -> int:
    """Count executed remediation events for a deployment in the window."""
    ensure_schema()
    since = (_now() - timedelta(seconds=window_seconds)).isoformat()
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_events "
            "WHERE resource = ? AND event_type = 'remediation_executed' "
            "AND timestamp >= ? AND action_claim_id IS NULL",
            (resource_deployment, since),
        ).fetchone()
    return int(row["n"])


def create_approval(
    *,
    incident_ref: str,
    proposed_action: dict,
    context: dict,
    requested_by: str,
    action_claim_id: str | None = None,
    ttl_seconds: int = 3600,
    require_active_incident: bool = False,
    expected_service: str | None = None,
) -> dict:
    ensure_schema()
    now = _now()
    proposed_action_json = json.dumps(
        proposed_action, sort_keys=True, separators=(",", ":"), default=str
    )
    approval = {
        "approval_id": f"apr-{uuid.uuid4().hex[:12]}",
        "incident_ref": incident_ref,
        "status": "PENDING",
        "proposed_action": proposed_action_json,
        "context": json.dumps(context, default=str),
        "action_claim_id": action_claim_id,
        "requested_by": requested_by,
        "requested_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(),
    }
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if require_active_incident:
            incident = conn.execute(
                """SELECT status, service FROM incidents
                   WHERE incident_id = ? ORDER BY id DESC LIMIT 1""",
                (incident_ref,),
            ).fetchone()
            if (
                incident is None
                or incident["status"] not in {"OPEN", "REMEDIATING"}
                or (
                    expected_service is not None
                    and incident["service"] != expected_service
                )
            ):
                raise ApprovalError(
                    "incident_not_active",
                    "Approval requires an active incident for the expected service",
                )
        workflow_claim_id = None
        if action_claim_id is not None:
            claim = conn.execute(
                """SELECT a.*, w.incident_ref AS claim_incident_ref,
                          w.claim_id AS workflow_claim_id
                   FROM action_claims a
                   JOIN workflow_claims w ON w.claim_id = a.workflow_claim_id
                   WHERE a.action_claim_id = ?""",
                (action_claim_id,),
            ).fetchone()
            action_hash = hashlib.sha256(
                json.dumps(
                    proposed_action, sort_keys=True, separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            policy_json = json.dumps(
                context.get("policy"), sort_keys=True,
                separators=(",", ":"), default=str,
            )
            if (
                claim is None
                or claim["claim_incident_ref"] != incident_ref
                or claim["status"] != "RESERVED"
                or claim["action_hash"] != action_hash
                or claim["policy_json"] != policy_json
            ):
                raise ApprovalError(
                    "claim_mismatch",
                    "Approval does not match the reserved action claim",
                )
            workflow_claim_id = claim["workflow_claim_id"]
        pending = conn.execute(
            """SELECT proposed_action FROM approval_requests
               WHERE incident_ref = ? AND status = 'PENDING'""",
            (incident_ref,),
        ).fetchall()
        if any(json.loads(row["proposed_action"]) == proposed_action for row in pending):
            raise ApprovalError(
                "duplicate_pending",
                "A pending approval already exists for this incident and action",
            )
        conn.execute(
            """INSERT INTO approval_requests (approval_id, incident_ref, status,
                proposed_action, context, action_claim_id, requested_by,
                requested_at, expires_at)
               VALUES (:approval_id, :incident_ref, :status, :proposed_action,
                :context, :action_claim_id, :requested_by, :requested_at,
                :expires_at)""",
            approval,
        )
        record_event_in_transaction(
            conn,
            incident_ref=incident_ref,
            event_type="approval_requested",
            actor=requested_by,
            summary=f"Approval {approval['approval_id']} requested",
            resource=proposed_action.get("deployment"),
            approval_id=approval["approval_id"],
            action_claim_id=action_claim_id,
            workflow_claim_id=workflow_claim_id,
            outcome="pending",
            details={
                "requested_by": requested_by,
                "expires_at": approval["expires_at"],
                **(
                    {"incident_row_id": context["occurrence_id"]}
                    if isinstance(context.get("occurrence_id"), int) else {}
                ),
            },
        )
        if action_claim_id is not None:
            cursor = conn.execute(
                """UPDATE action_claims
                   SET status = 'AWAITING_APPROVAL', approval_id = ?,
                       updated_at = ?
                   WHERE action_claim_id = ? AND status = 'RESERVED'""",
                (approval["approval_id"], now.isoformat(), action_claim_id),
            )
            if cursor.rowcount != 1:
                raise ApprovalError(
                    "claim_mismatch",
                    "Action claim changed while approval was created",
                )
    return get_approval(approval["approval_id"])


def get_approval(approval_id: str) -> dict | None:
    ensure_schema()
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT * FROM approval_requests WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
    return _decode(row) if row else None


def list_approvals(status: str | None = None, limit: int = 100) -> list[dict]:
    ensure_schema()
    limit = max(1, min(int(limit), 1000))
    with closing(get_connection()) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM approval_requests WHERE status = ? "
                "ORDER BY requested_at DESC LIMIT ?",
                (status.upper(), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM approval_requests "
                "ORDER BY requested_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [_decode(r) for r in rows]


def count_approvals(status: str) -> int:
    ensure_schema()
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM approval_requests WHERE status = ?",
            (status.upper(),),
        ).fetchone()
    return int(row[0])


def reconcile_pending_approvals(approval_id: str | None = None) -> int:
    """Audit and close pending approvals whose incident is inactive or absent."""
    from app.services import incident_repository

    pending = list_approvals("PENDING", 1000)
    if approval_id is not None:
        pending = [
            approval for approval in pending
            if approval["approval_id"] == approval_id
        ]

    reconciled = 0
    for approval in pending:
        occurrence = incident_repository.get_latest_occurrence_by_incident_id(
            approval["incident_ref"]
        )
        try:
            expires_at = datetime.fromisoformat(approval["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue

        now = _now()
        expired = expires_at <= now
        if not expired and occurrence is not None and occurrence.get("status") in {
            "OPEN", "REMEDIATING",
        }:
            continue

        new_status = "EXPIRED" if expired else "CANCELLED"
        reason = (
            "approval_expired"
            if expired
            else "incident_not_found"
            if occurrence is None
            else "incident_resolved"
        )
        decided_at = now.isoformat()
        with closing(get_connection()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?",
                (approval["approval_id"],),
            ).fetchone()
            if current is None or current["status"] != "PENDING":
                continue
            cursor = conn.execute(
                """UPDATE approval_requests
                   SET status = ?, decided_by = ?, decided_at = ?,
                       decision_reason = ?
                   WHERE approval_id = ? AND status = 'PENDING'""",
                (
                    new_status,
                    "kybernaut-approval-reconciler",
                    decided_at,
                    reason,
                    approval["approval_id"],
                ),
            )
            if cursor.rowcount != 1:
                continue

            action_claim_id = current["action_claim_id"]
            workflow_claim_id = None
            if action_claim_id:
                action_claim = conn.execute(
                    """SELECT workflow_claim_id, status FROM action_claims
                       WHERE action_claim_id = ?""",
                    (action_claim_id,),
                ).fetchone()
                if action_claim is not None:
                    workflow_claim_id = action_claim["workflow_claim_id"]
                    if action_claim["status"] == "AWAITING_APPROVAL":
                        conn.execute(
                            """UPDATE action_claims SET status = ?, updated_at = ?
                               WHERE action_claim_id = ?
                                 AND status = 'AWAITING_APPROVAL'""",
                            (
                                "EXPIRED" if expired else "BLOCKED",
                                decided_at,
                                action_claim_id,
                            ),
                        )
                    if workflow_claim_id:
                        if expired:
                            conn.execute(
                                """UPDATE workflow_claims
                                   SET status = 'RECONCILIATION_REQUIRED',
                                       updated_at = ?
                                   WHERE claim_id = ?
                                     AND status = 'AWAITING_APPROVAL'""",
                                (decided_at, workflow_claim_id),
                            )
                        else:
                            response = json.dumps({
                                "incident_id": approval["incident_ref"],
                                "workflow_claim_id": workflow_claim_id,
                                "status": "condition_cleared",
                                "resolution_reason": reason,
                            }, sort_keys=True)
                            conn.execute(
                                """UPDATE workflow_claims
                                   SET status = 'COMPLETED', response_json = ?,
                                       updated_at = ?
                                   WHERE claim_id = ?
                                     AND status IN (
                                         'RUNNING', 'AWAITING_APPROVAL',
                                         'RECONCILIATION_REQUIRED'
                                     )""",
                                (response, decided_at, workflow_claim_id),
                            )

            action = json.loads(current["proposed_action"] or "{}")
            record_event_in_transaction(
                conn,
                incident_ref=current["incident_ref"],
                event_type="approval_expired" if expired else "approval_cancelled",
                actor="kybernaut-approval-reconciler",
                summary=(
                    "Approval expired before a decision"
                    if expired
                    else "Approval cancelled because its incident is inactive"
                ),
                resource=action.get("deployment"),
                approval_id=current["approval_id"],
                action_claim_id=action_claim_id,
                workflow_claim_id=workflow_claim_id,
                outcome=reason,
                details={
                    "reason": reason,
                    "incident_status": occurrence.get("status")
                    if occurrence else "NOT_FOUND",
                },
            )
            reconciled += 1
    return reconciled


def list_approvals_for_incident(
    incident_ref: str,
    limit: int = 100,
) -> list[dict]:
    """Return approval records associated with one incident."""
    ensure_schema()
    limit = max(1, min(int(limit), 1000))
    with closing(get_connection()) as conn:
        rows = conn.execute(
            """SELECT approval_id, incident_ref, status, proposed_action,
                      requested_at, expires_at, decided_at
               FROM approval_requests
               WHERE incident_ref = ?
               ORDER BY requested_at DESC
               LIMIT ?""",
            (incident_ref, limit),
        ).fetchall()
    return [_decode(row) for row in rows]


def decide_approval(
    approval_id: str,
    decision: str,
    decided_by: str,
    reason: str = "",
) -> dict:
    """Atomically move PENDING -> APPROVED/REJECTED. Raises ApprovalError."""
    if decision not in {"approved", "rejected"}:
        raise ApprovalError("invalid_decision", "decision must be approved or rejected")
    ensure_schema()
    now_dt = _now()
    now = now_dt.isoformat()
    new_status = decision.upper()

    error: ApprovalError | None = None
    with closing(get_connection()) as conn, conn:
        approval_row = conn.execute(
            "SELECT * FROM approval_requests WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        context = (
            json.loads(approval_row["context"] or "{}")
            if approval_row is not None else {}
        )
        if approval_row is not None and approval_row["action_claim_id"]:
            action = json.loads(approval_row["proposed_action"])
            context = json.loads(approval_row["context"] or "{}")
            action_hash = hashlib.sha256(
                json.dumps(
                    action, sort_keys=True, separators=(",", ":"), default=str
                ).encode("utf-8")
            ).hexdigest()
            claim = conn.execute(
                """SELECT a.*, w.incident_ref AS claim_incident_ref,
                          w.status AS workflow_status
                   FROM action_claims a
                   JOIN workflow_claims w ON w.claim_id = a.workflow_claim_id
                   WHERE a.action_claim_id = ?""",
                (approval_row["action_claim_id"],),
            ).fetchone()
            if (
                claim is None
                or claim["claim_incident_ref"] != approval_row["incident_ref"]
                or claim["approval_id"] != approval_id
                or claim["status"] != "AWAITING_APPROVAL"
                or claim["action_hash"] != action_hash
                or claim["policy_json"] != json.dumps(
                    context.get("policy"), sort_keys=True,
                    separators=(",", ":"), default=str,
                )
                or claim["deployment"] != action.get("deployment")
            ):
                raise ApprovalError(
                    "claim_mismatch",
                    "Approval no longer matches its reserved action claim",
                )
            if claim["workflow_status"] not in {
                "AWAITING_APPROVAL", "RUNNING",
            }:
                raise ApprovalError(
                    "claim_mismatch",
                    "Approval workflow claim could not be resumed safely",
                )
        cursor = conn.execute(
            """UPDATE approval_requests
               SET status = ?, decided_by = ?, decided_at = ?, decision_reason = ?
               WHERE approval_id = ? AND status = 'PENDING' AND expires_at > ?""",
            (new_status, decided_by, now, reason, approval_id, now),
        )
        if cursor.rowcount != 1:
            row = conn.execute(
                "SELECT status FROM approval_requests WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if row is None:
                error = ApprovalError("not_found", "Approval request not found")
            elif row["status"] == "PENDING":
                conn.execute(
                    "UPDATE approval_requests SET status = 'EXPIRED' "
                    "WHERE approval_id = ? AND status = 'PENDING'",
                    (approval_id,),
                )
                error = ApprovalError("expired", "Approval request has expired")
            else:
                error = ApprovalError(
                    "already_decided", f"Approval already {row['status']}"
                )
        if cursor.rowcount == 1 and approval_row is not None \
                and approval_row["action_claim_id"]:
            claim_status = "APPROVED" if decision == "approved" else "REJECTED"
            claim_cursor = conn.execute(
                """UPDATE action_claims SET status = ?, updated_at = ?
                   WHERE action_claim_id = ? AND status = 'AWAITING_APPROVAL'
                     AND approval_id = ?""",
                (
                    claim_status, now, approval_row["action_claim_id"],
                    approval_id,
                ),
            )
            if claim_cursor.rowcount != 1:
                raise ApprovalError(
                    "claim_mismatch",
                    "Approval claim could not be transitioned",
                )
        if (
            error is not None and error.code == "expired"
            and approval_row is not None and approval_row["action_claim_id"]
        ):
            expired_claim = conn.execute(
                """UPDATE action_claims SET status = 'EXPIRED', updated_at = ?
                   WHERE action_claim_id = ? AND status = 'AWAITING_APPROVAL'
                     AND approval_id = ?""",
                (now, approval_row["action_claim_id"], approval_id),
            )
            if expired_claim.rowcount == 1:
                action_claim = conn.execute(
                    "SELECT workflow_claim_id FROM action_claims "
                    "WHERE action_claim_id = ?",
                    (approval_row["action_claim_id"],),
                ).fetchone()
                conn.execute(
                    """UPDATE workflow_claims
                       SET status = 'RECONCILIATION_REQUIRED', updated_at = ?
                       WHERE claim_id = ? AND status = 'AWAITING_APPROVAL'""",
                    (now, action_claim["workflow_claim_id"]),
                )
        if approval_row is not None and (
            cursor.rowcount == 1
            or (error is not None and error.code == "expired")
        ):
            workflow_claim_id = None
            action_claim_id = approval_row["action_claim_id"]
            if action_claim_id:
                action_claim = conn.execute(
                    """SELECT workflow_claim_id FROM action_claims
                       WHERE action_claim_id = ?""",
                    (action_claim_id,),
                ).fetchone()
                workflow_claim_id = (
                    action_claim["workflow_claim_id"]
                    if action_claim is not None else None
                )
            event_decision = (
                "expired" if error is not None and error.code == "expired"
                else decision
            )
            record_event_in_transaction(
                conn,
                incident_ref=approval_row["incident_ref"],
                event_type=f"approval_{event_decision}",
                actor=decided_by,
                summary=f"Approval {approval_id} {event_decision}",
                resource=(
                    json.loads(approval_row["proposed_action"]).get("deployment")
                ),
                approval_id=approval_id,
                action_claim_id=action_claim_id,
                workflow_claim_id=workflow_claim_id,
                outcome=event_decision,
                details={
                    "reason": reason,
                    **(
                        {"incident_row_id": context["occurrence_id"]}
                        if isinstance(context.get("occurrence_id"), int) else {}
                    ),
                },
            )
    # Raise only after the transaction commits so EXPIRED is persisted.
    if error is not None:
        raise error
    return get_approval(approval_id)