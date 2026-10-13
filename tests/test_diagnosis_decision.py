
import pytest

from app.services import incident_diagnoser, decision_engine


class MockResponse:
    def __init__(self, result):
        self.result = result

    def raise_for_status(self):
        pass

    def json(self):
        return {
            "status": "success",
            "data": {"result": self.result},
        }


class MockAsyncClient:
    responses = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, *args, **kwargs):
        if not self.responses:
            raise AssertionError("Unexpected Prometheus request")
        return self.responses.pop(0)


def metric(value):
    return [{"value": [0, str(value)]}]


@pytest.fixture
def mock_prometheus(monkeypatch):
    def install(is_up=1, error_rate=0, latency=0.1):
        MockAsyncClient.responses = [
            MockResponse(metric(is_up)),
            MockResponse(metric(error_rate)),
            MockResponse(metric(latency)),
        ]
        monkeypatch.setattr(
            incident_diagnoser.httpx,
            "AsyncClient",
            MockAsyncClient,
        )

    return install


@pytest.mark.asyncio
async def test_healthy_service_continues_monitoring(
    monkeypatch, mock_prometheus
):
    mock_prometheus(is_up=1, error_rate=0, latency=0.1)

    result = await incident_diagnoser.diagnose_service("user-service")

    assert result["status"] == "up"
    assert result["findings"][0]["type"] == "no_detected_anomaly"

    mock_prometheus(is_up=1, error_rate=0, latency=0.1)
    decision = await decision_engine.decide_action("user-service")

    assert decision["decision"]["action"] == "continue_monitoring"
    assert decision["decision"]["automatic_execution"] is False


@pytest.mark.asyncio
async def test_down_service_requests_approval(
    monkeypatch, mock_prometheus
):
    mock_prometheus(is_up=0)

    result = await incident_diagnoser.diagnose_service("user-service")
    assert any(
        item["type"] == "service_unavailable"
        for item in result["findings"]
    )

    mock_prometheus(is_up=0)
    decision = await decision_engine.decide_action("user-service")

    assert decision["decision"]["action"] == "request_approval"
    assert decision["decision"]["automatic_execution"] is False


@pytest.mark.asyncio
async def test_http_5xx_errors_trigger_investigation(
    monkeypatch, mock_prometheus
):
    mock_prometheus(is_up=1, error_rate=0.5, latency=0.1)

    decision = await decision_engine.decide_action("user-service")

    assert decision["decision"]["action"] == "investigate_errors"
    assert decision["decision"]["automatic_execution"] is False


@pytest.mark.asyncio
async def test_high_latency_triggers_investigation(
    monkeypatch, mock_prometheus
):
    mock_prometheus(is_up=1, error_rate=0, latency=1.5)

    decision = await decision_engine.decide_action("user-service")

    assert decision["decision"]["action"] == "investigate_latency"
    assert decision["decision"]["automatic_execution"] is False
