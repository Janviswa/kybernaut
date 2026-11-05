"""Durable approval and execution state for scoped lab request changes."""

from __future__ import annotations

import json
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone

from app.database import get_connection
from app.services import audit_repository

APPROVAL_TTL_HOURS = 24


class CostResizeStateError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _decode(row) -> dict:
    data = dict(row)
    for key in ("plan_json", "execution_json"):
        if data.get(key):
            data[key.removesuffix("_json")] = json.loads(data.pop(key))
    return data


def ensure_schema() -> None:
    audit_repository.ensure_schema()
    with closing(get_connection()) as connection, connection:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS cost_resize_requests (
                request_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                requested_by TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                approved_by TEXT,
                approved_at TEXT,
                approval_reason TEXT,
                execution_json TEXT,
                updated_at TEXT NOT NULL
            )"""
        )
        columns = {
            row["name"] for row in connection.execute(
                "PRAGMA table_info(cost_resize_requests)"
            )
        }
        if "execution_stage" not in columns:
            connection.execute(
                "ALTER TABLE cost_resize_requests ADD COLUMN execution_stage TEXT"
            )
        if "reconciliation_id" not in columns:
            connection.execute(
                "ALTER TABLE cost_resize_requests ADD COLUMN reconciliation_id TEXT"
            )


def create_request(
    plan: dict, *, requested_by: str
) -> dict:
    ensure_schema()
    now = _now()
    request = {
        "request_id": f"resize-{uuid.uuid4().hex}",
        "status": "PENDING",
        "plan_json": json.dumps(plan, sort_keys=True),
        "requested_by": requested_by,
        "requested_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=APPROVAL_TTL_HOURS)).isoformat(),
        "updated_at": now.isoformat(),
    }
    with closing(get_connection()) as connection, connection:
        connection.execute(
            """INSERT INTO cost_resize_requests (
                request_id, status, plan_json, requested_by, requested_at,
                expires_at, updated_at
            ) VALUES (
                :request_id, :status, :plan_json, :requested_by, :requested_at,
                :expires_at, :updated_at
            )""",
            request,
        )
        audit_repository.record_event_in_transaction(
            connection,
            incident_ref=f"cost-resize:{request['request_id']}",
            event_type="cost_resize_requested",
            actor=requested_by,
            summary="Lab Deployment request resize submitted for approval.",
            resource=plan["workload"],
            outcome="pending_approval",
            details={"plan": plan},
        )
    return get_request(request["request_id"])


def get_request(request_id: str) -> dict | None:
    ensure_schema()
    with closing(get_connection()) as connection:
        row = connection.execute(
            "SELECT * FROM cost_resize_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
    return _decode(row) if row else None


def list_requests(limit: int = 100) -> list[dict]:
    ensure_schema()
    limit = max(1, min(int(limit), 500))
    with closing(get_connection()) as connection:
        rows = connection.execute(
            "SELECT * FROM cost_resize_requests "
            "ORDER BY requested_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_decode(row) for row in rows]


def approve_request(
    request_id: str, *, approved_by: str, reason: str
) -> dict:
    ensure_schema()
    now = _now()
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM cost_resize_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise CostResizeStateError("not_found", "Resize request not found")
        request = _decode(row)
        if request["status"] != "PENDING":
            raise CostResizeStateError(
                "already_decided", "Resize request is no longer pending"
            )
        if request["requested_by"] == approved_by:
            raise CostResizeStateError(
                "same_operator", "A different operator must approve this request"
            )
        if _now() >= datetime.fromisoformat(request["expires_at"]):
            connection.execute(
                "UPDATE cost_resize_requests SET status='EXPIRED', updated_at=? "
                "WHERE request_id=? AND status='PENDING'",
                (now.isoformat(), request_id),
            )
            audit_repository.record_event_in_transaction(
                connection,
                incident_ref=f"cost-resize:{request_id}",
                event_type="cost_resize_expired",
                actor=approved_by,
                summary="Lab Deployment request resize expired before approval.",
                resource=request["plan"]["workload"],
                approval_id=request_id,
                outcome="expired",
                details={"plan": request["plan"]},
            )
            connection.commit()
            raise CostResizeStateError("expired", "Resize request has expired")
        connection.execute(
            """UPDATE cost_resize_requests SET status='APPROVED',
               approved_by=?, approved_at=?, approval_reason=?, updated_at=?
               WHERE request_id=? AND status='PENDING'""",
            (approved_by, now.isoformat(), reason, now.isoformat(), request_id),
        )
        audit_repository.record_event_in_transaction(
            connection,
            incident_ref=f"cost-resize:{request_id}",
            event_type="cost_resize_approved",
            actor=approved_by,
            summary="Lab Deployment request resize approved.",
            resource=request["plan"]["workload"],
            approval_id=request_id,
            outcome="approved",
            details={"reason": reason, "plan": request["plan"]},
        )
        connection.commit()
    return get_request(request_id)


def start_execution(request_id: str, *, operator_id: str) -> dict:
    ensure_schema()
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM cost_resize_requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise CostResizeStateError("not_found", "Resize request not found")
        request = _decode(row)
        if request["status"] != "APPROVED":
            raise CostResizeStateError(
                "not_approved", "Resize request is not approved"
            )
        if _now() >= datetime.fromisoformat(request["expires_at"]):
            now = _now()
            connection.execute(
                "UPDATE cost_resize_requests SET status='EXPIRED', updated_at=? "
                "WHERE request_id=? AND status='APPROVED'",
                (now.isoformat(), request_id),
            )
            audit_repository.record_event_in_transaction(
                connection,
                incident_ref=f"cost-resize:{request_id}",
                event_type="cost_resize_expired",
                actor=operator_id,
                summary="Approved lab Deployment request resize expired before execution.",
                resource=request["plan"]["workload"],
                approval_id=request_id,
                outcome="expired",
                details={"plan": request["plan"]},
            )
            connection.commit()
            raise CostResizeStateError("expired", "Resize request has expired")
        connection.execute(
            "UPDATE cost_resize_requests SET status='EXECUTING', "
            "execution_stage='AUTHORIZED_NOT_ATTEMPTED', updated_at=? "
            "WHERE request_id=? AND status='APPROVED'",
            (_now().isoformat(), request_id),
        )
        audit_repository.record_event_in_transaction(
            connection,
            incident_ref=f"cost-resize:{request_id}",
            event_type="cost_resize_execution_started",
            actor=operator_id,
            summary="Approved lab Deployment request resize execution started.",
            resource=request["plan"]["workload"],
            approval_id=request_id,
            outcome="executing",
            details={"plan": request["plan"]},
        )
        connection.commit()
    return get_request(request_id)


def update_execution_stage(
    request_id: str, stage: str, *, operator_id: str
) -> dict:
    allowed = {
        "PREFLIGHT_VALIDATION",
        "PREFLIGHT_FAILED",
        "PATCH_ATTEMPT_STARTED",
        "PATCH_REJECTED",
        "PATCH_OUTCOME_UNKNOWN",
        "PATCH_ACCEPTED_VERIFICATION_PENDING",
        "EXECUTION_VERIFIED",
        "VERIFICATION_FAILED",
    }
    if stage not in allowed:
        raise CostResizeStateError("invalid_stage", "Invalid resize execution stage")
    ensure_schema()
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """UPDATE cost_resize_requests SET execution_stage=?, updated_at=?
               WHERE request_id=? AND status='EXECUTING'""",
            (stage, _now().isoformat(), request_id),
        )
        if cursor.rowcount != 1:
            raise CostResizeStateError(
                "invalid_transition", "Resize request is not executing"
            )
        row = connection.execute(
            "SELECT plan_json FROM cost_resize_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        plan = json.loads(row["plan_json"])
        audit_repository.record_event_in_transaction(
            connection,
            incident_ref=f"cost-resize:{request_id}",
            event_type="cost_resize_stage_changed",
            actor=operator_id,
            summary=f"Lab resize execution stage changed to {stage}.",
            resource=plan["workload"],
            approval_id=request_id,
            outcome=stage.lower(),
            details={"execution_stage": stage},
        )
        connection.commit()
    return get_request(request_id)


def begin_reconciliation(
    request_id: str, *, operator_id: str
) -> tuple[dict, str]:
    ensure_schema()
    attempt_id = uuid.uuid4().hex
    reconcilable = {
        "EXECUTING",
        "EXECUTION_UNKNOWN",
        "EXECUTION_FAILED",
        "VERIFICATION_FAILED",
        "RECONCILIATION_UNRESOLVED",
        "RECONCILING",
    }
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM cost_resize_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise CostResizeStateError("not_found", "Resize request not found")
        request = _decode(row)
        if request["status"] not in reconcilable:
            raise CostResizeStateError(
                "not_reconcilable",
                "Resize request is not in an unresolved execution state",
            )
        connection.execute(
            """UPDATE cost_resize_requests
               SET status='RECONCILING', execution_stage='RECONCILIATION_STARTED',
                   reconciliation_id=?, updated_at=?
               WHERE request_id=? AND status=?""",
            (
                attempt_id,
                _now().isoformat(),
                request_id,
                request["status"],
            ),
        )
        audit_repository.record_event_in_transaction(
            connection,
            incident_ref=f"cost-resize:{request_id}",
            event_type="cost_resize_reconciliation_started",
            actor=operator_id,
            summary="Read-only lab Deployment reconciliation started.",
            resource=request["plan"]["workload"],
            approval_id=request_id,
            outcome="reconciling",
            details={"reconciliation_id": attempt_id},
        )
        connection.commit()
    return get_request(request_id), attempt_id


def finish_reconciliation(
    request_id: str,
    attempt_id: str,
    result: dict,
    *,
    operator_id: str,
) -> dict:
    allowed = {
        "RECONCILED_ORIGINAL_STATE",
        "RECONCILED_PROPOSED_STATE",
        "RECONCILIATION_UNRESOLVED",
    }
    status = result.get("status")
    if status not in allowed:
        raise CostResizeStateError(
            "invalid_reconciliation", "Invalid reconciliation result"
        )
    ensure_schema()
    with closing(get_connection()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            """UPDATE cost_resize_requests
               SET status=?, execution_stage=?, execution_json=?, updated_at=?
               WHERE request_id=? AND status='RECONCILING'
                 AND reconciliation_id=?""",
            (
                status,
                result.get("execution_stage", status),
                json.dumps(result, sort_keys=True, default=str),
                _now().isoformat(),
                request_id,
                attempt_id,
            ),
        )
        if cursor.rowcount != 1:
            raise CostResizeStateError(
                "stale_reconciliation",
                "A newer reconciliation attempt superseded this result",
            )
        row = connection.execute(
            "SELECT plan_json FROM cost_resize_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        plan = json.loads(row["plan_json"])
        audit_repository.record_event_in_transaction(
            connection,
            incident_ref=f"cost-resize:{request_id}",
            event_type="cost_resize_reconciliation_finished",
            actor=operator_id,
            summary=f"Read-only resize reconciliation finished with {status}.",
            resource=plan["workload"],
            approval_id=request_id,
            outcome=status.lower(),
            details={
                "reconciliation_id": attempt_id,
                "result": result,
            },
        )
        connection.commit()
    return get_request(request_id)


def finish_execution(
    request_id: str, result: dict, *, status: str, operator_id: str
) -> dict:
    if status not in {
        "VERIFIED", "EXECUTION_FAILED", "VERIFICATION_FAILED",
        "EXECUTION_UNKNOWN", "BLOCKED",
    }:
        raise ValueError("Invalid resize execution status")
    ensure_schema()
    with closing(get_connection()) as connection, connection:
        cursor = connection.execute(
            """UPDATE cost_resize_requests
               SET status=?, execution_json=?, execution_stage=?, updated_at=?
               WHERE request_id=? AND status='EXECUTING'""",
            (
                status,
                json.dumps(result, sort_keys=True, default=str),
                result.get("execution_stage", status),
                _now().isoformat(),
                request_id,
            ),
        )
        if cursor.rowcount != 1:
            raise CostResizeStateError(
                "invalid_transition", "Resize request is not executing"
            )
        row = connection.execute(
            "SELECT plan_json FROM cost_resize_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
        plan = json.loads(row["plan_json"])
        audit_repository.record_event_in_transaction(
            connection,
            incident_ref=f"cost-resize:{request_id}",
            event_type="cost_resize_execution_finished",
            actor=operator_id,
            summary=f"Lab Deployment request resize finished with {status}.",
            resource=plan["workload"],
            approval_id=request_id,
            outcome=status.lower(),
            details={"execution": result},
        )
    return get_request(request_id)
