
import hashlib

import httpx

from app.services.incident_repository import (
    record_incident,
    resolve_open_incidents,
)

PROMETHEUS_URL = "http://localhost:9090"

EXPECTED_SERVICES = {
    "order-service",
    "payment-service",
    "user-service",
    "inventory-service",
}


def make_incident_id(service: str, incident_type: str) -> str:
    """Generate a stable ID for a service and incident type."""
    raw = f"{service}:{incident_type}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:12]
    return f"inc-{digest}"


async def detect_unhealthy_services() -> dict:
    """Detect service outages and missing Prometheus targets."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        query_response = await client.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": 'up{job=~".*-service"}'},
        )
        query_response.raise_for_status()
        query_result = query_response.json()

        if query_result.get("status") != "success":
            raise RuntimeError("Prometheus query failed")

        query_results = query_result.get("data", {}).get("result", [])

        # Empty query results are inconclusive. Do not resolve incidents
        # or infer missing targets without a valid health response.
        if not query_results:
            return {"incident_count": 0, "incidents": []}

        targets_response = await client.get(
            f"{PROMETHEUS_URL}/api/v1/targets",
        )
        targets_response.raise_for_status()
        targets_result = targets_response.json()

    if targets_result.get("status") != "success":
        raise RuntimeError("Prometheus targets query failed")

    targets_data = targets_result.get("data", {})
    active_targets = targets_data.get("activeTargets")

    if not isinstance(active_targets, list):
        raise RuntimeError("Prometheus target inventory is invalid")

    # Only consider active targets whose job names are expected.
    active_jobs = {
        target.get("labels", {}).get("job")
        for target in active_targets
        if isinstance(target, dict)
    }

    incidents = []
    observed_services = set()

    for item in query_results:
        metric = item.get("metric", {})
        service = metric.get("job")

        if service not in EXPECTED_SERVICES:
            continue

        observed_services.add(service)
        value = float(item["value"][1])

        if value == 0:
            incident_type = "service_unavailable"
            incident = {
                "incident_id": make_incident_id(
                    service, incident_type
                ),
                "service": service,
                "severity": "critical",
                "type": incident_type,
                "message": f"{service} is down",
            }
            record_incident(incident)
            incidents.append(incident)

        elif value == 1:
            resolve_open_incidents(service)

    # A missing active target is a monitoring warning, not proof that
    # the application itself is down.
    for service in sorted(EXPECTED_SERVICES - active_jobs):
        incident_type = "monitoring_target_missing"
        incident = {
            "incident_id": make_incident_id(
                service, incident_type
            ),
            "service": service,
            "severity": "warning",
            "type": incident_type,
            "message": (
                f"Prometheus has no active target for {service}; "
                "application health is unknown"
            ),
        }
        record_incident(incident)
        incidents.append(incident)

    return {
        "incident_count": len(incidents),
        "incidents": incidents,
    }
