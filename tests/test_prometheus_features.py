import httpx
import pytest

from app.services.prometheus_features import (
    EXPECTED_SERVICES,
    collect_service_features,
    parse_vector_response,
)


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


@pytest.mark.asyncio
async def test_collector_returns_complete_rows_only():
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
                            "value": [123, value],
                        }
                        for service in services
                    ],
                },
            },
        )

    transport = httpx.MockTransport(handler)

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
    }
    assert "inventory-service" not in rows
