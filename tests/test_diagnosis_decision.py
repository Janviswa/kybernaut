
import pytest

from app.services import incident_diagnoser, decision_engine
from app.services.incident_contract import build_incident_contract


class MockResponse:
    def __init__(self, result=None, *, body=None):
        self.result = result
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        if self.body is not None:
            return self.body
        return {
            "status": "success",
            "data": {"result": self.result},
        }


class MockAsyncClient:
    responses = []
    queries = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get(self, *args, **kwargs):
        if not self.responses:
            raise AssertionError("Unexpected Prometheus request")
        self.queries.append(kwargs.get("params", {}).get("query"))
        return self.responses.pop(0)


def metric(value, timestamp=0):
    return [{"value": [timestamp, str(value)]}]


def request_rate(value, timestamp=0, status="2xx"):
    return [{
        "metric": {"status": status},
        "value": [timestamp, str(value)],
    }]


@pytest.fixture
def mock_prometheus(monkeypatch):
    def install(is_up=1, error_rate=0, latency=0.1):
        observed_rates = (
            request_rate(error_rate, status="5xx")
            if error_rate
            else request_rate(0)
        )
        MockAsyncClient.responses = [
            MockResponse(metric(is_up)),
            MockResponse(observed_rates),
            MockResponse(metric(latency)),
        ]
        MockAsyncClient.queries = []
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
    assert result["evidence"]["http_5xx_rate_per_second"] == 0
    assert result["evidence_status"] == "available"
    assert "sum by (status)" in MockAsyncClient.queries[1]
    assert 'handler!="/metrics"' in MockAsyncClient.queries[1]
    assert 'status="5xx"' not in MockAsyncClient.queries[1]

    mock_prometheus(is_up=1, error_rate=0, latency=0.1)
    decision = await decision_engine.decide_action("user-service")

    assert decision["decision"]["action"] == "continue_monitoring"
    assert decision["decision"]["automatic_execution"] is False


@pytest.mark.asyncio
async def test_diagnosis_preserves_each_query_timestamp_and_provenance(
    monkeypatch,
):
    MockAsyncClient.responses = [
        MockResponse(metric(1, 100)),
        MockResponse(metric(0, 200)),
        MockResponse(metric(0.1, 300)),
    ]
    monkeypatch.setattr(
        incident_diagnoser.httpx,
        "AsyncClient",
        MockAsyncClient,
    )

    result = await incident_diagnoser.diagnose_service("user-service")

    assert result["evidence_timestamps"] == {
        "service_availability": {
            "timestamp": "1970-01-01T00:01:40+00:00",
            "provenance": "prometheus_sample_timestamp",
        },
        "http_5xx_rate": {
            "timestamp": "1970-01-01T00:03:20+00:00",
            "provenance": "instant_query_result_timestamp",
        },
        "p95_latency": {
            "timestamp": "1970-01-01T00:05:00+00:00",
            "provenance": "instant_query_result_timestamp",
        },
    }
    assert "observed_at" not in result
    contract = build_incident_contract({
        "service": "user-service",
        "diagnosis": result,
        "kubernetes_status": "unavailable",
    })
    prometheus = next(
        source for source in contract.evidence_sources
        if source.source == "prometheus"
    )
    assert prometheus.observed_at is None


@pytest.mark.asyncio
async def test_malformed_prometheus_timestamp_is_incomplete_and_not_healthy(
    monkeypatch,
):
    MockAsyncClient.responses = [
        MockResponse(metric(1, "bad-timestamp")),
        MockResponse(metric(0, 100)),
        MockResponse(metric(0.1, 100)),
    ]
    monkeypatch.setattr(
        incident_diagnoser.httpx,
        "AsyncClient",
        MockAsyncClient,
    )

    diagnosis = await incident_diagnoser.diagnose_service("user-service")

    assert diagnosis["status"] == "up"
    assert diagnosis["evidence_status"] == "invalid"
    assert diagnosis["evidence_timestamps"]["service_availability"] == {
        "timestamp": None,
        "provenance": "prometheus_sample_timestamp",
    }
    MockAsyncClient.responses = [
        MockResponse(metric(1, "bad-timestamp")),
        MockResponse(metric(0, 100)),
        MockResponse(metric(0.1, 100)),
    ]
    decision = await decision_engine.decide_action("user-service")
    assert decision["decision"]["action"] == "investigate_monitoring"


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
async def test_positive_5xx_rate_is_retained_as_evidence(
    monkeypatch, mock_prometheus
):
    mock_prometheus(is_up=1, error_rate=0.5, latency=0.1)

    result = await incident_diagnoser.diagnose_service("user-service")

    assert result["evidence"]["http_5xx_rate_per_second"] == 0.5
    assert result["evidence_status"] == "available"
    assert "sum by (status)" in MockAsyncClient.queries[1]
    assert any(
        finding["type"] == "http_5xx_errors"
        for finding in result["findings"]
    )


@pytest.mark.asyncio
async def test_high_latency_triggers_investigation(
    monkeypatch, mock_prometheus
):
    mock_prometheus(is_up=1, error_rate=0, latency=1.5)

    decision = await decision_engine.decide_action("user-service")

    assert decision["decision"]["action"] == "investigate_latency"
    assert decision["decision"]["automatic_execution"] is False


@pytest.mark.asyncio
async def test_missing_availability_metric_is_not_classified_as_down(
    monkeypatch,
):
    MockAsyncClient.responses = [
        MockResponse([]),          # No availability sample
        MockResponse(request_rate(0)),  # Observed instrumented requests, no 5xx
        MockResponse(metric(0.1)), # Normal latency
    ]

    monkeypatch.setattr(
        incident_diagnoser.httpx,
        "AsyncClient",
        MockAsyncClient,
    )

    result = await incident_diagnoser.diagnose_service("user-service")

    assert result["status"] != "down"
    assert not any(
        finding["type"] == "service_unavailable"
        for finding in result["findings"]
    )


@pytest.mark.asyncio
async def test_unknown_availability_investigates_monitoring(
    monkeypatch, mock_prometheus
):
    mock_prometheus(is_up=1)
    MockAsyncClient.responses[0] = MockResponse([])

    decision = await decision_engine.decide_action("user-service")

    assert decision["diagnosis"]["status"] == "unknown"
    assert decision["decision"]["action"] == "investigate_monitoring"
    assert decision["decision"]["automatic_execution"] is False


@pytest.mark.asyncio
async def test_missing_5xx_metric_is_unavailable_not_zero_or_healthy(
    monkeypatch,
):
    MockAsyncClient.responses = [
        MockResponse(metric(1)),
        MockResponse([]),
        MockResponse(metric(0.1)),
    ]
    monkeypatch.setattr(
        incident_diagnoser.httpx,
        "AsyncClient",
        MockAsyncClient,
    )

    result = await incident_diagnoser.diagnose_service("user-service")

    assert result["evidence"]["http_5xx_rate_per_second"] is None
    assert result["evidence_status"] == "unavailable"
    assert "http_5xx_rate" in result["missing_evidence"]
    assert all(
        finding["type"] != "no_detected_anomaly"
        for finding in result["findings"]
    )

    MockAsyncClient.responses = [
        MockResponse(metric(1)),
        MockResponse([]),
        MockResponse(metric(0.1)),
    ]
    decision = await decision_engine.decide_action("user-service")
    assert decision["decision"]["action"] == "investigate_monitoring"


@pytest.mark.asyncio
async def test_observed_2xx_counter_proves_zero_5xx_rate(
    monkeypatch,
):
    observed_2xx_series = [{
        "metric": {
            "job": "user-service",
            "handler": "/orders",
            "method": "GET",
            "status": "2xx",
        },
        "value": [100, "12"],
    }]
    MockAsyncClient.responses = [
        MockResponse(metric(1)),
        MockResponse(observed_2xx_series),
        MockResponse(metric(0.1)),
    ]
    MockAsyncClient.queries = []
    monkeypatch.setattr(
        incident_diagnoser.httpx,
        "AsyncClient",
        MockAsyncClient,
    )

    result = await incident_diagnoser.diagnose_service("user-service")

    assert "sum by (status)" in MockAsyncClient.queries[1]
    assert 'handler!="/metrics"' in MockAsyncClient.queries[1]
    assert result["evidence"]["http_5xx_rate_per_second"] == 0
    assert result["evidence_status"] == "available"
    assert "http_5xx_rate" not in result["missing_evidence"]


@pytest.mark.parametrize(
    "invalid_body",
    [
        {"status": "error", "data": {"result": []}},
        {"status": "success", "data": {"result": None}},
    ],
)
@pytest.mark.asyncio
async def test_invalid_5xx_query_response_fails_without_zero_fallback(
    monkeypatch, invalid_body
):
    MockAsyncClient.responses = [
        MockResponse(metric(1)),
        MockResponse(body=invalid_body),
    ]
    MockAsyncClient.queries = []
    monkeypatch.setattr(
        incident_diagnoser.httpx,
        "AsyncClient",
        MockAsyncClient,
    )

    with pytest.raises(RuntimeError, match="Prometheus query"):
        await incident_diagnoser.diagnose_service("user-service")


@pytest.mark.parametrize("timestamp", [None, "invalid-timestamp"])
@pytest.mark.asyncio
async def test_5xx_sample_without_valid_timestamp_is_invalid_evidence(
    monkeypatch, timestamp
):
    MockAsyncClient.responses = [
        MockResponse(metric(1, 100)),
        MockResponse([{
            "metric": {"status": "5xx"},
            "value": [timestamp, "0.5"],
        }]),
        MockResponse(metric(0.1, 100)),
    ]
    MockAsyncClient.queries = []
    monkeypatch.setattr(
        incident_diagnoser.httpx,
        "AsyncClient",
        MockAsyncClient,
    )

    result = await incident_diagnoser.diagnose_service("user-service")

    assert result["evidence"]["http_5xx_rate_per_second"] == 0.5
    assert result["evidence_status"] == "invalid"
    assert "http_5xx_rate_timestamp" in result["missing_evidence"]


@pytest.mark.asyncio
async def test_missing_latency_metric_is_not_treated_as_zero(
    monkeypatch,
):
    MockAsyncClient.responses = [
        MockResponse(metric(1)),
        MockResponse(metric(0)),
        MockResponse([]),
    ]
    monkeypatch.setattr(
        incident_diagnoser.httpx,
        "AsyncClient",
        MockAsyncClient,
    )

    result = await incident_diagnoser.diagnose_service("user-service")

    assert result["evidence"]["p95_latency_seconds"] is None
    assert result["evidence_status"] == "unavailable"
    assert "p95_latency" in result["missing_evidence"]
    assert all(
        finding["type"] != "no_detected_anomaly"
        for finding in result["findings"]
    )


@pytest.mark.asyncio
async def test_empty_malformed_latency_sample_does_not_become_zero(
    monkeypatch,
):
    MockAsyncClient.responses = [
        MockResponse(metric(1)),
        MockResponse(metric(0)),
        MockResponse([{"value": [0, ""]}]),
    ]
    monkeypatch.setattr(
        incident_diagnoser.httpx,
        "AsyncClient",
        MockAsyncClient,
    )

    result = await incident_diagnoser.diagnose_service("user-service")

    assert result["evidence"]["p95_latency_seconds"] is None
    assert "p95_latency" in result["missing_evidence"]


@pytest.mark.asyncio
async def test_stale_diagnosis_evidence_requires_monitoring_investigation(
    monkeypatch,
):
    async def stale_diagnosis(_service):
        return {
            "status": "up",
            "evidence_status": "stale",
            "missing_evidence": [],
            "findings": [{
                "type": "no_detected_anomaly",
                "severity": "info",
                "evidence": "Synthetic stale sample.",
            }],
        }

    monkeypatch.setattr(decision_engine, "diagnose_service", stale_diagnosis)

    decision = await decision_engine.decide_action("user-service")

    assert decision["decision"]["action"] == "investigate_monitoring"


@pytest.mark.asyncio
async def test_missing_evidence_list_cannot_be_overridden_by_healthy_finding(
    monkeypatch,
):
    async def incomplete_diagnosis(_service):
        return {
            "status": "up",
            "missing_evidence": ["http_5xx_rate"],
            "findings": [{
                "type": "no_detected_anomaly",
                "severity": "info",
                "evidence": "Synthetic incomplete sample.",
            }],
        }

    monkeypatch.setattr(decision_engine, "diagnose_service", incomplete_diagnosis)

    decision = await decision_engine.decide_action("user-service")

    assert decision["decision"]["action"] == "investigate_monitoring"
