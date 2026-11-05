import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# Load application configuration before importing modules that consume it.
load_dotenv()

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from app.services.anomaly_runtime import initialize_anomaly_policies
from app.services.application_anomaly_monitor import (
    get_application_anomaly_monitor_status,
    start_application_anomaly_monitor,
    stop_application_anomaly_monitor,
)
from app.database import initialize_database
from app.services.decision_engine import decide_action
from app.services.incident_detector import detect_unhealthy_services
from app.services.incident_metrics import (
    calculate_incident_metrics,
)
from app.services.incident_repository import (
    classify_incident_source,
    get_incident_detail,
    list_incidents,
)
from app.services.incident_diagnoser import diagnose_service
from app.services.prometheus_features import (
    EXPECTED_SERVICES,
    collect_service_features,
    freshness_limits_from_environment,
)
from app.services.live_cost_estimates import (
    CostPricingConfigurationError,
    build_live_cost_estimate,
)
from app.services.system_health import check_system_dependencies
from app.services.remediation_service import remediate_service
from app.core.jwt_auth import (
    require_permission,
    verify_operator_token,
)
from app.api.auth import router as auth_router
from app.api.demo import router as demo_router
from app.services.demo_incident_scheduler import (
    DemoIncidentScheduler,
    demo_mode_enabled,
)
from app.api.lab import router as lab_router
from app.services.audit_repository import record_event as record_audit_event
from app.services.lab_incident_scheduler import LabIncidentScheduler
from app.services.local_service_incident_scheduler import (
    LocalServiceIncidentScheduler,
)

ALLOWED_SERVICES = {
    "order-service",
    "payment-service",
    "user-service",
    "inventory-service",
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_database()
    initialize_anomaly_policies()
    # Dormant unless a demo session is explicitly entered through the API;
    # the web UI no longer offers a demo page.
    app.state.demo_incident_scheduler = DemoIncidentScheduler()
    app.state.lab_incident_scheduler = LabIncidentScheduler(
        audit=lambda event, summary, details: record_audit_event(
            incident_ref="lab-automation", event_type=event,
            actor="kybernaut-lab-scheduler", summary=summary,
            resource="kybernaut-lab", outcome="recorded", details=details,
        ),
    )
    app.state.local_service_incident_scheduler = (
        LocalServiceIncidentScheduler()
    )
    await start_application_anomaly_monitor(app)
    if app.state.local_service_incident_scheduler.state["enabled"]:
        app.state.lab_incident_scheduler.state["enabled"] = False
        app.state.lab_incident_scheduler.state["disabled_reason"] = (
            "local_service_simulator_active"
        )
    else:
        await app.state.lab_incident_scheduler.start()
    await app.state.local_service_incident_scheduler.start()
    try:
        yield
    finally:
        await app.state.lab_incident_scheduler.shutdown()
        await app.state.local_service_incident_scheduler.shutdown()
        await app.state.demo_incident_scheduler.shutdown()
        await stop_application_anomaly_monitor(app)

app = FastAPI(
    title="Kybernaut",
    description=(
        "Agentic AIOps for Infrastructure Reliability "
        "and Cost Optimization"
    ),
    version="0.1.0",
    lifespan=lifespan,
)
app.include_router(auth_router)
if demo_mode_enabled():
    app.include_router(demo_router)
app.include_router(lab_router)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request,
    exc: RequestValidationError,
):
    if request.url.path == "/auth/login":
        return JSONResponse(
            status_code=422,
            content={"detail": "Invalid login request."},
        )
    from fastapi.exception_handlers import request_validation_exception_handler

    return await request_validation_exception_handler(request, exc)


app.mount(
    "/console",
    StaticFiles(
        directory=os.path.join(os.path.dirname(__file__), "static", "console"),
        html=True,
    ),
    name="operations-console",
)


class RemediationRequest(BaseModel):
    approved: bool = False
    incident_id: str


@app.get("/health")
async def health_check():
    checked_at = datetime.now(timezone.utc).isoformat()
    components = await check_system_dependencies()
    components["api"] = {
        "status": "healthy",
        "checked_at": checked_at,
        "reason": "Health endpoint responded successfully.",
    }
    return {
        "status": "healthy",
        "service": "kybernaut-api",
        "version": "0.1.0",
        "checked_at": checked_at,
        "components": components,
    }


@app.get("/")
def root():
    return {
        "message": "Welcome to Kybernaut",
        "workflow": [
            "Monitor",
            "Detect",
            "Diagnose",
            "Decide",
            "Remediate",
            "Verify",
            "Audit",
        ],
    }

@app.get("/incidents")
async def get_incidents(
    authorization: str | None = Header(default=None),
):
    _operator(authorization, "read")
    return await detect_unhealthy_services()

@app.get("/incidents/history")
def get_incident_history(
    status: str | None = None,
    source: str = "operational",
    authorization: str | None = Header(
        default=None,
        alias="Authorization",
    ),
):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization[len("Bearer "):].strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    operator = verify_operator_token(token)
    require_permission(operator, "read")

    allowed_statuses = {
        "OPEN",
        "REMEDIATING",
        "RESOLVED",
    }

    if status is not None:
        status = status.upper()

        if status not in allowed_statuses:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid status. Use OPEN, "
                    "REMEDIATING, or RESOLVED."
                ),
            )

    source = source.lower()
    if source not in {"operational", "demo", "all"}:
        raise HTTPException(
            status_code=400,
            detail="Invalid source. Use operational, demo, or all.",
        )

    incidents = list_incidents(
        status=status,
        source=source,
        latest_per_incident=True,
    )
    for incident in incidents:
        incident["source"] = classify_incident_source(incident)
    return {"incidents": incidents, "source_filter": source}


@app.get("/incidents/{incident_id}")
def get_incident(
    incident_id: str,
    authorization: str | None = Header(
        default=None,
        alias="Authorization",
    ),
):
    """Return one persisted incident (latest occurrence). Read-only."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[len("Bearer "):].strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    require_permission(verify_operator_token(token), "read")

    incident = get_incident_detail(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident["source"] = classify_incident_source(incident)
    return incident




@app.get("/metrics/incidents")
def get_incident_metrics(
    authorization: str | None = Header(
        default=None,
        alias="Authorization",
    ),
):
    """Return read-only reliability metrics for authorized operators."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization[len("Bearer "):].strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    operator = verify_operator_token(token)
    require_permission(operator, "read")

    return calculate_incident_metrics()


@app.get("/diagnose/{service}")
async def get_diagnosis(
    service: str,
    authorization: str | None = Header(default=None),
):
    _operator(authorization, "read")

    if service not in ALLOWED_SERVICES:
        raise HTTPException(
            status_code=404,
            detail="Unknown service",
        )

    try:
        return await diagnose_service(service)
    except Exception as exc:
        import logging

        logging.getLogger(__name__).exception(
            "Diagnosis failed for service %s",
            service,
        )
        raise HTTPException(
            status_code=503,
            detail="Monitoring data is temporarily unavailable",
        ) from exc


@app.get("/services")
async def get_configured_services(
    authorization: str | None = Header(default=None),
):
    """Return configured service identifiers without querying their health."""
    _operator(authorization, "read")
    return {"services": sorted(ALLOWED_SERVICES)}


def _service_health_snapshot(diagnosis: dict) -> dict:
    """Derive health only from complete, fresh diagnosis evidence."""
    freshness = "unavailable"
    timestamps = diagnosis.get("evidence_timestamps")
    required = ("service_availability", "http_5xx_rate", "p95_latency")
    if diagnosis.get("evidence_status") == "invalid":
        freshness = "invalid"
    elif diagnosis.get("evidence_status") == "available" and isinstance(
        timestamps, dict
    ):
        try:
            samples = []
            for name in required:
                entry = timestamps.get(name)
                value = entry.get("timestamp") if isinstance(entry, dict) else None
                if not isinstance(value, str):
                    raise ValueError("evidence_timestamp_missing")
                sample = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if sample.tzinfo is None or sample.utcoffset() is None:
                    raise ValueError("evidence_timestamp_invalid")
                samples.append(sample.astimezone(timezone.utc))
            max_age, max_skew = freshness_limits_from_environment()
            now = datetime.now(timezone.utc)
            ages = [(now - sample).total_seconds() for sample in samples]
            skew = (max(samples) - min(samples)).total_seconds()
            if any(age < 0 for age in ages):
                freshness = "invalid"
            elif any(age > max_age for age in ages) or skew > max_skew:
                freshness = "stale"
            else:
                freshness = "fresh"
        except (ValueError, TypeError):
            freshness = "invalid"

    if freshness != "fresh":
        health_status = "unknown"
    elif diagnosis.get("status") == "down":
        health_status = "unhealthy"
    elif diagnosis.get("status") == "up":
        findings = diagnosis.get("findings")
        if not isinstance(findings, list) or not findings:
            health_status = "unknown"
        elif all(
            isinstance(item, dict)
            and item.get("type") == "no_detected_anomaly"
            for item in findings
        ):
            health_status = "healthy"
        else:
            health_status = "degraded"
    else:
        health_status = "unknown"
    return {
        **diagnosis,
        "health_status": health_status,
        "freshness_status": freshness,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/service-health/{service}")
async def get_live_service_health(
    service: str,
    authorization: str | None = Header(default=None),
):
    """Return read-only Prometheus health with explicit evidence freshness."""
    _operator(authorization, "read")
    if service not in ALLOWED_SERVICES:
        raise HTTPException(status_code=404, detail="Unknown service")
    try:
        diagnosis = await diagnose_service(service)
        return _service_health_snapshot(diagnosis)
    except Exception as exc:
        logging.getLogger(__name__).exception(
            "Service health query failed for %s (exception_type=%s)",
            service,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Service health data is temporarily unavailable.",
        ) from exc


@app.get("/telemetry")
async def get_live_telemetry(
    authorization: str | None = Header(default=None),
):
    """Return fresh read-only Prometheus features for configured services."""
    _operator(authorization, "read")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            features = await collect_service_features(client)
    except Exception as exc:
        logging.getLogger(__name__).exception(
            "Live telemetry query failed (exception_type=%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Live telemetry is temporarily unavailable.",
        ) from exc

    return {
        "source": "prometheus",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "services": {
            service: features.get(service)
            for service in EXPECTED_SERVICES
        },
        "unavailable_services": sorted(
            set(EXPECTED_SERVICES) - set(features)
        ),
    }


@app.get("/cost/usage")
async def get_live_usage_cost(
    authorization: str | None = Header(default=None),
):
    """Estimate current four-service usage cost from Prometheus and owner rates."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization[len("Bearer "):].strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    require_permission(verify_operator_token(token), "read")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            features = await collect_service_features(
                client,
                required_features=("cpu_cores_used", "memory_bytes"),
            )
    except Exception as exc:
        logging.getLogger(__name__).exception(
            "Live usage cost telemetry query failed (exception_type=%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Live usage cost telemetry is temporarily unavailable.",
        ) from exc
    try:
        estimate = build_live_cost_estimate(features)
    except CostPricingConfigurationError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Cost pricing configuration {exc.variable} must be "
                "a non-negative number."
            ),
        ) from exc
    estimate["observed_at"] = datetime.now(timezone.utc).isoformat()
    return estimate


@app.get("/decide/{service}")
async def get_decision(
    service: str,
    authorization: str | None = Header(default=None),
):
    _operator(authorization, "read")

    if service not in ALLOWED_SERVICES:
        raise HTTPException(
            status_code=404,
            detail="Unknown service",
        )

    try:
        return await decide_action(service)
    except Exception as exc:
        import logging

        logging.getLogger(__name__).exception(
            "Decision failed for service %s",
            service,
        )
        raise HTTPException(
            status_code=503,
            detail="Monitoring data is temporarily unavailable",
        ) from exc


@app.post("/remediate/{service}")
async def post_remediation(
    service: str,
    request: RemediationRequest,
    authorization: str | None = Header(
        default=None,
        alias="Authorization",
    ),
):
    if service not in ALLOWED_SERVICES:
        raise HTTPException(status_code=404, detail="Unknown service")

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization[len("Bearer "):].strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    operator = verify_operator_token(token)
    require_permission(operator, "remediation")

    raise HTTPException(
        status_code=410,
        detail=(
            "Legacy remediation is disabled. Use "
            "POST /workflow/{service}/respond for persisted approval, "
            "authorization, execution, verification, and audit."
        ),
    )


# LangGraph advisory incident workflow.
from app.services.incident_workflow import run_incident_workflow


@app.get("/workflow/{service}")
async def incident_workflow_endpoint(
    service: str,
    authorization: str | None = Header(default=None),
):
    """Run advisory diagnosis, RCA, and policy evaluation."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="Bearer operator token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization.split(None, 1)[1].strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Bearer operator token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    operator = verify_operator_token(token)
    require_permission(operator, "read")

    try:
        return await run_incident_workflow(service)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        # Do not expose internal errors or credentials to API clients.
        raise HTTPException(
            status_code=503,
            detail="Incident workflow is temporarily unavailable.",
        ) from exc


# Read-only Kubernetes incident observer and incident persistence.
from app.services.k8s_observer import observe_namespace
from app.services.k8s_incident_sync import persist_observation


@app.get("/kubernetes/incidents")
async def kubernetes_incidents_endpoint(
    authorization: str | None = Header(default=None),
):
    """Return read-only Kubernetes observations for authorized operators."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="Bearer operator token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization.split(None, 1)[1].strip()
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Bearer operator token required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    operator = verify_operator_token(token)
    require_permission(operator, "read")

    try:
        observation = await asyncio.to_thread(observe_namespace)

        # Persist only after the entire observation succeeds.
        persistence = await asyncio.to_thread(
            persist_observation, observation
        )

        return {
            **observation,
            "persistence": persistence,
        }
    except Exception as exc:
        logging.getLogger(__name__).exception(
            "Kubernetes incident observation or persistence failed"
        )
        raise HTTPException(
            status_code=503,
            detail="Kubernetes observations are temporarily unavailable.",
        ) from exc


# Operations API: response workflow, approvals, audit, cost report.
from app.api.operations import _operator, router as operations_router

app.include_router(operations_router)


@app.get("/monitor/status")
async def monitor_status(
    authorization: str | None = Header(default=None),
):
    """Return available monitor state; this does not run a poll."""
    _operator(authorization, "read")
    return get_application_anomaly_monitor_status(app)