import json
from datetime import datetime, timedelta, timezone
import uuid

from app.database import get_connection


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode_optional_json(value: str | None) -> tuple[dict | None, bool]:
    if value is None:
        return None, False
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None, True
    return (decoded, False) if isinstance(decoded, dict) else (None, True)


def record_incident(
    incident: dict,
    *,
    enqueue_handoff: bool = False,
) -> dict:
    """Create an incident or refresh its matching active record."""
    now = utc_now()
    if enqueue_handoff:
        from app.services import audit_repository

        audit_repository.ensure_schema()

    with get_connection() as connection:
        if enqueue_handoff:
            connection.execute("BEGIN IMMEDIATE")
        row = record_incident_in_transaction(
            connection,
            incident,
            now=now,
        )

        if enqueue_handoff:
            occurrence_id = int(row["id"])
            inserted = connection.execute(
                """INSERT OR IGNORE INTO incident_handoffs (
                    incident_row_id, service, incident_id, fingerprint,
                    evidence_timestamp, status, attempts, idempotency_key,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'PENDING', 0, ?, ?, ?)""",
                (
                    occurrence_id,
                    row["service"],
                    row["incident_id"],
                    row["fingerprint"],
                    row["source_timestamp"],
                    f"incident-occurrence-{occurrence_id}",
                    now,
                    now,
                ),
            )
            handoff = connection.execute(
                "SELECT * FROM incident_handoffs WHERE incident_row_id = ?",
                (occurrence_id,),
            ).fetchone()
            if inserted.rowcount == 1:
                event_type = "incident_handoff_enqueued"
            else:
                event_type = "incident_handoff_refreshed"
                connection.execute(
                    """UPDATE incident_handoffs
                       SET evidence_timestamp = ?, updated_at = ?
                       WHERE incident_row_id = ?
                         AND status IN ('PENDING', 'RETRY')""",
                    (row["source_timestamp"], now, occurrence_id),
                )
            _record_handoff_audit(
                connection,
                incident_ref=row["incident_id"],
                event_type=event_type,
                actor="kybernaut-monitor",
                summary=f"Incident occurrence {occurrence_id} handoff "
                f"{handoff['status'].lower()}",
                details={
                    "incident_row_id": occurrence_id,
                    "fingerprint": row["fingerprint"],
                    "handoff_status": handoff["status"],
                },
            )
        return dict(row)


def record_incident_in_transaction(
    connection,
    incident: dict,
    *,
    now: str | None = None,
) -> dict:
    """Create or refresh an incident using a caller-owned transaction."""
    timestamp = now or utc_now()
    fingerprint = incident.get("fingerprint")
    source_timestamp = incident.get("source_timestamp")
    source_evidence = incident.get("source_evidence")
    source_evidence_json = (
        json.dumps(source_evidence, sort_keys=True, separators=(",", ":"))
        if isinstance(source_evidence, dict) else None
    )

    if fingerprint:
        existing = connection.execute(
            """
            SELECT *
            FROM incidents
            WHERE fingerprint = ?
              AND status IN ('OPEN', 'REMEDIATING')
            ORDER BY id DESC
            LIMIT 1
            """,
            (fingerprint,),
        ).fetchone()
    else:
        existing = connection.execute(
            """
            SELECT *
            FROM incidents
            WHERE incident_id = ?
              AND status IN ('OPEN', 'REMEDIATING')
            ORDER BY id DESC
            LIMIT 1
            """,
            (incident["incident_id"],),
        ).fetchone()

    if existing:
        connection.execute(
            """
            UPDATE incidents
            SET updated_at = ?,
                last_seen_at = ?,
                severity = ?,
                message = ?,
                fingerprint = COALESCE(?, fingerprint),
                source_timestamp = COALESCE(?, source_timestamp),
                source_evidence_json = COALESCE(
                    ?, source_evidence_json
                )
            WHERE id = ?
            """,
            (
                timestamp,
                timestamp,
                incident["severity"],
                incident["message"],
                fingerprint,
                source_timestamp,
                source_evidence_json,
                existing["id"],
            ),
        )
        row = connection.execute(
            "SELECT * FROM incidents WHERE id = ?",
            (existing["id"],),
        ).fetchone()
    else:
        cursor = connection.execute(
            """
            INSERT INTO incidents (
                incident_id,
                service,
                incident_type,
                severity,
                message,
                status,
                opened_at,
                updated_at,
                fingerprint,
                last_seen_at,
                source_timestamp,
                source_evidence_json
            )
            VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?, ?, ?)
            """,
            (
                incident["incident_id"],
                incident["service"],
                incident["type"],
                incident["severity"],
                incident["message"],
                timestamp,
                timestamp,
                fingerprint,
                timestamp,
                source_timestamp,
                source_evidence_json,
            ),
        )
        row = connection.execute(
            "SELECT * FROM incidents WHERE id = ?",
            (cursor.lastrowid,),
        ).fetchone()

    return dict(row)


INCIDENT_SOURCES = {"all", "operational", "demo"}
DEMO_FINGERPRINT_PREFIX = "demo:"


def classify_incident_source(incident: dict) -> str:
    """Return "demo" for synthetic scenario records, else "operational".

    Uses the persisted source evidence marker written by the demo scheduler
    (simulation == "demo") and its reserved fingerprint prefix.
    """
    evidence = incident.get("source_evidence")
    if isinstance(evidence, dict) and evidence.get("simulation") == "demo":
        return "demo"
    fingerprint = incident.get("fingerprint")
    if isinstance(fingerprint, str) and fingerprint.startswith(
        DEMO_FINGERPRINT_PREFIX
    ):
        return "demo"
    return "operational"


def list_incidents(
    status: str | None = None,
    source: str = "all",
    *,
    latest_per_incident: bool = False,
) -> list[dict]:
    """Return incident occurrences, optionally reduced to latest IDs.

    ``source`` is "all" (default, unchanged behaviour), "operational" or
    "demo". Filtering is applied here so callers cannot bypass it. An
    incident ID is the stable public detail identifier; API list callers can
    request only the latest occurrence for each ID while internal lifecycle
    consumers retain the complete occurrence history by default.
    """
    if source not in INCIDENT_SOURCES:
        raise ValueError("source must be all, operational or demo")
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT incidents.*,
                   incident_diagnoses.status AS diagnosis_status,
                   incident_diagnoses.result_json AS diagnosis_result_json
            FROM incidents
            LEFT JOIN incident_diagnoses
              ON incident_diagnoses.incident_row_id = incidents.id
            ORDER BY incidents.id DESC
            """
        ).fetchall()

        incidents = []
        seen_incident_ids = set()
        for row in rows:
            incident = dict(row)
            incident_id = incident["incident_id"]
            if latest_per_incident:
                if incident_id in seen_incident_ids:
                    continue
                seen_incident_ids.add(incident_id)
            if status is not None and incident["status"] != status:
                continue
            evidence_json = incident.pop("source_evidence_json", None)
            diagnosis_json = incident.pop("diagnosis_result_json", None)
            source_evidence, invalid_evidence = _decode_optional_json(
                evidence_json
            )
            diagnosis, invalid_diagnosis = _decode_optional_json(diagnosis_json)
            incident["source_evidence"] = (
                {"_decode_error": "invalid_json"}
                if invalid_evidence else source_evidence
            )
            incident["diagnosis"] = diagnosis
            incident["diagnosis_result_invalid"] = invalid_diagnosis
            if (
                source != "all"
                and classify_incident_source(incident) != source
            ):
                continue
            incidents.append(incident)
        return incidents


def get_incident_detail(incident_id: str) -> dict | None:
    """Return the latest occurrence of an incident with decoded evidence.

    Read-only. Looks up by stable incident_id regardless of source so that a
    deliberately opened demo record can still be inspected.
    """
    matches = [
        item for item in list_incidents()
        if item.get("incident_id") == incident_id
    ]
    return matches[0] if matches else None


def claim_incident_diagnosis(
    incident_row_id: int,
    evidence_digest: str,
    *,
    processing_timeout_seconds: int = 300,
) -> dict:
    """Claim new/changed diagnosis evidence or retry a failed attempt."""
    now = datetime.now(timezone.utc)
    now_text = now.isoformat()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM incident_diagnoses WHERE incident_row_id = ?",
            (incident_row_id,),
        ).fetchone()
        if row is not None and row["evidence_digest"] == evidence_digest:
            if row["status"] in {"completed", "limited"}:
                return {"claimed": False, "reason": "unchanged"}
            if row["status"] == "processing":
                try:
                    updated_at = datetime.fromisoformat(row["updated_at"])
                except ValueError:
                    updated_at = now - timedelta(
                        seconds=processing_timeout_seconds + 1
                    )
                if now - updated_at < timedelta(
                    seconds=processing_timeout_seconds
                ):
                    return {"claimed": False, "reason": "in_progress"}
            if row["status"] == "failed" and row["next_retry_at"]:
                try:
                    next_retry = datetime.fromisoformat(row["next_retry_at"])
                except ValueError:
                    next_retry = now
                if next_retry > now:
                    return {"claimed": False, "reason": "retry_backoff"}
            attempts = int(row["attempts"]) + 1
            connection.execute(
                """
                UPDATE incident_diagnoses
                SET status = 'processing', attempts = ?, updated_at = ?,
                    next_retry_at = NULL
                WHERE incident_row_id = ?
                """,
                (attempts, now_text, incident_row_id),
            )
        else:
            attempts = 1
            connection.execute(
                """
                INSERT INTO incident_diagnoses (
                    incident_row_id, evidence_digest, status, result_json,
                    attempts, updated_at, next_retry_at
                )
                VALUES (?, ?, 'processing', NULL, ?, ?, NULL)
                ON CONFLICT(incident_row_id) DO UPDATE SET
                    evidence_digest = excluded.evidence_digest,
                    status = 'processing',
                    result_json = NULL,
                    attempts = excluded.attempts,
                    updated_at = excluded.updated_at,
                    next_retry_at = NULL
                """,
                (incident_row_id, evidence_digest, attempts, now_text),
            )
    return {"claimed": True, "attempts": attempts}


def finish_incident_diagnosis(
    incident_row_id: int,
    evidence_digest: str,
    status: str,
    result: dict,
    *,
    attempts: int,
    retry_base_seconds: int = 60,
    retry_max_seconds: int = 900,
) -> None:
    if status not in {"completed", "limited", "failed"}:
        raise ValueError("Invalid diagnosis status")
    now = datetime.now(timezone.utc)
    retry_at = None
    if status == "failed":
        delay = min(
            retry_base_seconds * (2 ** min(max(0, attempts - 1), 20)),
            retry_max_seconds,
        )
        retry_at = (now + timedelta(seconds=delay)).isoformat()
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE incident_diagnoses
            SET status = ?, result_json = ?, updated_at = ?,
                next_retry_at = ?
            WHERE incident_row_id = ? AND evidence_digest = ?
            """,
            (
                status,
                json.dumps(result, sort_keys=True, separators=(",", ":")),
                now.isoformat(),
                retry_at,
                incident_row_id,
                evidence_digest,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Incident diagnosis claim is no longer current")


def resolve_open_incidents(
    service: str,
    incident_type: str | None = None,
) -> int:
    """Resolve incidents cleared by a healthy service observation."""
    now = utc_now()
    from app.services import audit_repository

    audit_repository.ensure_schema()

    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        query = """SELECT id, incident_id FROM incidents
                   WHERE service = ? AND status IN ('OPEN', 'REMEDIATING')"""
        params: tuple = (service,)
        if incident_type is not None:
            query += " AND incident_type = ?"
            params = (service, incident_type)
        active = connection.execute(query, params).fetchall()
        return _resolve_healthy_occurrences(
            connection,
            active,
            resolution_reason="healthy_service_observation",
            now=now,
        )



def resolve_incident_fingerprint(
    fingerprint: str,
    *,
    resolution_reason: str = "telemetry_condition_cleared",
) -> int:
    """Resolve an incident only from healthy telemetry, not recovery proof."""
    now = utc_now()
    from app.services import audit_repository

    audit_repository.ensure_schema()

    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            """SELECT id, incident_id FROM incidents
               WHERE fingerprint = ?
                 AND status IN ('OPEN', 'REMEDIATING')""",
            (fingerprint,),
        ).fetchall()
        return _resolve_healthy_occurrences(
            connection,
            active,
            resolution_reason=resolution_reason,
            now=now,
        )


def _resolve_healthy_occurrences(
    connection,
    incidents,
    *,
    resolution_reason: str,
    now: str,
) -> int:
    resolved = 0
    for incident in incidents:
        occurrence_id = int(incident["id"])
        handoff = connection.execute(
            """SELECT * FROM incident_handoffs
               WHERE incident_row_id = ?""",
            (occurrence_id,),
        ).fetchone()
        workflows = connection.execute(
            """SELECT claim_id FROM workflow_claims
               WHERE incident_occurrence_id = ?""",
            (occurrence_id,),
        ).fetchall()
        workflow_ids = {row["claim_id"] for row in workflows}
        if handoff is not None and handoff["workflow_claim_id"]:
            workflow_ids.add(handoff["workflow_claim_id"])
        correlation_workflow_id = (
            handoff["workflow_claim_id"]
            if handoff is not None and handoff["workflow_claim_id"]
            else min(workflow_ids, default=None)
        )
        actions = []
        if workflow_ids:
            placeholders = ",".join("?" for _ in workflow_ids)
            actions = connection.execute(
                f"""SELECT * FROM action_claims
                    WHERE workflow_claim_id IN ({placeholders})""",
                tuple(workflow_ids),
            ).fetchall()

        blocking = [
            action for action in actions
            if action["status"] in {
                "EXECUTING", "EXECUTION_UNKNOWN", "COMPLETED", "VERIFIED",
            }
        ]
        if blocking:
            _record_handoff_audit(
                connection,
                incident_ref=incident["incident_id"],
                event_type="incident_resolution_deferred",
                actor="kybernaut-monitor",
                summary="Healthy telemetry cannot resolve an executing or "
                "unverified remediation",
                workflow_claim_id=correlation_workflow_id,
                details={
                    "incident_row_id": occurrence_id,
                    "resolution_reason": resolution_reason,
                    "blocking_action_claim_ids": [
                        action["action_claim_id"] for action in blocking
                    ],
                },
            )
            continue

        cursor = connection.execute(
            """UPDATE incidents
               SET status = 'RESOLVED', updated_at = ?, resolved_at = ?,
                   resolution_reason = ?
               WHERE id = ? AND status IN ('OPEN', 'REMEDIATING')""",
            (now, now, resolution_reason, occurrence_id),
        )
        if cursor.rowcount != 1:
            continue
        resolved += 1

        for action in actions:
            if action["status"] in {
                "RESERVED", "AWAITING_APPROVAL", "APPROVED", "AUTO_AUTHORIZED",
            }:
                connection.execute(
                    """UPDATE action_claims
                       SET status = 'BLOCKED', updated_at = ?
                       WHERE action_claim_id = ? AND status = ?""",
                    (now, action["action_claim_id"], action["status"]),
                )
                if action["approval_id"]:
                    approval_cancelled = connection.execute(
                        """UPDATE approval_requests
                           SET status = 'CANCELLED', decided_by = ?,
                               decided_at = ?, decision_reason = ?
                           WHERE approval_id = ? AND status = 'PENDING'""",
                        (
                            "kybernaut-monitor",
                            now,
                            resolution_reason,
                            action["approval_id"],
                        ),
                    )
                else:
                    approval_cancelled = None
                if approval_cancelled is not None and approval_cancelled.rowcount:
                    _record_handoff_audit(
                        connection,
                        incident_ref=incident["incident_id"],
                        event_type="approval_cancelled",
                        actor="kybernaut-monitor",
                        summary="Approval cancelled because the incident "
                        "condition cleared before execution",
                        workflow_claim_id=correlation_workflow_id,
                        action_claim_id=action["action_claim_id"],
                        outcome="condition_cleared",
                        details={
                            "incident_row_id": occurrence_id,
                            "approval_id": action["approval_id"],
                            "resolution_reason": resolution_reason,
                        },
                    )
        unclaimed_approvals = connection.execute(
            """SELECT approval_id, proposed_action, action_claim_id
               FROM approval_requests
               WHERE incident_ref = ? AND status = 'PENDING'""",
            (incident["incident_id"],),
        ).fetchall()
        for approval in unclaimed_approvals:
            cancelled = connection.execute(
                """UPDATE approval_requests
                   SET status = 'CANCELLED', decided_by = ?,
                       decided_at = ?, decision_reason = ?
                   WHERE approval_id = ? AND status = 'PENDING'""",
                (
                    "kybernaut-monitor",
                    now,
                    resolution_reason,
                    approval["approval_id"],
                ),
            )
            if cancelled.rowcount:
                try:
                    action = json.loads(approval["proposed_action"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    action = {}
                _record_handoff_audit(
                    connection,
                    incident_ref=incident["incident_id"],
                    event_type="approval_cancelled",
                    actor="kybernaut-monitor",
                    summary="Approval cancelled because the incident "
                    "condition cleared before execution",
                    workflow_claim_id=correlation_workflow_id,
                    action_claim_id=approval["action_claim_id"],
                    outcome="condition_cleared",
                    details={
                        "incident_row_id": occurrence_id,
                        "approval_id": approval["approval_id"],
                        "resolution_reason": resolution_reason,
                    },
                )
        for workflow_id in workflow_ids:
            connection.execute(
                """UPDATE workflow_claims
                   SET status = 'COMPLETED', response_json = ?, updated_at = ?
                   WHERE claim_id = ? AND status IN (
                       'RUNNING', 'AWAITING_APPROVAL',
                       'RECONCILIATION_REQUIRED'
                   )""",
                (
                    json.dumps({
                        "incident_id": incident["incident_id"],
                        "workflow_claim_id": workflow_id,
                        "status": "condition_cleared",
                        "resolution_reason": resolution_reason,
                        "occurrence_id": occurrence_id,
                    }, sort_keys=True),
                    now,
                    workflow_id,
                ),
            )

        if handoff is not None:
            connection.execute(
                """UPDATE incident_handoffs
                   SET status = 'CANCELLED', completed_at = ?,
                       updated_at = ?, lease_owner = NULL, lease_token = NULL,
                       lease_expires_at = NULL, last_error = ?
                   WHERE incident_row_id = ?
                     AND status IN (
                         'PENDING', 'RETRY', 'DISPATCHING',
                         'AWAITING_APPROVAL', 'RECONCILIATION_REQUIRED'
                     )""",
                (now, now, resolution_reason, occurrence_id),
            )
        _record_handoff_audit(
            connection,
            incident_ref=incident["incident_id"],
            event_type="incident_resolved",
            actor="kybernaut-monitor",
            summary="Incident condition cleared by a healthy observation",
            workflow_claim_id=correlation_workflow_id,
            outcome=resolution_reason,
            details={
                "incident_row_id": occurrence_id,
                "resolution_reason": resolution_reason,
                "evidence_type": "healthy_telemetry",
            },
        )
    return resolved


def finalize_verified_recovery(
    *,
    occurrence_id: int,
    workflow_claim_id: str,
    action_claim_id: str,
    response: dict,
) -> bool:
    """Atomically persist a bound workflow's verified recovery terminal state."""
    from app.services.action_claim_repository import _json

    now = utc_now()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        occurrence = connection.execute(
            """SELECT incident_id, status FROM incidents WHERE id = ?""",
            (occurrence_id,),
        ).fetchone()
        action = connection.execute(
            """SELECT * FROM action_claims
               WHERE action_claim_id = ? AND workflow_claim_id = ?""",
            (action_claim_id, workflow_claim_id),
        ).fetchone()
        workflow = connection.execute(
            """SELECT status, incident_occurrence_id FROM workflow_claims
               WHERE claim_id = ?""",
            (workflow_claim_id,),
        ).fetchone()
        approval = connection.execute(
            """SELECT p.decided_by FROM approval_requests p
               JOIN action_claims a ON a.approval_id = p.approval_id
               WHERE a.action_claim_id = ?""",
            (action_claim_id,),
        ).fetchone()
        handoff = connection.execute(
            """SELECT status FROM incident_handoffs
               WHERE incident_row_id = ? AND workflow_claim_id = ?""",
            (occurrence_id, workflow_claim_id),
        ).fetchone()
        if (
            occurrence is None
            or occurrence["status"] not in {"OPEN", "REMEDIATING"}
            or action is None
            or action["status"] != "VERIFIED"
            or workflow is None
            or workflow["incident_occurrence_id"] != occurrence_id
            or workflow["status"] not in {
                "RUNNING", "AWAITING_APPROVAL", "RECONCILIATION_REQUIRED",
                "COMPLETED",
            }
            or (
                handoff is not None
                and handoff["status"] not in {
                    "DISPATCHING", "AWAITING_APPROVAL",
                    "RECONCILIATION_REQUIRED",
                }
            )
        ):
            return False
        try:
            execution = json.loads(action["execution_json"] or "{}")
            verification = json.loads(action["verification_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return False
        if not (
            execution.get("mode") == "live"
            and execution.get("executed") is True
            and execution.get("ok") is True
            and verification.get("recovered") is True
        ):
            return False

        incident_update = connection.execute(
            """UPDATE incidents
               SET status = 'RESOLVED', updated_at = ?, resolved_at = ?,
                   resolution_reason = 'verified_kubernetes_recovery'
               WHERE id = ? AND status IN ('OPEN', 'REMEDIATING')""",
            (now, now, occurrence_id),
        )
        handoff_update = None
        if handoff is not None:
            handoff_update = connection.execute(
                """UPDATE incident_handoffs
                   SET status = 'COMPLETED', updated_at = ?, completed_at = ?,
                       lease_owner = NULL, lease_token = NULL,
                       lease_expires_at = NULL, last_error = NULL
                   WHERE incident_row_id = ? AND workflow_claim_id = ?
                     AND status IN (
                         'DISPATCHING', 'AWAITING_APPROVAL',
                         'RECONCILIATION_REQUIRED'
                     )""",
                (now, now, occurrence_id, workflow_claim_id),
            )
        workflow_update = connection.execute(
            """UPDATE workflow_claims
               SET status = 'COMPLETED', response_json = ?, updated_at = ?
               WHERE claim_id = ? AND status IN (
                   'RUNNING', 'AWAITING_APPROVAL',
                   'RECONCILIATION_REQUIRED', 'COMPLETED'
               )""",
            (_json(response), now, workflow_claim_id),
        )
        if not (
            incident_update.rowcount == 1
            and (handoff_update is None or handoff_update.rowcount == 1)
            and workflow_update.rowcount == 1
        ):
            raise RuntimeError("Verified recovery terminal transition conflicted")

        _record_handoff_audit(
            connection,
            incident_ref=occurrence["incident_id"],
            event_type="incident_recovery_verified",
            actor=(
                approval["decided_by"]
                if approval is not None and approval["decided_by"]
                else "kybernaut-workflow"
            ),
            summary="Live remediation recovery verified and incident resolved",
            workflow_claim_id=workflow_claim_id,
            action_claim_id=action_claim_id,
            outcome="resolved",
            details={
                "incident_row_id": occurrence_id,
                "resolution_reason": "verified_kubernetes_recovery",
                "execution_mode": "live",
                "verification_recovered": True,
            },
        )
        return True


def _record_handoff_audit(
    connection,
    *,
    incident_ref: str,
    event_type: str,
    actor: str,
    summary: str,
    details: dict,
    workflow_claim_id: str | None = None,
    action_claim_id: str | None = None,
    outcome: str | None = None,
) -> None:
    connection.execute(
        """INSERT INTO audit_events (
            event_id, incident_ref, timestamp, actor, event_type, summary,
            workflow_claim_id, action_claim_id, outcome, details
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            f"evt-{uuid.uuid4().hex[:16]}",
            incident_ref,
            utc_now(),
            actor,
            event_type,
            summary,
            workflow_claim_id,
            action_claim_id,
            outcome or event_type.removeprefix("incident_handoff_"),
            json.dumps(details, sort_keys=True, separators=(",", ":")),
        ),
    )


def get_incident_occurrence(occurrence_id: int) -> dict | None:
    with get_connection() as connection:
        row = connection.execute(
            "SELECT * FROM incidents WHERE id = ?", (occurrence_id,)
        ).fetchone()
    return dict(row) if row else None


def get_active_occurrence_by_incident_id(incident_id: str) -> dict | None:
    occurrence = get_latest_occurrence_by_incident_id(incident_id)
    if occurrence is None or occurrence["status"] not in {"OPEN", "REMEDIATING"}:
        return None
    return occurrence


def get_latest_occurrence_by_incident_id(incident_id: str) -> dict | None:
    with get_connection() as connection:
        incidents_table = connection.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type = 'table' AND name = 'incidents'"""
        ).fetchone()
        if incidents_table is None:
            return None
        row = connection.execute(
            """SELECT * FROM incidents
               WHERE incident_id = ?
               ORDER BY id DESC LIMIT 1""",
            (incident_id,),
        ).fetchone()
    return dict(row) if row else None