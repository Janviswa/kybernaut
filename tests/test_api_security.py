
import importlib
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient


TEST_JWT_SECRET = "test-only-jwt-secret-do-not-use-in-production"
TEST_JWT_ISSUER = "kybernaut-test"
TEST_JWT_AUDIENCE = "kybernaut-api-test"


def create_operator_token(
    *,
    operator_id="test-operator",
    permissions=None,
    expires_delta=timedelta(minutes=5),
    secret=TEST_JWT_SECRET,
):
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
    monkeypatch.setenv("KYBERNAUT_ANOMALY_MONITOR_ENABLED", "false")
    monkeypatch.delenv("KYBERNAUT_REMEDIATION_TOKEN", raising=False)
    monkeypatch.setenv("KYBERNAUT_JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("KYBERNAUT_JWT_ISSUER", TEST_JWT_ISSUER)
    monkeypatch.setenv("KYBERNAUT_JWT_AUDIENCE", TEST_JWT_AUDIENCE)

    # Reload authentication settings before importing the API.
    import app.core.jwt_auth as jwt_auth

    importlib.reload(jwt_auth)

    import app.main as main

    main = importlib.reload(main)

    # Prevent real remediation from being executed by API tests.
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


def remediation_headers(*, token=None):
    headers = {}

    if token is not None:
        headers["Authorization"] = f"Bearer {token}"

    return headers


def test_remediation_rejects_missing_jwt(api_client):
    response = api_client.post(
        "/remediate/user-service",
        headers={
            "X-Operator-ID": "spoofed-operator",
        },
        json={
            "approved": True,
            "incident_id": "inc-test-123",
        },
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_monitor_status_requires_authenticated_read_permission(api_client):
    unauthenticated = api_client.get("/monitor/status")
    assert unauthenticated.status_code == 401

    token = create_operator_token(permissions=["remediation"])
    unauthorized = api_client.get(
        "/monitor/status",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert unauthorized.status_code == 403
    assert unauthorized.json()["detail"] == "Operator lacks read permission"

    read_token = create_operator_token(permissions=["read"])
    response = api_client.get(
        "/monitor/status",
        headers={"Authorization": f"Bearer {read_token}"},
    )
    assert response.status_code == 200
    status = response.json()
    assert status["configured_enabled"] is False
    assert status["lifecycle_state"] == "disabled"
    assert status["health"] == "disabled"
    assert status["workflow_health"] == "disabled"
    assert status["last_poll_attempt_at"] is None
    assert status["last_successful_poll_at"] is None
    assert all(
        component["freshness"] == "unknown"
        for component in status["components"].values()
    )


def test_monitor_status_is_read_only_and_contains_no_sensitive_details(
    api_client, monkeypatch, tmp_path
):
    import app.main as main
    import app.services.application_anomaly_monitor as monitor_module
    from app.services.application_anomaly_monitor import ApplicationAnomalyMonitor

    def unexpected_call(*_args, **_kwargs):
        pytest.fail("Status reads must not initiate monitoring operations")

    monitor = ApplicationAnomalyMonitor(
        60,
        services=("order-service",),
        lock_path=tmp_path / "monitor.lock",
    )
    main.app.state.anomaly_monitor = monitor
    main.app.state.anomaly_monitor_enabled = True
    monkeypatch.setattr(
        monitor_module.ApplicationAnomalyMonitor,
        "poll_once",
        unexpected_call,
    )
    monkeypatch.setattr(monitor_module, "evaluate_service_anomaly", unexpected_call)
    monkeypatch.setattr(monitor_module, "dispatch_pending_handoffs", unexpected_call)
    monkeypatch.setattr(monitor_module, "process_active_incidents", unexpected_call)
    monkeypatch.setattr(main, "observe_namespace", unexpected_call, raising=False)
    monkeypatch.setattr(main, "remediate_service", unexpected_call)
    monkeypatch.setattr(
        "app.services.k8s_remediation.execute_action",
        unexpected_call,
    )

    token = create_operator_token(permissions=["read"])
    response = api_client.get(
        "/monitor/status",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["configured_enabled"] is True
    assert body["lifecycle_state"] == "not_started"
    assert body["workflow_health"] == "unknown"
    assert body["last_poll_attempt_at"] is None
    assert monitor._last_poll_attempt_at is None
    assert "traceback" not in str(body).lower()
    assert "token" not in str(body).lower()
    assert "secret" not in str(body).lower()


def test_remediation_rejects_invalid_jwt(api_client):
    response = api_client.post(
        "/remediate/user-service",
        headers=remediation_headers(token="not-a-valid-jwt"),
        json={
            "approved": True,
            "incident_id": "inc-test-123",
        },
    )

    assert response.status_code == 401


def test_remediation_rejects_expired_jwt(api_client):
    expired_token = create_operator_token(
        expires_delta=timedelta(seconds=-10),
    )

    response = api_client.post(
        "/remediate/user-service",
        headers=remediation_headers(token=expired_token),
        json={
            "approved": True,
            "incident_id": "inc-test-123",
        },
    )

    assert response.status_code == 401


def test_remediation_rejects_operator_without_permission(api_client):
    token = create_operator_token(permissions=["read"])

    response = api_client.post(
        "/remediate/user-service",
        headers=remediation_headers(token=token),
        json={
            "approved": True,
            "incident_id": "inc-test-123",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Operator lacks remediation permission"
    )


def test_legacy_remediation_is_deprecated_for_authorized_operator(api_client):
    token = create_operator_token(operator_id="authenticated-operator")

    response = api_client.post(
        "/remediate/user-service",
        headers=remediation_headers(token=token),
        json={
            "approved": True,
            "incident_id": "inc-test-123",
        },
    )

    assert response.status_code == 410
    assert "POST /workflow/{service}/respond" in response.json()["detail"]


def test_remediation_ignores_spoofed_operator_id_header(api_client):
    token = create_operator_token(operator_id="verified-operator")

    headers = remediation_headers(token=token)
    headers["X-Operator-ID"] = "spoofed-operator"

    response = api_client.post(
        "/remediate/user-service",
        headers=headers,
        json={
            "approved": True,
            "incident_id": "inc-test-123",
        },
    )

    assert response.status_code == 410


def test_legacy_remediation_route_never_invokes_helper(
    api_client,
    monkeypatch,
):
    import app.main as main

    calls = []

    async def unexpected_remediation(**kwargs):
        calls.append(kwargs)
        pytest.fail("Deprecated route must not invoke the legacy executor")

    monkeypatch.setattr(main, "remediate_service", unexpected_remediation)
    token = create_operator_token(permissions=["remediation"])
    response = api_client.post(
        "/remediate/user-service",
        headers=remediation_headers(token=token),
        json={"approved": True, "incident_id": "inc-test-123"},
    )

    assert response.status_code == 410
    assert calls == []


def test_remediation_rejects_unknown_service(api_client):
    token = create_operator_token()

    response = api_client.post(
        "/remediate/unknown-service",
        headers=remediation_headers(token=token),
        json={
            "approved": True,
            "incident_id": "inc-test-123",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown service"


def test_remediation_rejects_jwt_signed_with_wrong_secret(api_client):
    token = create_operator_token(
        secret="incorrect-signing-secret-for-tests-only",
    )

    response = api_client.post(
        "/remediate/user-service",
        headers=remediation_headers(token=token),
        json={
            "approved": True,
            "incident_id": "inc-test-123",
        },
    )

    assert response.status_code == 401


def test_remediation_rejects_jwt_with_wrong_issuer(api_client):
    token = create_operator_token()
    payload = jwt.decode(
        token,
        TEST_JWT_SECRET,
        algorithms=["HS256"],
        audience=TEST_JWT_AUDIENCE,
    )
    payload["iss"] = "untrusted-issuer"

    now = datetime.now(timezone.utc)
    payload["iat"] = now
    payload["exp"] = now + timedelta(minutes=5)

    wrong_issuer_token = jwt.encode(
        payload,
        TEST_JWT_SECRET,
        algorithm="HS256",
    )

    response = api_client.post(
        "/remediate/user-service",
        headers=remediation_headers(token=wrong_issuer_token),
        json={
            "approved": True,
            "incident_id": "inc-test-123",
        },
    )

    assert response.status_code == 401


def test_remediation_rejects_jwt_with_wrong_audience(api_client):
    token = create_operator_token()
    payload = jwt.decode(
        token,
        TEST_JWT_SECRET,
        algorithms=["HS256"],
        audience=TEST_JWT_AUDIENCE,
    )
    payload["aud"] = "untrusted-api"

    now = datetime.now(timezone.utc)
    payload["iat"] = now
    payload["exp"] = now + timedelta(minutes=5)

    wrong_audience_token = jwt.encode(
        payload,
        TEST_JWT_SECRET,
        algorithm="HS256",
    )

    response = api_client.post(
        "/remediate/user-service",
        headers=remediation_headers(token=wrong_audience_token),
        json={
            "approved": True,
            "incident_id": "inc-test-123",
        },
    )

    assert response.status_code == 401



def test_remediation_never_runs_when_jwt_is_invalid(
    api_client,
    monkeypatch,
):
    import app.main as main

    calls = []

    async def track_remediation(**kwargs):
        calls.append(kwargs)
        return {"status": "unexpected"}

    monkeypatch.setattr(
        main,
        "remediate_service",
        track_remediation,
    )

    response = api_client.post(
        "/remediate/user-service",
        headers=remediation_headers(token="invalid-token"),
        json={
            "approved": True,
            "incident_id": "inc-test-123",
        },
    )

    assert response.status_code == 401
    assert calls == []

def test_diagnosis_handles_prometheus_failure(api_client, monkeypatch):
    import app.main as main

    async def failing_diagnosis(service: str):
        raise RuntimeError("Prometheus query failed")

    monkeypatch.setattr(main, "diagnose_service", failing_diagnosis)

    token = create_operator_token(permissions=["read"])
    response = api_client.get(
        "/diagnose/user-service",
        headers=remediation_headers(token=token),
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Monitoring data is temporarily unavailable"
    )

def test_decision_handles_prometheus_failure(api_client, monkeypatch):
    import app.main as main

    async def failing_decision(service: str):
        raise RuntimeError("Prometheus query failed")

    monkeypatch.setattr(main, "decide_action", failing_decision)

    token = create_operator_token(permissions=["read"])
    response = api_client.get(
        "/decide/user-service",
        headers=remediation_headers(token=token),
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Monitoring data is temporarily unavailable"
    )


def test_incidents_endpoint_returns_detector_result(
    api_client,
    monkeypatch,
):
    import app.main as main

    expected = {
        "incident_count": 0,
        "incidents": [],
    }

    async def fake_detect_unhealthy_services():
        return expected

    monkeypatch.setattr(
        main,
        "detect_unhealthy_services",
        fake_detect_unhealthy_services,
    )

    token = create_operator_token(permissions=["read"])
    response = api_client.get(
        "/incidents",
        headers=remediation_headers(token=token),
    )

    assert response.status_code == 200
    assert response.json() == expected


@pytest.mark.parametrize(
    ("path", "function_name"),
    [
        ("/incidents", "detect_unhealthy_services"),
        ("/diagnose/user-service", "diagnose_service"),
        ("/decide/user-service", "decide_action"),
    ],
)
@pytest.mark.parametrize(
    ("token_kind", "expected_status"),
    [
        ("missing", 401),
        ("invalid", 401),
        ("expired", 401),
        ("insufficient", 403),
    ],
)
def test_legacy_diagnostic_routes_reject_unauthorized_requests_before_work(
    api_client,
    monkeypatch,
    path,
    function_name,
    token_kind,
    expected_status,
):
    import app.main as main

    calls = []

    async def track_call(*args, **kwargs):
        calls.append((args, kwargs))
        return {"unexpected": True}

    monkeypatch.setattr(main, function_name, track_call)

    headers = None
    if token_kind == "invalid":
        headers = remediation_headers(token="invalid-token")
    elif token_kind == "expired":
        expired = create_operator_token(
            permissions=["read"],
            expires_delta=timedelta(seconds=-10),
        )
        headers = remediation_headers(token=expired)
    elif token_kind == "insufficient":
        token = create_operator_token(permissions=["remediation"])
        headers = remediation_headers(token=token)

    response = api_client.get(path, headers=headers)

    assert response.status_code == expected_status
    if expected_status == 401:
        assert response.headers["www-authenticate"] == "Bearer"
    else:
        assert response.json()["detail"] == "Operator lacks read permission"
    assert calls == []


@pytest.mark.parametrize(
    ("path", "function_name"),
    [
        ("/incidents", "detect_unhealthy_services"),
        ("/diagnose/user-service", "diagnose_service"),
        ("/decide/user-service", "decide_action"),
    ],
)
def test_legacy_diagnostic_routes_preserve_read_only_response(
    api_client,
    monkeypatch,
    path,
    function_name,
):
    import app.main as main

    expected = {"source": "test", "incident_count": 1}
    calls = []

    async def track_call(*args, **kwargs):
        calls.append((args, kwargs))
        return expected

    monkeypatch.setattr(main, function_name, track_call)
    token = create_operator_token(permissions=["read"])

    response = api_client.get(
        path,
        headers=remediation_headers(token=token),
    )

    assert response.status_code == 200
    assert response.json() == expected
    assert len(calls) == 1


def test_incident_history_returns_stored_incidents(
    api_client,
    monkeypatch,
):
    import app.main as main

    expected = [
        {
            "incident_id": "inc-k8s-test123",
            "service": "kubernetes-lab",
            "incident_type": "pod_container_failure",
            "severity": "high",
            "message": "ImagePullBackOff detected",
            "status": "RESOLVED",
            "fingerprint": (
                "k8s:kybernaut-lab:Pod:demo-web-test-pod:"
                "pod_container_failure"
            ),
            "opened_at": "2026-10-19T14:42:18+00:00",
            "updated_at": "2026-10-19T14:42:29+00:00",
            "resolved_at": "2026-10-19T14:42:29+00:00",
            "last_seen_at": "2026-10-19T14:42:18+00:00",
            "remediation_count": 0,
            "last_remediation_status": None,
        }
    ]

    monkeypatch.setattr(
        main,
        "list_incidents",
        lambda status=None, source="operational", *, latest_per_incident=False: expected,
    )

    token = create_operator_token(permissions=["read"])
    response = api_client.get(
        "/incidents/history",
        headers=remediation_headers(token=token),
    )

    assert response.status_code == 200
    assert response.json() == {
        "incidents": expected,
        "source_filter": "operational",
    }


def test_incident_history_filters_by_status(
    api_client,
    monkeypatch,
):
    import app.main as main

    calls = []

    def fake_list_incidents(
        status=None, source="operational", *, latest_per_incident=False
    ):
        calls.append((status, source, latest_per_incident))
        return []

    monkeypatch.setattr(main, "list_incidents", fake_list_incidents)

    token = create_operator_token(permissions=["read"])
    response = api_client.get(
        "/incidents/history?status=resolved",
        headers=remediation_headers(token=token),
    )

    assert response.status_code == 200
    assert response.json() == {
        "incidents": [],
        "source_filter": "operational",
    }
    assert calls == [("RESOLVED", "operational", True)]


def test_incident_history_rejects_invalid_status(api_client):
    token = create_operator_token(permissions=["read"])
    response = api_client.get(
        "/incidents/history?status=UNKNOWN",
        headers=remediation_headers(token=token),
    )

    assert response.status_code == 400
    assert response.json()["detail"].startswith("Invalid status")


def test_kubernetes_incidents_rejects_missing_token(api_client, monkeypatch):
    import app.main as main

    observer_calls = []

    def guarded_observer():
        observer_calls.append(True)
        return {}

    monkeypatch.setattr(main, "observe_namespace", guarded_observer)
    response = api_client.get("/kubernetes/incidents")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert observer_calls == []


def test_kubernetes_incidents_rejects_operator_without_read_permission(
    api_client, monkeypatch,
):
    import app.main as main

    observer_calls = []

    def guarded_observer():
        observer_calls.append(True)
        return {}

    monkeypatch.setattr(main, "observe_namespace", guarded_observer)
    token = create_operator_token(permissions=["remediation"])

    response = api_client.get(
        "/kubernetes/incidents",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Operator lacks read permission"
    assert observer_calls == []


def test_kubernetes_incidents_returns_observations(
    api_client,
    monkeypatch,
):
    import app.main as main

    expected = {
        "namespace": "kybernaut-lab",
        "incident_count": 0,
        "incidents": [],
        "deployments": [
            {
                "name": "demo-web",
                "desired_replicas": 2,
                "available_replicas": 2,
                "ready_replicas": 2,
            }
        ],
    }

    expected_persistence = {
        "persisted_count": 0,
        "active_fingerprints": [],
    }
    persistence_calls = []

    monkeypatch.setattr(
        main,
        "observe_namespace",
        lambda: expected,
    )
    monkeypatch.setattr(
        main,
        "persist_observation",
        lambda observation: (
            persistence_calls.append(observation)
            or expected_persistence
        ),
    )

    token = create_operator_token(permissions=["read"])

    response = api_client.get(
        "/kubernetes/incidents",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["namespace"] == "kybernaut-lab"
    assert body["incident_count"] == 0
    assert body["deployments"][0]["available_replicas"] == 2
    assert body["persistence"] == expected_persistence
    assert persistence_calls == [expected]


def test_kubernetes_incidents_hides_observer_errors(
    api_client,
    monkeypatch,
):
    import app.main as main

    def fail_observer():
        raise RuntimeError("internal Kubernetes details")

    persistence_calls = []

    monkeypatch.setattr(main, "observe_namespace", fail_observer)
    monkeypatch.setattr(
        main,
        "persist_observation",
        lambda observation: persistence_calls.append(observation),
    )

    token = create_operator_token(permissions=["read"])

    response = api_client.get(
        "/kubernetes/incidents",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Kubernetes observations are temporarily unavailable."
    )
    assert "internal Kubernetes details" not in response.text
    assert persistence_calls == []
