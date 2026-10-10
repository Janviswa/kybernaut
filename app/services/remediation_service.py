
import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.services.remediation_repository import (
    reserve_attempt,
    update_attempt_result,
)

HELPER_PATH = "/usr/local/bin/kybernaut-remediate"
PROMETHEUS_URL = "http://localhost:9090"
AUDIT_PATH = Path.home() / "kybernaut" / "remediation_audit.jsonl"

ALLOWED_SERVICES = {
    "order-service": {"port": 8001},
    "payment-service": {"port": 8002},
    "user-service": {"port": 8003},
    "inventory-service": {"port": 8004},
}

# Serialize remediation requests per service within this Python process.
SERVICE_LOCKS = {
    service: asyncio.Lock()
    for service in ALLOWED_SERVICES
}


def write_audit_record(record: dict) -> None:
    """Append a remediation decision or outcome to the JSONL audit log."""
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)

    with AUDIT_PATH.open("a", encoding="utf-8") as file:
        file.write(json.dumps(record) + "\n")
        file.flush()


def make_record(
    audit_id: str,
    service: str,
    incident_id: str,
    status: str,
    message: str,
    **extra,
) -> dict:
    """Create a structured remediation audit record."""
    return {
        "audit_id": audit_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": service,
        "incident_id": incident_id,
        "action": "restart",
        "status": status,
        "message": message,
        **extra,
    }


async def check_service_health(service: str) -> bool:
    """Return True if the service health endpoint responds with HTTP 200."""
    port = ALLOWED_SERVICES[service]["port"]

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(
                f"http://127.0.0.1:{port}/health"
            )
            return response.status_code == 200
    except httpx.RequestError:
        return False


async def get_active_incident_id(service: str) -> str | None:
    """Return the expected incident ID only when Prometheus confirms an outage."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{PROMETHEUS_URL}/api/v1/query",
                params={"query": f'up{{job="{service}"}}'},
            )
            response.raise_for_status()
            payload = response.json()

        if payload.get("status") != "success":
            return None

        results = payload["data"]["result"]

        if not results or float(results[0]["value"][1]) != 0.0:
            return None

        from app.services.incident_detector import make_incident_id

        return make_incident_id(service, "service_unavailable")

    except (
        httpx.HTTPError,
        KeyError,
        ValueError,
        IndexError,
    ):
        return None


async def _remediate_service_locked(
    service: str,
    approved: bool,
    incident_id: str,
) -> dict:
    """Validate, reserve and execute a remediation while holding its service lock."""
    audit_id = str(uuid.uuid4())

    if service not in ALLOWED_SERVICES:
        raise ValueError("Service is not allowlisted")

    if not incident_id or not incident_id.strip():
        raise ValueError("A non-empty incident_id is required")

    # Require explicit approval before performing any remediation.
    if not approved:
        result = make_record(
            audit_id,
            service,
            incident_id,
            "rejected",
            "Explicit approval is required.",
        )
        write_audit_record(result)
        return result

    # Confirm the submitted ID matches the currently detected outage.
    active_incident_id = await get_active_incident_id(service)
    outage_confirmed = active_incident_id is not None
    incident_matches = active_incident_id == incident_id

    # Independently verify that the application is unhealthy.
    healthy_before = await check_service_health(service)

    if not incident_matches or healthy_before:
        result = make_record(
            audit_id,
            service,
            incident_id,
            "skipped",
            (
                "Restart skipped: the incident ID does not match the active "
                "incident, or the service is already healthy."
            ),
            outage_confirmed=outage_confirmed,
            incident_matches=incident_matches,
            health_before=healthy_before,
        )
        write_audit_record(result)
        return result

    # Atomically check cooldown/retry limits and reserve one attempt.
    # The repository persists this reservation before the helper is invoked.
    reservation = reserve_attempt(
        audit_id=audit_id,
        incident_id=incident_id,
        service=service,
    )

    if not reservation["allowed"]:
        result = make_record(
            audit_id,
            service,
            incident_id,
            "blocked",
            reservation["reason"],
            attempt_count=reservation["attempt_count"],
        )
        write_audit_record(result)
        return result

    # Invoke the restricted helper only after every check has passed.
    try:
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/sudo",
            HELPER_PATH,
            service,
            "restart",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(
            process.communicate(),
            timeout=30,
        )


        if process.returncode != 0:
            result = make_record(
                audit_id,
                service,
                incident_id,
                "failed",
                stderr.decode(errors="replace").strip()
                or "Restricted helper returned an error.",
                outage_confirmed=outage_confirmed,
                health_before=healthy_before,
                attempt_count=reservation["attempt_count"],
            )

            update_attempt_result(
                audit_id=audit_id,
                status="failed",
                message=result["message"],
            )

            write_audit_record(result)
            return result

        # Poll for recovery for up to 30 seconds.
        healthy_after = False

        for _ in range(10):
            await asyncio.sleep(3)

            if await check_service_health(service):
                healthy_after = True
                break

        result = make_record(
            audit_id,
            service,
            incident_id,
            "verified" if healthy_after else "unverified",
            (
                "Restart completed and health check passed."
                if healthy_after
                else (
                    "Restart completed, but health recovery "
                    "was not verified."
                )
            ),
            outage_confirmed=outage_confirmed,
            health_before=healthy_before,
            health_after=healthy_after,
            helper_output=stdout.decode(errors="replace").strip(),
            attempt_count=reservation["attempt_count"],
        )

        update_attempt_result(
            audit_id=audit_id,
            status=result["status"],
            message=result["message"],
        )
        write_audit_record(result)
        return result

    except (OSError, asyncio.TimeoutError) as exc:
        result = make_record(
            audit_id,
            service,
            incident_id,
            "failed",
            str(exc),
            outage_confirmed=outage_confirmed,
            health_before=healthy_before,
            attempt_count=reservation["attempt_count"],
        )

        update_attempt_result(
            audit_id=audit_id,
            status="failed",
            message=result["message"],
        )
        write_audit_record(result)
        return result


async def remediate_service(
    service: str,
    approved: bool,
    incident_id: str,
) -> dict:
    """Serialize remediation requests for each allowlisted service."""
    if service not in SERVICE_LOCKS:
        raise ValueError("Service is not allowlisted")

    async with SERVICE_LOCKS[service]:
        return await _remediate_service_locked(
            service=service,
            approved=approved,
            incident_id=incident_id,
        )
