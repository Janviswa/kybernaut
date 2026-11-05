
"""Read-only Prometheus telemetry collector for Kybernaut ML."""

import math
import os
from datetime import datetime, timezone

import httpx

PROMETHEUS_URL = "http://localhost:9090"
DEFAULT_MAX_SAMPLE_AGE_SECONDS = 30.0
DEFAULT_MAX_SAMPLE_SKEW_SECONDS = 10.0

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
    values, _ = _parse_timestamped_vector(payload, require_timestamps=False)
    return values


def _sample_timestamp(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError("Prometheus sample timestamp is invalid")
    if not math.isfinite(value):
        raise RuntimeError("Prometheus sample timestamp is invalid")
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise RuntimeError("Prometheus sample timestamp is invalid") from exc


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def freshness_limits_from_environment() -> tuple[float, float]:
    try:
        max_age = float(
            os.getenv(
                "KYBERNAUT_PROMETHEUS_MAX_SAMPLE_AGE_SECONDS",
                str(DEFAULT_MAX_SAMPLE_AGE_SECONDS),
            )
        )
    except ValueError as exc:
        raise ValueError(
            "KYBERNAUT_PROMETHEUS_MAX_SAMPLE_AGE_SECONDS must be positive"
        ) from exc
    if not math.isfinite(max_age) or max_age <= 0:
        raise ValueError(
            "KYBERNAUT_PROMETHEUS_MAX_SAMPLE_AGE_SECONDS must be positive"
        )

    try:
        max_skew = float(
            os.getenv(
                "KYBERNAUT_PROMETHEUS_MAX_SAMPLE_SKEW_SECONDS",
                str(DEFAULT_MAX_SAMPLE_SKEW_SECONDS),
            )
        )
    except ValueError as exc:
        raise ValueError(
            "KYBERNAUT_PROMETHEUS_MAX_SAMPLE_SKEW_SECONDS must be positive"
        ) from exc
    if not math.isfinite(max_skew) or max_skew <= 0:
        raise ValueError(
            "KYBERNAUT_PROMETHEUS_MAX_SAMPLE_SKEW_SECONDS must be positive"
        )
    return max_age, max_skew


def _parse_timestamped_vector(
    payload: object,
    *,
    ignore_invalid_values: bool = False,
    require_timestamps: bool = True,
) -> tuple[dict[str, float], dict[str, datetime]]:
    if not isinstance(payload, dict) or payload.get("status") != "success":
        raise RuntimeError("Prometheus query failed")

    data = payload.get("data")
    if not isinstance(data, dict) or data.get("resultType") != "vector":
        raise RuntimeError("Prometheus response is not a vector")

    results = data.get("result")
    if not isinstance(results, list):
        raise RuntimeError("Prometheus result list is invalid")

    values: dict[str, float] = {}
    timestamps: dict[str, datetime] = {}

    for item in results:
        if not isinstance(item, dict):
            raise RuntimeError("Prometheus result item is invalid")
        metric = item.get("metric")
        sample = item.get("value")
        if not isinstance(metric, dict):
            raise RuntimeError("Prometheus metric labels are invalid")

        service = metric.get("job")
        if service not in EXPECTED_SERVICES:
            continue
        if service in values:
            raise RuntimeError(f"Duplicate Prometheus series for {service}")
        if not isinstance(sample, list) or len(sample) < 2:
            if ignore_invalid_values:
                continue
            raise RuntimeError("Prometheus sample is invalid")

        timestamp = (
            _sample_timestamp(sample[0]) if require_timestamps else None
        )
        if isinstance(sample[1], bool):
            raise RuntimeError("Prometheus sample is not numeric")
        try:
            value = float(sample[1])
        except (TypeError, ValueError) as exc:
            if ignore_invalid_values:
                continue
            raise RuntimeError("Prometheus sample is not numeric") from exc
        if not math.isfinite(value) or value < 0:
            if ignore_invalid_values:
                continue
            raise RuntimeError("Prometheus sample is invalid")

        values[service] = value
        if timestamp is not None:
            timestamps[service] = timestamp

    return values, timestamps


def parse_latency_response(payload: object) -> dict[str, float]:
    """Parse valid p95 values while ignoring NaN or infinite results.

    An unavailable latency measurement is omitted, never replaced with zero.
    """
    values, _ = _parse_timestamped_vector(
        payload,
        ignore_invalid_values=True,
        require_timestamps=False,
    )
    return values


async def collect_service_features(
    client: httpx.AsyncClient,
    *,
    required_features: tuple[str, ...] = FEATURE_NAMES,
) -> dict[str, dict]:
    """Collect fresh feature rows without substituting missing measurements."""
    required_features = tuple(required_features)
    if not required_features or any(
        name not in FEATURE_NAMES for name in required_features
    ):
        raise ValueError("required_features must contain known feature names")

    collected: dict[str, dict[str, float]] = {}
    timestamps: dict[str, dict[str, datetime]] = {}

    for feature_name, query in QUERIES.items():
        response = await client.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
        )
        response.raise_for_status()
        payload = response.json()

        feature_values, feature_timestamps = _parse_timestamped_vector(
            payload,
            ignore_invalid_values=feature_name == "p95_latency_seconds",
        )

        for service, value in feature_values.items():
            collected.setdefault(service, {})[feature_name] = value
            timestamps.setdefault(service, {})[feature_name] = (
                feature_timestamps[service]
            )

    max_age, max_skew = freshness_limits_from_environment()
    now = _utcnow()
    rows: dict[str, dict] = {}

    for service in EXPECTED_SERVICES:
        features = collected.get(service, {})
        feature_times = timestamps.get(service, {})

        if any(
            name not in features or name not in feature_times
            for name in required_features
        ):
            continue

        if any(
            not math.isfinite(features[name]) or features[name] < 0
            for name in required_features
        ):
            continue

        required_times = [feature_times[name] for name in required_features]
        if any(
            (now - timestamp).total_seconds() < 0
            or (now - timestamp).total_seconds() > max_age
            for timestamp in required_times
        ):
            continue
        if (max(required_times) - min(required_times)).total_seconds() > max_skew:
            continue

        row_features = {}
        row_timestamps = {}
        for name, value in features.items():
            timestamp = feature_times[name]
            age = (now - timestamp).total_seconds()
            if age < 0 or age > max_age:
                continue
            if (
                name not in required_features
                and max(
                    abs((timestamp - required_time).total_seconds())
                    for required_time in required_times
                ) > max_skew
            ):
                continue
            row_features[name] = value
            row_timestamps[name] = timestamp.isoformat()

        rows[service] = {
            **row_features,
            "sample_timestamps": row_timestamps,
            "observed_at": max(
                datetime.fromisoformat(value)
                for value in row_timestamps.values()
            ).isoformat(),
        }

    return rows
