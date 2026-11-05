"""Operations API: response workflow, approvals, audit, timeline, cost.

All routes require a valid operator JWT. Mutating routes additionally
require the ``remediation`` permission.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from app.core.jwt_auth import require_permission, verify_operator_token
from app.services import audit_repository as audit
from app.services import action_claim_repository as claims
from app.services import demo_results
from app.services.incident_response_graph import (
    SUPPORTED_SERVICES,
    WorkflowExecutionAmbiguous,
    WorkflowNotStarted,
    resume_after_decision,
    run_response_workflow,
)
from app.services import incident_repository
from app.services.k8s_incident_sync import build_fingerprint
from app.services.policy_engine import PolicyConfig

router = APIRouter(tags=["operations"])
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COST_REPORT_PATH = PROJECT_ROOT / "reports" / "cost" / "latest.json"
WEEKLY_COST_REPORT_DIR = Path(os.getenv(
    "KYBERNAUT_WEEKLY_COST_REPORT_DIR",
    str(PROJECT_ROOT / "reports" / "weekly-cost"),
))


def _operator(authorization: str | None, permission: str):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401, detail="Bearer operator token required",
            headers={"WWW-Authenticate": "Bearer"})
    token = authorization.split(None, 1)[1].strip()
    if not token:
        raise HTTPException(
            status_code=401, detail="Bearer operator token required",
            headers={"WWW-Authenticate": "Bearer"})
    operator = verify_operator_token(token)
    require_permission(operator, permission)
    return operator


class WorkflowRequest(BaseModel):
    incident_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$",
    )


class DecisionRequest(BaseModel):
    decision: Literal["approved", "rejected"]
    reason: str = Field(default="", max_length=500)


@router.post("/workflow/{service}/respond")
async def respond(
    service: str, body: WorkflowRequest | None = None,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key"
    ),
):
    """Run detect->classify->diagnose->plan->policy. Execution still needs
    approval unless auto-remediation is explicitly enabled."""
    operator = _operator(authorization, "remediation")
    if service not in SUPPORTED_SERVICES:
        raise HTTPException(status_code=400, detail="Unsupported service")
    if (
        not idempotency_key
        or len(idempotency_key) > 128
        or re.fullmatch(r"[A-Za-z0-9._:-]+", idempotency_key) is None
    ):
        raise HTTPException(
            status_code=400,
            detail="A valid Idempotency-Key header is required.",
        )
    correlation = body.incident_id if body else None
    incident_ref = correlation or f"INC-{uuid.uuid4().hex[:12]}"
    try:
        request_claim, created, request_conflict = claims.create_workflow_claim(
            operator_id=operator.operator_id,
            service=service,
            idempotency_key=idempotency_key,
            incident_ref=incident_ref,
            request_correlation=correlation,
        )
        if not created:
            if request_conflict:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency key was already used for another request.",
                )
            if request_claim["status"] == "COMPLETED":
                return json.loads(request_claim["response_json"])
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "A workflow with this idempotency key is still unresolved.",
                    "workflow_claim_id": request_claim["claim_id"],
                    "status": request_claim["status"].lower(),
                },
            )
        return await run_response_workflow(
            service,
            incident_id=request_claim["incident_ref"],
            requested_by=operator.operator_id,
            workflow_claim_id=request_claim["claim_id"],
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Incident response workflow failed (exception_type=%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Incident response workflow is temporarily unavailable.",
        ) from exc


@router.post("/incidents/{incident_id}/investigate")
async def investigate_kubernetes_incident(
    incident_id: str,
    preferred_action: str | None = None,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key"
    ),
):
    """Run read-only RCA/policy for a persisted lab occurrence.

    Any proposed action is forced through the existing human-approval graph.
    """
    operator = _operator(authorization, "read")
    if preferred_action is not None and preferred_action not in {
        "restart_pod", "scale_deployment", "rollback_deployment",
        "set_image", "restore_command", "cleanup_evicted_pod",
    }:
        raise HTTPException(
            status_code=400,
            detail="preferred_action must be restart_pod, scale_deployment, "
                   "rollback_deployment, set_image, restore_command or "
                   "cleanup_evicted_pod.",
        )
    if (
        not idempotency_key
        or len(idempotency_key) > 128
        or re.fullmatch(r"[A-Za-z0-9._:-]+", idempotency_key) is None
    ):
        raise HTTPException(
            status_code=400,
            detail="A valid Idempotency-Key header is required.",
        )

    occurrence = incident_repository.get_latest_occurrence_by_incident_id(
        incident_id
    )
    if occurrence is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    if occurrence.get("status") not in {"OPEN", "REMEDIATING"}:
        raise HTTPException(
            status_code=409, detail="Incident occurrence is no longer active"
        )
    try:
        source_evidence = json.loads(
            occurrence.get("source_evidence_json") or "null"
        )
    except (TypeError, json.JSONDecodeError):
        source_evidence = None
    evidence_identity_valid = (
        isinstance(source_evidence, dict)
        and isinstance(source_evidence.get("type"), str)
        and isinstance(source_evidence.get("resource"), str)
        and isinstance(source_evidence.get("resource_kind"), str)
    )
    expected_fingerprint = (
        build_fingerprint(source_evidence, "kybernaut-lab")
        if evidence_identity_valid else None
    )
    if (
        occurrence.get("service") != "kubernetes-lab"
        or not isinstance(source_evidence, dict)
        or source_evidence.get("source") != "kubernetes"
        or source_evidence.get("namespace") != "kybernaut-lab"
        or source_evidence.get("source_timestamp")
        != occurrence.get("source_timestamp")
        or not isinstance(occurrence.get("source_timestamp"), str)
        or occurrence.get("fingerprint") != expected_fingerprint
    ):
        raise HTTPException(
            status_code=409,
            detail="Incident lacks supported, timestamped Kubernetes evidence.",
        )
    resource = source_evidence.get("resource")
    resource_kind = source_evidence.get("resource_kind")
    allowed_deployments = PolicyConfig.from_env().allowed_deployments
    supported_targets = []
    for deployment in allowed_deployments:
        if resource_kind == "Deployment" and resource == deployment:
            supported_targets.append(deployment)
        elif (
            resource_kind == "Pod"
            and isinstance(resource, str)
            and resource.startswith(deployment + "-")
        ):
            supported_targets.append(deployment)
    if len(supported_targets) != 1:
        raise HTTPException(
            status_code=400,
            detail="Incident workload is outside the supported lab allowlist.",
        )

    try:
        request_claim, created, request_conflict = claims.create_workflow_claim(
            operator_id=operator.operator_id,
            service="kubernetes-lab",
            idempotency_key=idempotency_key,
            incident_ref=incident_id,
            request_correlation=incident_id,
            incident_occurrence_id=int(occurrence["id"]),
        )
        if not created:
            if request_conflict:
                raise HTTPException(
                    status_code=409,
                    detail="Idempotency key was already used for another incident.",
                )
            if request_claim["status"] == "COMPLETED":
                return json.loads(request_claim["response_json"])
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "An investigation for this incident is unresolved.",
                    "workflow_claim_id": request_claim["claim_id"],
                    "status": request_claim["status"].lower(),
                },
            )
        return await run_response_workflow(
            "kubernetes-lab",
            incident_id=incident_id,
            requested_by=operator.operator_id,
            workflow_claim_id=request_claim["claim_id"],
            occurrence_bound=True,
            force_approval=True,
            preferred_action=preferred_action,
        )
    except HTTPException:
        raise
    except WorkflowNotStarted as exc:
        if not claims.finish_workflow_claim(
            request_claim["claim_id"],
            {
                "incident_id": incident_id,
                "status": "blocked",
                "reason": str(exc),
            },
        ):
            logger.error(
                "Could not finish blocked investigation claim %s",
                request_claim["claim_id"],
            )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WorkflowExecutionAmbiguous as exc:
        raise HTTPException(
            status_code=503,
            detail="Investigation outcome is uncertain; check the incident timeline before retrying.",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Kubernetes incident investigation failed (exception_type=%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Kubernetes incident investigation is temporarily unavailable.",
        ) from exc


@router.get("/approvals")
def list_approvals(
    status: str | None = None, limit: int = 100,
    authorization: str | None = Header(default=None),
):
    _operator(authorization, "read")
    if status and status.upper() not in {
            "PENDING", "APPROVED", "REJECTED", "EXPIRED", "CANCELLED"}:
        raise HTTPException(status_code=400, detail="Invalid status")
    return {"approvals": audit.list_approvals(status, limit)}


@router.get("/approvals/{approval_id}")
def get_approval(approval_id: str,
                 authorization: str | None = Header(default=None)):
    _operator(authorization, "read")
    approval = audit.get_approval(approval_id)
    if approval is None:
        raise HTTPException(status_code=404, detail="Approval not found")
    return approval


@router.post("/approvals/{approval_id}/decision")
async def decide(
    approval_id: str, body: DecisionRequest,
    authorization: str | None = Header(default=None),
):
    operator = _operator(authorization, "remediation")
    try:
        return await resume_after_decision(
            approval_id, body.decision, operator.operator_id, body.reason)
    except audit.ApprovalError as exc:
        code = {"not_found": 404, "already_decided": 409,
                "same_operator": 403,
                "claim_mismatch": 409, "expired": 410}.get(exc.code, 400)
        raise HTTPException(status_code=code, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Approval decision processing failed (exception_type=%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Approval decision outcome is uncertain; check approval "
                   "and workflow status before retrying.") from exc


@router.get("/audit")
def get_audit(
    limit: int = Query(default=100, ge=1, le=1000),
    incident_id: str | None = None,
    source: Literal["all", "operational", "demo"] = "all",
    authorization: str | None = Header(default=None),
):
    _operator(authorization, "read")
    if source == "all":
        events = audit.list_events(
            incident_id, limit, newest_first=True
        )
        return {"events": events}

    demo_incident_ids = {
        incident["incident_id"]
        for incident in incident_repository.list_incidents(
            source="demo", latest_per_incident=True
        )
    }
    if incident_id:
        is_demo = incident_id in demo_incident_ids
        if is_demo != (source == "demo"):
            return {"events": []}
        return {
            "events": audit.list_events(
                incident_id, limit, newest_first=True
            )
        }

    events = audit.list_events(None, 1000, newest_first=True)
    if source == "operational":
        events = [
            event for event in events
            if event["incident_ref"] not in demo_incident_ids
        ]
    else:
        events = [
            event for event in events
            if event["incident_ref"] in demo_incident_ids
        ]
    return {
        "events": events[:limit]
    }


@router.get("/demo/results")
def list_demo_results(authorization: str | None = Header(default=None)):
    """List isolated synthetic demo records; never reads the app database."""
    _operator(authorization, "read")
    try:
        return {"synthetic": True, **demo_results.list_demo_results()}
    except demo_results.DemoResultsError as exc:
        raise HTTPException(
            status_code=503, detail="Synthetic demo results are unavailable"
        ) from exc


@router.get("/demo/results/{incident_id}")
def get_demo_result(
    incident_id: str, authorization: str | None = Header(default=None)
):
    """Read one synthetic demo bundle from its fixed, read-only store."""
    _operator(authorization, "read")
    try:
        return demo_results.get_demo_result(incident_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="Synthetic demo result not found"
        ) from exc
    except demo_results.DemoResultsError as exc:
        raise HTTPException(
            status_code=503, detail="Synthetic demo results are unavailable"
        ) from exc


@router.get("/incidents/{incident_id}/timeline")
def timeline(incident_id: str,
             authorization: str | None = Header(default=None)):
    _operator(authorization, "read")
    if incident_repository.get_incident_detail(incident_id) is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    events = audit.list_events(incident_id, 1000)
    return {"incident_id": incident_id, "events": events}


@router.get("/cost/report")
def cost_report(authorization: str | None = Header(default=None)):
    """Return the latest weekly report, with legacy report fallback."""
    _operator(authorization, "read")
    reports = sorted(
        WEEKLY_COST_REPORT_DIR.glob("weekly-*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ) if WEEKLY_COST_REPORT_DIR.is_dir() else []
    report_path = reports[0] if reports else COST_REPORT_PATH
    if not report_path.is_file():
        raise HTTPException(status_code=404, detail="No cost report generated yet")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=503, detail="Latest cost report is unreadable"
        ) from exc
    if not isinstance(report, dict):
        raise HTTPException(
            status_code=503, detail="Latest cost report has an invalid format"
        )

    period = report.get("reporting_period")
    if isinstance(period, dict) and period.get("start") and period.get(
        "end_exclusive"
    ):
        period_key = f"{period['start']}/{period['end_exclusive']}"
        ledger_path = report_path.parent / "weekly-runs.sqlite3"
        delivery = {"status": "not_recorded", "sent": False}
        if ledger_path.is_file():
            try:
                with sqlite3.connect(ledger_path) as connection:
                    row = connection.execute(
                        """SELECT status, delivery_outcome, updated_at
                           FROM weekly_cost_runs WHERE period_key = ?""",
                        (period_key,),
                    ).fetchone()
            except sqlite3.Error as exc:
                raise HTTPException(
                    status_code=503, detail="Cost report delivery status is unreadable"
                ) from exc
            if row:
                delivery = {
                    "status": row[1],
                    "run_status": row[0],
                    "updated_at": row[2],
                    "sent": row[1] == "sent",
                }
        report["delivery"] = delivery
    return report