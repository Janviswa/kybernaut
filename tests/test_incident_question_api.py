import json
from datetime import datetime, timezone

import pytest
import app.api.operations as operations
import app.database as database
from app.services import audit_repository, incident_repository, remediation_repository
from tests.conftest import create_operator_token


@pytest.fixture(autouse=True)
def isolate_database(tmp_path, monkeypatch):
    database_path = tmp_path / "incident-question-tests.sqlite3"
    monkeypatch.setattr(database, "DATABASE_PATH", database_path)
    monkeypatch.setenv("KYBERNAUT_DATABASE_PATH", str(database_path))


def _auth(permissions=("read",)):
    token = create_operator_token(permissions=list(permissions))
    return {"Authorization": f"Bearer {token}"}


def _memory_incident():
    observed_at = datetime.now(timezone.utc).isoformat()
    incident = incident_repository.record_incident({
        "incident_id": "inc-question-memory",
        "service": "inventory-service",
        "type": "memory_leak",
        "severity": "high",
        "message": (
            "Prometheus detected a memory anomaly. "
            "Authorization: Bearer test-secret-token"
        ),
        "fingerprint": "prometheus:inventory-service:memory_bytes",
        "source_timestamp": observed_at,
        "source_evidence": {
            "schema_version": 1,
            "source": "prometheus",
            "service": "inventory-service",
            "incident_type": "memory_leak",
            "observed_at": observed_at,
            "source_timestamp": observed_at,
            "metric": {
                "name": "memory_bytes",
                "value": 50_962_432,
                "unit": "bytes",
                "baseline_median": 49_991_680,
                "deviation_score": 23.07,
                "threshold": 5.0,
                "direction": "HIGH",
                "classification": "actionable_candidate",
            },
        },
    })
    with database.get_connection() as connection:
        connection.execute(
            "UPDATE incidents SET status = 'RESOLVED' WHERE id = ?",
            (incident["id"],),
        )
    return incident


def test_incident_question_returns_grounded_memory_evidence_without_fake_cause_or_fix(
    api_client, monkeypatch,
):
    incident = _memory_incident()
    captured = {}

    async def answer(incident_id, question, evidence, history):
        captured.update(
            incident_id=incident_id,
            question=question,
            evidence=evidence,
            history=history,
        )
        return (
            "The inventory-service memory anomaly recorded 50,962,432 bytes "
            "against a 49,991,680-byte baseline, with a 23.07 score over "
            "the 5.0 threshold. The available record does not confirm the "
            "underlying cause or a remediation fix."
        )

    monkeypatch.setattr(operations, "answer_incident_question", answer)
    response = api_client.post(
        f"/incidents/{incident['incident_id']}/ask",
        headers=_auth(),
        json={
            "question": "How did the memory leak happen, and how was it fixed?",
            "history": [{
                "role": "user",
                "content": "What happened? api_key=test-chat-secret",
            }],
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["incident_id"] == incident["incident_id"]
    assert "50,962,432" in result["answer"]
    assert "does not confirm" in result["answer"]
    assert result["generated_at"]
    anomaly = next(
        item for item in result["attachments"]
        if item["id"] == "anomaly-evidence"
    )
    assert anomaly["content"]["metric"] == {
        "name": "memory_bytes",
        "value": 50_962_432,
        "unit": "bytes",
        "baseline_median": 49_991_680,
        "deviation_score": 23.07,
        "threshold": 5.0,
        "direction": "HIGH",
        "classification": "actionable_candidate",
    }
    assert "confirmed root cause" in result["missing_evidence"]
    assert "recorded remediation execution" in result["missing_evidence"]
    assert captured["incident_id"] == incident["incident_id"]
    assert captured["history"] == [{
        "role": "user",
        "content": "What happened? api_key=[REDACTED]",
    }]
    assert captured["evidence"]["incident"]["status"] == "RESOLVED"
    assert captured["evidence"]["remediation_attempts"] == []
    assert "test-secret-token" not in response.text
    assert "test-chat-secret" not in response.text
    assert "test-secret-token" not in json.dumps(captured["evidence"])


def test_incident_question_includes_only_exact_incident_approval_and_remediation_records(
    api_client, monkeypatch,
):
    incident = _memory_incident()
    audit_repository.create_approval(
        incident_ref=incident["incident_id"],
        proposed_action={"kind": "restart"},
        context={},
        requested_by="operator-a",
    )
    audit_repository.create_approval(
        incident_ref="inc-unrelated",
        proposed_action={"kind": "restart"},
        context={},
        requested_by="operator-b",
    )
    remediation_repository.record_attempt(
        audit_id="attempt-question-memory",
        incident_id=incident["incident_id"],
        service="inventory-service",
        status="verified",
        message="Verification succeeded.",
    )
    remediation_repository.record_attempt(
        audit_id="attempt-unrelated",
        incident_id="inc-unrelated",
        service="payment-service",
        status="verified",
        message="Unrelated execution.",
    )
    seen = {}

    async def answer(_incident_id, _question, evidence, _history):
        seen.update(evidence)
        return "A remediation attempt is recorded with verified status."

    monkeypatch.setattr(operations, "answer_incident_question", answer)
    response = api_client.post(
        f"/incidents/{incident['incident_id']}/ask",
        headers=_auth(),
        json={"question": "How was it fixed?"},
    )

    assert response.status_code == 200
    assert len(seen["approvals"]) == 1
    assert seen["approvals"][0]["status"] == "PENDING"
    assert len(seen["remediation_attempts"]) == 1
    assert seen["remediation_attempts"][0]["status"] == "verified"
    assert all(
        event["incident_ref"] == incident["incident_id"]
        for event in audit_repository.list_events(incident["incident_id"])
    )


def test_incident_question_requires_read_authorization_before_retrieval(
    api_client, monkeypatch,
):
    incident = _memory_incident()

    async def unexpected_answer(*_args):
        raise AssertionError("LLM must not be called for an unauthorized request")

    monkeypatch.setattr(operations, "answer_incident_question", unexpected_answer)
    response = api_client.post(
        f"/incidents/{incident['incident_id']}/ask",
        json={"question": "What happened?"},
    )

    assert response.status_code == 401


def test_incident_question_provider_failure_is_explicit_and_sanitized(
    api_client, monkeypatch,
):
    incident = _memory_incident()

    async def failed_answer(*_args):
        raise RuntimeError("provider response contained sensitive data")

    monkeypatch.setattr(operations, "answer_incident_question", failed_answer)
    response = api_client.post(
        f"/incidents/{incident['incident_id']}/ask",
        headers=_auth(),
        json={"question": "Summarize the RCA"},
    )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Incident question answering is temporarily unavailable."
    )
    assert "sensitive data" not in response.text


def test_incident_question_returns_not_found_without_calling_model(
    api_client, monkeypatch,
):
    async def unexpected_answer(*_args):
        raise AssertionError("LLM must not be called for a missing incident")

    monkeypatch.setattr(operations, "answer_incident_question", unexpected_answer)
    response = api_client.post(
        "/incidents/inc-missing-question/ask",
        headers=_auth(),
        json={"question": "What happened?"},
    )

    assert response.status_code == 404


def test_incident_question_rejects_oversized_history(api_client):
    response = api_client.post(
        "/incidents/inc-question-memory/ask",
        headers=_auth(),
        json={
            "question": "What happened?",
            "history": [
                {"role": "user", "content": str(index)}
                for index in range(9)
            ],
        },
    )

    assert response.status_code == 422
