import importlib
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.database as database
from app.services import audit_repository, demo_incident_scheduler as demo
from app.services.incident_repository import list_incidents
from tests.conftest import (
    TEST_JWT_AUDIENCE,
    TEST_JWT_SECRET,
    TEST_JWT_ISSUER,
    create_operator_token,
)


@pytest.fixture(autouse=True)
def isolate_default_database(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "test.db")
    monkeypatch.delenv(demo.DEMO_ENABLED_ENV, raising=False)
    monkeypatch.delenv(demo.DEMO_ENVIRONMENT_ENV, raising=False)


@pytest.fixture
def isolated_demo_database(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "demo.db")
    monkeypatch.setenv(demo.DEMO_ENABLED_ENV, "true")
    monkeypatch.setenv(demo.DEMO_ENVIRONMENT_ENV, "local-demo")
    database.initialize_database()
    audit_repository.ensure_schema()
    return tmp_path / "demo.db"


@pytest.fixture
def enabled_demo_api_client(monkeypatch):
    monkeypatch.setenv(demo.DEMO_ENABLED_ENV, "true")
    monkeypatch.setenv(demo.DEMO_ENVIRONMENT_ENV, "local-demo")
    monkeypatch.setenv("KYBERNAUT_ANOMALY_MONITOR_ENABLED", "false")
    monkeypatch.setenv("KYBERNAUT_JWT_SECRET", TEST_JWT_SECRET)
    monkeypatch.setenv("KYBERNAUT_JWT_ISSUER", TEST_JWT_ISSUER)
    monkeypatch.setenv("KYBERNAUT_JWT_AUDIENCE", TEST_JWT_AUDIENCE)

    import app.core.jwt_auth as jwt_auth

    importlib.reload(jwt_auth)
    import app.main as main

    main = importlib.reload(main)
    with TestClient(main.app) as client:
        yield client


def _successful_rca(_state):
    async def result():
        return {
            "rca": {
                "root_cause": "Synthetic scenario used to demonstrate the RCA path.",
                "confidence": 0.5,
                "evidence": ["SIMULATED DEMO: no live evidence was collected."],
                "recommended_action": "investigate_further",
                "requires_human_approval": True,
            },
            "rca_status": "completed",
            "rca_error": "",
        }

    return result()


@pytest.mark.parametrize(
    ("environment", "enabled", "expected"),
    [
        ({}, False, False),
        ({"KYBERNAUT_DEMO_MODE": "true"}, False, False),
        (
            {
                "KYBERNAUT_DEMO_MODE": "true",
                "KYBERNAUT_ENVIRONMENT": "production",
            },
            False,
            False,
        ),
        (
            {
                "KYBERNAUT_DEMO_MODE": "true",
                "KYBERNAUT_ENVIRONMENT": "local-demo",
            },
            True,
            True,
        ),
    ],
)
def test_demo_mode_requires_explicit_local_opt_in(
    monkeypatch, environment, enabled, expected
):
    for key in (demo.DEMO_ENABLED_ENV, demo.DEMO_ENVIRONMENT_ENV):
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    assert demo.demo_mode_enabled() is expected


def test_demo_interval_defaults_to_180_and_validates(monkeypatch):
    monkeypatch.delenv(demo.DEMO_INTERVAL_ENV, raising=False)
    assert demo.demo_interval_seconds() == 180

    monkeypatch.setenv(demo.DEMO_INTERVAL_ENV, "90")
    assert demo.demo_interval_seconds() == 90
    for invalid in ("", "NaN", "9", "86401", "not-a-number"):
        monkeypatch.setenv(demo.DEMO_INTERVAL_ENV, invalid)
        with pytest.raises(demo.DemoConfigurationError):
            demo.demo_interval_seconds()


def test_first_occurrence_is_scheduled_one_full_interval_after_entry():
    anchor = datetime(2026, 10, 27, tzinfo=timezone.utc)
    due, index = demo._next_occurrence(
        anchor, anchor + timedelta(seconds=1), 180
    )
    assert due == anchor + timedelta(seconds=180)
    assert index == 1


@pytest.mark.asyncio
async def test_demo_incident_persists_idempotently_and_runs_safe_diagnosis(
    isolated_demo_database, monkeypatch
):
    calls = []

    async def successful_rca(state):
        calls.append(state)
        return await _successful_rca(state)

    monkeypatch.setattr(demo, "analyze_root_cause", successful_rca)
    import app.services.k8s_remediation as remediation

    def forbidden_remediation(*_args, **_kwargs):
        raise AssertionError("Demo incident must never submit remediation")

    monkeypatch.setattr(remediation, "execute_action", forbidden_remediation)
    slot = "2026-10-27T00:00:00+00:00#1"

    first = await demo.generate_demo_occurrence(slot, 0)
    duplicate = await demo.generate_demo_occurrence(slot, 0)
    records = list_incidents()

    assert first["created"] is True
    assert first["simulation"] == "demo"
    assert first["scenario"] == "PodCrashLoopBackOff"
    assert first["diagnosis_status"] == "completed"
    assert first["rca_status"] == "completed"
    assert first["policy_action"] == "investigate_monitoring"
    assert first["audit_status"] == "success"
    assert duplicate["created"] is False
    assert len(records) == 1
    assert records[0]["service"] == "demo-synthetic"
    assert records[0]["source_evidence"]["simulated"] is True
    assert records[0]["source_evidence"]["workload_reference"] is None
    assert records[0]["diagnosis"]["simulation"] == "demo"
    assert records[0]["diagnosis"]["remediation"] == "not_submitted"
    assert len(calls) == 1
    events = audit_repository.list_events(first["incident_id"])
    assert {
        event["event_type"] for event in events
    } == {
        "simulated_demo_incident_created",
        "simulated_demo_diagnosis_completed",
    }


@pytest.mark.asyncio
async def test_demo_rca_failure_is_persisted_as_limited_not_success(
    isolated_demo_database, monkeypatch
):
    async def unavailable_rca(_state):
        return {
            "rca": None,
            "rca_status": "unavailable",
            "rca_error": "groq_api_key_not_configured",
        }

    monkeypatch.setattr(demo, "analyze_root_cause", unavailable_rca)
    result = await demo.generate_demo_occurrence(
        "2026-10-27T00:03:00+00:00#1", 1
    )
    incident = list_incidents()[0]

    assert result["diagnosis_status"] == "limited"
    assert result["rca_status"] == "unavailable"
    assert result["rca_error"] == "groq_api_key_not_configured"
    assert incident["diagnosis"]["root_cause_analysis"] is None
    assert incident["diagnosis"]["root_cause_analysis_status"] == "unavailable"


@pytest.mark.asyncio
async def test_demo_persistence_failure_is_not_reported_as_created(
    isolated_demo_database, monkeypatch
):
    from app.services import incident_repository

    def fail_persist(*_args, **_kwargs):
        raise OSError("synthetic database failure")

    monkeypatch.setattr(
        incident_repository, "record_incident_in_transaction", fail_persist
    )
    with pytest.raises(OSError, match="synthetic database failure"):
        await demo.generate_demo_occurrence(
            "2026-10-27T00:06:00+00:00#1", 2
        )
    assert list_incidents() == []


@pytest.mark.asyncio
async def test_scheduler_is_singleton_per_app_and_cancels_on_shutdown(
    isolated_demo_database, monkeypatch
):
    monkeypatch.setenv(demo.DEMO_ENABLED_ENV, "true")
    monkeypatch.setenv(demo.DEMO_ENVIRONMENT_ENV, "local-demo")
    scheduler = demo.DemoIncidentScheduler()

    status = await scheduler.enter_session("session-one", "operator")
    first_task = scheduler._task
    repeated = await scheduler.enter_session("session-one", "operator")

    assert status["enabled"] is True
    assert status["interval_seconds"] == 180
    assert status["next_scheduled_at"] is not None
    assert scheduler._task is first_task
    assert repeated["status"] == "running"

    await scheduler.shutdown()
    assert scheduler._task is None


def test_demo_session_and_occurrence_tables_have_durable_unique_keys(
    isolated_demo_database,
):
    with database.get_connection() as connection:
        sessions = {
            row["name"] for row in connection.execute(
                "PRAGMA table_info(demo_sessions)"
            )
        }
        occurrences = {
            row["name"] for row in connection.execute(
                "PRAGMA table_info(demo_incident_occurrences)"
            )
        }
    assert {"session_id", "operator_id", "expires_at"} <= sessions
    assert {
        "scheduled_slot",
        "incident_id",
        "incident_row_id",
        "status",
    } <= occurrences


def test_demo_tables_are_not_created_without_explicit_local_opt_in():
    database.initialize_database()
    with database.get_connection() as connection:
        names = {
            row["name"] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "demo_sessions" not in names
    assert "demo_incident_occurrences" not in names


@pytest.mark.asyncio
async def test_status_surfaces_recent_scheduler_failure_over_older_occurrence(
    isolated_demo_database,
):
    slot = "2026-10-27T00:00:00+00:00#1"
    demo._persist_demo_incident(slot, demo.SCENARIOS[0])
    demo._set_occurrence_outcome(slot, status="completed")
    scheduler = demo.DemoIncidentScheduler()
    scheduler._last_outcome = {
        "scheduled_slot": "2026-10-27T00:03:00+00:00#1",
        "status": "failed",
        "error_code": "OSError",
        "simulation": "demo",
        "updated_at": (demo._now() + timedelta(seconds=1)).isoformat(),
    }

    result = await scheduler.status()

    assert result["last_generation"]["status"] == "failed"
    assert result["last_generation"]["error_code"] == "OSError"


def test_generated_demo_incident_is_discoverable_through_authenticated_history(
    enabled_demo_api_client, monkeypatch
):
    async def successful_rca(_state):
        return {
            "rca": None,
            "rca_status": "unavailable",
            "rca_error": "test_provider_unavailable",
        }

    monkeypatch.setattr(demo, "analyze_root_cause", successful_rca)
    asyncio.run(demo.generate_demo_occurrence(
        "2026-10-27T00:00:00+00:00#1", 0
    ))
    token = create_operator_token(
        operator_id="read-only",
        permissions=["read"],
    )

    response = enabled_demo_api_client.get(
        "/incidents/history?source=demo",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    incident = response.json()["incidents"][0]
    assert incident["source_evidence"]["simulation"] == "demo"
    assert incident["diagnosis"]["root_cause_analysis_status"] == "unavailable"
    assert incident["diagnosis"]["policy_recommendation"]["action"] == (
        "investigate_monitoring"
    )


def test_three_distinct_demo_incidents_are_traceable_without_remediation(
    enabled_demo_api_client, monkeypatch
):
    async def successful_rca(_state):
        return {
            "rca": {
                "root_cause": "Synthetic scenario only; no live cause was inferred.",
                "confidence": 0.0,
                "evidence": ["SIMULATED DEMO: no live evidence was collected."],
                "recommended_action": "investigate_further",
                "requires_human_approval": False,
            },
            "rca_status": "completed",
            "rca_error": "",
        }

    monkeypatch.setattr(demo, "analyze_root_cause", successful_rca)
    scenarios = (0, 2, 3)
    slots = (
        "2026-10-27T00:00:00+00:00#1",
        "2026-10-27T00:03:00+00:00#1",
        "2026-10-27T00:06:00+00:00#1",
    )
    outcomes = [
        asyncio.run(demo.generate_demo_occurrence(slot, index))
        for slot, index in zip(slots, scenarios)
    ]
    token = create_operator_token(
        operator_id="read-only",
        permissions=["read"],
    )

    response = enabled_demo_api_client.get(
        "/incidents/history?source=demo",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    incidents = response.json()["incidents"]
    demos = [
        incident for incident in incidents
        if incident["source_evidence"].get("simulation") == "demo"
    ]
    assert len(demos) == 3
    assert len({incident["incident_id"] for incident in demos}) == 3
    assert {
        incident["source_evidence"]["scenario"] for incident in demos
    } == {"PodCrashLoopBackOff", "CpuSaturation", "MemoryPressure"}
    assert all(
        incident["source_evidence"]["workload_reference"] is None
        and incident["diagnosis_status"] == "completed"
        and incident["diagnosis"]["root_cause_analysis_status"] == "completed"
        and incident["diagnosis"]["policy_recommendation"]["action"]
        == "investigate_monitoring"
        and incident["diagnosis"]["remediation"] == "not_submitted"
        for incident in demos
    )
    assert all(outcome["created"] for outcome in outcomes)
    for incident in demos:
        events = audit_repository.list_events(incident["incident_id"])
        assert {
            event["event_type"] for event in events
        } == {
            "simulated_demo_incident_created",
            "simulated_demo_diagnosis_completed",
        }

    with database.get_connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM approval_requests"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM action_claims"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM incident_handoffs"
        ).fetchone()[0] == 0


def test_read_only_operator_can_query_status_but_cannot_start_without_opt_in(
    api_client, monkeypatch
):
    token = create_operator_token(
        operator_id="read-only",
        permissions=["read"],
    )
    headers = {"Authorization": f"Bearer {token}"}

    status_response = api_client.get("/demo/status", headers=headers)
    enter_response = api_client.post(
        "/demo/session",
        headers=headers,
        json={"session_id": "bcabde39-3886-4d6e-bfc4-795ce9519e89"},
    )

    assert status_response.status_code == 404
    assert enter_response.status_code == 404


def test_enabled_demo_session_requires_read_permission(
    enabled_demo_api_client,
):
    assert enabled_demo_api_client.get("/demo/status").status_code == 401
    remediation_token = create_operator_token(
        operator_id="remediation-only",
        permissions=["remediation"],
    )
    assert enabled_demo_api_client.get(
        "/demo/status",
        headers={"Authorization": f"Bearer {remediation_token}"},
    ).status_code == 403

    read_token = create_operator_token(
        operator_id="read-only",
        permissions=["read"],
    )
    headers = {"Authorization": f"Bearer {read_token}"}
    session_id = "bcabde39-3886-4d6e-bfc4-795ce9519e89"
    entered = enabled_demo_api_client.post(
        "/demo/session",
        headers=headers,
        json={"session_id": session_id},
    )
    assert entered.status_code == 200
    assert entered.json()["status"] == "running"
    assert enabled_demo_api_client.request(
        "DELETE",
        "/demo/session",
        headers=headers,
        json={"session_id": session_id},
    ).status_code == 200


def test_demo_routes_are_absent_when_demo_mode_is_disabled(api_client):
    assert api_client.get("/demo/status").status_code == 404
