"""Opt-in two-incident batches (one critical, one high) every 10 minutes across four application services."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from app.services import audit_repository
from app.services.incident_repository import (
    list_incidents,
    record_incident,
    resolve_incident_fingerprint,
)

ENABLED_ENV = "KYBERNAUT_SERVICE_SIMULATION_ENABLED"
ENVIRONMENT_ENV = "KYBERNAUT_ENVIRONMENT"
TOKEN_ENV = "KYBERNAUT_FAILURE_SIMULATION_TOKEN"
BATCH_INTERVAL_SECONDS = 600
FAILURE_DURATION_SECONDS = 600
CONTROL_PATH = "/__kybernaut/simulation"

SERVICE_SCENARIOS: tuple[dict[str, Any], ...] = tuple(
    {
        "service": service,
        "port": port,
        "scenario": scenario,
        "incident_type": scenario,
        "description": description.format(service=label),
        "evidence": evidence.format(service=label),
    }
    for service, port, label in (
        ("order-service", 8001, "Order"),
        ("payment-service", 8002, "Payment"),
        ("user-service", 8003, "User"),
        ("inventory-service", 8004, "Inventory"),
    )
    for scenario, description, evidence in (
        (
            "high_latency",
            "{service} requests are delayed by two seconds.",
            "The {service} service added a bounded two-second delay to requests.",
        ),
        (
            "http_5xx",
            "{service} business requests return HTTP 503.",
            "The {service} service returned HTTP 503 for business requests.",
        ),
        (
            "service_unavailable",
            "{service} requests return HTTP 503.",
            "The {service} service returned HTTP 503 while its fault was active.",
        ),
        (
            "request_failure",
            "{service} business requests return HTTP 500.",
            "The {service} service returned HTTP 500 for business requests.",
        ),
    )
)

SERVICES = ("order-service", "payment-service", "user-service", "inventory-service")
SCENARIO_NAMES = ("high_latency", "http_5xx", "service_unavailable", "request_failure")

CYCLE_SERVICE_PAIRS: tuple[tuple[str, str], ...] = (
    ("order-service", "payment-service"),
    ("user-service", "inventory-service"),
    ("payment-service", "user-service"),
    ("inventory-service", "order-service"),
    ("user-service", "payment-service"),
    ("order-service", "inventory-service"),
    ("payment-service", "order-service"),
    ("inventory-service", "user-service"),
    ("order-service", "user-service"),
    ("payment-service", "inventory-service"),
    ("user-service", "order-service"),
    ("inventory-service", "payment-service"),
)

logger = logging.getLogger(__name__)


def enabled() -> bool:
    setting = os.getenv(ENABLED_ENV, "true").strip().lower()
    return (
        setting in {"1", "true", "yes"}
        and os.getenv(ENVIRONMENT_ENV, "").strip().lower() == "local-demo"
        and bool(os.getenv(TOKEN_ENV, "").strip())
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(slot: str, scenario: dict[str, Any]) -> str:
    return (
        f"local-service-simulation:{slot}:"
        f"{scenario['service']}:{scenario['scenario']}"
    )


def _incident_id(fingerprint: str) -> str:
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12]
    return f"inc-sim-{digest}"


def _scenario_for_cycle(
    service: str, cycle_number: int, last_scenario: str | None,
) -> dict[str, Any]:
    scenarios = [
        item for item in SERVICE_SCENARIOS if item["service"] == service
    ]
    if last_scenario in SCENARIO_NAMES:
        index = (SCENARIO_NAMES.index(last_scenario) + 1) % len(SCENARIO_NAMES)
    else:
        index = (
            SERVICES.index(service) + cycle_number - 1
        ) % len(SCENARIO_NAMES)
    return scenarios[index]


class LocalServiceIncidentScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._cycle_lock = asyncio.Lock()
        self._schedule_reset = asyncio.Event()
        persisted = list_incidents()
        self._cycle = max(
            (
                int(item["source_evidence"]["simulation_cycle"])
                for item in persisted
                if isinstance(item.get("source_evidence"), dict)
                and isinstance(
                    item["source_evidence"].get("simulation_cycle"), int,
                )
            ),
            default=0,
        )
        self._last_scenario_by_service: dict[str, str] = {}
        for item in sorted(
            persisted,
            key=lambda row: (
                row.get("source_evidence", {}).get("simulation_cycle", 0)
                if isinstance(row.get("source_evidence"), dict) else 0
            ),
        ):
            evidence = item.get("source_evidence")
            service = item.get("service")
            scenario = (
                evidence.get("scenario")
                if isinstance(evidence, dict) else None
            )
            if (
                isinstance(service, str)
                and service in SERVICES
                and isinstance(scenario, str)
                and scenario in SCENARIO_NAMES
            ):
                self._last_scenario_by_service[service] = scenario
        active = [
            item for item in persisted
            if item.get("status") in {"OPEN", "REMEDIATING"}
            and item.get("fingerprint")
            and isinstance(item.get("source_evidence"), dict)
            and item["source_evidence"].get("simulation")
            == "local_service_failure"
        ]
        self._active_fingerprints = [
            str(item["fingerprint"]) for item in active
        ]
        self._active_scenarios = [
            self._scenario_for_incident(
                str(item["service"]),
                str(item["source_evidence"].get("scenario")),
            )
            for item in active
        ]
        self._batch_resolved = asyncio.Event()
        if not self._active_fingerprints:
            self._batch_resolved.set()
        self.state: dict[str, Any] = {
            "enabled": enabled(),
            "running": False,
            "cycle_in_progress": False,
            "interval_seconds": BATCH_INTERVAL_SECONDS,
            "interval_anchor": "fixed_interval",
            "active_services": [],
            "last_run_at": None,
            "last_error": None,
            "cycles": 0,
        }
        self.state["active_services"] = [
            item["service"] for item in self._active_scenarios
        ]

    async def start(self) -> None:
        self.state["enabled"] = enabled()
        if not self.state["enabled"] or self._task is not None:
            return
        self.state["running"] = True
        self._task = asyncio.create_task(
            self._loop(), name="local-service-incident-gen"
        )

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        self.state["running"] = False
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._active_fingerprints or self._active_scenarios:
            try:
                async with self._cycle_lock:
                    await self._clear_batch()
            except Exception:
                logger.exception(
                    "Could not clear local service simulation during shutdown"
                )

    def _scenario_for_incident(
        self, service: str, scenario_name: str,
    ) -> dict[str, Any]:
        scenario = next(
            (
                item for item in SERVICE_SCENARIOS
                if item["service"] == service
                and item["scenario"] == scenario_name
            ),
            None,
        )
        if scenario is None:
            raise ValueError("Incident does not match a supported local simulation")
        return scenario

    async def _control(
        self, scenario: dict[str, Any], *, action: str,
    ) -> dict[str, Any]:
        token = os.getenv(TOKEN_ENV, "").strip()
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                f"http://127.0.0.1:{scenario['port']}{CONTROL_PATH}",
                headers={"X-Kybernaut-Simulation-Token": token},
                json={
                    "action": action,
                    "scenario": scenario["scenario"] if action == "start" else None,
                    "duration_seconds": FAILURE_DURATION_SECONDS,
                },
            )
        response.raise_for_status()
        payload = response.json()
        if (
            payload.get("service") != scenario["service"]
            or payload.get("active") is not (action == "start")
            or (
                action == "start"
                and payload.get("scenario") != scenario["scenario"]
            )
        ):
            raise RuntimeError(
                f"Service simulator did not confirm {action} for "
                f"{scenario['service']}"
            )
        return payload

    async def _clear_batch(self) -> None:
        active_scenarios = list(self._active_scenarios)
        outcomes = await asyncio.gather(
            *(
                self._control(item, action="clear")
                for item in active_scenarios
            ),
            return_exceptions=True,
        )
        failures = [
            f"{scenario['service']}:{type(outcome).__name__}"
            for scenario, outcome in zip(active_scenarios, outcomes)
            if isinstance(outcome, BaseException)
        ]
        if failures:
            raise RuntimeError(
                "Could not clear previous local service batch: "
                + ", ".join(failures)
            )

        for fingerprint in self._active_fingerprints:
            resolve_incident_fingerprint(
                fingerprint,
                resolution_reason="simulator_cycle_completed",
            )
        self._active_fingerprints.clear()
        self._active_scenarios.clear()
        self.state["active_services"] = []
        self._batch_resolved.set()

    def _persist_batch(
        self,
        slot: str,
        scenarios: list[dict[str, Any]],
        cycle_number: int,
    ) -> list[str]:
        fingerprints = []
        self._batch_resolved.clear()
        try:
            for index, scenario in enumerate(scenarios):
                fingerprint = _fingerprint(slot, scenario)
                severity = "critical" if index == 0 else "high"
                incident = {
                    "incident_id": _incident_id(fingerprint),
                    "service": scenario["service"],
                    "type": scenario["incident_type"],
                    "severity": severity,
                    "message": (
                        f"{scenario['service']} — {scenario['description']}"
                    ),
                    "fingerprint": fingerprint,
                    "source_timestamp": _now(),
                    "source_evidence": {
                        "source": "local_service_simulator",
                        "simulation": "local_service_failure",
                        "simulated": True,
                        "scenario": scenario["scenario"],
                        "service": scenario["service"],
                        "severity": severity,
                        "simulation_cycle": cycle_number,
                        "description": scenario["description"],
                        "evidence": [scenario["evidence"]],
                        "remediation_scope": scenario["service"],
                    },
                }
                record_incident(incident)
                fingerprints.append(fingerprint)
                self._active_fingerprints.append(fingerprint)
        except Exception:
            unresolved = []
            for fingerprint in fingerprints:
                try:
                    resolve_incident_fingerprint(
                        fingerprint,
                        resolution_reason="simulator_batch_start_failed",
                    )
                except Exception:
                    unresolved.append(fingerprint)
                    logger.exception(
                        "Could not resolve partially persisted local simulation incident"
                    )
            self._active_fingerprints = unresolved
            raise
        self._active_fingerprints = list(fingerprints)
        return fingerprints

    async def clear_incident(
        self, *, incident_id: str, service: str, scenario_name: str,
        fingerprint: str,
    ) -> dict[str, Any]:
        async with self._cycle_lock:
            scenario = self._scenario_for_incident(service, scenario_name)
            if (
                fingerprint not in self._active_fingerprints
                or not fingerprint.startswith("local-service-simulation:")
                or not fingerprint.endswith(f":{service}:{scenario_name}")
            ):
                raise ValueError(
                    "Incident is not part of the active simulation batch"
                )

            result = await self._control(scenario, action="clear")
            resolved = resolve_incident_fingerprint(
                fingerprint,
                resolution_reason="local_simulation_cleared",
            )
            if resolved != 1:
                raise RuntimeError(
                    f"Cleared {service} but could not resolve its active incident"
                )
            self._active_fingerprints.remove(fingerprint)
            self._active_scenarios = [
                item for item in self._active_scenarios
                if not (
                    item["service"] == service
                    and item["scenario"] == scenario_name
                )
            ]
            self.state["active_services"] = [
                item for item in self.state["active_services"]
                if item != service
            ]
            if not self._active_fingerprints:
                self._batch_resolved.set()
            return {
                "incident_id": incident_id,
                "service": service,
                "active": result["active"],
                "resolved": True,
                "resolution_reason": "local_simulation_cleared",
            }

    def _resolve_orphaned_batches(self) -> None:
        """Close generated incidents left open by an earlier batch or a restart,
        so only the newest pair is ever open."""
        for item in list_incidents(status="OPEN"):
            fingerprint = str(item.get("fingerprint") or "")
            if fingerprint.startswith("local-service-simulation:"):
                resolve_incident_fingerprint(
                    fingerprint,
                    resolution_reason="simulator_cycle_completed",
                )

    async def run_cycle(self, *, manual: bool = False) -> dict[str, Any]:
        async with self._cycle_lock:
            result = await self._run_cycle()
            if manual:
                self._schedule_reset.set()
            return result

    async def _run_cycle(self) -> dict[str, Any]:
        self.state.update(cycle_in_progress=True, last_error=None)
        try:
            if self._active_fingerprints or self._active_scenarios:
                await self._clear_batch()

            self._resolve_orphaned_batches()

            cycle_number = self._cycle + 1
            services = CYCLE_SERVICE_PAIRS[
                (cycle_number - 1) % len(CYCLE_SERVICE_PAIRS)
            ]
            scenarios = [
                _scenario_for_cycle(
                    service,
                    cycle_number,
                    self._last_scenario_by_service.get(service),
                )
                for service in services
            ]
            self._active_scenarios = scenarios
            outcomes = await asyncio.gather(
                *(
                    self._control(item, action="start")
                    for item in scenarios
                ),
                return_exceptions=True,
            )
            for scenario, outcome in zip(scenarios, outcomes):
                if isinstance(outcome, BaseException):
                    raise RuntimeError(
                        f"Could not start {scenario['service']} "
                        f"simulation ({type(outcome).__name__})"
                    ) from outcome

            slot = _now()
            fingerprints = self._persist_batch(slot, scenarios, cycle_number)
            self._active_fingerprints = fingerprints
            self._cycle = cycle_number
            for scenario in scenarios:
                self._last_scenario_by_service[
                    scenario["service"]
                ] = scenario["scenario"]
            self.state.update(
                active_services=[item["service"] for item in scenarios],
                last_run_at=slot,
                cycles=self.state["cycles"] + 1,
            )
            try:
                audit_repository.record_event(
                    incident_ref="local-service-simulator",
                    event_type="local_service_simulation_started",
                    actor="kybernaut-local-service-scheduler",
                    summary="Two-incident local failure batch started",
                    resource=",".join(item["service"] for item in scenarios),
                    outcome="started",
                    details={
                        "cycle": cycle_number,
                        "services": [
                            {
                                "service": item["service"],
                                "scenario": item["scenario"],
                                "incident_type": item["incident_type"],
                                "severity": (
                                    "critical" if index == 0 else "high"
                                ),
                            }
                            for index, item in enumerate(scenarios)
                        ],
                        "incidents_created": len(fingerprints),
                        "duration_seconds": FAILURE_DURATION_SECONDS,
                    },
                )
            except Exception:
                logger.exception("Could not audit local service simulation")
            return {
                "services": [item["service"] for item in scenarios],
                "incidents_created": len(fingerprints),
                "cycle": cycle_number,
                "duration_seconds": FAILURE_DURATION_SECONDS,
                "at": slot,
            }
        except Exception as exc:
            cleanup_failures = []
            try:
                await self._clear_batch()
            except Exception as cleanup_error:
                cleanup_failures.append(type(cleanup_error).__name__)
                logger.exception("Could not roll back failed local service batch")
            message = type(exc).__name__
            if cleanup_failures:
                message += "; cleanup failed for " + ", ".join(cleanup_failures)
            self.state["last_error"] = message
            raise
        finally:
            self.state["cycle_in_progress"] = False

    async def _loop(self) -> None:
        """Start a fresh pair every BATCH_INTERVAL_SECONDS. The timer does not wait
        for the previous pair to be resolved; a manual run restarts the timer."""
        while True:
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Local service simulation cycle failed")
            while True:
                self._schedule_reset.clear()
                try:
                    await asyncio.wait_for(
                        self._schedule_reset.wait(),
                        timeout=BATCH_INTERVAL_SECONDS,
                    )
                except asyncio.TimeoutError:
                    break