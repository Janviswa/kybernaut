"""Post-remediation verification using fresh telemetry.

A remediation command returning success is NOT proof of recovery. This
module re-observes the cluster and only reports ``recovered`` when the
affected workload is healthy on consecutive observations. An unavailable
observation is a failed check, never evidence of health.
"""

from __future__ import annotations

import asyncio
import inspect
import math
from datetime import datetime, timezone
from typing import Any, Callable


async def _call(observe: Callable[[], Any]) -> dict:
    if inspect.iscoroutinefunction(observe):
        return await observe()
    return await asyncio.to_thread(observe)


def _evaluate(
    observation: dict,
    deployment: str,
    target_resource: str | None,
    target_incident_type: str | None,
) -> list[dict]:
    checks: list[dict] = []

    incidents = observation.get("incidents")
    incidents_available = (
        isinstance(incidents, list)
        and all(
            isinstance(item, dict)
            and isinstance(item.get("resource"), str)
            and bool(item["resource"].strip())
            and isinstance(item.get("type"), str)
            and bool(item["type"].strip())
            for item in incidents
        )
    )
    if incidents_available:
        checks.append({
            "name": "incident_evidence_available",
            "passed": True,
            "detail": "incident evidence is available",
        })
    else:
        checks.append({
            "name": "incident_evidence_available",
            "passed": False,
            "detail": "incident evidence is unavailable or malformed",
        })
        incidents = []
    if target_resource is not None and target_incident_type is not None:
        related = [
            item for item in incidents
            if item.get("resource") == target_resource
            and item.get("type") == target_incident_type
        ]
    else:
        related = [
            item for item in incidents
            if isinstance(item, dict)
            and isinstance(item.get("resource"), str)
            and (
                item["resource"] == deployment
                or item["resource"].startswith(deployment + "-")
            )
        ]
    if not related:
        incident_detail = (
            "no active incidents for target"
            if target_resource is not None and target_incident_type is not None
            else "no active incidents"
        )
    else:
        incident_detail = (
            f"{len(related)} active incident(s): "
            + ", ".join(sorted({i.get("type", "?") for i in related}))
        )
    checks.append({
        "name": "no_active_incident_for_workload",
        "passed": not related,
        "detail": incident_detail,
    })

    summaries = observation.get("deployments")
    matching_summaries = (
        [
            item for item in summaries
            if isinstance(item, dict) and item.get("name") == deployment
        ]
        if isinstance(summaries, list) else []
    )

    if not matching_summaries:
        checks.append({
            "name": "deployment_available",
            "passed": False,
            "detail": "deployment not present in observation",
        })
    elif len(matching_summaries) > 1:
        checks.append({
            "name": "deployment_available",
            "passed": False,
            "detail": "multiple deployment summaries match the workload",
        })
    else:
        summary = matching_summaries[0]
        desired = summary.get("desired_replicas")
        available = summary.get("available_replicas")
        ok = (
            isinstance(desired, int) and not isinstance(desired, bool)
            and isinstance(available, int) and not isinstance(available, bool)
            and desired >= 1 and available >= desired
        )
        checks.append({
            "name": "deployment_available",
            "passed": ok,
            "detail": f"available={available} desired={desired}",
        })
    return checks


async def verify_recovery(
    *,
    deployment: str,
    observe: Callable[[], Any],
    target_resource: str | None = None,
    target_incident_type: str | None = None,
    attempts: int = 5,
    interval_seconds: float = 3.0,
    consecutive_required: int = 2,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> dict:
    """Poll fresh observations; require N consecutive healthy results."""
    if (
        isinstance(attempts, bool)
        or not isinstance(attempts, int)
        or attempts < 1
    ):
        raise ValueError("attempts must be a positive integer")
    if (
        isinstance(consecutive_required, bool)
        or not isinstance(consecutive_required, int)
        or not 1 <= consecutive_required <= attempts
    ):
        raise ValueError(
            "consecutive_required must be between 1 and attempts"
        )
    if (
        isinstance(interval_seconds, bool)
        or not isinstance(interval_seconds, (int, float))
        or not math.isfinite(interval_seconds)
        or interval_seconds < 0
    ):
        raise ValueError("interval_seconds must be finite and non-negative")
    if (target_resource is None) != (target_incident_type is None):
        raise ValueError(
            "target_resource and target_incident_type must be provided together"
        )
    if target_resource is not None and (
        not target_resource.strip()
        or not target_incident_type
        or not target_incident_type.strip()
    ):
        raise ValueError("verification target identity must be non-empty")

    consecutive = 0
    checks: list[dict] = []
    used = 0

    for used in range(1, attempts + 1):
        try:
            observation = await _call(observe)
            if not isinstance(observation, dict):
                raise TypeError("observation is not a mapping")
            checks = _evaluate(
                observation, deployment, target_resource, target_incident_type
            )
        except Exception as exc:  # noqa: BLE001
            checks = [{
                "name": "observation_available",
                "passed": False,
                "detail": f"observation failed: {type(exc).__name__}",
            }]

        if all(c["passed"] for c in checks):
            consecutive += 1
            if consecutive >= consecutive_required:
                break
        else:
            consecutive = 0

        if used < attempts:
            await sleep(interval_seconds)

    recovered = consecutive >= consecutive_required
    return {
        "recovered": recovered,
        "attempts_used": used,
        "consecutive_healthy": consecutive,
        "checks": checks,
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "reason": "recovered" if recovered else "not_recovered_within_budget",
    }
