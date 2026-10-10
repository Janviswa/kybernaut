
import hashlib

import httpx

from app.services.incident_repository import (
    record_incident,
    resolve_open_incidents,
)

PROMETHEUS_URL = "http://localhost:9090"


def make_incident_id(service: str, incident_type: str) -> str:
    """Generate a stable ID for a service and incident type."""
    raw = f"{service}:{incident_type}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return f"inc-{digest}"


async def detect_unhealthy_services() -> dict:
    query = 'up{job=~".*-service"}'

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
        )
        response.raise_for_status()
        result = response.json()

    if result.get("status") != "success":
        raise RuntimeError("Prometheus query failed")

    incidents = []

    for item in result["data"]["result"]:
        metric = item["metric"]
        value = float(item["value"][1])
        service = metric.get("job", "unknown")

        if value == 0:
            incident_type = "service_unavailable"

            incident = {
                "incident_id": make_incident_id(
                    service,
                    incident_type,
                ),
                "service": service,
                "severity": "critical",
                "type": incident_type,
                "message": f"{service} is down",
            }

            # Persist the incident or refresh its existing open record.
            record_incident(incident)
            incidents.append(incident)

        else:
            # Resolve open incidents when the service recovers.
            resolve_open_incidents(service)

    return {
        "incident_count": len(incidents),
        "incidents": incidents,
    }

