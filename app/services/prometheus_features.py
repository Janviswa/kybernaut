
"""Read-only Prometheus telemetry collector for Kybernaut ML."""

import math

import httpx

PROMETHEUS_URL = "http://localhost:9090"

EXPECTED_SERVICES = (
    "order-service",
    "payment-service",
    "user-service",
    "inventory-service",
)

FEATURE_NAMES = (
    "request_rate",
    "p95_latency_seconds",
    "memory_bytes",
    "cpu_cores_used",
)

QUERIES = {
    "request_rate": (
        'sum by (job) (rate(http_requests_total'
        '{job=~".*-service",handler!="/metrics"}[5m]))'
    ),
    "p95_latency_seconds": (
        'histogram_quantile(0.95, '
        'sum by (job, le) (rate(http_request_duration_seconds_bucket'
        '{job=~".*-service",handler!="/metrics"}[5m])))'
    ),
    "memory_bytes": (
        'process_resident_memory_bytes{job=~".*-service"}'
    ),
    "cpu_cores_used": (
        'sum by (job) (rate(process_cpu_seconds_total'
        '{job=~".*-service"}[5m]))'
    ),
}


def parse_vector_response(payload: object) -> dict[str, float]:
    """Validate a Prometheus instant-query response."""
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise RuntimeError("Prometheus query failed")

    data = payload.get("data")
    if not isinstance(data, dict) or data.get("resultType") != "vector":
        raise RuntimeError("Prometheus response is not a vector")

    results = data.get("result")
    if not isinstance(results, list):
        raise RuntimeError("Prometheus result list is invalid")

    values: dict[str, float] = {}

    for item in results:
        if not isinstance(item, dict):
            raise RuntimeError("Prometheus result item is invalid")

        metric = item.get("metric")
        sample = item.get("value")

        if not isinstance(metric, dict):
            raise RuntimeError("Prometheus metric labels are invalid")

        job = metric.get("job")
        if job not in EXPECTED_SERVICES:
            continue

        if job in values:
            raise RuntimeError(f"Duplicate Prometheus series for {job}")

        if not isinstance(sample, list) or len(sample) < 2:
            raise RuntimeError("Prometheus sample is invalid")

        try:
            value = float(sample[1])
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Prometheus sample is not numeric") from exc

        if not math.isfinite(value) or value < 0:
            raise RuntimeError("Prometheus sample is invalid")

        values[job] = value

    return values


def parse_latency_response(payload: object) -> dict[str, float]:
    """Parse valid p95 values while ignoring NaN or infinite results.

    An unavailable latency measurement is omitted, never replaced with zero.
    """
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise RuntimeError("Prometheus latency query failed")

    data = payload.get("data")
    if not isinstance(data, dict) or data.get("resultType") != "vector":
        raise RuntimeError("Prometheus latency response is not a vector")

    results = data.get("result")
    if not isinstance(results, list):
        raise RuntimeError("Prometheus latency result list is invalid")

    values: dict[str, float] = {}

    for item in results:
        if not isinstance(item, dict):
            raise RuntimeError("Prometheus latency result item is invalid")

        metric = item.get("metric")
        sample = item.get("value")

        if not isinstance(metric, dict):
            raise RuntimeError("Prometheus latency labels are invalid")

        job = metric.get("job")
        if job not in EXPECTED_SERVICES:
            continue

        if not isinstance(sample, list) or len(sample) < 2:
            continue

        try:
            value = float(sample[1])
        except (TypeError, ValueError):
            continue

        # Ignore unavailable measurements; never manufacture a latency.
        if not math.isfinite(value) or value < 0:
            continue

        if job in values:
            raise RuntimeError(f"Duplicate latency series for {job}")

        values[job] = value

    return values


async def collect_service_features(
    client: httpx.AsyncClient,
) -> dict[str, dict[str, float]]:
    """Collect complete feature rows without substituting missing metrics."""
    collected: dict[str, dict[str, float]] = {}

    for feature_name, query in QUERIES.items():
        response = await client.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
        )
        response.raise_for_status()
        payload = response.json()

        if feature_name == "p95_latency_seconds":
            feature_values = parse_latency_response(payload)
        else:
            feature_values = parse_vector_response(payload)

        for service, value in feature_values.items():
            collected.setdefault(service, {})[feature_name] = value

    rows: dict[str, dict[str, float]] = {}

    for service in EXPECTED_SERVICES:
        features = collected.get(service, {})

        # Skip services missing any feature, including unavailable p95 latency.
        if any(name not in features for name in FEATURE_NAMES):
            continue

        if any(
            not math.isfinite(features[name]) or features[name] < 0
            for name in FEATURE_NAMES
        ):
            continue

        rows[service] = {
            name: features[name]
            for name in FEATURE_NAMES
        }

    return rows
