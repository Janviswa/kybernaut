
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
                request=httpx.Request("GET", "http://localhost:9090"),
                response=httpx.Response(self.status_code),
            )

    def json(self):
        return self.data


class MockAsyncClient:
    response_data = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, *args, **kwargs):
        return self.response_data


def prometheus_response(service, value):
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


@pytest.mark.asyncio
async def test_detector_records_down_service(monkeypatch):
    MockAsyncClient.response_data = MockResponse(
        prometheus_response("user-service", 0)
    )
    monkeypatch.setattr(
        "app.services.incident_detector.httpx.AsyncClient",
        MockAsyncClient,
    )

    result = await detect_unhealthy_services()

    assert result["incident_count"] == 1
    assert result["incidents"][0]["service"] == "user-service"
    assert result["incidents"][0]["severity"] == "critical"
    assert len(list_incidents(status="OPEN")) == 1


@pytest.mark.asyncio
async def test_detector_resolves_incident_when_service_recovers(monkeypatch):
    record_incident({
        "incident_id": "inc-test-user-down",
        "service": "user-service",
        "type": "service_unavailable",
        "severity": "critical",
        "message": "user-service is down",
    })

    MockAsyncClient.response_data = MockResponse(
        prometheus_response("user-service", 1)
    )
    monkeypatch.setattr(
        "app.services.incident_detector.httpx.AsyncClient",
        MockAsyncClient,
    )

    result = await detect_unhealthy_services()

    assert result["incident_count"] == 0
    assert list_incidents(status="OPEN") == []
    assert len(list_incidents(status="RESOLVED")) == 1


@pytest.mark.asyncio
async def test_detector_raises_when_prometheus_query_fails(monkeypatch):
    MockAsyncClient.response_data = MockResponse(
        {"status": "error", "data": {"result": []}}
    )
    monkeypatch.setattr(
        "app.services.incident_detector.httpx.AsyncClient",
        MockAsyncClient,
    )

    with pytest.raises(RuntimeError, match="Prometheus query failed"):
        await detect_unhealthy_services()
