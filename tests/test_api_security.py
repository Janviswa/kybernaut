
import importlib
import secrets

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def api_client(monkeypatch):
    # Configure a test-only token before importing the application.
    monkeypatch.setenv(
        "KYBERNAUT_REMEDIATION_TOKEN",
        "test-only-approval-token",
    )

    import app.main as main

    main = importlib.reload(main)

    # Prevent real remediation from being executed by API tests.
    async def fake_remediate_service(**kwargs):
        return {
            "status": "test-approved",
            "service": kwargs["service"],
            "incident_id": kwargs["incident_id"],
        }

    monkeypatch.setattr(
        main,
        "remediate_service",
        fake_remediate_service,
    )

    with TestClient(main.app) as client:
        yield client


def test_remediation_rejects_missing_approval_token(api_client):
    response = api_client.post(
        "/remediate/user-service",
        json={
            "approved": True,
            "incident_id": "inc-test-123",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Valid approval token required"


def test_remediation_rejects_invalid_approval_token(api_client):
    response = api_client.post(
        "/remediate/user-service",
        headers={"X-Approval-Token": "incorrect-token"},
        json={
            "approved": True,
            "incident_id": "inc-test-123",
        },
    )

    assert response.status_code == 403


def test_remediation_accepts_valid_token(api_client):
    response = api_client.post(
        "/remediate/user-service",
        headers={
            "X-Approval-Token": "test-only-approval-token",
        },
        json={
            "approved": True,
            "incident_id": "inc-test-123",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "test-approved"


def test_remediation_rejects_unknown_service(api_client):
    response = api_client.post(
        "/remediate/unknown-service",
        headers={
            "X-Approval-Token": "test-only-approval-token",
        },
        json={
            "approved": True,
            "incident_id": "inc-test-123",
        },
    )

    assert response.status_code == 404
