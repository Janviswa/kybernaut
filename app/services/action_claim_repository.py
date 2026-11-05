"""Durable idempotency and action-capacity claims backed by SQLite."""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone

from app.database import get_connection

ACTIVE_ACTION_STATES = (
    "RESERVED",
    "AWAITING_APPROVAL",
    "APPROVED",
    "AUTO_AUTHORIZED",
    "EXECUTING",
    "EXECUTION_UNKNOWN",
)
EXECUTED_ACTION_STATES = (
    "COMPLETED",
    "VERIFIED",
    "VERIFICATION_FAILED",
    "EXECUTION_FAILED",
)

ACTION_TRANSITIONS = {
    "RESERVED": {"AWAITING_APPROVAL", "BLOCKED"},
    "AWAITING_APPROVAL": {"APPROVED", "REJECTED", "EXPIRED", "BLOCKED"},
    "APPROVED": {"EXECUTING", "BLOCKED", "FAILED_BEFORE_EXECUTION"},
    "AUTO_AUTHORIZED": {"EXECUTING", "BLOCKED"},
    "EXECUTING": {
        "FAILED_BEFORE_EXECUTION",
        "COMPLETED",
        "EXECUTION_FAILED",
        "DRY_RUN_COMPLETE",
        "SIMULATED",
        "SIMULATION_FAILED",
        "EXECUTION_UNKNOWN",
    },
    "COMPLETED": {"VERIFIED", "VERIFICATION_FAILED"},
    "EXECUTION_UNKNOWN": set(),
    "VERIFIED": set(),
    "VERIFICATION_FAILED": set(),
    "EXECUTION_FAILED": set(),
    "DRY_RUN_COMPLETE": set(),
    "SIMULATED": set(),
    "SIMULATION_FAILED": set(),
    "REJECTED": set(),
    "EXPIRED": set(),
    "BLOCKED": set(),
    "FAILED_BEFORE_EXECUTION": set(),
}


class ClaimError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _legacy_execution_count(conn, deployment: str, cutoff: str) -> int:
    table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'audit_events'"
    ).fetchone()
    if table is None:
        return 0
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(audit_events)")
    }
    claim_filter = "AND action_claim_id IS NULL" if "action_claim_id" in columns else ""
    row = conn.execute(
        f"""SELECT COUNT(*) AS n FROM audit_events
            WHERE resource = ? AND event_type = 'remediation_executed'
              AND timestamp >= ? {claim_filter}""",
        (deployment, cutoff),
    ).fetchone()
    return int(row["n"])


def ensure_schema() -> None:
    with closing(get_connection()) as conn, conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS workflow_claims (
                claim_id TEXT PRIMARY KEY,
                operator_id TEXT NOT NULL,
                service TEXT NOT NULL,
                incident_occurrence_id INTEGER,
                idempotency_hash TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                incident_ref TEXT NOT NULL,
                status TEXT NOT NULL,
                response_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(operator_id, service, idempotency_hash)
            )"""
        )
        workflow_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(workflow_claims)")
        }
        if "incident_occurrence_id" not in workflow_columns:
            conn.execute(
                "ALTER TABLE workflow_claims "
                "ADD COLUMN incident_occurrence_id INTEGER"
            )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS action_claims (
                action_claim_id TEXT PRIMARY KEY,
                workflow_claim_id TEXT NOT NULL,
                attempt_no INTEGER NOT NULL,
                logical_key TEXT NOT NULL,
                deployment TEXT NOT NULL,
                action_hash TEXT NOT NULL,
                action_json TEXT NOT NULL,
                policy_json TEXT NOT NULL,
                status TEXT NOT NULL,
                approval_id TEXT UNIQUE,
                execution_json TEXT,
                verification_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(workflow_claim_id, attempt_no)
            )"""
        )
        action_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(action_claims)")
        }
        if "verification_json" not in action_columns:
            conn.execute(
                "ALTER TABLE action_claims ADD COLUMN verification_json TEXT"
            )
        active = ",".join(repr(value) for value in ACTIVE_ACTION_STATES)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_action_claim_active_logical "
            f"ON action_claims (logical_key) WHERE status IN ({active})"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_action_claim_deployment_created "
            "ON action_claims (deployment, created_at, status)"
        )


def create_workflow_claim(
    *,
    operator_id: str,
    service: str,
    idempotency_key: str,
    incident_ref: str,
    request_correlation: str | None = None,
    incident_occurrence_id: int | None = None,
) -> tuple[dict, bool, bool]:
    """Create a request claim; return (row, created, request_conflict)."""
    ensure_schema()
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        return create_workflow_claim_in_transaction(
            conn,
            operator_id=operator_id,
            service=service,
            idempotency_key=idempotency_key,
            incident_ref=incident_ref,
            request_correlation=request_correlation,
            incident_occurrence_id=incident_occurrence_id,
        )


def create_workflow_claim_in_transaction(
    conn,
    *,
    operator_id: str,
    service: str,
    idempotency_key: str,
    incident_ref: str,
    request_correlation: str | None = None,
    incident_occurrence_id: int | None = None,
) -> tuple[dict, bool, bool]:
    """Create or find a workflow claim inside a caller-owned transaction."""
    now = _now().isoformat()
    key_hash = _digest(idempotency_key)
    request_hash = _digest(_json({
        "service": service,
        "request_correlation": request_correlation,
    }))
    claim_id = f"wfc-{uuid.uuid4().hex}"
    row = conn.execute(
        """SELECT * FROM workflow_claims
           WHERE operator_id = ? AND service = ? AND idempotency_hash = ?""",
        (operator_id, service, key_hash),
    ).fetchone()
    if row is not None:
        if (
            incident_occurrence_id is not None
            and row["incident_occurrence_id"] is None
        ):
            conn.execute(
                """UPDATE workflow_claims
                   SET incident_occurrence_id = ?, updated_at = ?
                   WHERE claim_id = ? AND incident_occurrence_id IS NULL""",
                (incident_occurrence_id, now, row["claim_id"]),
            )
            row = conn.execute(
                "SELECT * FROM workflow_claims WHERE claim_id = ?",
                (row["claim_id"],),
            ).fetchone()
        data = dict(row)
        return data, False, data["request_hash"] != request_hash
    conn.execute(
        """INSERT INTO workflow_claims (
            claim_id, operator_id, service, incident_occurrence_id,
            idempotency_hash, request_hash, incident_ref, status,
            response_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'RUNNING', NULL, ?, ?)""",
        (
            claim_id, operator_id, service, incident_occurrence_id,
            key_hash, request_hash, incident_ref, now, now,
        ),
    )
    row = conn.execute(
        "SELECT * FROM workflow_claims WHERE claim_id = ?", (claim_id,)
    ).fetchone()
    return dict(row), True, False


def bind_workflow_claim_occurrence(
    claim_id: str,
    *,
    incident_ref: str,
    incident_occurrence_id: int,
) -> bool:
    """Bind a running request claim to its active persisted occurrence."""
    ensure_schema()
    now = _now().isoformat()
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        occurrence = conn.execute(
            """SELECT incident_id, status FROM incidents WHERE id = ?""",
            (incident_occurrence_id,),
        ).fetchone()
        if (
            occurrence is None
            or occurrence["incident_id"] != incident_ref
            or occurrence["status"] not in {"OPEN", "REMEDIATING"}
        ):
            return False
        cursor = conn.execute(
            """UPDATE workflow_claims
               SET incident_occurrence_id = ?, updated_at = ?
               WHERE claim_id = ? AND incident_ref = ?
                 AND status = 'RUNNING'
                 AND (
                     incident_occurrence_id IS NULL
                     OR incident_occurrence_id = ?
                 )""",
            (
                incident_occurrence_id,
                now,
                claim_id,
                incident_ref,
                incident_occurrence_id,
            ),
        )
    return cursor.rowcount == 1


def mark_workflow_claim_reconciliation_required(claim_id: str) -> bool:
    ensure_schema()
    now = _now().isoformat()
    with closing(get_connection()) as conn, conn:
        cursor = conn.execute(
            """UPDATE workflow_claims SET status = 'RECONCILIATION_REQUIRED',
               updated_at = ? WHERE claim_id = ?
               AND status IN ('RUNNING', 'AWAITING_APPROVAL')""",
            (now, claim_id),
        )
    return cursor.rowcount == 1


def complete_workflow_claim_after_decision(
    claim_id: str, response: dict
) -> bool:
    ensure_schema()
    now = _now().isoformat()
    with closing(get_connection()) as conn, conn:
        cursor = conn.execute(
            """UPDATE workflow_claims
               SET status = 'COMPLETED', response_json = ?, updated_at = ?
               WHERE claim_id = ? AND status IN (
                   'RUNNING', 'AWAITING_APPROVAL'
               )""",
            (_json(response), now, claim_id),
        )
    return cursor.rowcount == 1


def begin_approved_workflow_claim(
    workflow_claim_id: str, action_claim_id: str, approval_id: str
) -> bool:
    """Atomically begin execution for the matching approved action."""
    ensure_schema()
    now = _now().isoformat()
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT w.status AS workflow_status, a.status AS action_status,
                      a.approval_id, p.status AS approval_status
               FROM workflow_claims w
               JOIN action_claims a ON a.workflow_claim_id = w.claim_id
               JOIN approval_requests p ON p.approval_id = a.approval_id
               WHERE w.claim_id = ? AND a.action_claim_id = ?""",
            (workflow_claim_id, action_claim_id),
        ).fetchone()
        if (
            row is None
            or row["workflow_status"] != "AWAITING_APPROVAL"
            or row["action_status"] != "APPROVED"
            or row["approval_id"] != approval_id
            or row["approval_status"] != "APPROVED"
        ):
            return False
        cursor = conn.execute(
            """UPDATE workflow_claims SET status = 'RUNNING', updated_at = ?
               WHERE claim_id = ? AND status = 'AWAITING_APPROVAL'""",
            (now, workflow_claim_id),
        )
    return cursor.rowcount == 1


def get_workflow_claim(claim_id: str) -> dict | None:
    ensure_schema()
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT * FROM workflow_claims WHERE claim_id = ?", (claim_id,)
        ).fetchone()
    return dict(row) if row else None


def finish_workflow_claim(claim_id: str, response: dict) -> bool:
    ensure_schema()
    now = _now().isoformat()
    status = (
        "AWAITING_APPROVAL"
        if response.get("status") == "awaiting_approval"
        else "COMPLETED"
    )
    with closing(get_connection()) as conn, conn:
        cursor = conn.execute(
            """UPDATE workflow_claims
               SET status = ?, response_json = ?, updated_at = ?
               WHERE claim_id = ? AND status = 'RUNNING'""",
            (status, _json(response), now, claim_id),
        )
    return cursor.rowcount == 1


def update_workflow_claim_response(claim_id: str, response: dict) -> bool:
    """Replace the replay response after an approval-driven continuation."""
    ensure_schema()
    now = _now().isoformat()
    with closing(get_connection()) as conn, conn:
        cursor = conn.execute(
            """UPDATE workflow_claims
               SET response_json = ?, updated_at = ?
               WHERE claim_id = ? AND status IN (
                   'RUNNING', 'AWAITING_APPROVAL', 'RECONCILIATION_REQUIRED'
               )""",
            (_json(response), now, claim_id),
        )
    return cursor.rowcount == 1


def reserve_action_claim(
    *,
    workflow_claim_id: str,
    attempt_no: int,
    logical_key: str,
    deployment: str,
    action: dict,
    policy: dict,
    max_actions: int,
    window_seconds: int,
    auto_authorized: bool,
    incident_occurrence_id: int | None = None,
    expected_service: str | None = None,
    expected_incident_type: str | None = None,
    expected_fingerprint: str | None = None,
) -> dict:
    """Atomically reserve unique logical action and deployment rate capacity."""
    ensure_schema()
    now = _now()
    now_text = now.isoformat()
    cutoff = (now - timedelta(seconds=max(0, window_seconds))).isoformat()
    action_json = _json(action)
    action_hash = _digest(action_json)
    policy_json = _json(policy)
    claim_id = f"ac-{uuid.uuid4().hex}"

    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            """SELECT * FROM action_claims
               WHERE workflow_claim_id = ? AND attempt_no = ?""",
            (workflow_claim_id, attempt_no),
        ).fetchone()
        if incident_occurrence_id is not None:
            occurrence = conn.execute(
                """SELECT service, incident_type, fingerprint, status
                   FROM incidents WHERE id = ?""",
                (incident_occurrence_id,),
            ).fetchone()
            workflow = conn.execute(
                """SELECT incident_occurrence_id FROM workflow_claims
                   WHERE claim_id = ?""",
                (workflow_claim_id,),
            ).fetchone()
            if (
                occurrence is None
                or occurrence["status"] not in {"OPEN", "REMEDIATING"}
                or occurrence["service"] != expected_service
                or occurrence["incident_type"] != expected_incident_type
                or occurrence["fingerprint"] != expected_fingerprint
                or workflow is None
                or workflow["incident_occurrence_id"] != incident_occurrence_id
            ):
                if existing is not None and existing["status"] == "RESERVED":
                    conn.execute(
                        """UPDATE action_claims SET status = 'BLOCKED',
                           updated_at = ? WHERE action_claim_id = ?
                             AND status = 'RESERVED'""",
                        (now_text, existing["action_claim_id"]),
                    )
                return {
                    "allowed": False,
                    "reason": "incident_occurrence_inactive_or_changed",
                }
        if existing is not None:
            return {"allowed": True, "existing": True, **dict(existing)}

        duplicate = conn.execute(
            f"""SELECT * FROM action_claims
                WHERE logical_key = ? AND status IN ({",".join("?" for _ in ACTIVE_ACTION_STATES)})
                LIMIT 1""",
            (logical_key, *ACTIVE_ACTION_STATES),
        ).fetchone()
        if duplicate is not None:
            return {
                "allowed": False,
                "reason": "An active claim already exists for this logical action.",
                "duplicate_claim_id": duplicate["action_claim_id"],
            }

        count = conn.execute(
            f"""SELECT COUNT(*) AS n FROM action_claims
                WHERE deployment = ?
                  AND (
                    status IN ({",".join("?" for _ in ACTIVE_ACTION_STATES)})
                    OR (
                        created_at >= ?
                        AND status IN (
                            {",".join("?" for _ in EXECUTED_ACTION_STATES)}
                        )
                    )
                  )""",
            (
                deployment,
                *ACTIVE_ACTION_STATES,
                cutoff,
                *EXECUTED_ACTION_STATES,
            ),
        ).fetchone()["n"]
        count += _legacy_execution_count(conn, deployment, cutoff)
        if count >= max_actions:
            return {
                "allowed": False,
                "reason": (
                    f"Action claim limit reached ({count}/{max_actions}) "
                    f"for the last {window_seconds} seconds."
                ),
                "attempt_count": count,
            }

        status = "AUTO_AUTHORIZED" if auto_authorized else "RESERVED"
        conn.execute(
            """INSERT INTO action_claims (
                action_claim_id, workflow_claim_id, attempt_no, logical_key,
                deployment, action_hash, action_json, policy_json, status,
                approval_id, execution_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)""",
            (
                claim_id, workflow_claim_id, attempt_no, logical_key,
                deployment, action_hash, action_json, policy_json, status,
                now_text, now_text,
            ),
        )
        return {
            "allowed": True,
            "existing": False,
            "action_claim_id": claim_id,
            "status": status,
            "attempt_count": count + 1,
        }


def get_action_claim(action_claim_id: str) -> dict | None:
    ensure_schema()
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT * FROM action_claims WHERE action_claim_id = ?",
            (action_claim_id,),
        ).fetchone()
    return dict(row) if row else None


def count_recent_action_claims(
    deployment: str,
    window_seconds: int,
    *,
    exclude_claim_id: str | None = None,
) -> int:
    ensure_schema()
    cutoff = (_now() - timedelta(seconds=max(0, window_seconds))).isoformat()
    active = ",".join("?" for _ in ACTIVE_ACTION_STATES)
    executed = ",".join("?" for _ in EXECUTED_ACTION_STATES)
    query = f"""SELECT COUNT(*) AS n FROM action_claims
                WHERE deployment = ?
                  AND (
                    status IN ({active})
                    OR (created_at >= ? AND status IN ({executed}))
                  )"""
    params: list[object] = [
        deployment,
        *ACTIVE_ACTION_STATES,
        cutoff,
        *EXECUTED_ACTION_STATES,
    ]
    if exclude_claim_id:
        query += " AND action_claim_id != ?"
        params.append(exclude_claim_id)
    with closing(get_connection()) as conn:
        row = conn.execute(query, params).fetchone()
        legacy_count = _legacy_execution_count(conn, deployment, cutoff)
    return int(row["n"]) + legacy_count


def transition_action_claim(
    action_claim_id: str,
    expected_status: str,
    new_status: str,
    *,
    execution: dict | None = None,
    audit_event: dict | None = None,
) -> bool:
    if new_status not in ACTION_TRANSITIONS.get(expected_status, set()):
        raise ClaimError("invalid_transition", "Illegal action-claim transition")
    ensure_schema()
    if audit_event is not None:
        from app.services import audit_repository

        audit_repository.ensure_schema()
    now = _now().isoformat()
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """UPDATE action_claims
               SET status = ?, execution_json = COALESCE(?, execution_json),
                   updated_at = ?
               WHERE action_claim_id = ? AND status = ?""",
            (
                new_status,
                _json(execution) if execution is not None else None,
                now,
                action_claim_id,
                expected_status,
            ),
        )
        if cursor.rowcount == 1 and audit_event is not None:
            from app.services.audit_repository import record_event_in_transaction

            record_event_in_transaction(conn, **audit_event)
    return cursor.rowcount == 1


def authorize_action_execution(
    action_claim_id: str,
    expected_status: str,
    *,
    incident_occurrence_id: int,
    expected_service: str,
    expected_incident_type: str,
    expected_fingerprint: str,
    require_handoff: bool = False,
    actor: str = "kybernaut-auto",
    approval_id: str | None = None,
    policy_decision: dict | None = None,
    audit_incident_ref: str | None = None,
) -> bool:
    """Atomically check the occurrence and reserve the execution boundary."""
    if expected_status not in {"APPROVED", "AUTO_AUTHORIZED"}:
        raise ClaimError("invalid_transition", "Invalid execution authorization")
    ensure_schema()
    from app.services import audit_repository

    audit_repository.ensure_schema()
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT a.status, a.workflow_claim_id, a.deployment,
                      a.action_hash, a.policy_json,
                      a.approval_id AS action_approval_id,
                      i.id AS occurrence_id, i.incident_id,
                      i.status AS incident_status,
                      i.service, i.incident_type, i.fingerprint,
                      h.workflow_claim_id AS handoff_workflow_claim_id,
                      w.incident_occurrence_id, w.operator_id,
                      w.service AS workflow_service,
                      p.approval_id AS persisted_approval_id,
                      p.status AS approval_status,
                      p.expires_at AS approval_expires_at,
                      p.proposed_action AS approved_action_json,
                      p.action_claim_id AS approval_action_claim_id,
                      p.incident_ref AS approval_incident_ref,
                      p.requested_by, p.decided_by, p.decided_at,
                      p.context AS approval_context
               FROM action_claims a
               JOIN incidents i ON i.id = ?
               JOIN workflow_claims w ON w.claim_id = a.workflow_claim_id
               LEFT JOIN incident_handoffs h ON h.incident_row_id = i.id
               LEFT JOIN approval_requests p ON p.approval_id = a.approval_id
               WHERE a.action_claim_id = ?""",
            (incident_occurrence_id, action_claim_id),
        ).fetchone()
        approval_context = None
        approved_action_hash = None
        approval_expiry_valid = False
        if row is not None and isinstance(row["approval_context"], str):
            try:
                approval_context = json.loads(row["approval_context"])
            except json.JSONDecodeError:
                approval_context = None
        if row is not None and isinstance(row["approved_action_json"], str):
            try:
                approved_action = json.loads(row["approved_action_json"])
                approved_action_hash = _digest(_json(approved_action))
            except (TypeError, ValueError):
                approved_action_hash = None
        authorization_time = _now()
        now = authorization_time.isoformat()
        if row is not None and isinstance(row["approval_expires_at"], str):
            try:
                expires_at = datetime.fromisoformat(
                    row["approval_expires_at"]
                )
                approval_expiry_valid = (
                    expires_at.utcoffset() is not None
                    and authorization_time < expires_at
                )
            except (TypeError, ValueError):
                approval_expiry_valid = False
        occurrence_valid = (
            row is not None
            and row["incident_status"] in {"OPEN", "REMEDIATING"}
            and row["service"] == expected_service
            and row["incident_type"] == expected_incident_type
            and row["fingerprint"] == expected_fingerprint
            and row["incident_occurrence_id"] == incident_occurrence_id
            and (
                not require_handoff
                or (
                    expected_status == "APPROVED"
                    and row["workflow_claim_id"]
                    == row["handoff_workflow_claim_id"]
                    and row["workflow_service"] == expected_service
                    and row["operator_id"] == "kybernaut-monitor"
                    and approval_id is not None
                    and row["persisted_approval_id"] == approval_id
                    and row["action_approval_id"] == approval_id
                    and row["approval_status"] == "APPROVED"
                    and row["decided_at"] is not None
                    and approval_expiry_valid
                    and row["approval_action_claim_id"] == action_claim_id
                    and row["action_hash"] == approved_action_hash
                    and row["approval_incident_ref"] == row["incident_id"]
                    and row["requested_by"] == "kybernaut-monitor"
                    and bool(row["decided_by"])
                    and row["decided_by"] != row["requested_by"]
                    and row["decided_by"] != "kybernaut-monitor"
                    and actor == row["decided_by"]
                    and isinstance(approval_context, dict)
                    and approval_context.get("monitor_handoff") is True
                    and type(approval_context.get("occurrence_id")) is int
                    and approval_context["occurrence_id"] == incident_occurrence_id
                    and approval_context.get("service") == expected_service
                    and approval_context.get(
                        "expected_occurrence_service"
                    ) == expected_service
                    and approval_context.get("expected_incident_type")
                    == expected_incident_type
                    and approval_context.get("expected_fingerprint")
                    == expected_fingerprint
                    and isinstance(approval_context.get("policy"), dict)
                    and _json(approval_context["policy"]) == row["policy_json"]
                )
            )
            and row["status"] == expected_status
        )
        if not occurrence_valid:
            reason = (
                "claim_or_occurrence_missing"
                if row is None
                else "claim_status_changed"
                if row["status"] != expected_status
                else "occurrence_or_authorization_invalid"
            )
            if row is not None and row["status"] == expected_status:
                blocked = conn.execute(
                    """UPDATE action_claims SET status = 'BLOCKED',
                       updated_at = ? WHERE action_claim_id = ?
                         AND status = ?""",
                    (now, action_claim_id, expected_status),
                )
                if blocked.rowcount != 1:
                    reason = "claim_status_changed"
            incident_ref = (
                row["incident_id"] if row is not None else None
            ) or audit_incident_ref
            if incident_ref is None:
                incident = conn.execute(
                    "SELECT incident_id FROM incidents WHERE id = ?",
                    (incident_occurrence_id,),
                ).fetchone()
                incident_ref = incident["incident_id"] if incident else None
            if incident_ref is not None:
                from app.services.audit_repository import record_event_in_transaction

                observed_context = dict(row) if row is not None else None
                if observed_context is not None:
                    observed_context.pop("status", None)
                denial_context = {
                    "action_claim_id": action_claim_id,
                    "expected_claim_status": expected_status,
                    "expected_service": expected_service,
                    "expected_incident_type": expected_incident_type,
                    "expected_fingerprint": expected_fingerprint,
                    "incident_row_id": incident_occurrence_id,
                    "require_handoff": require_handoff,
                    "actor": actor,
                    "approval_id": approval_id,
                    "policy_decision": policy_decision,
                    "observed": observed_context,
                }
                denial_fingerprint = hashlib.sha256(
                    _json(denial_context).encode("utf-8")
                ).hexdigest()
                existing_denials = conn.execute(
                    """SELECT details
                       FROM audit_events
                       WHERE action_claim_id = ?
                         AND event_type = 'action_authorization_denied'""",
                    (action_claim_id,),
                ).fetchall()
                already_recorded = False
                legacy_details = {
                    "expected_claim_status": expected_status,
                    "incident_row_id": incident_occurrence_id,
                }
                for event in existing_denials:
                    try:
                        details = json.loads(event["details"] or "{}")
                    except json.JSONDecodeError:
                        continue
                    if details.get("denial_fingerprint") == denial_fingerprint:
                        already_recorded = True
                        break
                if not already_recorded:
                    record_event_in_transaction(
                        conn,
                        incident_ref=incident_ref,
                        event_type="action_authorization_denied",
                        actor=actor,
                        summary=(
                            "Action authorization denied; execution was not started"
                        ),
                        resource=row["deployment"] if row is not None else None,
                        policy_decision=policy_decision,
                        approval_id=approval_id,
                        action_claim_id=action_claim_id,
                        workflow_claim_id=(
                            row["workflow_claim_id"] if row is not None else None
                        ),
                        outcome="blocked",
                        details={
                            **legacy_details,
                            "denial_fingerprint": denial_fingerprint,
                        },
                    )
            return False
        cursor = conn.execute(
            """UPDATE action_claims
               SET status = 'EXECUTING', updated_at = ?
               WHERE action_claim_id = ? AND status = ?""",
            (now, action_claim_id, expected_status),
        )
        if cursor.rowcount == 1:
            from app.services.audit_repository import record_event_in_transaction

            record_event_in_transaction(
                conn,
                incident_ref=row["incident_id"],
                event_type="remediation_started",
                actor=actor,
                summary="Execution authorization granted; attempt initiation recorded",
                resource=row["deployment"],
                policy_decision=policy_decision,
                approval_id=approval_id,
                action_claim_id=action_claim_id,
                workflow_claim_id=row["workflow_claim_id"],
                outcome="authorized",
                details={
                    "execution_boundary": "authorized_attempt_initiation",
                    "incident_row_id": incident_occurrence_id,
                },
            )
        return cursor.rowcount == 1


def record_action_verification(
    action_claim_id: str,
    verification: dict,
    *,
    audit_event: dict | None = None,
) -> bool:
    new_status = "VERIFIED" if verification.get("recovered") is True else (
        "VERIFICATION_FAILED"
    )
    if new_status not in ACTION_TRANSITIONS["COMPLETED"]:
        raise ClaimError("invalid_transition", "Illegal verification transition")
    ensure_schema()
    if audit_event is not None:
        from app.services import audit_repository

        audit_repository.ensure_schema()
    now = _now().isoformat()
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """UPDATE action_claims
               SET status = ?, verification_json = ?, updated_at = ?
               WHERE action_claim_id = ? AND status = 'COMPLETED'""",
            (new_status, _json(verification), now, action_claim_id),
        )
        if cursor.rowcount == 1 and audit_event is not None:
            from app.services.audit_repository import record_event_in_transaction

            record_event_in_transaction(conn, **audit_event)
    return cursor.rowcount == 1
