
import httpx

PROMETHEUS_URL = "http://localhost:9090"


async def query_prometheus(query: str) -> list:
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
        )
        response.raise_for_status()
        result = response.json()

    if result.get("status") != "success":
        raise RuntimeError("Prometheus query failed")

    return result["data"]["result"]


async def diagnose_service(service: str) -> dict:
    # 1. Check service availability.
    availability = await query_prometheus(
        f'up{{job="{service}"}}'
    )

    # 2. Check the HTTP 5xx error rate over five minutes.
    errors = await query_prometheus(
        f'sum(rate(http_requests_total{{job="{service}",status=~"5.."}}[5m]))'
    )

    # 3. Check the 95th percentile request latency.
    latency = await query_prometheus(
        f'histogram_quantile(0.95, '
        f'sum by (le) (rate(http_request_duration_seconds_bucket'
        f'{{job="{service}"}}[5m])))'
    )

    is_up = bool(availability) and float(
        availability[0]["value"][1]
    ) == 1.0

    error_rate = (
        float(errors[0]["value"][1])
        if errors and errors[0]["value"][1] != ""
        else 0.0
    )

    p95_latency = (
        float(latency[0]["value"][1])
        if latency and latency[0]["value"][1] != ""
        else None
    )

    findings = []

    if not is_up:
        findings.append({
            "type": "service_unavailable",
            "severity": "critical",
            "evidence": "Prometheus reports the service as DOWN.",
            "recommendation": (
                "Inspect container status and application logs."
            ),
        })

    if error_rate > 0:
        findings.append({
            "type": "http_5xx_errors",
            "severity": "high",
            "evidence": (
                f"HTTP 5xx responses are occurring at "
                f"{error_rate:.4f} requests/second."
            ),
            "recommendation": (
                "Inspect application logs and recent changes."
            ),
        })

    if p95_latency is not None and p95_latency > 1.0:
        findings.append({
            "type": "high_latency",
            "severity": "warning",
            "evidence": (
                f"P95 request latency is {p95_latency:.3f} seconds."
            ),
            "recommendation": (
                "Investigate slow endpoints, database queries, "
                "and resource usage."
            ),
        })

    if not findings:
        findings.append({
            "type": "no_detected_anomaly",
            "severity": "info",
            "evidence": (
                "No outage, active 5xx rate, or P95 latency "
                "above one second was detected by these checks."
            ),
            "recommendation": (
                "Continue monitoring the service."
            ),
        })

    return {
        "service": service,
        "status": "up" if is_up else "down",
        "evidence": {
            "http_5xx_rate_per_second": round(error_rate, 6),
            "p95_latency_seconds": (
                round(p95_latency, 4)
                if p95_latency is not None
                else None
            ),
        },
        "findings": findings,
    }
