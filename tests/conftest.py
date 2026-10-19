import importlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient


TEST_JWT_SECRET = "test-only-jwt-secret-do-not-use-in-production"
TEST_JWT_ISSUER = "kybernaut-test"
TEST_JWT_AUDIENCE = "kybernaut-api-test"
TEST_APPROVAL_TOKEN = "test-only-approval-token"


def create_operator_token(
    *,
    operator_id="test-operator",
    permissions=None,
    expires_delta=timedelta(minutes=5),
    secret=TEST_JWT_SECRET,
):
    import jwt

    now = datetime.now(timezone.utc)
    payload = {
        "sub": operator_id,
        "iss": TEST_JWT_ISSUER,
        "aud": TEST_JWT_AUDIENCE,
        "iat": now,
        "exp": now + expires_delta,
        "permissions": (
            ["remediation"] if permissions is None else permissions
        ),
    }
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture
def api_client(monkeypatch):
    monkeypatch.setenv(
        "KYBERNAUT_REMEDIATION_TOKEN",
        TEST_APPROVAL_TOKEN,
    )
    monkeypatch.setenv("KYBERNAUT_JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("KYBERNAUT_JWT_ISSUER", TEST_JWT_ISSUER)
    monkeypatch.setenv("KYBERNAUT_JWT_AUDIENCE", TEST_JWT_AUDIENCE)

    import app.core.jwt_auth as jwt_auth
    importlib.reload(jwt_auth)

    import app.main as main
    main = importlib.reload(main)

    async def fake_remediate_service(**kwargs):
        return {
            "status": "test-approved",
            "service": kwargs["service"],
            "incident_id": kwargs["incident_id"],
            "operator_id": kwargs["operator_id"],
        }

    monkeypatch.setattr(
        main,
        "remediate_service",
        fake_remediate_service,
    )

    with TestClient(main.app) as client:
        yield client
