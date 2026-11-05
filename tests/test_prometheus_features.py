import httpx
import pytest

from app.services import prometheus_features
from app.services.prometheus_features import (
    EXPECTED_SERVICES,
    collect_service_features,
    parse_vector_response,
)
from app.services.live_cost_estimates import build_live_cost_estimate


def test_parse_valid_prometheus_vector():
    payload = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {"job": "order-service"},
                    "value": [123, "0.25"],
                }
            ],
        },
    }

    assert parse_vector_response(payload) == {"order-service": 0.25}


def test_reject_failed_prometheus_response():
    with pytest.raises(RuntimeError, match="query failed"):
        parse_vector_response({"status": "error"})


def test_reject_non_finite_values():
    payload = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {"job": "order-service"},
                    "value": [123, "NaN"],
                }
            ],
        },
    }

    with pytest.raises(RuntimeError, match="not numeric|invalid"):
        parse_vector_response(payload)


@pytest.mark.parametrize("value", [True, False])
def test_reject_boolean_prometheus_sample_values(value):
    payload = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{
                "metric": {"job": "order-service"},
                "value": [123, value],
            }],
        },
    }

    with pytest.raises(RuntimeError, match="not numeric"):
        parse_vector_response(payload)


@pytest.mark.asyncio
async def test_collector_returns_complete_rows_only(monkeypatch):
    timestamp = 123

    def handler(request):
        query = request.url.params["query"]

        if "http_requests_total" in query:
            value = "0.2"
        elif "histogram_quantile" in query:
            value = "0.095"
        elif "process_resident_memory_bytes" in query:
            value = "49000000"
        else:
            value = "0.02"

        # Deliberately omit inventory-service from the memory query.
        services = EXPECTED_SERVICES
        if "process_resident_memory_bytes" in query:
            services = tuple(
                service for service in services
                if service != "inventory-service"
            )

        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {
                            "metric": {"job": service},
                            "value": [timestamp, value],
                        }
                        for service in services
                    ],
                },
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        prometheus_features,
        "_utcnow",
        lambda: prometheus_features._sample_timestamp(timestamp),
    )

    async with httpx.AsyncClient(transport=transport) as client:
        rows = await collect_service_features(client)

    assert set(rows) == {
        "order-service",
        "payment-service",
        "user-service",
    }
    assert rows["order-service"] == {
        "request_rate": 0.2,
        "p95_latency_seconds": 0.095,
        "memory_bytes": 49_000_000.0,
        "cpu_cores_used": 0.02,
        "sample_timestamps": {
            feature: "1970-01-01T00:02:03+00:00"
            for feature in prometheus_features.FEATURE_NAMES
        },
        "observed_at": "1970-01-01T00:02:03+00:00",
    }
    assert "inventory-service" not in rows


@pytest.mark.asyncio
async def test_cost_collection_keeps_cpu_and_memory_when_latency_is_unavailable(
    monkeypatch,
):
    now = prometheus_features._sample_timestamp(1_700_000_000)
    monkeypatch.setenv("KYBERNAUT_ENVIRONMENT", "local-demo")

    def handler(request):
        feature = _feature_for_query(request.url.params["query"])
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {
                            "metric": {"job": service},
                            "value": [
                                now.timestamp(),
                                "NaN" if (
                                    feature == "p95_latency_seconds"
                                    and service == "user-service"
                                ) else "0.1",
                            ],
                        }
                        for service in EXPECTED_SERVICES
                    ],
                },
            },
        )

    monkeypatch.setattr(prometheus_features, "_utcnow", lambda: now)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        rows = await collect_service_features(
            client,
            required_features=("cpu_cores_used", "memory_bytes"),
        )

    user = rows["user-service"]
    assert user["cpu_cores_used"] == 0.1
    assert user["memory_bytes"] == 0.1
    assert user["request_rate"] == 0.1
    assert "p95_latency_seconds" not in user
    assert set(user["sample_timestamps"]) == {
        "request_rate", "memory_bytes", "cpu_cores_used",
    }
    estimate = build_live_cost_estimate(rows)
    assert estimate["status"] == "estimated"
    assert (
        estimate["services"]["user-service"]["estimated_hourly_cost_usd"]
        is not None
    )
    assert estimate["summary"]["estimated_hourly_cost_usd"] is not None


def _feature_for_query(query):
    if "http_requests_total" in query:
        return "request_rate"
    if "histogram_quantile" in query:
        return "p95_latency_seconds"
    if "process_resident_memory_bytes" in query:
        return "memory_bytes"
    return "cpu_cores_used"


@pytest.mark.asyncio
async def test_collector_preserves_fresh_complete_sample_timestamps(monkeypatch):
    now = prometheus_features._sample_timestamp(1_700_000_000)

    def handler(request):
        feature = _feature_for_query(request.url.params["query"])
        value = "0.095" if feature == "p95_latency_seconds" else "1"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{
                        "metric": {"job": "order-service"},
                        "value": [now.timestamp(), value],
                    }],
                },
            },
        )

    monkeypatch.setattr(prometheus_features, "_utcnow", lambda: now)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        rows = await collect_service_features(client)

    row = rows["order-service"]
    assert row["observed_at"] == now.isoformat()
    assert set(row["sample_timestamps"]) == set(
        prometheus_features.FEATURE_NAMES
    )
    assert all(
        row["sample_timestamps"][name] == now.isoformat()
        for name in prometheus_features.FEATURE_NAMES
    )


@pytest.mark.asyncio
async def test_collector_omits_stale_sample_rows(monkeypatch):
    now = prometheus_features._sample_timestamp(1_700_000_000)
    monkeypatch.setenv("KYBERNAUT_PROMETHEUS_MAX_SAMPLE_AGE_SECONDS", "30")

    def handler(request):
        feature = _feature_for_query(request.url.params["query"])
        sample_time = now.timestamp() - (31 if feature == "memory_bytes" else 1)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{
                        "metric": {"job": "order-service"},
                        "value": [sample_time, "1"],
                    }],
                },
            },
        )

    monkeypatch.setattr(prometheus_features, "_utcnow", lambda: now)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        rows = await collect_service_features(client)

    assert "order-service" not in rows


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_timestamp", ["missing", None, "not-a-timestamp", "nan"]
)
async def test_collector_rejects_missing_or_invalid_timestamps(
    monkeypatch, bad_timestamp
):
    now = prometheus_features._sample_timestamp(1_700_000_000)

    def handler(request):
        timestamp = (
            bad_timestamp
            if _feature_for_query(request.url.params["query"]) == "memory_bytes"
            else now.timestamp()
        )
        sample = (
            ["1"]
            if timestamp == "missing"
            else [timestamp, "1"]
        )
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{
                        "metric": {"job": "order-service"},
                        "value": sample,
                    }],
                },
            },
        )

    monkeypatch.setattr(prometheus_features, "_utcnow", lambda: now)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(
            RuntimeError, match="sample is invalid|timestamp is invalid"
        ):
            await collect_service_features(client)


@pytest.mark.asyncio
async def test_collector_omits_rows_with_inconsistent_sample_timestamps(
    monkeypatch,
):
    now = prometheus_features._sample_timestamp(1_700_000_000)
    monkeypatch.setenv("KYBERNAUT_PROMETHEUS_MAX_SAMPLE_AGE_SECONDS", "30")
    monkeypatch.setenv("KYBERNAUT_PROMETHEUS_MAX_SAMPLE_SKEW_SECONDS", "10")

    def handler(request):
        feature = _feature_for_query(request.url.params["query"])
        sample_time = now.timestamp() - (12 if feature == "memory_bytes" else 1)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [{
                        "metric": {"job": "order-service"},
                        "value": [sample_time, "1"],
                    }],
                },
            },
        )

    monkeypatch.setattr(prometheus_features, "_utcnow", lambda: now)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    ) as client:
        rows = await collect_service_features(client)

    assert "order-service" not in rows
