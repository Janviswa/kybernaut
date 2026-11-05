"""Opt-in simulated incident lifecycle for local dashboard demonstrations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from app.database import get_connection
from app.services import audit_repository, incident_repository
from app.services.incident_workflow import analyze_root_cause, enforce_policy

DEMO_ENABLED_ENV = "KYBERNAUT_DEMO_MODE"
DEMO_ENVIRONMENT_ENV = "KYBERNAUT_ENVIRONMENT"
DEMO_INTERVAL_ENV = "KYBERNAUT_DEMO_INTERVAL_SECONDS"
DEFAULT_DEMO_INTERVAL_SECONDS = 180.0
MIN_DEMO_INTERVAL_SECONDS = 10.0
MAX_DEMO_INTERVAL_SECONDS = 86_400.0
DEMO_SESSION_TTL_SECONDS = 90
SIMULATED_SERVICE = "demo-synthetic"

SCENARIOS = (
    {
        "id": "PodCrashLoopBackOff",
        "incident_type": "pod_crash",
        "severity": "high",
        "evidence": (
            "Illustrative CrashLoopBackOff scenario; no Kubernetes workload "
            "was observed or changed."
        ),
    },
    {
        "id": "ElevatedLatency",
        "incident_type": "high_latency",
        "severity": "medium",
        "evidence": (
            "Illustrative elevated-latency scenario; no latency measurement "
            "was collected or fabricated."
        ),
    },
    {
        "id": "CpuSaturation",
        "incident_type": "cpu_spike",
        "severity": "medium",
        "evidence": (
            "Illustrative CPU-saturation scenario; no CPU measurement "
            "was collected or fabricated."
        ),
    },
    {
        "id": "MemoryPressure",
        "incident_type": "memory_leak",
        "severity": "medium",
        "evidence": (
            "Illustrative memory-pressure scenario; no memory measurement "
            "was collected or fabricated."
        ),
    },
)

logger = logging.getLogger(__name__)


class DemoConfigurationError(ValueError):
    """Local demonstration configuration is invalid."""


def demo_mode_enabled() -> bool:
    return (
        os.getenv(DEMO_ENVIRONMENT_ENV, "").strip().lower() == "local-demo"
        and os.getenv(DEMO_ENABLED_ENV, "").strip().lower() == "true"
    )


def demo_interval_seconds() -> float:
    raw = os.getenv(
        DEMO_INTERVAL_ENV, str(DEFAULT_DEMO_INTERVAL_SECONDS)
    )
    try:
        interval = float(raw)
    except (TypeError, ValueError) as exc:
        raise DemoConfigurationError(
            f"{DEMO_INTERVAL_ENV} must be between "
            f"{MIN_DEMO_INTERVAL_SECONDS:g} and "
            f"{MAX_DEMO_INTERVAL_SECONDS:g} seconds"
        ) from exc
    if (
        not math.isfinite(interval)
        or interval < MIN_DEMO_INTERVAL_SECONDS
        or interval > MAX_DEMO_INTERVAL_SECONDS
    ):
        raise DemoConfigurationError(
            f"{DEMO_INTERVAL_ENV} must be between "
            f"{MIN_DEMO_INTERVAL_SECONDS:g} and "
            f"{MAX_DEMO_INTERVAL_SECONDS:g} seconds"
        )
    return interval


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def _active_session_anchor(now: datetime) -> datetime | None:
    with get_connection() as connection:
        row = connection.execute(
            """SELECT MIN(started_at) AS started_at
               FROM demo_sessions WHERE expires_at > ?""",
            (now.isoformat(),),
        ).fetchone()
    return _parse_utc(row["started_at"]) if row and row["started_at"] else None


def _next_occurrence(
    anchor: datetime, now: datetime, interval: float
) -> tuple[datetime, int]:
    elapsed = max(0.0, (now - anchor).total_seconds())
    index = math.floor(elapsed / interval) + 1
    return anchor + timedelta(seconds=index * interval), index


def _slot_key(anchor: datetime, index: int) -> str:
    return f"{anchor.astimezone(timezone.utc).isoformat()}#{index}"


def _occurrence_id(slot: str) -> str:
    return f"DEMO-{hashlib.sha256(slot.encode('utf-8')).hexdigest()[:12]}"


def _set_occurrence_outcome(
    slot: str, *, status: str, error_code: str | None = None
) -> None:
    with get_connection() as connection:
        connection.execute(
            """UPDATE demo_incident_occurrences
               SET status = ?, error_code = ?, updated_at = ?
               WHERE scheduled_slot = ?""",
            (status, error_code, _now().isoformat(), slot),
        )


def _persist_demo_incident(
    slot: str, scenario: dict[str, str]
) -> tuple[dict[str, Any], bool]:
    created_at = _now().isoformat()
    incident_id = _occurrence_id(slot)
    source_evidence = {
        "simulation": "demo",
        "simulated": True,
        "source": "kybernaut_demo",
        "evidence_kind": "synthetic_scenario",
        "scenario": scenario["id"],
        "created_at": created_at,
        "description": scenario["evidence"],
        "workload_reference": None,
    }
    incident = {
        "incident_id": incident_id,
        "service": SIMULATED_SERVICE,
        "type": scenario["incident_type"],
        "severity": scenario["severity"],
        "message": f"SIMULATED DEMO: {scenario['id']}. "
        "No real service or workload was affected.",
        "fingerprint": f"demo:{slot}",
        "source_timestamp": created_at,
        "source_evidence": source_evidence,
    }
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO demo_incident_occurrences (
                    scheduled_slot, incident_id, incident_row_id, scenario,
                    status, error_code, created_at, updated_at
                ) VALUES (?, ?, NULL, ?, 'creating', NULL, ?, ?)""",
                (slot, incident_id, scenario["id"], created_at, created_at),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    """SELECT incident_id, incident_row_id, scenario, status,
                              error_code, created_at
                       FROM demo_incident_occurrences
                       WHERE scheduled_slot = ?""",
                    (slot,),
                ).fetchone()
                return dict(row), False

            stored = incident_repository.record_incident_in_transaction(
                connection, incident, now=created_at
            )
            connection.execute(
                """UPDATE demo_incident_occurrences
                   SET incident_row_id = ?, status = 'created',
                       updated_at = ?
                   WHERE scheduled_slot = ?""",
                (stored["id"], _now().isoformat(), slot),
            )
        except Exception:
            connection.rollback()
            raise
    return {
        "incident_id": incident_id,
        "incident_row_id": int(stored["id"]),
        "scenario": scenario["id"],
        "status": "created",
        "error_code": None,
        "created_at": created_at,
    }, True


def _demo_diagnosis(incident: dict, scenario: dict[str, str]) -> dict:
    return {
        "service": SIMULATED_SERVICE,
        "status": "unknown",
        "evidence_status": "unavailable",
        "missing_evidence": [
            "live_telemetry_not_collected",
            "verified_workload_mapping_unavailable",
        ],
        "observed_at": incident["opened_at"],
        "findings": [{
            "type": "simulated_demo_scenario",
            "severity": scenario["severity"],
            "evidence": (
                f"SIMULATED DEMO scenario {scenario['id']}: "
                f"{scenario['evidence']}"
            ),
            "recommendation": (
                "Use this synthetic event to inspect the advisory workflow; "
                "do not infer a real infrastructure condition."
            ),
        }],
    }


async def generate_demo_occurrence(
    scheduled_slot: str, scenario_index: int
) -> dict[str, Any]:
    """Persist and process one idempotent simulated scheduled occurrence."""
    scenario = SCENARIOS[scenario_index % len(SCENARIOS)]
    occurrence, created = _persist_demo_incident(scheduled_slot, scenario)
    if not created:
        return {
            **occurrence,
            "created": False,
            "simulation": "demo",
        }

    row_id = occurrence["incident_row_id"]
    incident = next(
        item
        for item in incident_repository.list_incidents()
        if item.get("id") == row_id
    )
    encoded_evidence = incident.get("source_evidence")
    digest = hashlib.sha256(
        json.dumps(
            encoded_evidence,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    claim = incident_repository.claim_incident_diagnosis(row_id, digest)
    diagnosis = _demo_diagnosis(incident, scenario)
    rca_update = {
        "rca": None,
        "rca_status": "not_run",
        "rca_error": "demo_rca_not_started",
    }
    audit_status = "success"
    try:
        audit_repository.record_event(
            incident_ref=occurrence["incident_id"],
            event_type="simulated_demo_incident_created",
            actor="kybernaut-demo-scheduler",
            summary=f"SIMULATED DEMO incident created: {scenario['id']}",
            resource=SIMULATED_SERVICE,
            outcome="created",
            details={
                "simulation": "demo",
                "scenario": scenario["id"],
                "scheduled_slot": scheduled_slot,
                "workload_reference": None,
            },
        )
    except Exception as exc:
        audit_status = "failed"
        logger.exception("Demo incident creation audit failed")
        audit_error = type(exc).__name__
    else:
        audit_error = None

    if claim.get("claimed"):
        try:
            rca_update = await analyze_root_cause({
                "diagnosis": diagnosis,
                "kubernetes": None,
                "telemetry_anomaly": {
                    "status": "unavailable",
                    "reason": "simulated_demo_data_not_operational_evidence",
                    "simulated": True,
                },
                "runbook_guidance": [],
            })
        except Exception as exc:
            logger.exception("Demo RCA workflow failed")
            rca_update = {
                "rca": None,
                "rca_status": "failed",
                "rca_error": type(exc).__name__,
            }

    policy = enforce_policy({
        "diagnosis": diagnosis,
        "telemetry_anomaly": {
            "status": "unavailable",
            "reason": "simulated_demo_data_not_operational_evidence",
            "simulated": True,
        },
        "kubernetes": None,
        "kubernetes_status": "unavailable",
    })["policy"]
    rca_status = rca_update.get("rca_status", "unavailable")
    diagnosis_status = (
        "completed"
        if rca_status == "completed" and audit_status == "success"
        else "limited"
    )
    result = {
        "simulation": "demo",
        "simulated": True,
        "scenario": scenario["id"],
        "diagnosis": diagnosis,
        "root_cause_analysis": rca_update.get("rca"),
        "root_cause_analysis_status": rca_status,
        "root_cause_analysis_error": rca_update.get("rca_error"),
        "policy_recommendation": policy,
        "audit_status": audit_status,
        "audit_error": audit_error,
        "workload_reference": None,
        "remediation": "not_submitted",
    }
    error_code = (
        audit_error
        or (
            rca_update.get("rca_error")
            if rca_status != "completed" else None
        )
    )
    try:
        audit_repository.record_event(
            incident_ref=occurrence["incident_id"],
            event_type=(
                "simulated_demo_diagnosis_completed"
                if diagnosis_status == "completed"
                else "simulated_demo_diagnosis_limited"
            ),
            actor="kybernaut-demo-scheduler",
            summary=f"SIMULATED DEMO diagnosis {diagnosis_status}",
            resource=SIMULATED_SERVICE,
            outcome=diagnosis_status,
            details={
                "simulation": "demo",
                "scenario": scenario["id"],
                "rca_status": rca_status,
                "rca_error": rca_update.get("rca_error"),
                "policy_action": policy.get("action"),
                "audit_status": audit_status,
                "remediation": "not_submitted",
            },
        )
    except Exception as exc:
        logger.exception("Demo diagnosis audit failed")
        audit_status = "failed"
        result["audit_status"] = audit_status
        error_code = error_code or type(exc).__name__
        diagnosis_status = "limited"
        result["diagnosis_status"] = diagnosis_status

    if claim.get("claimed"):
        incident_repository.finish_incident_diagnosis(
            row_id,
            digest,
            diagnosis_status,
            result,
            attempts=claim["attempts"],
        )
    _set_occurrence_outcome(
        scheduled_slot,
        status=diagnosis_status,
        error_code=error_code,
    )
    return {
        **occurrence,
        "created": True,
        "simulation": "demo",
        "diagnosis_status": diagnosis_status,
        "rca_status": rca_status,
        "rca_error": rca_update.get("rca_error"),
        "policy_action": policy.get("action"),
        "audit_status": audit_status,
        "error_code": error_code,
    }


class DemoIncidentScheduler:
    """A per-application task backed by durable session and slot records."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._last_outcome: dict[str, Any] | None = None

    async def enter_session(
        self, session_id: str, operator_id: str
    ) -> dict[str, Any]:
        if not demo_mode_enabled():
            return await self.status()
        interval = demo_interval_seconds()
        now = _now()
        with get_connection() as connection:
            connection.execute(
                """INSERT INTO demo_sessions (
                    session_id, operator_id, started_at, heartbeat_at,
                    expires_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    heartbeat_at = excluded.heartbeat_at,
                    expires_at = excluded.expires_at
                WHERE demo_sessions.operator_id = excluded.operator_id""",
                (
                    session_id,
                    operator_id,
                    now.isoformat(),
                    now.isoformat(),
                    (now + timedelta(
                        seconds=DEMO_SESSION_TTL_SECONDS
                    )).isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT operator_id FROM demo_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None or row["operator_id"] != operator_id:
            raise PermissionError("Demo session belongs to another operator")
        async with self._lock:
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._run())
        self._wake.set()
        return await self.status()

    async def heartbeat(self, session_id: str, operator_id: str) -> bool:
        now = _now()
        with get_connection() as connection:
            cursor = connection.execute(
                """UPDATE demo_sessions
                   SET heartbeat_at = ?, expires_at = ?
                   WHERE session_id = ? AND operator_id = ?""",
                (
                    now.isoformat(),
                    (now + timedelta(
                        seconds=DEMO_SESSION_TTL_SECONDS
                    )).isoformat(),
                    session_id,
                    operator_id,
                ),
            )
        return cursor.rowcount == 1

    async def leave_session(self, session_id: str, operator_id: str) -> bool:
        with get_connection() as connection:
            cursor = connection.execute(
                """DELETE FROM demo_sessions
                   WHERE session_id = ? AND operator_id = ?""",
                (session_id, operator_id),
            )
        self._wake.set()
        return cursor.rowcount == 1

    async def status(self) -> dict[str, Any]:
        enabled = demo_mode_enabled()
        try:
            interval = demo_interval_seconds()
            config_error = None
        except DemoConfigurationError as exc:
            interval = None
            config_error = str(exc)
        now = _now()
        anchor = _active_session_anchor(now) if enabled else None
        next_at = None
        if anchor is not None and interval is not None:
            next_at, _ = _next_occurrence(anchor, now, interval)
        with get_connection() as connection:
            last = connection.execute(
                """SELECT incident_id, scenario, status, error_code,
                          created_at, updated_at
                   FROM demo_incident_occurrences
                   ORDER BY created_at DESC LIMIT 1"""
            ).fetchone()
        last_generation = dict(last) if last is not None else None
        if self._last_outcome is not None:
            local_updated_at = self._last_outcome.get("updated_at")
            stored_updated_at = (
                last_generation.get("updated_at") if last_generation else None
            )
            if (
                last_generation is None
                or not stored_updated_at
                or (
                    local_updated_at
                    and _parse_utc(local_updated_at)
                    >= _parse_utc(stored_updated_at)
                )
            ):
                last_generation = self._last_outcome
        return {
            "enabled": enabled and config_error is None,
            "environment": "local-demo" if enabled else "disabled",
            "status": (
                "disabled" if not enabled
                else "configuration_error" if config_error
                else "running" if anchor is not None
                else "idle"
            ),
            "interval_seconds": interval,
            "next_scheduled_at": next_at.isoformat() if next_at else None,
            "last_generation": last_generation,
            "configuration_error": config_error,
        }

    async def shutdown(self) -> None:
        async with self._lock:
            task = self._task
            self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            now = _now()
            anchor = _active_session_anchor(now)
            if anchor is None:
                return
            interval = demo_interval_seconds()
            next_at, index = _next_occurrence(anchor, now, interval)
            delay = max(0.0, (next_at - now).total_seconds())
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
                continue
            except asyncio.TimeoutError:
                pass

            slot = _slot_key(anchor, index)
            try:
                self._last_outcome = await generate_demo_occurrence(
                    slot, index - 1
                )
                self._last_outcome["updated_at"] = _now().isoformat()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Scheduled demo incident generation failed")
                self._last_outcome = {
                    "scheduled_slot": slot,
                    "status": "failed",
                    "error_code": type(exc).__name__,
                    "simulation": "demo",
                    "updated_at": _now().isoformat(),
                }
