"""Opt-in, self-expiring failure injection for local-demo target services."""

from __future__ import annotations

import asyncio
import hmac
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

MAX_FAILURE_SECONDS = 600
SIMULATION_PATH = "/__kybernaut/simulation"
MONITORING_PATHS = {"/metrics", SIMULATION_PATH}
SCENARIO_DESCRIPTIONS = {
    "high_latency": "{service} requests are delayed by two seconds.",
    "http_5xx": "{service} business requests return HTTP 503.",
    "service_unavailable": "{service} requests return HTTP 503.",
    "request_failure": "{service} business requests return HTTP 500.",
}


def scenarios_for_service(service: str) -> dict[str, str]:
    label = service.split("-", 1)[0].capitalize()
    return {
        name: description.format(service=label)
        for name, description in SCENARIO_DESCRIPTIONS.items()
    }


class FailureRequest(BaseModel):
    action: Literal["start", "clear"]
    scenario: str | None = None
    duration_seconds: int = Field(default=MAX_FAILURE_SECONDS, ge=1, le=MAX_FAILURE_SECONDS)


def install_local_failure_simulation(
    app: FastAPI,
    *,
    service: str,
    scenarios: dict[str, str],
) -> None:
    state: dict[str, str | float | None] = {
        "scenario": None,
        "expires_at": None,
        "expires_monotonic": None,
    }

    def enabled() -> bool:
        return (
            os.getenv("KYBERNAUT_ENVIRONMENT", "").strip().lower()
            == "local-demo"
            and bool(os.getenv("KYBERNAUT_FAILURE_SIMULATION_TOKEN", "").strip())
        )

    def current_scenario() -> str | None:
        expires = state.get("expires_monotonic")
        if not isinstance(expires, (int, float)) or time.monotonic() >= expires:
            state.update(scenario=None, expires_at=None, expires_monotonic=None)
            return None
        scenario = state.get("scenario")
        return scenario if isinstance(scenario, str) else None

    @app.middleware("http")
    async def apply_local_failure(request, call_next):
        if request.url.path in MONITORING_PATHS:
            return await call_next(request)

        scenario = current_scenario()
        if scenario is None:
            return await call_next(request)

        if scenario == "high_latency":
            if request.url.path != "/health":
                await asyncio.sleep(2.0)
            return await call_next(request)

        if scenario in {"http_5xx", "request_failure"} and request.url.path == "/health":
            return await call_next(request)

        if scenario == "service_unavailable" or scenario == "http_5xx":
            return JSONResponse(
                status_code=503,
                content={
                    "detail": f"{service} is unavailable during a local simulation",
                    "simulated": True,
                },
            )

        if scenario == "request_failure":
            return JSONResponse(
                status_code=500,
                content={
                    "detail": f"{service} request failure during a local simulation",
                    "simulated": True,
                },
            )

        return await call_next(request)

    router = APIRouter()

    @router.post(SIMULATION_PATH)
    async def control_failure(
        body: FailureRequest,
        x_kybernaut_simulation_token: str | None = Header(default=None),
    ):
        if not enabled():
            raise HTTPException(status_code=404, detail="Not found")
        expected = os.getenv("KYBERNAUT_FAILURE_SIMULATION_TOKEN", "")
        if not x_kybernaut_simulation_token or not hmac.compare_digest(
            x_kybernaut_simulation_token, expected
        ):
            raise HTTPException(status_code=403, detail="Simulation control denied")

        if body.action == "clear":
            state.update(scenario=None, expires_at=None, expires_monotonic=None)
            return {"service": service, "active": False}

        if body.scenario not in scenarios:
            raise HTTPException(status_code=422, detail="Unsupported service scenario")

        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=body.duration_seconds)
        state.update(
            scenario=body.scenario,
            expires_at=expires_at.isoformat(),
            expires_monotonic=time.monotonic() + body.duration_seconds,
        )
        return {
            "service": service,
            "active": True,
            "scenario": body.scenario,
            "expires_at": expires_at.isoformat(),
            "description": scenarios[body.scenario],
        }

    @router.get(SIMULATION_PATH)
    async def failure_status():
        if not enabled():
            raise HTTPException(status_code=404, detail="Not found")
        scenario = current_scenario()
        return {
            "service": service,
            "active": scenario is not None,
            "scenario": scenario,
            "expires_at": state.get("expires_at"),
        }

    app.include_router(router)
