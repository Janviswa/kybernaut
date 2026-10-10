
import json

import pytest

import app.services.remediation_service as remediation


@pytest.fixture
def audit_log(tmp_path, monkeypatch):
    """Keep audit records inside the temporary test directory."""
    path = tmp_path / "remediation_audit.jsonl"
    monkeypatch.setattr(remediation, "AUDIT_PATH", path)
    return path


@pytest.mark.asyncio
async def test_unapproved_remediation_is_rejected(
    audit_log, monkeypatch
):
    async def unexpected_call(*args, **kwargs):
        pytest.fail("No health check or remediation should run")

    monkeypatch.setattr(
        remediation, "get_active_incident_id", unexpected_call
    )
    monkeypatch.setattr(
        remediation, "check_service_health", unexpected_call
    )

    result = await remediation.remediate_service(
        service="user-service",
        approved=False,
        incident_id="incident-test-1",
    )

    assert result["status"] == "rejected"
    assert "approval is required" in result["message"]

    records = [
        json.loads(line)
        for line in audit_log.read_text().splitlines()
    ]
    assert len(records) == 1
    assert records[0]["status"] == "rejected"


@pytest.mark.asyncio
async def test_invalid_service_is_rejected():
    with pytest.raises(ValueError, match="not allowlisted"):
        await remediation.remediate_service(
            service="unknown-service",
            approved=True,
            incident_id="incident-test-2",
        )


@pytest.mark.asyncio
async def test_empty_incident_id_is_rejected():
    with pytest.raises(ValueError, match="incident_id"):
        await remediation.remediate_service(
            service="user-service",
            approved=True,
            incident_id="   ",
        )


@pytest.mark.asyncio
async def test_stale_incident_is_skipped(
    audit_log, monkeypatch
):
    async def fake_incident_id(service):
        return "current-incident"

    async def fake_health(service):
        return False

    monkeypatch.setattr(
        remediation, "get_active_incident_id", fake_incident_id
    )
    monkeypatch.setattr(
        remediation, "check_service_health", fake_health
    )

    def unexpected_reservation(**kwargs):
        pytest.fail("Stale incident must not reserve an attempt")

    monkeypatch.setattr(
        remediation, "reserve_attempt", unexpected_reservation
    )

    result = await remediation.remediate_service(
        service="user-service",
        approved=True,
        incident_id="old-incident",
    )

    assert result["status"] == "skipped"
    assert result["incident_matches"] is False

    records = audit_log.read_text().splitlines()
    assert len(records) == 1


@pytest.mark.asyncio
async def test_healthy_service_is_skipped(
    audit_log, monkeypatch
):
    async def fake_incident_id(service):
        return "current-incident"

    async def fake_health(service):
        return True

    monkeypatch.setattr(
        remediation, "get_active_incident_id", fake_incident_id
    )
    monkeypatch.setattr(
        remediation, "check_service_health", fake_health
    )

    def unexpected_reservation(**kwargs):
        pytest.fail("Healthy service must not reserve an attempt")

    monkeypatch.setattr(
        remediation, "reserve_attempt", unexpected_reservation
    )

    result = await remediation.remediate_service(
        service="user-service",
        approved=True,
        incident_id="current-incident",
    )

    assert result["status"] == "skipped"
    assert result["health_before"] is True

@pytest.mark.asyncio
async def test_blocked_reservation_does_not_run_helper(
    audit_log, monkeypatch
):
    async def fake_incident_id(service):
        return "current-incident"

    async def fake_health(service):
        return False

    monkeypatch.setattr(
        remediation, "get_active_incident_id", fake_incident_id
    )
    monkeypatch.setattr(
        remediation, "check_service_health", fake_health
    )

    monkeypatch.setattr(
        remediation,
        "reserve_attempt",
        lambda **kwargs: {
            "allowed": False,
            "attempt_count": 3,
            "reason": "Maximum remediation attempts reached.",
        },
    )

    async def unexpected_helper(*args, **kwargs):
        pytest.fail("A blocked reservation must not run the helper")

    monkeypatch.setattr(
        remediation.asyncio,
        "create_subprocess_exec",
        unexpected_helper,
    )

    result = await remediation.remediate_service(
        service="user-service",
        approved=True,
        incident_id="current-incident",
    )

    assert result["status"] == "blocked"
    assert result["attempt_count"] == 3


@pytest.mark.asyncio
async def test_successful_remediation_is_verified(
    audit_log, monkeypatch
):
    async def fake_incident_id(service):
        return "current-incident"

    health_results = iter([False, True])

    async def fake_health(service):
        return next(health_results)

    monkeypatch.setattr(
        remediation, "get_active_incident_id", fake_incident_id
    )
    monkeypatch.setattr(
        remediation, "check_service_health", fake_health
    )
    monkeypatch.setattr(
        remediation,
        "reserve_attempt",
        lambda **kwargs: {"allowed": True, "attempt_count": 1},
    )

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"Simulated restart", b""

    async def fake_subprocess(*args, **kwargs):
        return FakeProcess()

    async def no_wait(_seconds):
        return None

    updated_attempts = []

    def fake_update_attempt_result(**kwargs):
        updated_attempts.append(kwargs)
        return True

    monkeypatch.setattr(
        remediation.asyncio, "create_subprocess_exec", fake_subprocess
    )
    monkeypatch.setattr(remediation.asyncio, "sleep", no_wait)
    monkeypatch.setattr(
        remediation, "update_attempt_result", fake_update_attempt_result
    )

    result = await remediation.remediate_service(
        service="user-service",
        approved=True,
        incident_id="current-incident",
    )

    assert result["status"] == "verified"
    assert result["health_before"] is False
    assert result["health_after"] is True
    assert updated_attempts[0]["status"] == "verified"
