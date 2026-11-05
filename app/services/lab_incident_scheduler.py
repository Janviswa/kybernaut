"""Automatic lab incident generator.

Every KYBERNAUT_LAB_AUTOGEN_INTERVAL_SECONDS (default 600) this really injects
one failure scenario into the ``kybernaut-lab`` Kubernetes namespace, then runs
the normal Kubernetes observation + persistence path so an actual incident
record is created from observed cluster facts.

Safety:
* OFF unless KYBERNAUT_LAB_AUTOGEN=true.
* Only scenarios from ``failure_scenarios.SCENARIOS`` run, and
  ``run_scenario`` itself refuses any kubectl context other than the lab kind
  cluster and any namespace other than ``kybernaut-lab``.
* The previous scenario is cleaned up before the next one is injected.
* Nothing here executes remediation; the agent only proposes and a human
  approves through the existing approval workflow.

The ``expected_severity`` tag is what the scenario is *expected* to produce.
The persisted incident severity always comes from the observer/classifier.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from app.services.failure_scenarios import (
    SimulatorError,
    run_scenario,
    scenario_detected,
)
from app.services.k8s_incident_sync import persist_observation
from app.services.k8s_observer import observe_namespace

logger = logging.getLogger(__name__)

# (scenario, expected_severity, seconds to wait for the failure to surface)
ROTATION: tuple[tuple[str, str, int], ...] = (
    ("pod-crash", "high", 25),
    ("memory-leak", "medium", 120),
    ("disk-full", "high", 45),
    ("deployment-failure", "medium", 30),
)
MIN_INTERVAL_SECONDS = 600
SCENARIO_VERIFICATION_INTERVAL_SECONDS = 5
SCENARIO_VERIFICATION_TIMEOUT_SECONDS = 60


def autogen_enabled() -> bool:
    return os.getenv("KYBERNAUT_LAB_AUTOGEN", "").strip().lower() in {
        "1", "true", "yes",
    }


def interval_seconds() -> int:
    try:
        value = int(os.getenv("KYBERNAUT_LAB_AUTOGEN_INTERVAL_SECONDS", "600"))
    except ValueError:
        value = 600
    return max(MIN_INTERVAL_SECONDS, value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LabIncidentScheduler:
    def __init__(self, *, runner=None, audit=None):
        self._task: asyncio.Task | None = None
        self._runner = runner
        self._audit = audit
        self._index = 0
        self.active_scenario: str | None = None
        self.state: dict = {
            "enabled": autogen_enabled(),
            "interval_seconds": interval_seconds(),
            "running": False,
            "cycle_in_progress": False,
            "last_attempt_at": None,
            "last_run_at": None,
            "last_scenario": None,
            "last_completed_scenario": None,
            "last_expected_severity": None,
            "last_result": None,
            "last_error": None,
            "cycles": 0,
        }

    # ---- lifecycle -------------------------------------------------
    async def start(self) -> None:
        if not autogen_enabled() or self._task is not None:
            return
        self.state["running"] = True
        self._task = asyncio.create_task(self._loop(), name="lab-incident-gen")

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        self.state["running"] = False
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # ---- one cycle -------------------------------------------------
    def _record(self, event: str, message: str, details: dict) -> None:
        if self._audit is None:
            return
        try:
            self._audit(event, message, details)
        except Exception:  # noqa: BLE001 - auditing must not stop the loop
            logger.exception("Could not record lab scenario audit event")

    async def run_cycle(self) -> dict:
        name, severity, settle = ROTATION[self._index % len(ROTATION)]
        self._index += 1
        attempted_at = _now()
        self.state.update(
            cycle_in_progress=True,
            last_attempt_at=attempted_at,
            last_scenario=name,
            last_expected_severity=severity,
            last_error=None,
        )
        kwargs = {"dry_run": False}
        if self._runner is not None:
            kwargs["runner"] = self._runner

        failure_stage = "cleanup"
        try:
            # Clean up what the previous cycle left running before injecting.
            previous = self.active_scenario
            if previous:
                await asyncio.to_thread(
                    run_scenario, previous, "cleanup", **kwargs
                )
                self._record(
                    "lab_scenario_cleanup",
                    f"Lab scenario {previous} cleaned up before next cycle",
                    {"scenario": previous},
                )
                self.active_scenario = None

            if name != previous:
                await asyncio.to_thread(
                    run_scenario, name, "cleanup", **kwargs
                )
                self._record(
                    "lab_scenario_cleanup",
                    f"Lab scenario {name} reset before injection",
                    {"scenario": name, "reason": "prepare_fresh_injection"},
                )

            failure_stage = "injection"
            await asyncio.to_thread(run_scenario, name, "inject", **kwargs)
            self.active_scenario = name
            self._record(
                "lab_scenario_injected",
                f"Lab scenario {name} injected (expected severity {severity})",
                {"scenario": name, "expected_severity": severity},
            )
            failure_stage = "settling"
            await asyncio.sleep(settle)

            verification_attempts = (
                SCENARIO_VERIFICATION_TIMEOUT_SECONDS
                // SCENARIO_VERIFICATION_INTERVAL_SECONDS
                + 1
            )
            for attempt in range(verification_attempts):
                failure_stage = "observation"
                observation = await asyncio.to_thread(observe_namespace)
                observed_incidents = observation.get("incidents")
                failure_stage = "scenario_verification"
                if (
                    isinstance(observed_incidents, list)
                    and scenario_detected(name, observed_incidents)
                ):
                    break
                if attempt + 1 < verification_attempts:
                    await asyncio.sleep(
                        SCENARIO_VERIFICATION_INTERVAL_SECONDS
                    )
            else:
                raise RuntimeError(
                    f"Lab scenario {name} was not detected; "
                    "persistence was skipped"
                )
            failure_stage = "persistence"
            persistence = await asyncio.to_thread(
                persist_observation, observation
            )
            persisted_count = persistence.get("persisted_count")
            failure_stage = "persistence_verification"
            if persisted_count != len(observed_incidents):
                raise RuntimeError(
                    "Observed and persisted lab incident counts do not match"
                )
            result = {
                "scenario": name,
                "expected_severity": severity,
                "observed_incidents": len(observed_incidents),
                "persistence": persistence,
                "at": _now(),
            }
        except asyncio.CancelledError:
            self.state["cycle_in_progress"] = False
            raise
        except Exception as exc:
            message = str(exc).strip()
            if isinstance(exc, SimulatorError):
                error = f"scenario refused: {type(exc).__name__}"
            elif isinstance(exc, RuntimeError) and (
                message == (
                    f"Lab scenario {name} was not detected; "
                    "persistence was skipped"
                )
                or message == (
                    "Observed and persisted lab incident counts do not match"
                )
            ):
                error = message
            else:
                error = type(exc).__name__
            self.state["last_error"] = f"{failure_stage}: {error}"
            self._record(
                "lab_scenario_cycle_failed",
                f"Lab scenario {name} cycle failed",
                {
                    "scenario": name,
                    "attempted_at": attempted_at,
                    "failure_stage": failure_stage,
                    "error_type": type(exc).__name__,
                    "error": error,
                },
            )
            self.state["cycle_in_progress"] = False
            raise

        self.state.update(
            cycle_in_progress=False,
            last_run_at=result["at"],
            last_completed_scenario=name,
            last_result=result,
            last_error=None,
            cycles=self.state["cycles"] + 1,
        )
        self._record(
            "lab_scenario_cycle_completed",
            f"Lab scenario {name} observed and persisted",
            {
                "scenario": name,
                "cycle": self.state["cycles"],
                "attempted_at": attempted_at,
                "completed_at": result["at"],
                "observed_incidents": len(observed_incidents),
                "persisted_count": persisted_count,
            },
        )
        return result

    async def _loop(self) -> None:
        loop = asyncio.get_running_loop()
        next_cycle_at = loop.time()
        while True:
            delay = next_cycle_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("Lab incident cycle failed")
            next_cycle_at += interval_seconds()