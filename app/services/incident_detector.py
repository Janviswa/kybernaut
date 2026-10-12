
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


def build_incident(
    service: str,
    incident_type: str,
    severity: str,
    message: str,
) -> dict:
    """Build a consistently structured incident."""
    return {
        "incident_id": make_incident_id(service, incident_type),
        "service": service,
        "severity": severity,
        "type": incident_type,
        "message": message,
    }


async def detect_unhealthy_services() -> dict:
    """Detect outages, failed scrapes, and missing targets."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        query_response = await client.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": 'up{job=~".*-service"}'},
        )
        query_response.raise_for_status()
        query_result = query_response.json()

        if (
            not isinstance(query_result, dict)
            or query_result.get("status") != "success"
        ):
            raise RuntimeError("Prometheus query failed")

        data = query_result.get("data")
        if not isinstance(data, dict) or not isinstance(
            data.get("result"), list
        ):
            raise RuntimeError("Prometheus query response is invalid")

        query_results = data["result"]

        # An empty health query is inconclusive. Do not resolve incidents
        # or infer missing targets from an empty result.
        if not query_results:
            return {"incident_count": 0, "incidents": []}

        targets_response = await client.get(
            f"{PROMETHEUS_URL}/api/v1/targets",
        )
        targets_response.raise_for_status()
        targets_result = targets_response.json()

    if (
        not isinstance(targets_result, dict)
        or targets_result.get("status") != "success"
    ):
        raise RuntimeError("Prometheus targets query failed")

    targets_data = targets_result.get("data")
    if not isinstance(targets_data, dict):
        raise RuntimeError("Prometheus target inventory is invalid")

    active_targets = targets_data.get("activeTargets")
    if not isinstance(active_targets, list):
        raise RuntimeError("Prometheus target inventory is invalid")

    target_by_service = {}

    for target in active_targets:
        if not isinstance(target, dict):
            raise RuntimeError("Prometheus target inventory is invalid")

        labels = target.get("labels")
        if not isinstance(labels, dict):
            raise RuntimeError("Prometheus target labels are invalid")

        service = labels.get("job")
        if service in EXPECTED_SERVICES:
            target_by_service[service] = target

    incidents = []
    observed_services = set()

    for item in query_results:
        if not isinstance(item, dict):
            raise RuntimeError("Prometheus query result is invalid")

        metric = item.get("metric")
        value_data = item.get("value")

        if not isinstance(metric, dict):
            raise RuntimeError("Prometheus metric data is invalid")

        if (
            not isinstance(value_data, list)
            or len(value_data) < 2
        ):
            raise RuntimeError("Prometheus metric value is invalid")

        service = metric.get("job")

        if service not in EXPECTED_SERVICES:
            continue

        observed_services.add(service)
        target = target_by_service.get(service)

        try:
            value = float(value_data[1])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "Prometheus metric value is invalid"
            ) from exc

        if value not in (0.0, 1.0):
            raise RuntimeError("Prometheus metric value is invalid")

        # A missing active target is a monitoring warning, even if the
        # instant query still contains a recent sample for that service.
        if target is None:
            continue

        target_health = target.get("health")

        if target_health not in ("up", "down"):
            raise RuntimeError("Prometheus target health is invalid")

        if target_health == "down":
            incident = build_incident(
                service,
                "monitoring_scrape_failed",
                "warning",
                (
                    f"Prometheus scrape failed for {service}; "
                    "application health is not confirmed"
                ),
            )
            record_incident(incident)
            incidents.append(incident)
            continue

        # Only an active, healthy target with up=0 is classified as a
        # critical service outage.
        if value == 0:
            incident = build_incident(
                service,
                "service_unavailable",
                "critical",
                f"{service} is down",
            )
            record_incident(incident)
            incidents.append(incident)

        elif value == 1:
            resolve_open_incidents(service, "service_unavailable")

    # Report services absent from the active target inventory.
    for service in sorted(EXPECTED_SERVICES - set(target_by_service)):
        incident = build_incident(
            service,
            "monitoring_target_missing",
            "warning",
            (
                f"Prometheus has no active target for {service}; "
                "application health is unknown"
            ),
        )
        record_incident(incident)
        incidents.append(incident)

    return {
        "incident_count": len(incidents),
        "incidents": incidents,
    }
