import app.database as database
import pytest

from app.services.incident_metrics import calculate_incident_metrics
from app.services.incident_repository import record_incident, resolve_open_incidents


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "metrics.db")
    database.initialize_database()


def make_incident(incident_id, status_type, fingerprint=None):
    incident = {
        "incident_id": incident_id,
        "service": "kubernetes-lab",
        "type": status_type,
        "severity": "high",
        "message": "Test incident",
    }
    if fingerprint:
        incident["fingerprint"] = fingerprint
    return incident


def test_empty_database_returns_zero_metrics():
    metrics = calculate_incident_metrics()

    assert metrics["total_incidents"] == 0
    assert metrics["active_incidents"] == 0
    assert metrics["mttr_seconds"] is None
    assert metrics["recurrence_rate"] == 0.0


def test_counts_incidents_by_status_severity_and_service():
    record_incident(make_incident("inc-1", "pod_container_failure"))

    metrics = calculate_incident_metrics()

    assert metrics["total_incidents"] == 1
    assert metrics["active_incidents"] == 1
    assert metrics["by_status"] == {"OPEN": 1}
    assert metrics["by_severity"] == {"high": 1}
    assert metrics["by_service"] == {"kubernetes-lab": 1}


def test_recurring_fingerprint_is_counted_after_resolution():
    fingerprint = "k8s:kybernaut-lab:Pod:demo-web-pod:pod_container_failure"

    record_incident(make_incident("inc-1", "pod_container_failure", fingerprint))
    resolve_open_incidents("kubernetes-lab")
    record_incident(make_incident("inc-2", "pod_container_failure", fingerprint))

    metrics = calculate_incident_metrics()

    assert metrics["total_incidents"] == 2
    assert metrics["recurring_fingerprint_count"] == 1
    assert metrics["recurring_occurrences"] == 2
    assert metrics["recurrence_rate"] == 1.0
