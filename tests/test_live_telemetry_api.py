from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import create_operator_token


def _auth(permissions=("read",)):
    token = create_operator_token(permissions=list(permissions))
    return {"Authorization": f"Bearer {token}"}


def test_live_telemetry_returns_collector_data_and_marks_missing_services(
    api_client, monkeypatch
):
    import app.main as main

    calls = []

    async def collect(client):
        calls.append(client)
        return {
            "order-service": {
                "cpu_cores_used": 0.4,
                "memory_bytes": 2048,
                "request_rate": 3.2,
                "p95_latency_seconds": 0.12,
                "observed_at": "2026-10-27T13:00:00+00:00",
                "sample_timestamps": {
                    "cpu_cores_used": "2026-10-27T13:00:00+00:00",
                    "memory_bytes": "2026-10-27T13:00:00+00:00",
                    "request_rate": "2026-10-27T13:00:00+00:00",
                    "p95_latency_seconds": "2026-10-27T13:00:00+00:00",
                },
            }
        }

    monkeypatch.setattr(main, "collect_service_features", collect)
    response = api_client.get("/telemetry", headers=_auth())

    assert response.status_code == 200
    result = response.json()
    assert result["source"] == "prometheus"
    assert result["services"]["order-service"]["cpu_cores_used"] == 0.4
    assert result["services"]["payment-service"] is None
    assert result["unavailable_services"] == [
        "inventory-service",
        "payment-service",
        "user-service",
    ]
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [({}, 401), (_auth(("remediation",)), 403)],
)
def test_live_telemetry_requires_read_permission(
    api_client, monkeypatch, headers, expected_status
):
    import app.main as main

    async def unexpected_collection(_client):
        raise AssertionError("telemetry collection must not run")

    monkeypatch.setattr(main, "collect_service_features", unexpected_collection)
    response = api_client.get("/telemetry", headers=headers)

    assert response.status_code == expected_status


def test_live_telemetry_reports_prometheus_failure(api_client, monkeypatch):
    import app.main as main

    async def unavailable(_client):
        raise RuntimeError("private connection detail")

    monkeypatch.setattr(main, "collect_service_features", unavailable)
    response = api_client.get("/telemetry", headers=_auth())

    assert response.status_code == 503
    assert response.json()["detail"] == "Live telemetry is temporarily unavailable."
    assert "private connection detail" not in response.text


def test_cost_usage_shows_prometheus_usage_without_unconfigured_prices(
    api_client, monkeypatch
):
    import app.main as main

    async def collect(_client, *, required_features):
        assert required_features == ("cpu_cores_used", "memory_bytes")
        return {
            "order-service": {
                "cpu_cores_used": 0.4,
                "memory_bytes": 1024 ** 3,
                "request_rate": 3.2,
                "p95_latency_seconds": 0.12,
            }
        }

    monkeypatch.delenv("KYBERNAUT_COST_CPU_PER_CORE_HOUR_USD", raising=False)
    monkeypatch.delenv("KYBERNAUT_COST_MEMORY_PER_GIB_HOUR_USD", raising=False)
    monkeypatch.delenv("KYBERNAUT_ENVIRONMENT", raising=False)
    monkeypatch.setattr(main, "collect_service_features", collect)

    response = api_client.get("/cost/usage", headers=_auth())

    assert response.status_code == 200
    result = response.json()
    assert result["source"] == "prometheus"
    assert result["currency"] == "USD"
    assert result["status"] == "rates_not_configured"
    assert result["services"]["order-service"]["cpu_cores_used"] == 0.4
    assert result["services"]["order-service"]["estimated_hourly_cost_usd"] is None
    assert result["summary"]["estimated_monthly_run_rate_usd"] is None
    assert "KYBERNAUT_COST_CPU_PER_CORE_HOUR_USD" in result["pricing"]["configuration"]


def test_cost_usage_uses_owner_approved_local_demo_rates(
    api_client, monkeypatch
):
    import app.main as main

    async def collect(_client, *, required_features):
        assert required_features == ("cpu_cores_used", "memory_bytes")
        return {
            service: {
                "cpu_cores_used": 0.4,
                "memory_bytes": 1024 ** 3,
                "request_rate": 3.2,
                "p95_latency_seconds": 0.12,
            }
            for service in main.EXPECTED_SERVICES
        }

    monkeypatch.setenv("KYBERNAUT_ENVIRONMENT", "local-demo")
    monkeypatch.delenv("KYBERNAUT_COST_CPU_PER_CORE_HOUR_USD", raising=False)
    monkeypatch.delenv("KYBERNAUT_COST_MEMORY_PER_GIB_HOUR_USD", raising=False)
    monkeypatch.setattr(main, "collect_service_features", collect)

    response = api_client.get("/cost/usage", headers=_auth())

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "estimated"
    assert result["pricing"]["cpu_per_core_hour_usd"] == 10.00
    assert result["pricing"]["memory_per_gib_hour_usd"] == 5.25
    assert "illustrative owner-configured internal rates" in result["pricing"]["source"]
    assert "not actual cloud-provider costs" in result["disclaimer"]
    assert result["services"]["order-service"]["estimated_hourly_cost_usd"] == 9.25
    assert result["services"]["order-service"]["estimated_monthly_run_rate_usd"] == 6752.5
    assert result["services"]["order-service"]["estimated_cpu_monthly_cost_usd"] == 2920.0
    assert result["services"]["order-service"]["estimated_memory_monthly_cost_usd"] == 3832.5
    assert result["services"]["order-service"]["illustrative_10_percent_memory_reduction_monthly_savings_usd"] == 383.25
    assert result["summary"]["estimated_hourly_cost_usd"] == 37.0
    assert result["summary"]["estimated_monthly_run_rate_usd"] == 27010.0


def test_cost_usage_prices_current_usage_only_with_owner_configured_rates(
    api_client, monkeypatch
):
    import app.main as main

    async def collect(_client, *, required_features):
        assert required_features == ("cpu_cores_used", "memory_bytes")
        return {
            service: {
                "cpu_cores_used": 0.4,
                "memory_bytes": 1024 ** 3,
                "request_rate": 3.2,
                "p95_latency_seconds": 0.12,
            }
            for service in main.EXPECTED_SERVICES
        }

    monkeypatch.setenv("KYBERNAUT_COST_CPU_PER_CORE_HOUR_USD", "0.5")
    monkeypatch.setenv("KYBERNAUT_COST_MEMORY_PER_GIB_HOUR_USD", "0.2")
    monkeypatch.setattr(main, "collect_service_features", collect)

    response = api_client.get("/cost/usage", headers=_auth())

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "estimated"
    assert result["services"]["order-service"]["estimated_hourly_cost_usd"] == 0.4
    assert result["services"]["order-service"]["estimated_monthly_run_rate_usd"] == 292
    assert result["summary"]["estimated_hourly_cost_usd"] == 1.6
    assert result["summary"]["estimated_monthly_run_rate_usd"] == 1168


def test_cost_usage_does_not_report_partial_service_cost_as_total(
    api_client, monkeypatch
):
    import app.main as main

    async def collect(_client, *, required_features):
        assert required_features == ("cpu_cores_used", "memory_bytes")
        return {
            "order-service": {
                "cpu_cores_used": 0.4,
                "memory_bytes": 1024 ** 3,
                "request_rate": 3.2,
                "p95_latency_seconds": 0.12,
            }
        }

    monkeypatch.setenv("KYBERNAUT_COST_CPU_PER_CORE_HOUR_USD", "0.5")
    monkeypatch.setenv("KYBERNAUT_COST_MEMORY_PER_GIB_HOUR_USD", "0.2")
    monkeypatch.setattr(main, "collect_service_features", collect)

    response = api_client.get("/cost/usage", headers=_auth())

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "usage_incomplete"
    assert result["services"]["order-service"]["estimated_hourly_cost_usd"] == 0.4
    assert result["summary"]["all_services_available"] is False
    assert result["summary"]["estimated_hourly_cost_usd"] is None
    assert "payment-service" in result["unavailable_services"]


@pytest.mark.parametrize(
    "headers, expected_status",
    [({}, 401), (_auth(("remediation",)), 403)],
)
def test_cost_usage_requires_read_permission(api_client, monkeypatch, headers, expected_status):
    import app.main as main

    async def unexpected_collection(_client):
        raise AssertionError("cost telemetry collection must not run")

    monkeypatch.setattr(main, "collect_service_features", unexpected_collection)
    response = api_client.get("/cost/usage", headers=headers)
    assert response.status_code == expected_status


@pytest.mark.parametrize(
    (
        "sample_age",
        "evidence_status",
        "findings",
        "expected_health",
        "expected_freshness",
    ),
    [
        (0, "available", [{"type": "no_detected_anomaly"}], "healthy", "fresh"),
        (120, "available", [{"type": "no_detected_anomaly"}], "unknown", "stale"),
        (0, "unavailable", [{"type": "no_detected_anomaly"}], "unknown", "unavailable"),
        (0, "available", [], "unknown", "fresh"),
    ],
)
def test_service_health_reports_freshness_without_success_shaped_fallback(
    api_client,
    monkeypatch,
    sample_age,
    evidence_status,
    findings,
    expected_health,
    expected_freshness,
):
    import app.main as main

    sample_at = (
        datetime.now(timezone.utc) - timedelta(seconds=sample_age)
    ).isoformat()

    async def diagnosis(service):
        return {
            "service": service,
            "status": "up",
            "evidence_status": evidence_status,
            "evidence_timestamps": {
                name: {"timestamp": sample_at}
                for name in (
                    "service_availability",
                    "http_5xx_rate",
                    "p95_latency",
                )
            },
            "findings": findings,
        }

    monkeypatch.setenv("KYBERNAUT_PROMETHEUS_MAX_SAMPLE_AGE_SECONDS", "60")
    monkeypatch.setenv("KYBERNAUT_PROMETHEUS_MAX_SAMPLE_SKEW_SECONDS", "10")
    monkeypatch.setattr(main, "diagnose_service", diagnosis)

    response = api_client.get(
        "/service-health/order-service",
        headers=_auth(),
    )

    assert response.status_code == 200
    result = response.json()
    assert result["health_status"] == expected_health
    assert result["freshness_status"] == expected_freshness


def test_service_health_rejects_unknown_service(api_client):
    response = api_client.get(
        "/service-health/unconfigured-service",
        headers=_auth(),
    )

    assert response.status_code == 404
