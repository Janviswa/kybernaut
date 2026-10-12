import pytest

import app.database as database
from app.services.incident_repository import (
    list_incidents,
    record_incident,
    resolve_open_incidents,
)


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """Use a temporary SQLite database for each test."""
    test_db = tmp_path / "test_incidents.db"
    monkeypatch.setattr(database, "DATABASE_PATH", test_db)
    database.initialize_database()


def sample_incident():
    return {
        "incident_id": "inc-test-user-down",
        "service": "user-service",
        "type": "service_unavailable",
        "severity": "critical",
        "message": "user-service is down",
    }


def test_record_incident_creates_open_incident():
    incident = record_incident(sample_incident())

    assert incident["incident_id"] == "inc-test-user-down"
    assert incident["service"] == "user-service"
    assert incident["status"] == "OPEN"

    open_incidents = list_incidents(status="OPEN")
    assert len(open_incidents) == 1


def test_record_incident_refreshes_existing_open_incident():
    first = record_incident(sample_incident())

    updated_data = sample_incident()
    updated_data["message"] = "user-service remains down"
    second = record_incident(updated_data)

    assert second["id"] == first["id"]
    assert second["message"] == "user-service remains down"
    assert len(list_incidents(status="OPEN")) == 1


def test_resolve_open_incident_after_recovery():
    record_incident(sample_incident())

    resolved_count = resolve_open_incidents("user-service")

    assert resolved_count == 1
    assert list_incidents(status="OPEN") == []

    resolved = list_incidents(status="RESOLVED")
    assert len(resolved) == 1
    assert resolved[0]["resolved_at"] is not None


def test_resolving_service_without_open_incident_returns_zero():
    assert resolve_open_incidents("user-service") == 0


def test_recurring_incident_creates_new_occurrence():
    first = record_incident(sample_incident())

    resolve_open_incidents("user-service")

    second = record_incident(sample_incident())

    assert second["id"] != first["id"]
    assert second["incident_id"] == first["incident_id"]
    assert second["status"] == "OPEN"

    all_incidents = list_incidents()
    assert len(all_incidents) == 2

    resolved = list_incidents(status="RESOLVED")
    open_incidents = list_incidents(status="OPEN")

    assert len(resolved) == 1
    assert len(open_incidents) == 1
