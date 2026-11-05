
import httpx

from app.services.prometheus_features import _sample_timestamp

PROMETHEUS_URL = "http://localhost:9090"


async def query_prometheus(query: str) -> list:
    """Query Prometheus and return the instant-query results."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
        )
        response.raise_for_status()
        result = response.json()

    if not isinstance(result, dict) or result.get("status") != "success":
        raise RuntimeError("Prometheus query failed")

    data = result.get("data")
    if not isinstance(data, dict) or not isinstance(
        data.get("result"), list
    ):
        raise RuntimeError("Prometheus query response is invalid")

    return data["result"]


def _metric_sample(
    results: list, name: str
) -> tuple[float | None, str | None, bool]:
    if not results:
        return None, None, False
    sample = results[0]
    value = sample.get("value") if isinstance(sample, dict) else None
    if not isinstance(value, list) or len(value) < 2:
        raise RuntimeError(f"Prometheus {name} metric is invalid")
    if value[1] == "":
        return None, None, False
    try:
        parsed = float(value[1])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Prometheus {name} metric is invalid") from exc
    if not (0.0 <= parsed < float("inf")):
        raise RuntimeError(f"Prometheus {name} metric is invalid")
    try:
        timestamp = _sample_timestamp(value[0]).isoformat()
    except RuntimeError:
        return parsed, None, True
    return parsed, timestamp, False


def _http_5xx_sample(
    results: list,
) -> tuple[float | None, str | None, bool]:
    """Derive 5xx rate from the instrumentator's observed status classes."""
    if not results:
        return None, None, False

    rates: list[float] = []
    timestamps: list[str] = []
    timestamp_invalid = False
    for result in results:
        metric = result.get("metric") if isinstance(result, dict) else None
        status_class = metric.get("status") if isinstance(metric, dict) else None

        if metric is not None and not isinstance(metric, dict):
            continue
        if status_class not in {"2xx", "3xx", "4xx", "5xx", None}:
            continue

        rate, timestamp, invalid = _metric_sample([result], "HTTP request rate")
        if rate is None:
            continue
        if timestamp is None:
            timestamp_invalid = True
        else:
            timestamps.append(timestamp)
        timestamp_invalid = timestamp_invalid or invalid

        if status_class == "5xx" or status_class is None:
            rates.append(rate)

    if not timestamps:
        return (sum(rates) if rates else None), None, timestamp_invalid
    # A missing 5xx class means zero only when a fresh instrumented request
    # series exists for the same service.
    return sum(rates), min(timestamps), timestamp_invalid


async def diagnose_service(service: str) -> dict:
    """Diagnose availability, HTTP 5xx errors, and P95 latency."""

    # 1. Check service availability.
    availability = await query_prometheus(
        f'up{{job="{service}"}}'
    )

    # 2. Read the actual status classes emitted by prometheus-fastapi-instrumentator.
    errors = await query_prometheus(
        f'sum by (status) (rate(http_requests_total'
        f'{{job="{service}",handler!="/metrics"}}[5m]))'
    )

    # 3. Check the 95th percentile request latency.
    latency = await query_prometheus(
        f'histogram_quantile(0.95, '
        f'sum by (le) (rate(http_request_duration_seconds_bucket'
        f'{{job="{service}"}}[5m])))'
    )

    # Distinguish a confirmed outage from missing availability data.
    is_up = None

    (
        availability_value,
        availability_timestamp,
        availability_timestamp_invalid,
    ) = _metric_sample(availability, "availability")
    if availability_value is not None:
        if availability_value == 1.0:
            is_up = True
        elif availability_value == 0.0:
            is_up = False
        else:
            raise RuntimeError(
                "Prometheus availability metric is invalid"
            )

    # Missing vectors are unavailable evidence, not a measured zero.
    error_rate = None
    missing_evidence = []
    if not availability:
        missing_evidence.append("service_availability")
    if not errors:
        missing_evidence.append("http_5xx_rate")
    if not latency:
        missing_evidence.append("p95_latency")

    error_rate, errors_timestamp, errors_timestamp_invalid = (
        _http_5xx_sample(errors)
    )
    if availability and availability_value is None:
        missing_evidence.append("service_availability")

    # Parse P95 latency. Missing latency data remains unknown.
    p95_latency, latency_timestamp, latency_timestamp_invalid = _metric_sample(
        latency, "latency"
    )

    evidence_timestamps = {
        "service_availability": {
            "timestamp": availability_timestamp,
            "provenance": "prometheus_sample_timestamp",
        },
        "http_5xx_rate": {
            "timestamp": errors_timestamp,
            "provenance": "instant_query_result_timestamp",
        },
        "p95_latency": {
            "timestamp": latency_timestamp,
            "provenance": "instant_query_result_timestamp",
        },
    }
    timestamp_invalid = (
        availability_timestamp_invalid
        or errors_timestamp_invalid
        or latency_timestamp_invalid
    )
    for name, timestamp in (
        ("service_availability", availability_timestamp),
        ("http_5xx_rate", errors_timestamp),
        ("p95_latency", latency_timestamp),
    ):
        if timestamp is None and name not in missing_evidence:
            missing_evidence.append(name + "_timestamp")

    if errors and error_rate is None:
        missing_evidence.append("http_5xx_rate")
    if latency and p95_latency is None:
        missing_evidence.append("p95_latency")
    if p95_latency is not None and not (
        0.0 <= p95_latency < float("inf")
    ):
        raise RuntimeError("Prometheus latency metric is invalid")

    findings = []

    if is_up is False:
        findings.append({
            "type": "service_unavailable",
            "severity": "critical",
            "evidence": "Prometheus reports the service as DOWN.",
            "recommendation": (
                "Inspect container status and application logs."
            ),
        })

    elif is_up is None:
        findings.append({
            "type": "monitoring_data_unavailable",
            "severity": "warning",
            "evidence": (
                "Prometheus returned no availability sample; "
                "service status cannot be confirmed."
            ),
            "recommendation": (
                "Check Prometheus target health and metric collection."
            ),
        })

    if error_rate is not None and error_rate > 0:
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

    if missing_evidence and not any(
        finding["type"] == "monitoring_data_unavailable"
        for finding in findings
    ):
        findings.append({
            "type": "monitoring_data_unavailable",
            "severity": "warning",
            "evidence": (
                "Prometheus returned incomplete evidence: "
                + ", ".join(missing_evidence)
            ),
            "recommendation": (
                "Check metric availability and scrape configuration before "
                "treating the service as healthy."
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
            "recommendation": "Continue monitoring the service.",
        })

    return {
        "service": service,
        "status": (
            "up" if is_up is True
            else "down" if is_up is False
            else "unknown"
        ),
        "evidence_status": (
            "invalid" if timestamp_invalid
            else "available" if not missing_evidence
            else "unavailable"
        ),
        "missing_evidence": missing_evidence,
        "evidence_timestamps": evidence_timestamps,
        "evidence": {
            "http_5xx_rate_per_second": (
                round(error_rate, 6) if error_rate is not None else None
            ),
            "p95_latency_seconds": (
                round(p95_latency, 4)
                if p95_latency is not None
                else None
            ),
        },
        "findings": findings,
    }
