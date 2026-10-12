
import pytest
import httpx

import app.database as database
from app.services.incident_detector import detect_unhealthy_services
from app.services.incident_repository import (
    list_incidents,
    record_incident,
)


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """Use a temporary database for each test."""
    test_db = tmp_path / "test_incidents.db"
    monkeypatch.setattr(database, "DATABASE_PATH", test_db)
    database.initialize_database()


class MockResponse:
    def __init__(self, data, status_code=200):
        self.data = data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "Prometheus request failed",
                request=httpx.Request(
                    "GET",
                    "http://localhost:9090",
                ),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self.data


class MockAsyncClient:
    """Mock HTTP client supporting sequential responses."""

    responses = []
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, url, *args, **kwargs):
        self.calls.append((url, kwargs))

        if not self.responses:
            raise AssertionError(
                f"Unexpected HTTP request: {url}"
            )

        return self.responses.pop(0)


def prometheus_response(service, value):
    """Build a mock Prometheus instant-query response."""
    return {
        "status": "success",
        "data": {
            "result": [
                {
                    "metric": {"job": service},
                    "value": [0, str(value)],
                }
            ]
        },
    }


def prometheus_targets_response(services=None):
    """Build a mock Prometheus targets API response."""
    if services is None:
        services = [
            ("order-service", "order-service:8001"),
            ("payment-service", "payment-service:8002"),
            ("user-service", "user-service:8003"),
            ("inventory-service", "inventory-service:8004"),
        ]

    return {
        "status": "success",
        "data": {
            "activeTargets": [
                {
                    "labels": {"job": service},
                    "health": "up",
                    "scrapeUrl": f"http://{instance}/metrics",
                }
                for service, instance in services
            ]
        },
    }


def install_mock(monkeypatch):
    monkeypatch.setattr(
        "app.services.incident_detector.httpx.AsyncClient",
        MockAsyncClient,
    )


@pytest.mark.asyncio
async def test_detector_records_down_service(monkeypatch):
    MockAsyncClient.responses = [
        MockResponse(prometheus_response("user-service", 0)),
        MockResponse(prometheus_targets_response()),
    ]
    MockAsyncClient.calls = []
    install_mock(monkeypatch)

    result = await detect_unhealthy_services()

    assert result["incident_count"] == 1
    assert result["incidents"][0]["service"] == "user-service"
    assert result["incidents"][0]["severity"] == "critical"
    assert result["incidents"][0]["type"] == "service_unavailable"
    assert len(list_incidents(status="OPEN")) == 1


@pytest.mark.asyncio
async def test_detector_resolves_incident_when_service_recovers(
    monkeypatch,
):
    record_incident({
        "incident_id": "inc-test-user-down",
        "service": "user-service",
        "type": "service_unavailable",
        "severity": "critical",
        "message": "user-service is down",
    })

    MockAsyncClient.responses = [
        MockResponse(prometheus_response("user-service", 1)),
        MockResponse(prometheus_targets_response()),
    ]
    MockAsyncClient.calls = []
    install_mock(monkeypatch)

    result = await detect_unhealthy_services()

    assert result["incident_count"] == 0
    assert list_incidents(status="OPEN") == []
    assert len(list_incidents(status="RESOLVED")) == 1



@pytest.mark.asyncio
async def test_detector_preserves_monitoring_warning_when_service_recovers(
    monkeypatch,
):
    record_incident({
        "incident_id": "inc-test-user-monitoring",
        "service": "user-service",
        "type": "monitoring_scrape_failed",
        "severity": "warning",
        "message": "Prometheus scrape failed",
    })

    MockAsyncClient.responses = [
        MockResponse(prometheus_response("user-service", 1)),
        MockResponse(prometheus_targets_response()),
    ]
    MockAsyncClient.calls = []
    install_mock(monkeypatch)

    await detect_unhealthy_services()

    open_incidents = list_incidents(status="OPEN")

    assert any(
        incident["service"] == "user-service"
        and incident["incident_type"] == "monitoring_scrape_failed"
        for incident in open_incidents
    )


@pytest.mark.asyncio
async def test_detector_raises_when_prometheus_query_fails(monkeypatch):
    MockAsyncClient.responses = [
        MockResponse({
            "status": "error",
            "data": {"result": []},
        }),
    ]
    MockAsyncClient.calls = []
    install_mock(monkeypatch)

    with pytest.raises(RuntimeError, match="Prometheus query failed"):
        await detect_unhealthy_services()


@pytest.mark.asyncio
async def test_detector_does_not_resolve_incident_when_query_is_empty(
    monkeypatch,
):
    record_incident({
        "incident_id": "inc-test-user-down",
        "service": "user-service",
        "type": "service_unavailable",
        "severity": "critical",
        "message": "user-service is down",
    })

    MockAsyncClient.responses = [
        MockResponse({
            "status": "success",
            "data": {"result": []},
        }),
    ]
    MockAsyncClient.calls = []
    install_mock(monkeypatch)

    result = await detect_unhealthy_services()

    assert result["incident_count"] == 0
    assert len(list_incidents(status="OPEN")) == 1
    assert list_incidents(status="RESOLVED") == []


@pytest.mark.asyncio
async def test_detector_records_missing_prometheus_target(monkeypatch):
    MockAsyncClient.responses = [
        MockResponse(prometheus_response("user-service", 1)),
        MockResponse(
            prometheus_targets_response(
                services=[
                    ("order-service", "order-service:8001"),
                    ("payment-service", "payment-service:8002"),
                    ("inventory-service", "inventory-service:8004"),
                ]
            )
        ),
    ]
    MockAsyncClient.calls = []
    install_mock(monkeypatch)

    result = await detect_unhealthy_services()

    missing = [
        incident
        for incident in result["incidents"]
        if incident["service"] == "user-service"
        and incident["type"] == "monitoring_target_missing"
    ]

    assert len(missing) == 1
    assert missing[0]["severity"] == "warning"
    assert "health is unknown" in missing[0]["message"]


@pytest.mark.asyncio
async def test_detector_preserves_incidents_when_targets_api_fails(
    monkeypatch,
):
    record_incident({
        "incident_id": "inc-test-user-down",
        "service": "user-service",
        "type": "service_unavailable",
        "severity": "critical",
        "message": "user-service is down",
    })

    MockAsyncClient.responses = [
        MockResponse(prometheus_response("user-service", 1)),
        MockResponse({"status": "error", "data": {}}),
    ]
    MockAsyncClient.calls = []
    install_mock(monkeypatch)

    with pytest.raises(
        RuntimeError,
        match="Prometheus targets query failed",
    ):
        await detect_unhealthy_services()

    assert len(list_incidents(status="OPEN")) == 1
    assert list_incidents(status="RESOLVED") == []


@pytest.mark.asyncio
async def test_detector_classifies_unhealthy_target_as_scrape_failure(
    monkeypatch,
):
    targets = prometheus_targets_response()
    for target in targets["data"]["activeTargets"]:
        if target["labels"]["job"] == "user-service":
            target["health"] = "down"
            target["lastError"] = "connection refused"

    MockAsyncClient.responses = [
        MockResponse(prometheus_response("user-service", 0)),
        MockResponse(targets),
    ]
    MockAsyncClient.calls = []
    install_mock(monkeypatch)

    result = await detect_unhealthy_services()

    assert any(
        incident["service"] == "user-service"
        and incident["type"] == "monitoring_scrape_failed"
        for incident in result["incidents"]
    )
    assert not any(
        incident["service"] == "user-service"
        and incident["type"] == "service_unavailable"
        for incident in result["incidents"]
    )
    assert not any(
        incident["service"] == "user-service"
        and incident["type"] == "monitoring_target_missing"
        for incident in result["incidents"]
    )


@pytest.mark.asyncio
async def test_detector_rejects_invalid_prometheus_response(
    monkeypatch,
):
    MockAsyncClient.responses = [
        MockResponse({
            "status": "success",
            "data": {},
        }),
    ]
    MockAsyncClient.calls = []
    install_mock(monkeypatch)

    with pytest.raises(
        RuntimeError,
        match="Prometheus query response is invalid",
    ):
        await detect_unhealthy_services()


@pytest.mark.asyncio
async def test_detector_rejects_invalid_metric_value(monkeypatch):
    MockAsyncClient.responses = [
        MockResponse(prometheus_response("user-service", "invalid")),
        MockResponse(prometheus_targets_response()),
    ]
    MockAsyncClient.calls = []
    install_mock(monkeypatch)

    with pytest.raises(
        RuntimeError,
        match="Prometheus metric value is invalid",
    ):
        await detect_unhealthy_services()
