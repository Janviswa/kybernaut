import json
import os
import stat
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from argon2 import PasswordHasher, Type

from app.core.operator_accounts import (
    load_operator_configuration,
    provision_operator_password,
)


LOGIN_PASSWORD = "test-only-login-password-42"
JWT_SECRET = "test-only-jwt-secret-do-not-use-in-production"
JWT_ISSUER = "kybernaut-test"
JWT_AUDIENCE = "kybernaut-api-test"


@pytest.fixture
def operator_accounts(monkeypatch, tmp_path):
    operators_file = tmp_path / "operators.json"
    password_file = tmp_path / "password-hashes.json"
    operators_file.write_text(
        json.dumps({
            "operators": {
                "read-only": {"permissions": ["read"]},
                "approver": {"permissions": ["read", "remediation"]},
                "owner": {"permissions": ["owner", "read", "remediation"]},
            },
        }),
        encoding="utf-8",
    )
    password_hasher = PasswordHasher(type=Type.ID)
    password_file.write_text(
        json.dumps({
            "password_hashes": {
                "read-only": password_hasher.hash(LOGIN_PASSWORD),
                "approver": password_hasher.hash("another-test-password-91"),
                "owner": password_hasher.hash("owner-test-password-91"),
            },
        }),
        encoding="utf-8",
    )
    password_file.chmod(0o600)
    monkeypatch.setenv("KYBERNAUT_OPERATORS_FILE", str(operators_file))
    monkeypatch.setenv("KYBERNAUT_OPERATOR_PASSWORD_FILE", str(password_file))
    return operators_file, password_file


def login(client, username="read-only", password=LOGIN_PASSWORD):
    return client.post(
        "/auth/login",
        json={"username": username, "password": password},
    )


def test_login_issues_short_lived_token_with_configured_permissions(
    api_client, operator_accounts
):
    response = login(api_client)

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 15 * 60
    assert body["operator_id"] == "read-only"
    assert body["permissions"] == ["read"]
    assert "password" not in body
    assert "password_hash" not in body
    claims = jwt.decode(
        body["access_token"],
        JWT_SECRET,
        algorithms=["HS256"],
        issuer=JWT_ISSUER,
        audience=JWT_AUDIENCE,
    )
    assert claims["sub"] == "read-only"
    assert claims["permissions"] == ["read"]
    assert claims["exp"] - claims["iat"] == 15 * 60
    assert api_client.get(
        "/monitor/status",
        headers={"Authorization": f"Bearer {body['access_token']}"},
    ).status_code == 200


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("not-configured", LOGIN_PASSWORD),
        ("read-only", "incorrect-test-password"),
    ],
)
def test_login_uses_generic_unauthorized_response_for_invalid_credentials(
    api_client, operator_accounts, username, password
):
    response = login(api_client, username, password)

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid username or password."}
    assert "WWW-Authenticate" in response.headers
    assert LOGIN_PASSWORD not in response.text


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"username": "read-only"},
        {"password": LOGIN_PASSWORD},
        {"username": 42, "password": LOGIN_PASSWORD},
        {"username": "read-only", "password": 42},
        {"username": "read-only", "password": LOGIN_PASSWORD, "permissions": ["remediation"]},
    ],
)
def test_login_rejects_missing_or_malformed_fields(
    api_client, operator_accounts, payload
):
    response = api_client.post("/auth/login", json=payload)

    assert response.status_code == 422
    assert LOGIN_PASSWORD not in response.text


def test_read_only_login_cannot_submit_remediation_decision(
    api_client, operator_accounts
):
    response = login(api_client)
    token = response.json()["access_token"]

    denied = api_client.post(
        "/approvals/missing/decision",
        headers={"Authorization": f"Bearer {token}"},
        json={"decision": "approved", "reason": "test"},
    )

    assert denied.status_code == 403
    assert denied.json()["detail"] == "Operator lacks remediation permission"


def test_login_preserves_trusted_owner_permission_in_signed_token(
    api_client, operator_accounts
):
    response = login(api_client, "owner", "owner-test-password-91")

    assert response.status_code == 200
    body = response.json()
    assert body["permissions"] == ["owner", "read", "remediation"]
    claims = jwt.decode(
        body["access_token"],
        JWT_SECRET,
        algorithms=["HS256"],
        issuer=JWT_ISSUER,
        audience=JWT_AUDIENCE,
    )
    assert claims["permissions"] == ["owner", "read", "remediation"]


def test_local_test_operator_is_configured_as_owner(monkeypatch):
    monkeypatch.delenv("KYBERNAUT_OPERATORS_FILE", raising=False)

    operators = load_operator_configuration()

    assert set(operators["test-operator"]["permissions"]) == {
        "owner", "read", "remediation",
    }


def test_remediation_login_can_approve_without_shared_token(
    api_client, operator_accounts, monkeypatch
):
    from app.services import audit_repository as audit
    import app.api.operations as operations

    response = login(api_client, "approver", "another-test-password-91")
    token = response.json()["access_token"]
    approval = audit.create_approval(
        incident_ref="inc-remediation-login-token",
        proposed_action={},
        context={},
        requested_by="requester",
    )

    async def approve_without_token(
        approval_id, decision, operator_id, reason
    ):
        return {
            "approval_id": approval_id,
            "status": decision,
            "decided_by": operator_id,
            "decision_reason": reason,
        }

    monkeypatch.setattr(operations, "resume_after_decision", approve_without_token)
    approved = api_client.post(
        f"/approvals/{approval['approval_id']}/decision",
        headers={"Authorization": f"Bearer {token}"},
        json={"decision": "approved", "reason": "test"},
    )

    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["decided_by"] == "approver"


def test_password_provisioning_persists_only_argon2id_verifier(
    monkeypatch, tmp_path
):
    operators_file = tmp_path / "operators.json"
    password_file = tmp_path / "password-hashes.json"
    operators_file.write_text(
        json.dumps({"operators": {"new-operator": {"permissions": ["read"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("KYBERNAUT_OPERATORS_FILE", str(operators_file))
    monkeypatch.setenv("KYBERNAUT_OPERATOR_PASSWORD_FILE", str(password_file))

    provision_operator_password("new-operator", LOGIN_PASSWORD)

    operator_data = json.loads(operators_file.read_text(encoding="utf-8"))
    stored_data = json.loads(password_file.read_text(encoding="utf-8"))
    encoded_hash = stored_data["password_hashes"]["new-operator"]
    assert operator_data == {
        "operators": {"new-operator": {"permissions": ["read"]}}
    }
    assert LOGIN_PASSWORD not in operators_file.read_text(encoding="utf-8")
    assert LOGIN_PASSWORD not in password_file.read_text(encoding="utf-8")
    assert encoded_hash.startswith("$argon2id$")
    assert PasswordHasher().verify(encoded_hash, LOGIN_PASSWORD)
    assert stat.S_IMODE(os.stat(password_file).st_mode) == 0o600


def test_issued_access_token_is_rejected_after_expiration(api_client):
    expired = jwt.encode(
        {
            "sub": "read-only",
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "iat": datetime.now(timezone.utc) - timedelta(minutes=2),
            "exp": datetime.now(timezone.utc) - timedelta(minutes=1),
            "permissions": ["read"],
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    response = api_client.get(
        "/monitor/status",
        headers={"Authorization": f"Bearer {expired}"},
    )
    assert response.status_code == 401


def test_invalid_access_token_is_rejected(api_client):
    response = api_client.get(
        "/monitor/status",
        headers={"Authorization": "Bearer invalid-token"},
    )
    assert response.status_code == 401
