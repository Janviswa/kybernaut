import pytest

import app.api.lab as lab_api
from app.services import payment_compose_remediation as payment_action
from app.services.audit_repository import list_events, record_event
from app.services.incident_repository import record_incident
from tests.conftest import create_operator_token


PAYMENTS = [
    {
        "payment_id": "pay-1",
        "order_id": "order-1",
        "amount": 12.5,
        "status": "processed",
    },
    {
        "payment_id": "pay-2",
        "order_id": "order-2",
        "amount": 7.0,
        "status": "processed",
    },
]


def _active_payment_memory_incident(incident_id="inc-payment-memory"):
    return record_incident({
        "incident_id": incident_id,
        "service": "payment-service",
        "type": "memory_leak",
        "severity": "critical",
        "message": "Prometheus memory anomaly",
        "fingerprint": f"telemetry:{incident_id}",
        "source_evidence": {"source": "prometheus"},
    })


def test_payment_sql_escapes_values_and_rejects_conflicting_ids():
    sql = payment_action._payment_sql([{
        "payment_id": "pay-'1",
        "order_id": "order-1",
        "amount": 12.5,
        "status": "processed",
    }])

    assert "'pay-''1'" in sql
    assert "ON CONFLICT (payment_id) DO NOTHING" in sql
    assert "Existing payment identifier conflicts with source record" in sql
    assert "KYBERNAUT_PAYMENT_COUNT=" in sql


def test_payment_compose_action_uses_only_scoped_compose_commands(monkeypatch):
    calls = []

    def run(_docker, args, *, stage, timeout, input_text=None):
        calls.append((args, stage, timeout, input_text))
        return "KYBERNAUT_PAYMENT_COUNT=2\n" if stage == "migrate_payment_history" else ""

    monkeypatch.setattr(payment_action, "execution_unavailable_reason", lambda: None)
    monkeypatch.setattr(payment_action.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(payment_action, "_run", run)
    monkeypatch.setattr(payment_action, "_fetch_current_payments", lambda: PAYMENTS)
    monkeypatch.setattr(
        payment_action, "_read_json",
        lambda _docker, path, _stage: (
            {"status": "healthy", "history_storage": "postgresql"}
            if path == "/health" else PAYMENTS
        ),
    )

    result = payment_action.execute_payment_history_remediation()

    assert result == {
        "source_payment_count": 2,
        "database_payment_count": 2,
        "health_verified": True,
        "history_verified": True,
    }
    assert [call[1] for call in calls] == [
        "start_payment_database",
        "wait_for_payment_database",
        "migrate_payment_history",
        "build_payment_service",
        "replace_payment_service",
    ]
    assert calls[0][0] == ["up", "-d", "payment-postgres"]
    assert calls[2][0][:4] == [
        "exec", "-T", "payment-postgres", "psql",
    ]
    assert calls[2][0][4:8] == [
        "-v", "ON_ERROR_STOP=1", "-A", "-t",
    ]
    assert calls[3][0] == ["build", "payment-service"]
    assert calls[4][0] == [
        "up", "-d", "--no-deps", "payment-service",
    ]
    assert calls[2][3] is not None


def test_payment_compose_failure_does_not_replace_service(monkeypatch):
    calls = []

    def run(_docker, args, *, stage, timeout, input_text=None):
        calls.append(stage)
        if stage == "migrate_payment_history":
            raise payment_action.PaymentComposeRemediationError(
                stage, "migration failed"
            )
        return ""

    monkeypatch.setattr(payment_action, "execution_unavailable_reason", lambda: None)
    monkeypatch.setattr(payment_action.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(payment_action, "_run", run)
    monkeypatch.setattr(payment_action, "_fetch_current_payments", lambda: PAYMENTS)

    with pytest.raises(payment_action.PaymentComposeRemediationError) as error:
        payment_action.execute_payment_history_remediation()

    assert error.value.stage == "migrate_payment_history"
    assert "build_payment_service" not in calls
    assert "replace_payment_service" not in calls


def test_payment_service_health_verification_waits_for_the_replacement(
    monkeypatch,
):
    results = iter([
        None,
        {"status": "healthy", "history_storage": "postgresql"},
    ])
    monkeypatch.setattr(
        payment_action, "_read_json",
        lambda *_args: next(results),
    )
    monkeypatch.setattr(payment_action.time, "sleep", lambda _seconds: None)

    payment_action._wait_for_payment_health("/usr/bin/docker")


def test_payment_memory_option_is_executable_only_for_local_compose(
    api_client, monkeypatch,
):
    _active_payment_memory_incident()
    monkeypatch.setenv("KYBERNAUT_ENVIRONMENT", "local-demo")
    monkeypatch.setattr(payment_action.shutil, "which", lambda _: "/usr/bin/docker")

    response = api_client.get(
        "/incidents/inc-payment-memory/remediation-options",
        headers={
            "Authorization": f"Bearer {create_operator_token(permissions=['read'])}"
        },
    )

    assert response.status_code == 200
    body = response.json()
    action = body["options"][0]
    assert action["action"] == "migrate_payment_history_and_deploy"
    assert action["executable"] is True
    assert action["requires_approval"] is True
    assert "Pause all payment writes" in action["steps"][0]
    assert "incident stays open" in body["note"]


def test_payment_compose_endpoint_requires_write_pause_confirmation(
    api_client, monkeypatch,
):
    _active_payment_memory_incident()
    monkeypatch.setenv("KYBERNAUT_ENVIRONMENT", "local-demo")
    monkeypatch.setattr(payment_action.shutil, "which", lambda _: "/usr/bin/docker")

    response = api_client.post(
        "/incidents/inc-payment-memory/execute-payment-compose-remediation",
        headers={
            "Authorization": f"Bearer {create_operator_token()}"
        },
        json={"confirm_write_pause": False},
    )

    assert response.status_code == 422


def test_payment_compose_endpoint_audits_and_does_not_resolve_incident(
    api_client, monkeypatch,
):
    _active_payment_memory_incident()
    monkeypatch.setenv("KYBERNAUT_ENVIRONMENT", "local-demo")
    monkeypatch.setattr(payment_action.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        lab_api.payment_compose_remediation,
        "execute_payment_history_remediation",
        lambda: {
            "source_payment_count": 2,
            "database_payment_count": 2,
            "health_verified": True,
            "history_verified": True,
        },
    )

    response = api_client.post(
        "/incidents/inc-payment-memory/execute-payment-compose-remediation",
        headers={
            "Authorization": f"Bearer {create_operator_token()}"
        },
        json={"confirm_write_pause": True},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["incident_resolved"] is False
    assert body["audit_recorded"] is True
    assert body["health_verified"] is True
    events = list_events("inc-payment-memory", 20)
    assert [event["event_type"] for event in events] == [
        "payment_compose_remediation_started",
        "payment_compose_remediation_completed",
    ]


def test_payment_compose_endpoint_does_not_repeat_a_completed_action(
    api_client, monkeypatch,
):
    _active_payment_memory_incident()
    record_event(
        incident_ref="inc-payment-memory",
        event_type="payment_compose_remediation_completed",
        actor="test-operator",
        summary="Completed payment Compose remediation",
        resource="payment-service",
        outcome="deployed_unverified",
    )
    monkeypatch.setenv("KYBERNAUT_ENVIRONMENT", "local-demo")
    monkeypatch.setattr(payment_action.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        lab_api.payment_compose_remediation,
        "execute_payment_history_remediation",
        lambda: pytest.fail("completed remediation must not be repeated"),
    )

    response = api_client.post(
        "/incidents/inc-payment-memory/execute-payment-compose-remediation",
        headers={"Authorization": f"Bearer {create_operator_token()}"},
        json={"confirm_write_pause": True},
    )

    assert response.status_code == 409
    assert "already deployed" in response.json()["detail"]
