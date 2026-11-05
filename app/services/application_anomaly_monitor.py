"""Single-leader background polling for application telemetry anomalies."""

from __future__ import annotations

import asyncio
import fcntl
import logging
import math
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.services import incident_workflow
from app.services import incident_handoff_repository as handoffs
from app.services.incident_response_graph import (
    WorkflowExecutionAmbiguous,
    WorkflowNotStarted,
    run_response_workflow,
)
from app.services.incident_diagnosis_orchestrator import (
    process_active_incidents,
)
from app.services.anomaly_runtime import (
    EXPECTED_SERVICES,
    evaluate_service_anomaly,
)

DEFAULT_POLL_INTERVAL_SECONDS = 60.0
MIN_POLL_INTERVAL_SECONDS = 10.0
POLL_INTERVAL_ENV = "KYBERNAUT_ANOMALY_POLL_INTERVAL_SECONDS"
ENABLED_ENV = "KYBERNAUT_ANOMALY_MONITOR_ENABLED"

logger = logging.getLogger(__name__)
_PROCESS_LEADER_LOCK = threading.Lock()

MONITOR_COMPONENTS = (
    "telemetry_collection",
    "anomaly_evaluation",
    "incident_persistence",
    "handoff_enqueue",
    "handoff_dispatch",
    "reconciliation",
    "diagnosis",
    "diagnosis_audit",
)


class HandoffDispatchResults(list[dict[str, Any]]):
    """List-compatible dispatch results with the reconciliation count."""

    def __init__(self, reconciliation_count: int) -> None:
        super().__init__()
        self.reconciliation_count = reconciliation_count


def _empty_component() -> dict[str, str | None]:
    return {
        "status": "unknown",
        "reason": None,
        "last_completed_at": None,
        "last_successful_at": None,
    }


def _safe_reason(reason: Any) -> str | None:
    if not isinstance(reason, str) or not reason or len(reason) > 100:
        return None
    if not all(char.isalnum() or char in "_.:-" for char in reason):
        return "unspecified_failure"
    return reason


def _set_component(
    components: dict[str, dict[str, Any]],
    name: str,
    status: str,
    *,
    reason: Any = None,
    completed_at: str | None = None,
) -> None:
    component = components[name]
    component["status"] = status
    component["reason"] = _safe_reason(reason)
    if completed_at is not None:
        component["last_completed_at"] = completed_at
        if status == "success":
            component["last_successful_at"] = completed_at


def _aggregate_component(services: dict[str, dict[str, Any]], name: str):
    outcomes = [
        service["components"][name]["status"]
        for service in services.values()
    ]
    for status in ("failed", "retrying", "unavailable", "pending", "unknown"):
        if status in outcomes:
            return status
    return "success" if "success" in outcomes else "not_applicable"


def _aggregate_freshness(services: dict[str, dict[str, Any]], name: str):
    stale_values = [
        values["components"][name]["status_stale"]
        for values in services.values()
    ]
    if any(stale is True for stale in stale_values):
        return True
    if stale_values and all(stale is False for stale in stale_values):
        return False
    return None


def _component_health(components: dict[str, dict[str, Any]]) -> str:
    statuses = {component["status"] for component in components.values()}
    if statuses.intersection({"failed", "retrying", "unavailable"}):
        return "degraded"
    if statuses.intersection({"pending", "unknown"}):
        return "unknown"
    if any(
        component["status"] == "success"
        and component.get("status_stale") is True
        for component in components.values()
    ):
        return "degraded"
    if any(
        component["status"] == "success"
        and component.get("status_stale") is not False
        for component in components.values()
    ):
        return "unknown"
    return "healthy"


def _workflow_health(
    lifecycle_state: str,
    components: dict[str, dict[str, Any]],
) -> str:
    if lifecycle_state == "disabled":
        return "disabled"
    if lifecycle_state != "running":
        return "unknown"
    return _component_health(components)


def _diagnosis_outcomes(
    diagnoses: Any,
) -> tuple[str, str, str | None, str | None]:
    if not isinstance(diagnoses, list):
        return (
            "unknown",
            "unknown",
            "diagnosis_result_invalid",
            "diagnosis_result_invalid",
        )
    if not diagnoses:
        return "not_applicable", "not_applicable", None, None

    statuses = [
        item.get("status") if isinstance(item, dict) else None
        for item in diagnoses
    ]
    if any(status == "failed" for status in statuses):
        diagnosis_status = "failed"
    elif any(status == "limited" for status in statuses):
        diagnosis_status = "unavailable"
    elif any(status == "skipped" for status in statuses):
        diagnosis_status = "pending"
    elif all(status == "completed" for status in statuses):
        diagnosis_status = "success"
    else:
        diagnosis_status = "unknown"

    audit_failed = any(
        isinstance(item, dict)
        and (
            item.get("audit_status") == "failed"
            or (
                isinstance(item.get("limitations"), list)
                and "audit_persistence_failed" in item["limitations"]
            )
        )
        for item in diagnoses
    )
    if audit_failed:
        audit_status = "failed"
        audit_reason = "audit_persistence_failed"
    elif any(
        isinstance(item, dict) and item.get("audit_status") == "pending"
        for item in diagnoses
    ):
        audit_status = (
            "pending"
            if all(
                isinstance(item, dict)
                and item.get("audit_status")
                in {"success", "pending", "not_applicable"}
                for item in diagnoses
            )
            else "unknown"
        )
        audit_reason = (
            "diagnosis_in_progress"
            if audit_status == "pending"
            else "diagnosis_audit_outcome_not_reported"
        )
    elif all(status == "skipped" for status in statuses):
        audit_status = "not_applicable"
        audit_reason = None
    elif all(
        isinstance(item, dict)
        and (
            item.get("audit_status") == "success"
            or item.get("audit_status") == "not_applicable"
        )
        for item in diagnoses
    ) and any(
        isinstance(item, dict) and item.get("audit_status") == "success"
        for item in diagnoses
    ):
        audit_status = "success"
        audit_reason = None
    elif all(
        isinstance(item, dict)
        and item.get("audit_status") == "not_applicable"
        for item in diagnoses
    ):
        audit_status = "not_applicable"
        audit_reason = None
    else:
        audit_status = "unknown"
        audit_reason = "diagnosis_audit_outcome_not_reported"
    reason = "audit_persistence_failed" if audit_failed else None
    if reason is None:
        for item in diagnoses:
            if not isinstance(item, dict):
                continue
            candidate = (
                item.get("reason")
                or item.get("error_code")
                or item.get("root_cause_analysis_error")
            )
            if candidate is None and isinstance(item.get("limitations"), list):
                candidate = next(
                    (
                        limitation
                        for limitation in item["limitations"]
                        if isinstance(limitation, str)
                    ),
                    None,
                )
            reason = _safe_reason(candidate)
            if reason:
                break
    return diagnosis_status, audit_status, reason, audit_reason


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def dispatch_pending_handoffs(
    service: str,
    *,
    lease_owner: str = "application-anomaly-monitor",
    max_dispatches: int = 10,
) -> list[dict[str, Any]]:
    """Dispatch persisted occurrences without treating detection as approval."""
    try:
        reconciliation_count = handoffs.reconcile_expired_leases(service)
    except Exception as exc:
        try:
            setattr(exc, "_kybernaut_monitor_stage", "reconciliation")
        except (AttributeError, TypeError):
            pass
        raise
    results = HandoffDispatchResults(reconciliation_count)
    for _ in range(max_dispatches):
        claim = handoffs.claim_next_handoff(
            service, lease_owner=lease_owner
        )
        if claim is None:
            break
        if claim["kind"] != "claimed":
            results.append(claim)
            continue
        occurrence = incident_workflow.incident_repository.get_incident_occurrence(
            claim["occurrence_id"]
        )
        if (
            occurrence is None
            or occurrence["status"] not in {"OPEN", "REMEDIATING"}
            or occurrence["fingerprint"] != claim["fingerprint"]
        ):
            handoffs.finish_handoff(
                claim["occurrence_id"],
                claim["lease_token"],
                status="CANCELLED",
                workflow_claim_id=claim["workflow_claim_id"],
                last_error="incident_inactive_before_dispatch",
            )
            results.append({"kind": "cancelled", "occurrence_id": claim["occurrence_id"]})
            continue
        try:
            response = await run_response_workflow(
                claim["service"],
                incident_id=claim["incident_id"],
                requested_by=claim["requested_by"],
                workflow_claim_id=claim["workflow_claim_id"],
                monitor_handoff=True,
                occurrence_id=claim["occurrence_id"],
                expected_incident_type=claim["incident_type"],
                expected_fingerprint=claim["fingerprint"],
                require_active_occurrence=True,
            )
        except WorkflowNotStarted:
            handoffs.finish_handoff(
                claim["occurrence_id"],
                claim["lease_token"],
                status="RETRY",
                workflow_claim_id=claim["workflow_claim_id"],
                last_error="workflow_not_started",
            )
            results.append({
                "kind": "retry",
                "occurrence_id": claim["occurrence_id"],
                "reason": "workflow_not_started",
            })
        except WorkflowExecutionAmbiguous:
            handoffs.finish_handoff(
                claim["occurrence_id"],
                claim["lease_token"],
                status="RECONCILIATION_REQUIRED",
                workflow_claim_id=claim["workflow_claim_id"],
                last_error="workflow_execution_ambiguous",
            )
            results.append({
                "kind": "reconciliation",
                "occurrence_id": claim["occurrence_id"],
                "reason": "workflow_execution_ambiguous",
            })
        except Exception as exc:
            logger.exception("Incident handoff dispatch failed")
            handoffs.finish_handoff(
                claim["occurrence_id"],
                claim["lease_token"],
                status="RECONCILIATION_REQUIRED",
                workflow_claim_id=claim["workflow_claim_id"],
                last_error=type(exc).__name__,
            )
            results.append({
                "kind": "reconciliation",
                "occurrence_id": claim["occurrence_id"],
                "reason": type(exc).__name__,
            })
        else:
            status = handoffs.terminal_handoff_status(
                response,
                workflow_claim_id=claim["workflow_claim_id"],
            )
            finished = handoffs.finish_handoff(
                claim["occurrence_id"],
                claim["lease_token"],
                status=status,
                workflow_claim_id=claim["workflow_claim_id"],
            )
            if not finished:
                persisted = handoffs.get_handoff(claim["occurrence_id"])
                if persisted is not None and persisted["status"] == "CANCELLED":
                    results.append({
                        "kind": "cancelled",
                        "occurrence_id": claim["occurrence_id"],
                        "status": response.get("status"),
                    })
                    continue
            results.append({
                "kind": "dispatched",
                "occurrence_id": claim["occurrence_id"],
                "workflow_claim_id": claim["workflow_claim_id"],
                "status": response.get("status"),
            })
    return results


def poll_interval_from_environment() -> float:
    raw_value = os.getenv(
        POLL_INTERVAL_ENV, str(DEFAULT_POLL_INTERVAL_SECONDS)
    )
    try:
        interval = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{POLL_INTERVAL_ENV} must be a number") from exc
    if not math.isfinite(interval) or interval < MIN_POLL_INTERVAL_SECONDS:
        raise ValueError(
            f"{POLL_INTERVAL_ENV} must be at least "
            f"{MIN_POLL_INTERVAL_SECONDS:g} seconds"
        )
    return interval


def monitoring_enabled_from_environment() -> bool:
    value = os.getenv(ENABLED_ENV, "true").strip().lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{ENABLED_ENV} must be true or false")
    return value == "true"


def _lock_path() -> Path:
    import hashlib

    project_identity = str(Path(__file__).resolve().parents[2]).encode()
    suffix = hashlib.sha256(project_identity).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"kybernaut-monitor-{suffix}.lock"


class ApplicationAnomalyMonitor:
    """Poll the existing service anomaly evaluator and incident persistence."""

    def __init__(
        self,
        interval_seconds: float,
        *,
        services: tuple[str, ...] = EXPECTED_SERVICES,
        lock_path: Path | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not math.isfinite(interval_seconds) or interval_seconds <= 0:
            raise ValueError("interval_seconds must be finite and positive")
        if not services or any(service not in EXPECTED_SERVICES for service in services):
            raise ValueError("services must be supported application services")
        self.interval_seconds = interval_seconds
        self.services = services
        self.lock_path = lock_path or _lock_path()
        self._clock = clock
        self._service_locks = {
            service: asyncio.Lock() for service in services
        }
        self._task: asyncio.Task | None = None
        self._lock_file: Any = None
        self._lifecycle_state = "not_started"
        self._leadership_state = "unknown"
        self._poll_in_progress = False
        self._current_poll_outcomes: dict[str, str] = {}
        self._last_poll_attempt_at: str | None = None
        self._last_completed_poll_at: str | None = None
        self._last_successful_poll_at: str | None = None
        self._last_successful_workflow_at: str | None = None
        self._last_outcome = "unknown"
        self._service_status = {
            service: {
                "outcome": "unknown",
                "poll_activity": "idle",
                "last_poll_attempt_at": None,
                "last_completed_at": None,
                "last_successful_poll_at": None,
                "last_successful_workflow_at": None,
                "components": {
                    name: _empty_component() for name in MONITOR_COMPONENTS
                },
            }
            for service in services
        }

    def _timestamp(self) -> str:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("monitor clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc).isoformat()

    def _component_snapshots(
        self,
        components: dict[str, dict[str, Any]],
        now: datetime,
    ) -> dict[str, dict[str, Any]]:
        snapshots = {}
        for name, component in components.items():
            component_stale = self._is_stale(
                component["last_completed_at"], now
            )
            snapshots[name] = {
                **component,
                "status_stale": component_stale,
                "freshness": self._freshness(component_stale),
            }
        return snapshots

    def status(self) -> dict[str, Any]:
        """Return a bounded operational snapshot without triggering work."""
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("monitor clock must return a timezone-aware datetime")
        now = now.astimezone(timezone.utc)
        overall_stale = self._is_stale(self._last_completed_poll_at, now)
        service_status = {}
        for service, values in sorted(self._service_status.items()):
            component_snapshots = self._component_snapshots(
                values["components"], now
            )
            service_status[service] = {
                **values,
                "components": component_snapshots,
                "status_stale": self._is_stale(
                    values["last_completed_at"], now
                ),
                "workflow_health": _workflow_health(
                    self._lifecycle_state, component_snapshots
                ),
            }
        components = {
            name: {
                "status": _aggregate_component(service_status, name),
                "services": {
                    service: values["components"][name]
                    for service, values in service_status.items()
                },
                "status_stale": _aggregate_freshness(service_status, name),
                "freshness": self._freshness(
                    _aggregate_freshness(service_status, name)
                ),
            }
            for name in MONITOR_COMPONENTS
        }
        workflow_components = {
            f"{service}:{name}": component
            for service, values in service_status.items()
            for name, component in values["components"].items()
        }

        if self._lifecycle_state == "disabled":
            health = "disabled"
        elif self._lifecycle_state != "running":
            health = "unknown"
        elif self._poll_in_progress:
            previous_cycle_unhealthy = (
                self._last_completed_poll_at is not None
                and (
                    overall_stale is True
                    or self._last_outcome != "success"
                )
            )
            has_stale_service = any(
                service["status_stale"] is True
                for service in service_status.values()
            )
            has_current_poll_failure = any(
                outcome != "evaluated"
                for outcome in self._current_poll_outcomes.values()
            )
            health = (
                "degraded"
                if (
                    previous_cycle_unhealthy
                    or has_stale_service
                    or has_current_poll_failure
                )
                else "unknown"
            )
        elif self._last_completed_poll_at is None:
            health = "unknown"
        elif (
            self._last_outcome == "success"
            and overall_stale is False
            and all(
                service["outcome"] == "evaluated"
                and service["status_stale"] is False
                for service in service_status.values()
            )
        ):
            health = "healthy"
        else:
            health = "degraded"

        return {
            "configured_enabled": True,
            "lifecycle_state": self._lifecycle_state,
            "leadership_state": self._leadership_state,
            "is_leader": self._leadership_state == "leader",
            "poll_activity": (
                "in_progress" if self._poll_in_progress else "idle"
            ),
            "health": health,
            "freshness": self._freshness(overall_stale),
            "status_stale": overall_stale,
            "last_poll_attempt_at": self._last_poll_attempt_at,
            "last_completed_poll_at": self._last_completed_poll_at,
            "last_successful_poll_at": self._last_successful_poll_at,
            "last_successful_workflow_at": (
                self._last_successful_workflow_at
            ),
            "last_outcome": self._last_outcome,
            "workflow_health": _workflow_health(
                self._lifecycle_state, workflow_components
            ),
            "components": components,
            "services": service_status,
        }

    def _is_stale(self, timestamp: str | None, now: datetime) -> bool | None:
        if timestamp is None:
            return None
        completed_at = datetime.fromisoformat(timestamp)
        age_seconds = (now - completed_at).total_seconds()
        if age_seconds < 0:
            return None
        return age_seconds > self.interval_seconds * 2

    @staticmethod
    def _freshness(stale: bool | None) -> str:
        if stale is None:
            return "unknown"
        return "stale" if stale else "fresh"

    @staticmethod
    def _handoff_dispatch_outcome(
        results: Any,
        persistence: Any,
    ) -> tuple[str, str | None]:
        if not isinstance(results, list) or not isinstance(persistence, dict):
            return "unknown", "dispatch_outcome_invalid"

        kinds = {
            item.get("kind")
            for item in results
            if isinstance(item, dict)
        }
        if "retry" in kinds:
            return "retrying", "workflow_not_started"
        if "reconciliation" in kinds:
            return "pending", "workflow_reconciliation_required"
        if "dispatched" in kinds:
            if any(
                isinstance(item, dict)
                and item.get("status") == "awaiting_approval"
                for item in results
            ):
                return "pending", "awaiting_approval"
            return "success", None
        if "cancelled" in kinds or "reconciled" in kinds:
            return "success", None
        if kinds:
            return "unknown", "dispatch_outcome_unrecognized"

        occurrences = persistence.get("occurrences")
        if not isinstance(occurrences, list) or not occurrences:
            return "not_applicable", None

        observed_statuses = []
        for occurrence in occurrences:
            if not isinstance(occurrence, dict):
                return "unknown", "handoff_identity_unavailable"
            occurrence_id = occurrence.get("incident_row_id")
            if not isinstance(occurrence_id, int):
                return "unknown", "handoff_identity_unavailable"
            handoff = handoffs.get_handoff(occurrence_id)
            if not isinstance(handoff, dict):
                return "unknown", "handoff_state_unavailable"
            observed_statuses.append(handoff.get("status"))

        if "RETRY" in observed_statuses:
            return "retrying", "handoff_retry_pending"
        if any(
            status in {
                "PENDING", "DISPATCHING", "AWAITING_APPROVAL",
                "RECONCILIATION_REQUIRED",
            }
            for status in observed_statuses
        ):
            return "pending", "handoff_work_incomplete"
        if all(
            status in {
                "COMPLETED", "NO_ACTION", "BLOCKED", "REJECTED",
                "EXPIRED", "SIMULATED", "SIMULATION_FAILED",
                "DRY_RUN_COMPLETE", "FAILED", "VERIFICATION_FAILED",
                "CANCELLED",
            }
            for status in observed_statuses
        ):
            return "success", None
        return "unknown", "handoff_state_unrecognized"

    async def start(self) -> None:
        """Start one monitor task for this application lifespan."""
        if self._task is not None and not self._task.done():
            return
        self._lifecycle_state = "running"
        self._task = asyncio.create_task(
            self._run(), name="application-anomaly-monitor"
        )

    async def stop(self) -> None:
        """Cancel polling and release the process-wide leadership lock."""
        task = self._task
        if task is None:
            self._release_lock()
            self._lifecycle_state = "stopped"
            self._leadership_state = "not_leader"
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None
            self._release_lock()
            self._lifecycle_state = "stopped"
            self._leadership_state = "not_leader"

    async def poll_once(self) -> list[dict[str, Any]]:
        """Evaluate each configured service once, sequentially."""
        attempted_at = self._timestamp()
        self._poll_in_progress = True
        self._current_poll_outcomes = {}
        self._last_poll_attempt_at = attempted_at
        results = []
        try:
            for service in self.services:
                result = await self.poll_service(service)
                results.append(result)
                self._current_poll_outcomes[service] = result["status"]
        except asyncio.CancelledError:
            raise
        else:
            outcomes = [result.get("status") for result in results]
            if outcomes and all(outcome == "evaluated" for outcome in outcomes):
                outcome = "success"
            elif outcomes and all(outcome == "error" for outcome in outcomes):
                outcome = "error"
            elif outcomes and all(outcome == "unavailable" for outcome in outcomes):
                outcome = "unavailable"
            else:
                outcome = "partial"
            completed_at = self._timestamp()
            self._last_outcome = outcome
            self._last_completed_poll_at = completed_at
            if outcome == "success":
                self._last_successful_poll_at = completed_at
            status = self.status()
            if status["workflow_health"] == "healthy":
                self._last_successful_workflow_at = completed_at
                for service, service_result in status["services"].items():
                    if service_result["workflow_health"] == "healthy":
                        self._service_status[service][
                            "last_successful_workflow_at"
                        ] = completed_at
            return results
        finally:
            self._poll_in_progress = False

    async def poll_service(self, service: str) -> dict[str, Any]:
        """Evaluate one service; skip rather than overlap an active poll."""
        lock = self._service_locks.get(service)
        if lock is None:
            raise ValueError("Unsupported application service")
        if lock.locked():
            return {"service": service, "status": "skipped", "reason": "poll_in_progress"}

        async with lock:
            service_status = self._service_status[service]
            service_status["poll_activity"] = "in_progress"
            service_status["last_poll_attempt_at"] = self._timestamp()
            for component in service_status["components"].values():
                component["status"] = "pending"
                component["reason"] = "poll_in_progress"
            stage = "telemetry"
            try:
                evaluation = await evaluate_service_anomaly(service)
                completed_at = self._timestamp()
                collection_status = evaluation.get("collection_status")
                if collection_status in {
                    "success", "failed", "pending", "retrying",
                    "unavailable", "not_applicable", "unknown",
                }:
                    _set_component(
                        service_status["components"],
                        "telemetry_collection",
                        collection_status,
                        reason=evaluation.get("collection_reason"),
                        completed_at=completed_at,
                    )
                else:
                    _set_component(
                        service_status["components"],
                        "telemetry_collection",
                        "unknown",
                        reason="collection_outcome_not_reported",
                        completed_at=completed_at,
                    )

                evaluation_status = evaluation.get("evaluation_status")
                if evaluation_status not in {
                    "success", "failed", "pending", "retrying",
                    "unavailable", "not_applicable", "unknown",
                }:
                    evaluation_status = (
                        "success"
                        if evaluation.get("status") == "evaluated"
                        else "unavailable"
                        if evaluation.get("status") == "unavailable"
                        else "unknown"
                    )
                _set_component(
                    service_status["components"],
                    "anomaly_evaluation",
                    evaluation_status,
                    reason=evaluation.get("evaluation_reason"),
                    completed_at=completed_at,
                )

                stage = "persistence"
                persistence = incident_workflow._persist_telemetry_incidents(
                    service, evaluation
                )
                completed_at = self._timestamp()
                persistence_status = persistence.get("status")
                persistence_reason = persistence.get("reason")
                if persistence_status == "persisted":
                    incident_status = "success"
                    enqueue_status = "success"
                elif persistence_status == "healthy":
                    incident_status = "success"
                    enqueue_status = "not_applicable"
                elif persistence_status == "no_supported_anomaly":
                    incident_status = "unavailable"
                    enqueue_status = "not_applicable"
                    persistence_reason = persistence_reason or (
                        "candidate_not_persisted"
                    )
                elif persistence_status == "skipped":
                    incident_status = (
                        "not_applicable"
                        if evaluation.get("status") != "evaluated"
                        else "unavailable"
                    )
                    enqueue_status = "not_applicable"
                else:
                    incident_status = "unknown"
                    enqueue_status = "unknown"
                _set_component(
                    service_status["components"],
                    "incident_persistence",
                    incident_status,
                    reason=persistence_reason,
                    completed_at=completed_at,
                )
                _set_component(
                    service_status["components"],
                    "handoff_enqueue",
                    enqueue_status,
                    reason=persistence_reason,
                    completed_at=completed_at,
                )

                stage = "dispatch"
                handoff_results = await dispatch_pending_handoffs(service)
                completed_at = self._timestamp()
                dispatch_status, dispatch_reason = (
                    self._handoff_dispatch_outcome(
                        handoff_results, persistence
                    )
                )
                _set_component(
                    service_status["components"],
                    "handoff_dispatch",
                    dispatch_status,
                    reason=dispatch_reason,
                    completed_at=completed_at,
                )
                reconciliation_count = getattr(
                    handoff_results, "reconciliation_count", None
                )
                if isinstance(reconciliation_count, int):
                    reconciliation_status = "success"
                    reconciliation_reason = (
                        f"expired_leases:{reconciliation_count}"
                    )
                else:
                    reconciliation_status = "unknown"
                    reconciliation_reason = (
                        "reconciliation_outcome_not_reported"
                    )
                if any(
                    isinstance(item, dict)
                    and item.get("kind") == "reconciliation"
                    for item in handoff_results
                ):
                    reconciliation_status = "pending"
                    reconciliation_reason = "workflow_reconciliation_required"
                _set_component(
                    service_status["components"],
                    "reconciliation",
                    reconciliation_status,
                    reason=reconciliation_reason,
                    completed_at=completed_at,
                )

                stage = "diagnosis"
                diagnoses = await process_active_incidents(service)
                completed_at = self._timestamp()
                (
                    diagnosis_status,
                    audit_status,
                    diagnosis_reason,
                    audit_reason,
                ) = (
                    _diagnosis_outcomes(diagnoses)
                )
                _set_component(
                    service_status["components"],
                    "diagnosis",
                    diagnosis_status,
                    reason=diagnosis_reason,
                    completed_at=completed_at,
                )
                _set_component(
                    service_status["components"],
                    "diagnosis_audit",
                    audit_status,
                    reason=audit_reason,
                    completed_at=completed_at,
                )
                status = (
                    "evaluated"
                    if evaluation.get("status") == "evaluated"
                    else "unavailable"
                )
                service_status["outcome"] = status
                service_status["last_completed_at"] = completed_at
                if status == "evaluated":
                    service_status["last_successful_poll_at"] = completed_at
                component_snapshots = self._component_snapshots(
                    service_status["components"],
                    datetime.fromisoformat(completed_at),
                )
                service_status["workflow_health"] = _workflow_health(
                    self._lifecycle_state, component_snapshots
                )
                logger.info(
                    "Application anomaly poll service=%s status=%s "
                    "incident_status=%s incident_count=%d "
                    "workflow_health=%s",
                    service,
                    status,
                    persistence.get("status", "unknown"),
                    len(persistence.get("incident_ids", [])),
                    service_status["workflow_health"],
                )
                return {
                    "service": service,
                    "status": status,
                    "persistence": persistence,
                    "handoffs": handoff_results,
                    "diagnoses": diagnoses,
                    "components": component_snapshots,
                    "workflow_health": service_status["workflow_health"],
                }
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                completed_at = self._timestamp()
                stage_component = {
                    "telemetry": "anomaly_evaluation",
                    "persistence": "incident_persistence",
                    "dispatch": "handoff_dispatch",
                    "diagnosis": "diagnosis",
                }[stage]
                if stage == "telemetry":
                    _set_component(
                        service_status["components"],
                        "telemetry_collection",
                        "unknown",
                        reason="collection_outcome_not_reported",
                        completed_at=completed_at,
                    )
                _set_component(
                    service_status["components"],
                    stage_component,
                    "failed",
                    reason=type(exc).__name__,
                    completed_at=completed_at,
                )
                if stage == "persistence":
                    _set_component(
                        service_status["components"],
                        "handoff_enqueue",
                        "unknown",
                        reason="persistence_outcome_incomplete",
                        completed_at=completed_at,
                    )
                if stage == "dispatch":
                    reconciliation_failed = (
                        getattr(exc, "_kybernaut_monitor_stage", None)
                        == "reconciliation"
                    )
                    _set_component(
                        service_status["components"],
                        "reconciliation",
                        "failed" if reconciliation_failed else "unknown",
                        reason=(
                            type(exc).__name__
                            if reconciliation_failed
                            else "reconciliation_outcome_not_reported"
                        ),
                        completed_at=completed_at,
                    )
                if stage == "diagnosis":
                    _set_component(
                        service_status["components"],
                        "diagnosis_audit",
                        "unknown",
                        reason="diagnosis_audit_outcome_not_reported",
                        completed_at=completed_at,
                    )
                later_components = {
                    "telemetry": (
                        "incident_persistence", "handoff_enqueue",
                        "handoff_dispatch", "reconciliation", "diagnosis",
                        "diagnosis_audit",
                    ),
                    "persistence": (
                        "handoff_dispatch", "reconciliation", "diagnosis",
                        "diagnosis_audit",
                    ),
                    "dispatch": ("diagnosis", "diagnosis_audit"),
                    "diagnosis": (),
                }[stage]
                for name in later_components:
                    _set_component(
                        service_status["components"],
                        name,
                        "not_applicable",
                        reason="prior_stage_failed",
                        completed_at=completed_at,
                    )
                service_status["outcome"] = "error"
                service_status["last_completed_at"] = completed_at
                component_snapshots = self._component_snapshots(
                    service_status["components"],
                    datetime.fromisoformat(completed_at),
                )
                service_status["workflow_health"] = _workflow_health(
                    self._lifecycle_state, component_snapshots
                )
                logger.warning(
                    "Application anomaly poll failed service=%s error=%s",
                    service,
                    type(exc).__name__,
                )
                return {
                    "service": service,
                    "status": "error",
                    "reason": type(exc).__name__,
                    "components": component_snapshots,
                    "workflow_health": service_status["workflow_health"],
                }
            finally:
                service_status["poll_activity"] = "idle"

    async def _run(self) -> None:
        while True:
            if not self._acquire_lock():
                await asyncio.sleep(self.interval_seconds)
                continue

            logger.info("Application anomaly monitor leadership acquired")
            try:
                while True:
                    await self.poll_once()
                    await asyncio.sleep(self.interval_seconds)
            finally:
                self._release_lock()
                self._lifecycle_state = "stopped"

    def _acquire_lock(self) -> bool:
        if self._lock_file is not None:
            return True
        if not _PROCESS_LEADER_LOCK.acquire(blocking=False):
            self._leadership_state = "follower"
            return False
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = self.lock_path.open("a+")
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock_file.close()
                _PROCESS_LEADER_LOCK.release()
                self._leadership_state = "follower"
                return False
            except OSError:
                lock_file.close()
                raise
        except OSError as exc:
            _PROCESS_LEADER_LOCK.release()
            self._leadership_state = "lock_unavailable"
            logger.warning(
                "Application anomaly monitor lock unavailable error=%s",
                type(exc).__name__,
            )
            return False
        self._lock_file = lock_file
        self._leadership_state = "leader"
        return True

    def _release_lock(self) -> None:
        lock_file = self._lock_file
        self._lock_file = None
        if lock_file is None:
            return
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()
            _PROCESS_LEADER_LOCK.release()
            self._leadership_state = "not_leader"


def get_application_anomaly_monitor_status(app: Any) -> dict[str, Any]:
    """Read the current monitor snapshot without starting or polling it."""
    if not hasattr(app.state, "anomaly_monitor_enabled"):
        return {
            "configured_enabled": None,
            "lifecycle_state": "not_started",
            "leadership_state": "unknown",
            "is_leader": None,
            "poll_activity": "idle",
            "health": "unknown",
            "freshness": "unknown",
            "status_stale": None,
            "last_poll_attempt_at": None,
            "last_completed_poll_at": None,
            "last_successful_poll_at": None,
            "last_successful_workflow_at": None,
            "last_outcome": "unknown",
            "workflow_health": "unknown",
            "components": {
                name: {
                    "status": "unknown",
                    "status_stale": None,
                    "freshness": "unknown",
                    "services": {},
                }
                for name in MONITOR_COMPONENTS
            },
            "services": {},
        }

    monitor = getattr(app.state, "anomaly_monitor", None)
    if monitor is not None:
        return monitor.status()
    return dict(app.state.anomaly_monitor_status)


async def start_application_anomaly_monitor(app: Any) -> None:
    """Attach and start one validated monitor to a FastAPI lifespan."""
    if getattr(app.state, "anomaly_monitor", None) is not None:
        return
    enabled = monitoring_enabled_from_environment()
    app.state.anomaly_monitor_enabled = enabled
    if not enabled:
        logger.info("Application anomaly monitor disabled by configuration")
        app.state.anomaly_monitor = None
        app.state.anomaly_monitor_status = {
            "configured_enabled": False,
            "lifecycle_state": "disabled",
            "leadership_state": "not_applicable",
            "is_leader": False,
            "poll_activity": "idle",
            "health": "disabled",
            "freshness": "unknown",
            "status_stale": None,
            "last_poll_attempt_at": None,
            "last_completed_poll_at": None,
            "last_successful_poll_at": None,
            "last_successful_workflow_at": None,
            "last_outcome": "not_started",
            "workflow_health": "disabled",
            "components": {
                name: {
                    "status": "not_applicable",
                    "status_stale": None,
                    "freshness": "unknown",
                    "services": {},
                }
                for name in MONITOR_COMPONENTS
            },
            "services": {},
        }
        return
    monitor = ApplicationAnomalyMonitor(poll_interval_from_environment())
    app.state.anomaly_monitor = monitor
    await monitor.start()
    app.state.anomaly_monitor_status = monitor.status()


async def stop_application_anomaly_monitor(app: Any) -> None:
    monitor = getattr(app.state, "anomaly_monitor", None)
    if monitor is None:
        return
    try:
        await monitor.stop()
    finally:
        app.state.anomaly_monitor_status = monitor.status()
        app.state.anomaly_monitor = None
