from test_api_security import create_operator_token


def test_metrics_endpoint_rejects_missing_token(api_client):
    response = api_client.get("/metrics/incidents")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_metrics_endpoint_rejects_operator_without_read_permission(
    api_client,
):
    token = create_operator_token(permissions=["remediation"])

    response = api_client.get(
        "/metrics/incidents",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Operator lacks read permission"


def test_metrics_endpoint_returns_metrics(api_client, monkeypatch):
    import app.main as main

    expected = {
        "total_incidents": 2,
        "active_incidents": 1,
        "by_status": {"OPEN": 1, "RESOLVED": 1},
        "by_severity": {"high": 2},
        "by_service": {"kubernetes-lab": 2},
        "resolved_incidents_with_duration": 1,
        "mttr_seconds": 12.0,
        "recurring_fingerprint_count": 1,
        "recurring_occurrences": 2,
        "recurrence_rate": 1.0,
    }

    monkeypatch.setattr(
        main,
        "calculate_incident_metrics",
        lambda: expected,
    )

    token = create_operator_token(permissions=["read"])
    response = api_client.get(
        "/metrics/incidents",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    assert response.json() == expected
