"""Durable dispatch claims for detected incident occurrences."""

from __future__ import annotations

import json
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone

from app.database import get_connection
from app.services import action_claim_repository as claims
from app.services import audit_repository as audit

MONITOR_PRINCIPAL = "kybernaut-monitor"
LEASE_SECONDS = 60
RETRY_DELAY_SECONDS = 5
HANDOFF_TERMINAL_OUTCOMES = {
    "NO_ACTION",
    "BLOCKED",
    "REJECTED",
    "EXPIRED",
    "SIMULATED",
    "SIMULATION_FAILED",
    "DRY_RUN_COMPLETE",
    "FAILED",
    "VERIFICATION_FAILED",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_schema() -> None:
    claims.ensure_schema()
    audit.ensure_schema()
    with closing(get_connection()) as conn, conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS incident_handoffs (
                incident_row_id INTEGER PRIMARY KEY,
                service TEXT NOT NULL,
                incident_id TEXT NOT NULL,
                fingerprint TEXT,
                evidence_timestamp TEXT,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                idempotency_key TEXT NOT NULL UNIQUE,
                workflow_claim_id TEXT UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                retry_at TEXT,
                completed_at TEXT,
                last_error TEXT,
                FOREIGN KEY (incident_row_id) REFERENCES incidents(id)
            )"""
        )
        conn.execute(
            """CREATE INDEX IF NOT EXISTS idx_incident_handoff_dispatch
               ON incident_handoffs (status, retry_at, lease_expires_at)"""
        )


def claim_next_handoff(
    service: str,
    *,
    lease_owner: str,
    lease_seconds: int = LEASE_SECONDS,
) -> dict | None:
    """Atomically claim one eligible occurrence and its deterministic workflow."""
    ensure_schema()
    now = _now()
    now_text = now.isoformat()
    lease_token = uuid.uuid4().hex
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT h.*, i.status AS incident_status,
                      i.fingerprint AS current_fingerprint,
                      i.incident_type AS current_type
               FROM incident_handoffs h
               JOIN incidents i ON i.id = h.incident_row_id
               WHERE h.service = ?
                 AND h.status IN ('PENDING', 'RETRY')
                 AND (h.retry_at IS NULL OR h.retry_at <= ?)
               ORDER BY h.created_at, h.incident_row_id
               LIMIT 20""",
            (service, now_text),
        ).fetchall()
        selected = next(
            (
                row for row in rows
                if row["incident_status"] in {"OPEN", "REMEDIATING"}
                and row["fingerprint"] == row["current_fingerprint"]
            ),
            None,
        )
        if selected is None:
            for row in rows:
                if (
                    row["incident_status"] not in {"OPEN", "REMEDIATING"}
                    or row["fingerprint"] != row["current_fingerprint"]
                ):
                    reason = (
                        "incident_inactive"
                        if row["incident_status"] not in {"OPEN", "REMEDIATING"}
                        else "occurrence_identity_changed"
                    )
                    cancelled = conn.execute(
                        """UPDATE incident_handoffs
                           SET status = 'CANCELLED', completed_at = ?,
                               updated_at = ?, last_error = ?
                           WHERE incident_row_id = ?
                             AND status IN ('PENDING', 'RETRY')""",
                        (now_text, now_text, reason, row["incident_row_id"]),
                    )
                    if cancelled.rowcount:
                        _record_event_in_transaction(
                            conn,
                            incident_id=row["incident_id"],
                            occurrence_id=int(row["incident_row_id"]),
                            event_type="incident_handoff_cancelled",
                            outcome=reason,
                        )
            return None

        occurrence_id = int(selected["incident_row_id"])
        idempotency_key = selected["idempotency_key"]
        workflow_claim, created, conflict = (
            claims.create_workflow_claim_in_transaction(
                conn,
                operator_id=MONITOR_PRINCIPAL,
                service=service,
                idempotency_key=idempotency_key,
                incident_ref=selected["incident_id"],
                request_correlation=selected["incident_id"],
                incident_occurrence_id=occurrence_id,
            )
        )
        if conflict:
            _mark_handoff_in_transaction(
                conn, occurrence_id, "RECONCILIATION_REQUIRED", now_text,
                last_error="workflow_claim_conflict",
            )
            result = {
                "kind": "reconciliation",
                "incident_id": selected["incident_id"],
                "occurrence_id": occurrence_id,
                "workflow_claim_id": workflow_claim["claim_id"],
            }
        elif workflow_claim["status"] == "AWAITING_APPROVAL":
            _mark_handoff_in_transaction(
                conn, occurrence_id, "AWAITING_APPROVAL", now_text,
                workflow_claim_id=workflow_claim["claim_id"],
            )
            result = {
                "kind": "reconciled",
                "status": "AWAITING_APPROVAL",
                "incident_id": selected["incident_id"],
                "occurrence_id": occurrence_id,
                "workflow_claim_id": workflow_claim["claim_id"],
            }
        elif workflow_claim["status"] == "COMPLETED":
            response = _decode_json_dict(workflow_claim["response_json"])
            handoff_status = terminal_handoff_status(
                response or {},
                workflow_claim_id=workflow_claim["claim_id"],
            )
            if handoff_status == "COMPLETED":
                handoff_status = "RECONCILIATION_REQUIRED"
            _mark_handoff_in_transaction(
                conn, occurrence_id, handoff_status, now_text,
                workflow_claim_id=workflow_claim["claim_id"],
                last_error=(
                    "completed_workflow_requires_verification"
                    if handoff_status == "RECONCILIATION_REQUIRED" else None
                ),
            )
            result = {
                "kind": "reconciled",
                "status": handoff_status,
                "incident_id": selected["incident_id"],
                "occurrence_id": occurrence_id,
                "workflow_claim_id": workflow_claim["claim_id"],
            }
        elif (
            not created
            and not (
                selected["status"] == "RETRY"
                and selected["last_error"] == "workflow_not_started"
                and workflow_claim["status"] == "RUNNING"
            )
        ):
            _mark_handoff_in_transaction(
                conn, occurrence_id, "RECONCILIATION_REQUIRED", now_text,
                workflow_claim_id=workflow_claim["claim_id"],
                last_error="existing_workflow_state_ambiguous",
            )
            result = {
                "kind": "reconciliation",
                "incident_id": selected["incident_id"],
                "occurrence_id": occurrence_id,
                "workflow_claim_id": workflow_claim["claim_id"],
            }
        elif workflow_claim["status"] != "RUNNING":
            _mark_handoff_in_transaction(
                conn, occurrence_id, "RECONCILIATION_REQUIRED", now_text,
                workflow_claim_id=workflow_claim["claim_id"],
                last_error="workflow_state_not_dispatchable",
            )
            result = {
                "kind": "reconciliation",
                "incident_id": selected["incident_id"],
                "occurrence_id": occurrence_id,
                "workflow_claim_id": workflow_claim["claim_id"],
            }
        else:
            conn.execute(
                """UPDATE incident_handoffs
                   SET status = 'DISPATCHING', attempts = attempts + 1,
                       lease_owner = ?, lease_token = ?, lease_expires_at = ?,
                       workflow_claim_id = ?, retry_at = NULL, last_error = NULL,
                       updated_at = ?
                   WHERE incident_row_id = ? AND status IN ('PENDING', 'RETRY')""",
                (
                    lease_owner,
                    lease_token,
                    (now + timedelta(seconds=lease_seconds)).isoformat(),
                    workflow_claim["claim_id"],
                    now_text,
                    occurrence_id,
                ),
            )
            incident = conn.execute(
                "SELECT * FROM incidents WHERE id = ?", (occurrence_id,)
            ).fetchone()
            result = {
                "kind": "claimed",
                "incident_id": incident["incident_id"],
                "occurrence_id": occurrence_id,
                "incident_type": incident["incident_type"],
                "fingerprint": incident["fingerprint"],
                "service": incident["service"],
                "workflow_claim_id": workflow_claim["claim_id"],
                "requested_by": MONITOR_PRINCIPAL,
                "idempotency_key": idempotency_key,
                "lease_token": lease_token,
            }
        _record_event_in_transaction(
            conn,
            incident_id=result["incident_id"],
            occurrence_id=result["occurrence_id"],
            event_type=(
                "incident_handoff_dispatch_started"
                if result["kind"] == "claimed"
                else "incident_handoff_reconciliation_required"
                if result["kind"] == "reconciliation"
                else "incident_handoff_reconciled"
            ),
            outcome=(
                "dispatching" if result["kind"] == "claimed"
                else result.get("status", "reconciliation_required").lower()
            ),
            workflow_claim_id=result.get("workflow_claim_id"),
            details=(
                {"lease_owner": lease_owner}
                if result["kind"] == "claimed" else None
            ),
        )
    return result


def _mark_handoff_in_transaction(
    conn,
    occurrence_id: int,
    status: str,
    now_text: str,
    *,
    workflow_claim_id: str | None = None,
    last_error: str | None = None,
) -> None:
    conn.execute(
        """UPDATE incident_handoffs
           SET status = ?, workflow_claim_id = COALESCE(?, workflow_claim_id),
               updated_at = ?, completed_at = ?,
               lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL,
               last_error = ?
           WHERE incident_row_id = ?""",
        (
            status,
            workflow_claim_id,
            now_text,
            now_text if status in {
                "CANCELLED", "COMPLETED", *HANDOFF_TERMINAL_OUTCOMES,
            } else None,
            last_error,
            occurrence_id,
        ),
    )


def _record_event_in_transaction(
    conn,
    *,
    incident_id: str,
    occurrence_id: int,
    event_type: str,
    outcome: str,
    workflow_claim_id: str | None = None,
    action_claim_id: str | None = None,
    details: dict | None = None,
) -> None:
    conn.execute(
        """INSERT INTO audit_events (
            event_id, incident_ref, timestamp, actor, event_type, summary,
            workflow_claim_id, action_claim_id, outcome, details
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"evt-{uuid.uuid4().hex[:16]}",
            incident_id,
            _now().isoformat(),
            MONITOR_PRINCIPAL,
            event_type,
            f"Incident handoff {event_type.removeprefix('incident_handoff_')}",
            workflow_claim_id,
            action_claim_id,
            outcome,
            json.dumps(
                {"incident_row_id": occurrence_id, **(details or {})},
                sort_keys=True,
            ),
        ),
    )


def _decode_json_dict(value: str | None) -> dict | None:
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _verified_action_evidence(action) -> bool:
    execution = _decode_json_dict(action["execution_json"])
    verification = _decode_json_dict(action["verification_json"])
    return bool(
        execution
        and execution.get("mode") == "live"
        and execution.get("executed") is True
        and execution.get("ok") is True
        and verification
        and verification.get("recovered") is True
    )


def _action_outcome(action_status: str) -> tuple[str, str] | None:
    """Return a safe handoff outcome and workflow response status."""
    return {
        "SIMULATED": ("SIMULATED", "simulation_complete"),
        "SIMULATION_FAILED": ("SIMULATION_FAILED", "simulation_failed"),
        "DRY_RUN_COMPLETE": ("DRY_RUN_COMPLETE", "dry_run_complete"),
        "EXECUTION_FAILED": ("FAILED", "execution_failed"),
        "FAILED_BEFORE_EXECUTION": ("FAILED", "failed_before_execution"),
        "VERIFICATION_FAILED": ("VERIFICATION_FAILED", "verification_failed"),
        "BLOCKED": ("BLOCKED", "blocked"),
        "REJECTED": ("REJECTED", "rejected"),
        "EXPIRED": ("EXPIRED", "expired"),
    }.get(action_status)


def _response_outcome(response: dict | None) -> tuple[str, str] | None:
    if response is None:
        return None
    status = response.get("status")
    return {
        "no_action": ("NO_ACTION", "no_action"),
        "blocked": ("BLOCKED", "blocked"),
        "rejected": ("REJECTED", "rejected"),
        "dry_run_complete": ("DRY_RUN_COMPLETE", "dry_run_complete"),
        "simulation_complete": ("SIMULATED", "simulation_complete"),
        "simulation_failed": ("SIMULATION_FAILED", "simulation_failed"),
        "execution_failed": ("FAILED", "execution_failed"),
        "failed_before_execution": ("FAILED", "failed_before_execution"),
        "verification_failed": ("VERIFICATION_FAILED", "verification_failed"),
        "expired": ("EXPIRED", "expired"),
    }.get(status)


def terminal_handoff_status(
    response: dict,
    *,
    workflow_claim_id: str | None = None,
) -> str:
    """Map a known workflow outcome without treating it as verified recovery."""
    if response.get("status") == "awaiting_approval":
        return "AWAITING_APPROVAL"
    if workflow_claim_id:
        ensure_schema()
        with closing(get_connection()) as conn:
            action = conn.execute(
                """SELECT * FROM action_claims
                   WHERE workflow_claim_id = ?
                   ORDER BY attempt_no DESC LIMIT 1""",
                (workflow_claim_id,),
            ).fetchone()
            workflow = conn.execute(
                """SELECT w.status AS workflow_status, w.response_json,
                          i.status AS incident_status,
                          i.resolution_reason
                   FROM workflow_claims w
                   LEFT JOIN incidents i ON i.id = w.incident_occurrence_id
                   WHERE w.claim_id = ?""",
                (workflow_claim_id,),
            ).fetchone()
        if action is not None:
            if (
                response.get("status") == "resolved"
                and action["status"] == "VERIFIED"
                and _verified_action_evidence(action)
                and workflow is not None
                and workflow["workflow_status"] == "COMPLETED"
                and workflow["incident_status"] == "RESOLVED"
                and workflow["resolution_reason"] == "verified_kubernetes_recovery"
            ):
                return "COMPLETED"
            outcome = _action_outcome(action["status"])
            if outcome is not None:
                return outcome[0]
            return "RECONCILIATION_REQUIRED"
        persisted_response = (
            _decode_json_dict(workflow["response_json"])
            if workflow is not None else None
        )
        if (
            workflow is None
            or workflow["workflow_status"] != "COMPLETED"
            or persisted_response is None
            or persisted_response.get("status") != response.get("status")
        ):
            return "RECONCILIATION_REQUIRED"
        response = persisted_response
    outcome = _response_outcome(response)
    return outcome[0] if outcome is not None else "RECONCILIATION_REQUIRED"


def finish_handoff(
    occurrence_id: int,
    lease_token: str,
    *,
    status: str,
    workflow_claim_id: str,
    last_error: str | None = None,
) -> bool:
    if status not in {
        "RETRY", "AWAITING_APPROVAL", "COMPLETED",
        "RECONCILIATION_REQUIRED", "CANCELLED",
        *HANDOFF_TERMINAL_OUTCOMES,
    }:
        raise ValueError("Invalid handoff status")
    ensure_schema()
    now = _now()
    retry_at = (
        (now + timedelta(seconds=RETRY_DELAY_SECONDS)).isoformat()
        if status == "RETRY" else None
    )
    completed_at = (
        now.isoformat()
        if status in {
            "COMPLETED", "CANCELLED", *HANDOFF_TERMINAL_OUTCOMES,
        } else None
    )
    with closing(get_connection()) as conn, conn:
        cursor = conn.execute(
            """UPDATE incident_handoffs
               SET status = ?, workflow_claim_id = ?, updated_at = ?,
                   retry_at = ?, completed_at = ?,
                   lease_owner = NULL, lease_token = NULL,
                   lease_expires_at = NULL, last_error = ?
               WHERE incident_row_id = ? AND status = 'DISPATCHING'
                 AND lease_token = ?""",
            (
                status, workflow_claim_id, now.isoformat(), retry_at,
                completed_at, last_error, occurrence_id, lease_token,
            ),
        )
        updated = cursor.rowcount == 1
        incident = conn.execute(
            """SELECT incident_id FROM incidents WHERE id = ?""",
            (occurrence_id,),
        ).fetchone()
        if updated and incident is not None:
            _record_event_in_transaction(
                conn,
                incident_id=incident["incident_id"],
                occurrence_id=occurrence_id,
                event_type=f"incident_handoff_{status.lower()}",
                outcome=status.lower(),
                workflow_claim_id=workflow_claim_id,
                details={"error_category": last_error} if last_error else None,
            )
    return updated


def reconcile_expired_leases(service: str) -> int:
    """Reconcile expired dispatches from durable workflow and action state."""
    ensure_schema()
    now = _now().isoformat()
    verified_recoveries: list[tuple[dict, dict, dict | None]] = []
    changed = 0
    with closing(get_connection()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """SELECT incident_row_id, incident_id, workflow_claim_id, status
               FROM incident_handoffs
               WHERE service = ?
                 AND (
                     (status = 'DISPATCHING' AND lease_expires_at <= ?)
                     OR status = 'RECONCILIATION_REQUIRED'
                 )""",
            (service, now),
        ).fetchall()
        for row in rows:
            workflow = (
                conn.execute(
                    "SELECT * FROM workflow_claims WHERE claim_id = ?",
                    (row["workflow_claim_id"],),
                ).fetchone()
                if row["workflow_claim_id"] else None
            )
            action = (
                conn.execute(
                    """SELECT a.*, p.status AS approval_status,
                              p.approval_id AS persisted_approval_id
                       FROM action_claims a
                       LEFT JOIN approval_requests p
                         ON p.approval_id = a.approval_id
                       WHERE a.workflow_claim_id = ?
                       ORDER BY a.attempt_no DESC LIMIT 1""",
                    (row["workflow_claim_id"],),
                ).fetchone()
                if row["workflow_claim_id"] else None
            )
            response = (
                _decode_json_dict(workflow["response_json"])
                if workflow is not None else None
            )

            if (
                action is not None
                and workflow is not None
                and workflow["status"] in {
                    "RUNNING", "AWAITING_APPROVAL",
                    "RECONCILIATION_REQUIRED",
                }
                and action["status"] == "AWAITING_APPROVAL"
                and action["approval_status"] == "PENDING"
            ):
                reconciled_status = "AWAITING_APPROVAL"
                response = {
                    **(response or {}),
                    "incident_id": workflow["incident_ref"],
                    "workflow_claim_id": workflow["claim_id"],
                    "action_claim_id": action["action_claim_id"],
                    "approval_id": action["persisted_approval_id"],
                    "status": "awaiting_approval",
                }
                if workflow["status"] in {
                    "RUNNING", "RECONCILIATION_REQUIRED",
                }:
                    conn.execute(
                        """UPDATE workflow_claims
                           SET status = 'AWAITING_APPROVAL',
                               response_json = ?, updated_at = ?
                           WHERE claim_id = ?""",
                        (
                            json.dumps(response, sort_keys=True),
                            now,
                            workflow["claim_id"],
                        ),
                    )
                error = None
            elif (
                action is not None
                and action["status"] == "VERIFIED"
                and _verified_action_evidence(action)
                and workflow is not None
                and workflow["status"] in {
                    "RUNNING", "AWAITING_APPROVAL",
                    "RECONCILIATION_REQUIRED", "COMPLETED",
                }
            ):
                verified_recoveries.append(
                    (dict(row), dict(action), response)
                )
                continue
            else:
                outcome = (
                    _action_outcome(action["status"])
                    if action is not None and workflow is not None else None
                )
                if (
                    outcome is None
                    and action is None
                    and workflow is not None
                    and workflow["status"] == "COMPLETED"
                ):
                    outcome = _response_outcome(response)
                if outcome is not None:
                    reconciled_status, response_status = outcome
                    response = {
                        **(response or {}),
                        "incident_id": workflow["incident_ref"],
                        "workflow_claim_id": workflow["claim_id"],
                        "status": response_status,
                    }
                    if action is not None:
                        response["action_claim_id"] = action["action_claim_id"]
                    if workflow["status"] in {
                        "RUNNING", "AWAITING_APPROVAL",
                        "RECONCILIATION_REQUIRED", "COMPLETED",
                    }:
                        conn.execute(
                            """UPDATE workflow_claims
                               SET status = 'COMPLETED', response_json = ?,
                                   updated_at = ?
                               WHERE claim_id = ? AND status IN (
                                   'RUNNING', 'AWAITING_APPROVAL',
                                   'RECONCILIATION_REQUIRED', 'COMPLETED'
                               )""",
                            (
                                json.dumps(response, sort_keys=True),
                                now,
                                workflow["claim_id"],
                            ),
                        )
                    error = None
                else:
                    reconciled_status = "RECONCILIATION_REQUIRED"
                    if action is not None and action["status"] == "VERIFIED":
                        error = "verified_action_evidence_invalid"
                    elif action is not None and action["status"] == "COMPLETED":
                        error = "verification_evidence_missing"
                    elif action is not None and action["status"] == "EXECUTION_UNKNOWN":
                        error = "execution_outcome_unknown"
                    else:
                        error = "workflow_outcome_ambiguous"

            if (
                row["status"] == reconciled_status
                and row["status"] == "RECONCILIATION_REQUIRED"
                and row["workflow_claim_id"] == (
                    workflow["claim_id"] if workflow is not None else None
                )
            ):
                current_handoff = conn.execute(
                    """SELECT last_error FROM incident_handoffs
                       WHERE incident_row_id = ?""",
                    (row["incident_row_id"],),
                ).fetchone()
                if current_handoff is not None and current_handoff["last_error"] == error:
                    continue
            _mark_handoff_in_transaction(
                conn,
                int(row["incident_row_id"]),
                reconciled_status,
                now,
                workflow_claim_id=(
                    workflow["claim_id"] if workflow is not None
                    else row["workflow_claim_id"]
                ),
                last_error=error,
            )
            _record_event_in_transaction(
                conn,
                incident_id=row["incident_id"],
                occurrence_id=int(row["incident_row_id"]),
                event_type=(
                    "incident_handoff_reconciliation_required"
                    if reconciled_status == "RECONCILIATION_REQUIRED"
                    else "incident_handoff_reconciled"
                ),
                outcome=reconciled_status.lower(),
                workflow_claim_id=(
                    workflow["claim_id"] if workflow is not None
                    else row["workflow_claim_id"]
                ),
                action_claim_id=(
                    action["action_claim_id"] if action is not None else None
                ),
                details={
                    "workflow_status": (
                        workflow["status"] if workflow is not None else None
                    ),
                    "action_claim_status": (
                        action["status"] if action is not None else None
                    ),
                    "error_category": error,
                },
            )
            changed += 1

    for row, action, response in verified_recoveries:
        recovered_response = {
            **(response or {}),
            "incident_id": row["incident_id"],
            "workflow_claim_id": row["workflow_claim_id"],
            "action_claim_id": action["action_claim_id"],
            "status": "resolved",
            "execution": _decode_json_dict(action["execution_json"]),
            "verification": _decode_json_dict(action["verification_json"]),
        }
        from app.services import incident_repository

        finalized = incident_repository.finalize_verified_recovery(
            occurrence_id=int(row["incident_row_id"]),
            workflow_claim_id=row["workflow_claim_id"],
            action_claim_id=action["action_claim_id"],
            response=recovered_response,
        )
        if finalized:
            changed += 1
        else:
            changed += int(mark_handoff_reconciliation_required(
                int(row["incident_row_id"]),
                row["workflow_claim_id"],
                "verified_recovery_finalization_conflict",
            ))
    return changed


def mark_workflow_decision_complete(
    occurrence_id: int, workflow_claim_id: str
) -> None:
    ensure_schema()
    now = _now().isoformat()
    with closing(get_connection()) as conn, conn:
        workflow = conn.execute(
            """SELECT w.*, i.status AS incident_status,
                      i.resolution_reason
               FROM workflow_claims w
               LEFT JOIN incidents i ON i.id = w.incident_occurrence_id
               WHERE w.claim_id = ?""",
            (workflow_claim_id,),
        ).fetchone()
        action = conn.execute(
            """SELECT * FROM action_claims
               WHERE workflow_claim_id = ?
               ORDER BY attempt_no DESC LIMIT 1""",
            (workflow_claim_id,),
        ).fetchone()
        response = (
            _decode_json_dict(workflow["response_json"])
            if workflow is not None else None
        )
        if action is not None and action["status"] == "VERIFIED":
            status = (
                "COMPLETED"
                if (
                    _verified_action_evidence(action)
                    and response is not None
                    and response.get("status") == "resolved"
                    and workflow is not None
                    and workflow["incident_status"] == "RESOLVED"
                    and workflow["resolution_reason"]
                    == "verified_kubernetes_recovery"
                )
                else "RECONCILIATION_REQUIRED"
            )
        elif action is not None:
            status = (
                _action_outcome(action["status"]) or
                ("RECONCILIATION_REQUIRED", "reconciliation_required")
            )[0]
        else:
            status = (
                _response_outcome(response) or
                ("RECONCILIATION_REQUIRED", "reconciliation_required")
            )[0]
        if (
            action is not None
            and workflow is not None
            and action["status"] != "VERIFIED"
            and (outcome := _action_outcome(action["status"])) is not None
            and workflow["status"] == "COMPLETED"
        ):
            response = {
                **(response or {}),
                "incident_id": workflow["incident_ref"],
                "workflow_claim_id": workflow_claim_id,
                "action_claim_id": action["action_claim_id"],
                "status": outcome[1],
            }
            conn.execute(
                """UPDATE workflow_claims SET response_json = ?, updated_at = ?
                   WHERE claim_id = ? AND status = 'COMPLETED'""",
                (json.dumps(response, sort_keys=True), now, workflow_claim_id),
            )
        cursor = conn.execute(
            """UPDATE incident_handoffs
               SET status = ?, updated_at = ?, completed_at = ?,
                   lease_owner = NULL, lease_token = NULL,
                   lease_expires_at = NULL
               WHERE incident_row_id = ? AND workflow_claim_id = ?
                 AND status IN ('AWAITING_APPROVAL', 'RECONCILIATION_REQUIRED')
                 AND status != ?""",
            (
                status,
                now,
                now if status in {
                    "COMPLETED", *HANDOFF_TERMINAL_OUTCOMES,
                } else None,
                occurrence_id,
                workflow_claim_id,
                status,
            ),
        )
        incident = conn.execute(
            "SELECT incident_id FROM incidents WHERE id = ?", (occurrence_id,)
        ).fetchone()
        if cursor.rowcount and incident is not None:
            _record_event_in_transaction(
                conn,
                incident_id=incident["incident_id"],
                occurrence_id=occurrence_id,
                event_type=f"incident_handoff_{status.lower()}",
                outcome=status.lower(),
                workflow_claim_id=workflow_claim_id,
                action_claim_id=(
                    action["action_claim_id"] if action is not None else None
                ),
            )


def mark_handoff_reconciliation_required(
    occurrence_id: int,
    workflow_claim_id: str,
    error_category: str,
) -> bool:
    ensure_schema()
    now = _now().isoformat()
    with closing(get_connection()) as conn, conn:
        updated = conn.execute(
            """UPDATE incident_handoffs
               SET status = 'RECONCILIATION_REQUIRED', updated_at = ?,
                   completed_at = NULL, lease_owner = NULL, lease_token = NULL,
                   lease_expires_at = NULL, last_error = ?
               WHERE incident_row_id = ? AND workflow_claim_id = ?
                 AND status NOT IN ('CANCELLED', 'COMPLETED')
                 AND NOT (
                     status = 'RECONCILIATION_REQUIRED' AND last_error = ?
                 )""",
            (
                now, error_category, occurrence_id, workflow_claim_id,
                error_category,
            ),
        )
        if updated.rowcount != 1:
            return False
        incident = conn.execute(
            "SELECT incident_id FROM incidents WHERE id = ?", (occurrence_id,)
        ).fetchone()
        if incident is not None:
            _record_event_in_transaction(
                conn,
                incident_id=incident["incident_id"],
                occurrence_id=occurrence_id,
                event_type="incident_handoff_reconciliation_required",
                outcome=error_category,
                workflow_claim_id=workflow_claim_id,
                details={"error_category": error_category},
            )
    return True


def list_handoffs() -> list[dict]:
    ensure_schema()
    with closing(get_connection()) as conn:
        rows = conn.execute(
            "SELECT * FROM incident_handoffs ORDER BY incident_row_id"
        ).fetchall()
    return [dict(row) for row in rows]


def get_handoff(occurrence_id: int) -> dict | None:
    ensure_schema()
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT * FROM incident_handoffs WHERE incident_row_id = ?",
            (occurrence_id,),
        ).fetchone()
    return dict(row) if row else None
