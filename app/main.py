
import os
import secrets
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from app.database import initialize_database
from app.services.decision_engine import decide_action
from app.services.incident_detector import detect_unhealthy_services
from app.services.incident_diagnoser import diagnose_service
from app.services.remediation_service import remediate_service


# Load environment variables from the project's .env file.
load_dotenv()

REMEDIATION_TOKEN = os.getenv("KYBERNAUT_REMEDIATION_TOKEN")

if not REMEDIATION_TOKEN:
    raise RuntimeError(
        "KYBERNAUT_REMEDIATION_TOKEN is not configured"
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
    yield


app = FastAPI(
    title="Kybernaut",
    description=(
        "Agentic AIOps for Infrastructure Reliability "
        "and Cost Optimization"
    ),
    version="0.1.0",
    lifespan=lifespan,
)


class RemediationRequest(BaseModel):
    approved: bool = False
    incident_id: str


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "kybernaut-api",
        "version": "0.1.0",
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
async def get_incidents():
    return await detect_unhealthy_services()


@app.get("/diagnose/{service}")
async def get_diagnosis(service: str):
    if service not in ALLOWED_SERVICES:
        raise HTTPException(
            status_code=404,
            detail="Unknown service",
        )

    return await diagnose_service(service)


@app.get("/decide/{service}")
async def get_decision(service: str):
    if service not in ALLOWED_SERVICES:
        raise HTTPException(
            status_code=404,
            detail="Unknown service",
        )

    return await decide_action(service)


@app.post("/remediate/{service}")
async def post_remediation(
    service: str,
    request: RemediationRequest,
    approval_token: str | None = Header(
        default=None,
        alias="X-Approval-Token",
    ),
    operator_id: str | None = Header(
        default=None,
        alias="X-Operator-ID",
    ),
):
    # 1. Validate the requested target before taking any action.
    if service not in ALLOWED_SERVICES:
        raise HTTPException(
            status_code=404,
            detail="Unknown service",
        )

    # 2. Validate the configured approval token.
    if (
        not approval_token
        or not secrets.compare_digest(
            approval_token,
            REMEDIATION_TOKEN,
        )
    ):
        raise HTTPException(
            status_code=403,
            detail="Valid approval token required",
        )

    # 3. Require an operator identifier for audit attribution.
    if not operator_id or not operator_id.strip():
        raise HTTPException(
            status_code=400,
            detail="Operator identity required",
        )

    # 4. Delegate to the remediation service.
    return await remediate_service(
        service=service,
        approved=request.approved,
        incident_id=request.incident_id,
        operator_id=operator_id.strip(),
    )
