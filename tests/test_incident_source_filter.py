"""Operational vs. synthetic demo incident separation (read-only API)."""

import pytest

import app.database as database
from app.database import get_connection
from app.services import audit_repository as audit
from app.services.incident_metrics import calculate_incident_metrics
from app.services.incident_repository import list_incidents, record_incident
from tests.conftest import create_operator_token


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "source.db")
    database.initialize_database()


def auth(perms=("read",)):
    token = create_operator_token(permissions=list(perms))
    return {"Authorization": f"Bearer {token}"}


def _operational(incident_id="inc-real-1"):
    return record_incident({
        "incident_id": incident_id,
        "service": "kubernetes-lab",
        "type": "pod_container_failure",
        "severity": "high",
        "message": "Observed pod failure",
        "fingerprint": "k8s:kybernaut-lab:Pod:demo-web:pod_container_failure",
    })


def _demo(incident_id="DEMO-abc123"):
    return record_incident({
        "incident_id": incident_id,
        "service": "demo-synthetic",
        "type": "pod_crash",
        "severity": "high",
        "message": "SIMULATED DEMO: PodCrashLoopBackOff.",
        "fingerprint": "demo:slot-1",
        "source_evidence": {
            "simulation": "demo",
            "simulated": True,
            "scenario": "PodCrashLoopBackOff",
            "workload_reference": None,
        },
    })


def _ids(response):
    return {item["incident_id"] for item in response.json()["incidents"]}


def test_default_history_excludes_demo_incidents(api_client):
    _operational()
    _demo()

    response = api_client.get("/incidents/history", headers=auth())

    assert response.status_code == 200
    assert _ids(response) == {"inc-real-1"}
    assert response.json()["source_filter"] == "operational"
    assert all(i["source"] == "operational" for i in response.json()["incidents"])


def test_demo_and_all_sources_are_explicit(api_client):
    _operational()
    _demo()

    demo = api_client.get("/incidents/history?source=demo", headers=auth())
    everything = api_client.get("/incidents/history?source=all", headers=auth())

    assert _ids(demo) == {"DEMO-abc123"}
    assert demo.json()["incidents"][0]["source"] == "demo"
    assert _ids(everything) == {"inc-real-1", "DEMO-abc123"}


def test_status_filter_cannot_bypass_source_filter(api_client):
    _operational()
    _demo()

    response = api_client.get(
        "/incidents/history?status=OPEN", headers=auth()
    )

    assert _ids(response) == {"inc-real-1"}


def test_invalid_source_is_rejected(api_client):
    response = api_client.get("/incidents/history?source=bogus", headers=auth())

    assert response.status_code == 400


def test_empty_operational_feed_is_not_populated_with_demo(api_client):
    _demo()

    response = api_client.get("/incidents/history", headers=auth())

    assert response.status_code == 200
    assert response.json()["incidents"] == []


def test_detail_returns_demo_record_with_source_label(api_client):
    _demo()

    response = api_client.get("/incidents/DEMO-abc123", headers=auth())

    assert response.status_code == 200
    body = response.json()
    assert body["incident_id"] == "DEMO-abc123"
    assert body["source"] == "demo"
    assert body["source_evidence"]["simulation"] == "demo"


def test_detail_returns_operational_record(api_client):
    _operational()

    body = api_client.get("/incidents/inc-real-1", headers=auth()).json()

    assert body["source"] == "operational"


def test_detail_unknown_incident_is_404(api_client):
    response = api_client.get("/incidents/does-not-exist", headers=auth())

    assert response.status_code == 404


def test_list_deduplicates_reopened_incident_ids_and_detail_returns_latest(
    api_client,
):
    original = _operational("inc-reopened")
    with get_connection() as connection:
        connection.execute(
            "UPDATE incidents SET status = 'RESOLVED' WHERE id = ?",
            (original["id"],),
        )
    latest = record_incident({
        "incident_id": "inc-reopened",
        "service": "kubernetes-lab",
        "type": "pod_container_failure",
        "severity": "critical",
        "message": "Latest occurrence of the reopened incident",
        "fingerprint": original["fingerprint"],
    })

    history = api_client.get("/incidents/history", headers=auth())
    detail = api_client.get("/incidents/inc-reopened", headers=auth())

    matches = [
        item for item in history.json()["incidents"]
        if item["incident_id"] == "inc-reopened"
    ]
    assert history.status_code == 200
    assert len(matches) == 1
    assert matches[0]["id"] == latest["id"]
    assert detail.status_code == 200
    assert detail.json()["id"] == latest["id"]
    assert detail.json()["message"] == "Latest occurrence of the reopened incident"


def test_latest_demo_occurrence_remains_separate_and_detailable(api_client):
    original = _demo("DEMO-reopened")
    with get_connection() as connection:
        connection.execute(
            "UPDATE incidents SET status = 'RESOLVED' WHERE id = ?",
            (original["id"],),
        )
    latest = record_incident({
        "incident_id": "DEMO-reopened",
        "service": "demo-synthetic",
        "type": "pod_crash",
        "severity": "high",
        "message": "SIMULATED DEMO: Latest occurrence.",
        "fingerprint": original["fingerprint"],
        "source_evidence": {
            "simulation": "demo",
            "simulated": True,
            "scenario": "PodCrashLoopBackOff",
            "workload_reference": None,
        },
    })

    operational = api_client.get("/incidents/history", headers=auth())
    demo = api_client.get(
        "/incidents/history?source=demo", headers=auth()
    )
    detail = api_client.get("/incidents/DEMO-reopened", headers=auth())

    assert "DEMO-reopened" not in _ids(operational)
    assert _ids(demo) == {"DEMO-reopened"}
    assert demo.json()["incidents"][0]["id"] == latest["id"]
    assert detail.status_code == 200
    assert detail.json()["id"] == latest["id"]
    assert detail.json()["source"] == "demo"


@pytest.mark.parametrize(
    ("headers", "status"),
    [({}, 401), ({"Authorization": "Bearer invalid"}, 401),
     (auth(("remediation",)), 403)],
)
def test_detail_requires_read_permission(api_client, headers, status):
    _operational()

    assert api_client.get("/incidents/inc-real-1", headers=headers).status_code == status


def test_opening_incident_is_read_only(api_client):
    _operational()
    _demo()
    before_rows = len(list_incidents())
    before_events = len(audit.list_events("inc-real-1", 1000))

    for _ in range(3):
        api_client.get("/incidents/inc-real-1", headers=auth())
        api_client.get("/incidents/DEMO-abc123", headers=auth())
        api_client.get("/incidents/history?source=all", headers=auth())

    assert len(list_incidents()) == before_rows
    assert len(audit.list_events("inc-real-1", 1000)) == before_events


def test_filtering_does_not_delete_history():
    _operational()
    _demo()

    assert len(list_incidents(source="all")) == 2
    assert len(list_incidents(source="operational")) == 1
    assert len(list_incidents(source="demo")) == 1


def test_metrics_exclude_demo_incidents_and_report_the_exclusion():
    _operational()
    _demo()

    metrics = calculate_incident_metrics()

    assert metrics["total_incidents"] == 1
    assert metrics["active_incidents"] == 1
    assert metrics["excluded_demo_incidents"] == 1
    assert metrics["by_service"] == {"kubernetes-lab": 1}