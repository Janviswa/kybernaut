"""Lab automation status, agent/LLM status, cost PDF, saved investigations."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from typing import Literal

from app.api.operations import _operator
from app.services import audit_repository as audit
from app.services import action_claim_repository as claims
from app.services import (
    console_assistant, k8s_remediation, payment_compose_remediation,
    policy_engine, remediation_catalog,
)
from app.services import investigation_repository as repo
from app.services.incident_classifier import classify_incident
from app.services.incident_diagnoser import diagnose_service
from app.services.incident_repository import (
    get_incident_detail,
    list_incidents,
    resolve_incident_fingerprint,
)
from app.services.k8s_incident_sync import build_fingerprint, validate_observation
from app.services.k8s_observer import observe_namespace
from app.services.runbook_retriever import resolve_runbook
from app.services.cost_report_pdf import build_live_cost_report_pdf
from app.services.live_cost_estimates import (
    CostPricingConfigurationError,
    build_live_cost_estimate,
)
from app.services.prometheus_features import collect_service_features
from app.services.lab_incident_scheduler import ROTATION, autogen_enabled
from app.services.local_service_incident_scheduler import (
    SERVICE_SCENARIOS,
    enabled as service_simulation_enabled,
)

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------- lab incident generator ----------------
@router.get("/lab/auto-incidents")
def auto_incident_status(
    request: Request, authorization: str | None = Header(default=None),
):
    _operator(authorization, "read")
    scheduler = getattr(request.app.state, "lab_incident_scheduler", None)
    state = dict(scheduler.state) if scheduler else {"enabled": False}
    local_simulator_active = service_simulation_enabled()
    state["enabled"] = autogen_enabled() and not local_simulator_active
    state["disabled_reason"] = (
        "local_service_simulator_active" if local_simulator_active else None
    )
    state["rotation"] = [
        {"scenario": name, "expected_severity": severity}
        for name, severity, _ in ROTATION
    ]
    state["note"] = (
        "Expected severity is a tag for the scenario. The persisted incident "
        "severity is determined by the observer and classifier."
    )
    return state


@router.get("/local-service-simulation")
def local_service_simulation_status(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _operator(authorization, "read")
    scheduler = getattr(
        request.app.state, "local_service_incident_scheduler", None
    )
    state = dict(scheduler.state) if scheduler else {}
    configured = service_simulation_enabled()
    running = bool(scheduler and scheduler.state.get("running"))
    missing_configuration = []
    if os.getenv("KYBERNAUT_ENVIRONMENT", "").strip().lower() != "local-demo":
        missing_configuration.append("KYBERNAUT_ENVIRONMENT=local-demo")
    if os.getenv("KYBERNAUT_SERVICE_SIMULATION_ENABLED", "true").strip().lower() not in {
        "1", "true", "yes",
    }:
        missing_configuration.append("KYBERNAUT_SERVICE_SIMULATION_ENABLED=true")
    if not os.getenv("KYBERNAUT_FAILURE_SIMULATION_TOKEN", "").strip():
        missing_configuration.append(
            "KYBERNAUT_FAILURE_SIMULATION_TOKEN (same non-empty token in API and four services)"
        )
    state.update(
        enabled=configured and running,
        configured=configured,
        running=running,
        incidents_per_cycle=2,
        severity_mix=["critical", "high"],
        interval_anchor="both_incidents_resolved",
        missing_configuration=missing_configuration,
        required_environment={
            "KYBERNAUT_ENVIRONMENT": "local-demo",
            "KYBERNAUT_SERVICE_SIMULATION_ENABLED": "true",
            "KYBERNAUT_FAILURE_SIMULATION_TOKEN": "set to the same non-empty value in the API and all four service containers",
        },
        scenarios=[
            {
                "service": item["service"],
                "incident_type": item["incident_type"],
                "scenario": item["scenario"],
            }
            for item in SERVICE_SCENARIOS
        ],
    )
    return state


@router.post("/local-service-simulation/run-now")
async def run_local_service_simulation_now(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _operator(authorization, "remediation")
    scheduler = getattr(
        request.app.state, "local_service_incident_scheduler", None
    )
    if (
        scheduler is None
        or not service_simulation_enabled()
        or not scheduler.state.get("running")
    ):
        raise HTTPException(
            status_code=409,
            detail="The incident simulator is not enabled and running.",
        )
    if scheduler.state.get("cycle_in_progress"):
        raise HTTPException(
            status_code=409,
            detail="An incident simulation cycle is already in progress.",
        )
    try:
        return await scheduler.run_cycle(manual=True)
    except Exception as exc:
        logger.exception("Could not start a two-incident simulation cycle")
        raise HTTPException(
            status_code=502,
            detail=(
                "The two-incident simulation cycle failed. Check simulator "
                "status and API logs for the affected service."
            ),
        ) from exc


@router.post("/lab/auto-incidents/run-now")
async def run_auto_incident_now(
    request: Request,
    authorization: str | None = Header(default=None),
):
    _operator(authorization, "remediation")
    scheduler = getattr(request.app.state, "lab_incident_scheduler", None)
    if (
        scheduler is None
        or not autogen_enabled()
        or service_simulation_enabled()
    ):
        raise HTTPException(
            status_code=409,
            detail="Lab incident generation is disabled "
                   "or paused by the local service simulator.",
        )
    try:
        return await scheduler.run_cycle()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"Lab scenario failed: {type(exc).__name__}"
        ) from exc


@router.post("/incidents/{incident_id}/execute-local-simulation")
async def execute_local_simulation(
    incident_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Apply the approved recovery action to one explicitly selected incident."""
    operator = _operator(authorization, "remediation")
    incident = get_incident_detail(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    evidence = incident.get("source_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    if (
        incident.get("status") not in {"OPEN", "REMEDIATING"}
        or evidence.get("simulation") != "local_service_failure"
        or evidence.get("simulated") is not True
        or evidence.get("service") != incident.get("service")
        or not isinstance(incident.get("fingerprint"), str)
    ):
        raise HTTPException(
            status_code=409,
            detail="Incident is not an active local service simulation.",
        )

    scheduler = getattr(
        request.app.state, "local_service_incident_scheduler", None
    )
    if scheduler is None:
        raise HTTPException(
            status_code=409,
            detail="Local service simulation controls are unavailable.",
        )
    try:
        result = await scheduler.clear_incident(
            incident_id=incident_id,
            service=str(incident["service"]),
            scenario_name=str(evidence.get("scenario") or ""),
            fingerprint=str(incident["fingerprint"]),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except httpx.RequestError as exc:
        logger.exception(
            "Could not reach local simulation control for incident %s",
            incident_id,
        )
        raise HTTPException(
            status_code=502,
            detail=(
                f"Could not reach {incident['service']}. Confirm the service is "
                "running; the incident remains open."
            ),
        ) from exc
    except httpx.HTTPStatusError as exc:
        logger.exception(
            "Local simulation control rejected execution for incident %s",
            incident_id,
        )
        if exc.response.status_code == 403:
            detail = (
                f"{incident['service']} rejected the recovery request. "
                "Confirm the service permits this action."
            )
        elif exc.response.status_code == 404:
            detail = (
                f"Recovery control is unavailable for {incident['service']}. "
                "Confirm the service supports this action."
            )
        else:
            detail = (
                f"{incident['service']} returned HTTP "
                f"{exc.response.status_code}; the incident remains open."
            )
        raise HTTPException(status_code=502, detail=detail) from exc
    except Exception as exc:
        logger.exception(
            "Local simulation execution failed for incident %s",
            incident_id,
        )
        raise HTTPException(
            status_code=502,
            detail=(
                "The recovery action could not be confirmed and the incident "
                "was not reported as resolved."
            ),
        ) from exc

    try:
        audit.record_event(
            incident_ref=incident_id,
            event_type="local_service_simulation_cleared",
            actor=operator.operator_id,
            summary=f"Completed recovery action for {incident['service']}",
            resource=str(incident["service"]),
            outcome="resolved",
            details={
                "scenario": evidence.get("scenario"),
                "fingerprint": incident["fingerprint"],
                "resolution_reason": result["resolution_reason"],
            },
        )
        result["audit_recorded"] = True
    except Exception as exc:
        logger.exception(
            "Local simulation cleared but audit recording failed for %s",
            incident_id,
        )
        result["audit_recorded"] = False
        result["audit_error"] = type(exc).__name__
    return result


class PaymentComposeRemediationRequest(BaseModel):
    confirm_write_pause: Literal[True]


@router.post(
    "/incidents/{incident_id}/execute-payment-compose-remediation"
)
async def execute_payment_compose_remediation(
    incident_id: str,
    body: PaymentComposeRemediationRequest,
    authorization: str | None = Header(default=None),
):
    """Migrate payment history and replace only payment-service."""
    operator = _operator(authorization, "remediation")
    incident = get_incident_detail(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    evidence = incident.get("source_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    if (
        incident.get("status") not in {"OPEN", "REMEDIATING"}
        or incident.get("service") != "payment-service"
        or incident.get("incident_type") != "memory_leak"
        or evidence.get("source") != "prometheus"
        or evidence.get("simulation") is not None
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "This action requires an active Prometheus memory incident "
                "for payment-service."
            ),
        )
    unavailable = payment_compose_remediation.execution_unavailable_reason()
    if unavailable:
        raise HTTPException(status_code=409, detail=unavailable)
    try:
        completed_events = audit.list_events(
            incident_id, limit=100, newest_first=True,
        )
    except Exception as exc:
        logger.exception(
            "Payment remediation was not started because audit history failed for %s",
            incident_id,
        )
        raise HTTPException(
            status_code=503,
            detail="The incident audit history is unavailable; no deployment was run.",
        ) from exc
    if any(
        event.get("event_type") == "payment_compose_remediation_completed"
        for event in completed_events
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "The payment fix was already deployed for this incident. "
                "Wait for fresh memory telemetry; do not run it again."
            ),
        )

    try:
        audit.record_event(
            incident_ref=incident_id,
            event_type="payment_compose_remediation_started",
            actor=operator.operator_id,
            summary="Approved migration and PostgreSQL-backed payment-service deployment",
            resource="payment-service",
            outcome="approved",
            details={
                "write_pause_confirmed": body.confirm_write_pause,
                "scope": "payment-service",
            },
        )
    except Exception as exc:
        logger.exception(
            "Payment remediation was not started because approval audit failed for %s",
            incident_id,
        )
        raise HTTPException(
            status_code=503,
            detail="The approval could not be recorded; no deployment was run.",
        ) from exc

    try:
        result = await asyncio.to_thread(
            payment_compose_remediation.execute_payment_history_remediation
        )
    except payment_compose_remediation.PaymentComposeRemediationError as exc:
        logger.error(
            "Payment Compose remediation failed for %s at stage %s",
            incident_id, exc.stage,
        )
        try:
            audit.record_event(
                incident_ref=incident_id,
                event_type="payment_compose_remediation_failed",
                actor=operator.operator_id,
                summary="Payment-service remediation did not complete",
                resource="payment-service",
                outcome="failed",
                details={"failed_stage": exc.stage},
            )
        except Exception:
            logger.exception(
                "Could not record payment remediation failure for %s",
                incident_id,
            )
        raise HTTPException(
            status_code=502,
            detail=(
                f"{exc} The incident remains open. Review the failed stage "
                "before retrying; earlier migration steps may have completed."
            ),
        ) from exc

    try:
        audit.record_event(
            incident_ref=incident_id,
            event_type="payment_compose_remediation_completed",
            actor=operator.operator_id,
            summary="Payment history migrated and payment-service replacement verified",
            resource="payment-service",
            outcome="deployed_unverified",
            details=result,
        )
        result["audit_recorded"] = True
    except Exception as exc:
        logger.exception(
            "Payment-service replacement verified but completion audit failed for %s",
            incident_id,
        )
        result["audit_recorded"] = False
        result["audit_error"] = type(exc).__name__
    result["incident_resolved"] = False
    result["note"] = (
        "Payment history and PostgreSQL-backed storage were verified. "
        "The incident stays open until fresh memory telemetry confirms recovery."
    )
    return result


# ---------------- agent / LLM status ----------------
def _llm_config() -> dict:
    model = os.getenv("KYBERNAUT_LLM_MODEL", "openai/gpt-oss-120b").strip()
    return {
        "provider": "groq",
        "model": model,
        "api_key_configured": bool(os.getenv("GROQ_API_KEY")),
        "model_name_valid": bool(model) and not any(c.isspace() for c in model),
        "timeout_seconds": os.getenv("KYBERNAUT_RCA_TIMEOUT_SECONDS", "20"),
    }


def _probe_llm() -> dict:
    config = _llm_config()
    if not config["api_key_configured"]:
        return {"status": "not_configured", "error_code": "groq_api_key_not_configured"}
    try:
        from langchain_groq import ChatGroq

        llm = ChatGroq(
            model=config["model"], api_key=os.getenv("GROQ_API_KEY"),
            temperature=0, timeout=10, max_retries=0, max_tokens=8,
        )
        llm.invoke("Reply with the single word: ok")
        return {"status": "reachable"}
    except Exception as exc:  # noqa: BLE001 - never leak provider details
        return {"status": "unreachable", "error_code": type(exc).__name__}


@router.get("/agent/status")
async def agent_status(
    probe: bool = False, authorization: str | None = Header(default=None),
):
    _operator(authorization, "read")
    result = {
        "llm": _llm_config(),
        "remediation_mode": k8s_remediation.get_mode(),
        "remediation": k8s_remediation.mode_status(),
        "auto_remediation": os.getenv("KYBERNAUT_POLICY_AUTO_REMEDIATION", "false"),
        "self_approval_allowed": True,
        "supported_actions": list(policy_engine.ALLOWED_ACTIONS),
        "supported_incident_types": [
            "pod_crash", "memory_leak", "disk_full", "deployment_failure", "cpu_spike"],
    }
    if probe:
        result["llm"]["probe"] = await asyncio.to_thread(_probe_llm)
    return result


# ---------------- cost report PDF ----------------
@router.get("/cost/report.pdf")
async def cost_report_pdf(authorization: str | None = Header(default=None)):
    operator = _operator(authorization, "read")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            features = await collect_service_features(
                client,
                required_features=("cpu_cores_used", "memory_bytes"),
            )
    except Exception as exc:
        logger.exception(
            "Live usage cost PDF telemetry query failed (exception_type=%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Live usage cost telemetry is temporarily unavailable.",
        ) from exc
    try:
        report = build_live_cost_estimate(features)
    except CostPricingConfigurationError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Cost pricing configuration {exc.variable} must be "
                "a non-negative number."
            ),
        ) from exc
    report["observed_at"] = datetime.now(timezone.utc).isoformat()
    pdf = build_live_cost_report_pdf(
        report,
        requested_by=str(getattr(operator, "operator_id", "operator")),
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition":
                 'attachment; filename="kybernaut-live-cost-report.pdf"'},
    )


# ---------------- saved Console investigations ----------------
class InvestigationBody(BaseModel):
    title: str = Field(default="Untitled investigation", max_length=160)
    incident_id: str | None = Field(default=None, max_length=128)
    messages: list[dict] = Field(default_factory=list)


@router.get("/investigations")
def list_saved_investigations(authorization: str | None = Header(default=None)):
    _operator(authorization, "read")
    incidents_by_id = {
        str(item["incident_id"]): item
        for item in list_incidents(latest_per_incident=True)
        if item.get("incident_id")
    }
    investigations = []
    for item in repo.list_investigations():
        incident = incidents_by_id.get(str(item.get("incident_id")))
        investigations.append({
            **item,
            "incident_status": incident.get("status") if incident else None,
            "resolution_reason": incident.get("resolution_reason") if incident else None,
        })
    return {"investigations": investigations}


@router.get("/investigations/{investigation_id}")
def get_saved_investigation(
    investigation_id: str, authorization: str | None = Header(default=None),
):
    _operator(authorization, "read")
    item = repo.get_investigation(investigation_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Investigation not found")
    return item


@router.put("/investigations/{investigation_id}")
def save_investigation(
    investigation_id: str, body: InvestigationBody,
    authorization: str | None = Header(default=None),
):
    operator = _operator(authorization, "read")
    try:
        return repo.save_investigation(
            investigation_id, str(operator.operator_id),
            incident_id=body.incident_id, title=body.title, messages=body.messages,
        )
    except repo.InvestigationError as exc:
        status = {"forbidden": 403, "too_large": 413}.get(exc.code, 400)
        raise HTTPException(status_code=status, detail=str(exc)) from exc


@router.delete("/investigations/{investigation_id}")
def delete_saved_investigation(
    investigation_id: str, authorization: str | None = Header(default=None),
):
    """Remove one saved Console conversation (never the incident or its audit)."""
    operator = _operator(authorization, "read")
    try:
        deleted = repo.delete_investigation(
            investigation_id, str(operator.operator_id),
            is_owner="owner" in getattr(operator, "permissions", ()),
        )
    except repo.InvestigationError as exc:
        status = {"forbidden": 403}.get(exc.code, 400)
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Investigation not found")
    logger.info(
        "Saved investigation %s deleted by %s", investigation_id,
        getattr(operator, "operator_id", "operator"),
    )
    return {"investigation_id": investigation_id, "deleted": True}


# ---------------- remediation options (read-only) ----------------
_TELEMETRY_TYPES = {
    "memory_leak", "cpu_spike", "disk_full", "pod_crash",
    "deployment_failure", "service_unavailable", "high_latency",
    "http_5xx", "request_failure",
}

_SERVICE_RECOVERY_DETAILS = {
    "high_latency": {
        "title": "Remove the added 2-second request delay",
        "condition": "2-second request delay",
        "mechanism": (
            "Disabling the delay lets order-service requests proceed directly "
            "to normal request handling."
        ),
        "verification": (
            "The service confirms the delay is inactive; check current request "
            "latency to verify recovery."
        ),
    },
    "http_5xx": {
        "title": "Stop the forced HTTP 503 responses",
        "condition": "HTTP 503 response override",
        "mechanism": (
            "Disabling the response override sends payment-service business "
            "requests through its normal request handler."
        ),
        "verification": (
            "The service confirms the override is inactive; check a business "
            "request for a successful response."
        ),
    },
    "request_failure": {
        "title": "Stop the forced HTTP 500 responses",
        "condition": "HTTP 500 response override",
        "mechanism": (
            "Disabling the response override sends inventory-service business "
            "requests through its normal request handler."
        ),
        "verification": (
            "The service confirms the override is inactive; check a business "
            "request for a successful response."
        ),
    },
    "service_unavailable": {
        "title": "Stop rejecting user-service requests with HTTP 503",
        "condition": "HTTP 503 unavailability response",
        "mechanism": (
            "Disabling the unavailability response lets user-service requests "
            "reach the normal request handler."
        ),
        "verification": (
            "The service confirms the unavailability response is inactive; "
            "check a business request for a successful response."
        ),
    },
}


def _telemetry_remediation_note(incident: dict, incident_type: str) -> str:
    if (
        incident_type == "memory_leak"
        and incident.get("service") == "payment-service"
    ):
        return (
            "The payment-service memory fix stores payment history in "
            "PostgreSQL. The Execute action first migrates existing records, "
            "then rebuilds and replaces only payment-service. Pause all "
            "payment writes before approving it. The incident remains open "
            "until fresh memory telemetry confirms recovery."
        )
    if incident_type == "cpu_spike":
        diagnosis = incident.get("diagnosis")
        diagnosis = diagnosis if isinstance(diagnosis, dict) else {}
        limitations = diagnosis.get("limitations")
        limitations = limitations if isinstance(limitations, list) else []
        recorded_diagnosis = diagnosis.get("diagnosis")
        recorded_diagnosis = (
            recorded_diagnosis if isinstance(recorded_diagnosis, dict) else {}
        )
        source_evidence = incident.get("source_evidence")
        source_evidence = (
            source_evidence if isinstance(source_evidence, dict) else {}
        )
        metric = source_evidence.get("metric")
        metric = metric if isinstance(metric, dict) else {}

        blockers = []
        if "trusted_workload_mapping_missing" in limitations:
            blockers.append("no trusted workload mapping is configured")
        elif "trusted_workload_mapping_ambiguous" in limitations:
            blockers.append("the trusted workload mapping is ambiguous")
        elif "trusted_workload_mapping_invalid" in limitations:
            blockers.append("the trusted workload mapping is invalid")
        if recorded_diagnosis.get("status") == "unknown":
            blockers.append("current service health is unknown")
        if metric.get("name") == "cpu_cores_used":
            blockers.append(
                "the record contains one CPU sample, not sustained utilization evidence"
            )

        if blockers:
            return (
                "No executable CPU-spike remediation is available yet: "
                + "; ".join(blockers)
                + ". The high-CPU runbook requires CPU usage over time, "
                "correlation with request rate and p95 latency, CPU-throttling "
                "checks, and recent-release context. Configure one trusted "
                "workload target and gather that evidence before rerunning "
                "the investigation; an approved action must also pass the "
                "runbook and policy checks."
            )

    return (
        "Incident-specific operator remediation steps are listed below. "
        "This telemetry alert does not have a verified execution target and "
        "current evidence for Kybernaut to run an action safely."
    )


def _deployment_state(observation: dict, deployment: str | None) -> dict | None:
    for item in (observation or {}).get("deployments") or []:
        if isinstance(item, dict) and item.get("name") == deployment:
            return item
    return None


def _apply_action_capacity(
    options: list[dict], deployment: str, namespace: str,
) -> None:
    config = policy_engine.PolicyConfig.from_env()
    max_actions, window_seconds = config.action_capacity_for_namespace(namespace)
    try:
        recent_count = claims.count_recent_action_claims(
            deployment, window_seconds
        )
    except Exception as exc:
        logger.exception("Could not read remediation action capacity")
        raise HTTPException(
            status_code=503,
            detail="Remediation availability is temporarily unavailable.",
        ) from exc

    available = recent_count < max_actions
    for option in options:
        if option.get("kind") != "executable":
            continue
        option["policy_available"] = available
        if option.get("executable") is True and not available:
            option["policy_reason"] = "action_claim_limit_reached"
            option["policy_detail"] = (
                f"Action limit reached ({recent_count}/"
                f"{max_actions}) for the last "
                f"{window_seconds} seconds. Re-run the investigation "
                "after capacity is available."
            )


def _classify_stored_kubernetes_evidence(
    incident: dict, evidence: dict,
) -> dict | None:
    namespace = evidence.get("namespace")
    resource = evidence.get("resource")
    resource_kind = evidence.get("resource_kind")
    facts = evidence.get("evidence")
    if (
        not isinstance(namespace, str)
        or not isinstance(resource, str)
        or resource_kind not in {"Pod", "Deployment"}
        or not isinstance(facts, list)
    ):
        return None

    source_incident = {
        "type": evidence.get("type") or incident.get("incident_type"),
        "resource": resource,
        "resource_kind": resource_kind,
        "resource_uid": evidence.get("resource_uid"),
        "severity": incident.get("severity", "medium"),
        "source_timestamp": (
            evidence.get("source_timestamp") or incident.get("source_timestamp")
        ),
        "evidence": facts,
    }
    deployment = evidence.get("deployment")
    if not isinstance(deployment, str) and resource_kind == "Deployment":
        deployment = resource
    deployments = [{"name": deployment}] if isinstance(deployment, str) else []
    return classify_incident(
        None,
        {"namespace": namespace, "deployments": deployments,
         "incidents": [source_incident]},
        None,
    )


@router.get("/incidents/{incident_id}/remediation-options")
async def remediation_options(
    incident_id: str, authorization: str | None = Header(default=None),
):
    """Return remediation options and reconcile a confirmed-cleared condition."""
    operator = _operator(authorization, "read")
    incident = get_incident_detail(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    evidence = incident.get("source_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    status = str(incident.get("status"))
    result: dict = {
        "incident_id": incident_id, "incident_status": status,
        "service": incident.get("service"),
        "incident_alert": incident.get("message") or incident.get("incident_type"),
        "source": "telemetry", "incident_type": None, "options": [],
        "live_evidence": False, "note": "",
    }

    if evidence.get("simulation") == "local_service_failure":
        itype = str(incident.get("incident_type") or "unknown")
        itype = itype if itype in _TELEMETRY_TYPES else "unknown"
        service = str(incident.get("service") or "")
        options = remediation_catalog.build_options(
            itype, {"service": service}, None,
        )
        if status in {"OPEN", "REMEDIATING"}:
            recovery = _SERVICE_RECOVERY_DETAILS.get(itype)
            if recovery is None:
                recovery = {
                    "title": "Disable the active incident condition",
                    "condition": "active incident condition",
                    "mechanism": (
                        f"Disabling the condition lets {service} resume its "
                        "normal request handling."
                    ),
                    "verification": (
                        "The service confirms the condition is inactive; "
                        "verify the incident's observed behavior has recovered."
                    ),
                }
            options.insert(0, {
                "option_id": "clear-local-simulation",
                "kind": "executable",
                "action": "clear_local_simulation",
                "resource": service,
                "executable": True,
                "selected": True,
                "title": f"{recovery['title']} on {service}",
                "rationale": recovery["mechanism"],
                "steps": [
                    f"Disable the active {recovery['condition']} on {service}.",
                    recovery["verification"],
                ],
                "risk": (
                    f"This changes {service}'s request handling; the incident "
                    "is resolved after the service confirms the condition is inactive."
                ),
            })
        result.update(
            source="local_service_simulator",
            incident_type=itype,
            options=options,
            note="" if status in {"OPEN", "REMEDIATING"} else
            f"This incident is {status.lower()}; nothing needs to be run.",
        )
        return result

    payment_remediation = (
        status in {"OPEN", "REMEDIATING"}
        and incident.get("service") == "payment-service"
        and incident.get("incident_type") == "memory_leak"
        and evidence.get("source") == "prometheus"
        and evidence.get("simulation") is None
    )
    if payment_remediation:
        unavailable = payment_compose_remediation.execution_unavailable_reason()
        if unavailable is None:
            result["incident_type"] = "memory_leak"
            result["options"] = [
                {
                    "option_id": "payment-compose-migrate-history",
                    "kind": "executable",
                    "action": "migrate_payment_history_and_deploy",
                    "resource": "payment-service",
                    "executable": True,
                    "policy_available": True,
                    "title": (
                        "Migrate payment history and deploy the PostgreSQL-backed fix"
                    ),
                    "rationale": (
                        "Moves existing payment records out of process memory, "
                        "then replaces only payment-service with the database-backed "
                        "implementation."
                    ),
                    "steps": [
                        "Pause all payment writes before approving this action; keep them paused through verification.",
                        "Copy the current payment history to PostgreSQL and reject conflicting payment IDs.",
                        "Rebuild and replace only payment-service; do not resolve the incident automatically.",
                        "Verify PostgreSQL-backed health and the complete migrated payment history.",
                        "Resume payment writes, then confirm memory recovery with fresh telemetry.",
                    ],
                    "risk": (
                        "Payment requests must remain paused during migration. "
                        "A failed action can leave PostgreSQL started or history "
                        "migrated; inspect the reported stage before retrying."
                    ),
                    "requires_approval": True,
                    "selected": True,
                },
                *remediation_catalog.manual_options(
                    "memory_leak", "payment-service"
                ),
            ]
            result["note"] = (
                "Pause all payment writes before selecting Execute. "
                "The incident stays open until fresh memory telemetry "
                "confirms recovery."
            )
            return result
        result["note"] = (
            f"{unavailable} " + _telemetry_remediation_note(
                incident, "memory_leak"
            )
        )

    live = incident.get("service") == "kubernetes-lab" and evidence.get("source") == "kubernetes"
    classification = None
    if live and status in {"OPEN", "REMEDIATING"}:
        try:
            observation = await asyncio.to_thread(observe_namespace)
            namespace = observation.get("namespace")
            observed_incidents = observation.get("incidents")
            if not isinstance(namespace, str) or not isinstance(
                observed_incidents, list
            ):
                raise ValueError("Kubernetes observation identity is invalid")
            persisted_fingerprint = incident.get("fingerprint")
            matching_incidents = [
                item for item in observed_incidents
                if isinstance(item, dict)
                and isinstance(item.get("type"), str)
                and item.get("resource") == evidence.get("resource")
                and item.get("resource_uid") == evidence.get("resource_uid")
                and item.get("resource_kind") == evidence.get("resource_kind")
                and build_fingerprint(item, namespace) == persisted_fingerprint
            ]
            scoped_observation = {
                **observation,
                "incidents": matching_incidents,
            }
            classification = classify_incident(
                {"service": "kubernetes-lab", "status": "not_applicable", "findings": []},
                scoped_observation, {"status": "not_applicable"},
            )
        except Exception:  # noqa: BLE001
            observation, classification = None, None
            result["note"] = "Kubernetes could not be observed just now; only guidance is shown."
        if classification is not None:
            itype = classification["incident_type"]
            target = classification.get("target") or {}
            matches = (
                target.get("resource") == evidence.get("resource")
                and target.get("resource_uid") == evidence.get("resource_uid")
                and len(matching_incidents) == 1
            )
            result.update(source="kubernetes", incident_type=itype, live_evidence=matches,
                          classification=classification)
            if matches:
                deployment = target.get("deployment")
                dep_state = _deployment_state(observation, deployment)
                texts = [itype, *(i for i in (classification.get("evidence") or []) if isinstance(i, str))]

                def check(raw: dict) -> dict:
                    return resolve_runbook(
                        " ".join(texts), incident_type=itype,
                        evidence=classification.get("evidence") or [],
                        planned_action=raw["action"], deployment_state=dep_state,
                        allow_scaled_down_restore=(
                            itype == "disk_full"
                            and deployment == "sim-disk-fill"
                            and raw.get("target_replicas") == 1
                        ),
                    )
                result["options"] = remediation_catalog.build_options(
                    itype, target, dep_state, runbook_check=check)
                if isinstance(deployment, str):
                    _apply_action_capacity(
                        result["options"], deployment, namespace
                    )
            else:
                result["note"] = ("The cluster no longer shows this exact condition, so no action "
                                  "can be run against it; guidance is shown instead.")
                try:
                    validate_observation(observation)
                except ValueError:
                    result["note"] = (
                        "The Kubernetes observation was incomplete, so the incident "
                        "remains open and remediation is unavailable until fresh "
                        "evidence can be confirmed."
                    )
                else:
                    active_fingerprints = {
                        build_fingerprint(item, namespace)
                        for item in observed_incidents
                    }
                    if (
                        isinstance(persisted_fingerprint, str)
                        and persisted_fingerprint
                        and persisted_fingerprint not in active_fingerprints
                    ):
                        if "remediation" not in operator.permissions:
                            result["note"] = (
                                "The complete fresh observation no longer shows this "
                                "condition, but the incident remains unchanged because "
                                "reconciliation requires the remediation permission."
                            )
                        else:
                            resolved_count = resolve_incident_fingerprint(
                                persisted_fingerprint,
                                resolution_reason="condition_cleared",
                            )
                            refreshed_incident = get_incident_detail(incident_id)
                            if (
                                resolved_count
                                and refreshed_incident
                                and refreshed_incident.get("status") == "RESOLVED"
                                and refreshed_incident.get("resolution_reason")
                                == "condition_cleared"
                            ):
                                result.update(
                                    incident_status="RESOLVED",
                                    condition_cleared=True,
                                    resolution_reason="condition_cleared",
                                    resolved_at=refreshed_incident.get("resolved_at"),
                                    note=(
                                        "Resolved — a complete fresh Kubernetes observation "
                                        "confirmed the condition had cleared before remediation. "
                                        "No remediation was executed."
                                    ),
                                )
    if live and not result["options"]:
        stored_classification = _classify_stored_kubernetes_evidence(
            incident, evidence
        )
        if (
            stored_classification is not None
            and stored_classification["incident_type"] in _TELEMETRY_TYPES
        ):
            result.update(
                incident_type=stored_classification["incident_type"],
                classification=stored_classification,
            )
            if result["incident_status"] in {"OPEN", "REMEDIATING"}:
                reason = (
                    "incident_not_current" if classification is not None
                    else "live_evidence_unavailable"
                )
                result["options"] = remediation_catalog.build_options(
                    stored_classification["incident_type"],
                    stored_classification.get("target") or {},
                    None,
                    runbook_check=lambda _: {
                        "status": "stale_evidence",
                        "reason": reason,
                    },
                )
    if not result["options"]:
        itype = result["incident_type"] or str(incident.get("incident_type"))
        itype = itype if itype in _TELEMETRY_TYPES else "unknown"
        result["incident_type"] = itype
        result["options"] = remediation_catalog.build_options(
            itype, {"service": incident.get("service")}, None
        )
        if not result["note"] and status not in {"OPEN", "REMEDIATING"}:
            result["note"] = f"This incident is {status.lower()}; nothing needs to be run."
        elif not result["note"] and not live:
            result["note"] = _telemetry_remediation_note(incident, itype)
    return result


# ---------------- grounded Console Q&A ----------------
_APP_SERVICES = ("order-service", "payment-service", "user-service", "inventory-service")


class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    incident_id: str | None = Field(default=None, max_length=128)
    service: str | None = Field(default=None, max_length=64)


async def _service_snapshot(name: str) -> dict:
    try:
        diagnosis = await asyncio.wait_for(diagnose_service(name), timeout=8)
        return {k: diagnosis.get(k) for k in (
            "service", "status", "evidence_status", "missing_evidence", "findings", "evidence")}
    except Exception as exc:  # noqa: BLE001
        return {"status": "unavailable", "reason": type(exc).__name__}


@router.post("/console/ask")
async def console_ask(body: AskBody, authorization: str | None = Header(default=None)):
    """Answer a question from stored records only (see console_assistant)."""
    _operator(authorization, "read")
    question = body.question.strip()
    incident = get_incident_detail(body.incident_id) if body.incident_id else None
    if body.incident_id and incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    events: list[dict] = []
    approvals: list[dict] = []
    if incident:
        events = audit.list_events(str(incident["incident_id"]), 200)
        approvals = audit.list_approvals_for_incident(
            str(incident["incident_id"]), limit=200,
        )
    wanted = [name for name in _APP_SERVICES if name in question.lower()]
    for candidate in (body.service, incident.get("service") if incident else None):
        if candidate in _APP_SERVICES and candidate not in wanted:
            wanted.append(candidate)
    snapshots = {}
    if not console_assistant.is_cause_question(question):
        snapshots = dict(zip(
            wanted[:3],
            await asyncio.gather(*(
                _service_snapshot(name) for name in wanted[:3]
            )),
        ))
    context = console_assistant.build_context(
        incident=incident, audit_events=events, approvals=approvals, services=snapshots,
        recent_incidents=list_incidents(source="operational"),
    )
    return await console_assistant.answer_question(question, context)