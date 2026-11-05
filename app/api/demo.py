"""Authenticated session controls for the local simulated demo."""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from app.api.operations import _operator
from app.services.demo_incident_scheduler import (
    DemoConfigurationError,
    DemoIncidentScheduler,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/demo", tags=["local-demo"])


class DemoSessionRequest(BaseModel):
    session_id: UUID


def _scheduler(request: Request) -> DemoIncidentScheduler:
    scheduler = getattr(request.app.state, "demo_incident_scheduler", None)
    if not isinstance(scheduler, DemoIncidentScheduler):
        raise HTTPException(
            status_code=503,
            detail="The demo scheduler is unavailable.",
        )
    return scheduler


@router.get("/status")
async def demo_status(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _operator(authorization, "read")
    return await _scheduler(request).status()


@router.post("/session")
async def enter_demo_session(
    body: DemoSessionRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    operator = _operator(authorization, "read")
    try:
        return await _scheduler(request).enter_session(
            str(body.session_id), operator.operator_id
        )
    except DemoConfigurationError as exc:
        logger.error("Local demo scheduler configuration is invalid")
        raise HTTPException(
            status_code=503,
            detail="The local demo scheduler is misconfigured.",
        ) from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=403,
            detail="The demo session is not available to this operator.",
        ) from exc


@router.put("/session")
async def heartbeat_demo_session(
    body: DemoSessionRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    operator = _operator(authorization, "read")
    updated = await _scheduler(request).heartbeat(
        str(body.session_id), operator.operator_id
    )
    if not updated:
        raise HTTPException(
            status_code=404,
            detail="The demo session has expired.",
        )
    return {"status": "active", "simulation": "demo"}


@router.delete("/session")
async def leave_demo_session(
    body: DemoSessionRequest,
    request: Request,
    authorization: str | None = Header(default=None),
):
    operator = _operator(authorization, "read")
    await _scheduler(request).leave_session(
        str(body.session_id), operator.operator_id
    )
    return {"status": "stopped", "simulation": "demo"}
