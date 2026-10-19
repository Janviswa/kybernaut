import app.database as database
import pytest

from app.services.incident_repository import list_incidents
from app.services.k8s_incident_sync import persist_observation


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "test.db")
    database.initialize_database()


def observation(incidents):
    return {
        "namespace": "kybernaut-lab",
        "incident_count": len(incidents),
        "incidents": incidents,
        "deployments": [
            {
                "name": "demo-web",
                "desired_replicas": 2,
                "available_replicas": 2,
                "ready_replicas": 2,
            }
        ],
    }


def pod_incident(message="ImagePullBackOff detected"):
    return {
        "type": "pod_container_failure",
        "resource": "demo-web-bad",
        "severity": "high",
        "evidence": [message],
        "recommendation": "Inspect the Pod.",
    }


def test_repeated_observation_updates_one_record():
    persist_observation(observation([pod_incident()]))
    first = list_incidents(status="OPEN")
    assert len(first) == 1

    persist_observation(observation([
        pod_incident("ImagePullBackOff still detected")
    ]))
    second = list_incidents(status="OPEN")

    assert len(second) == 1
    assert second[0]["id"] == first[0]["id"]
    assert second[0]["message"] == "ImagePullBackOff still detected"
    assert second[0]["last_seen_at"]


def test_disappearance_resolves_and_recurrence_creates_new_record():
    persist_observation(observation([pod_incident()]))
    first = list_incidents(status="OPEN")[0]

    # An empty list represents a successful observation with no incidents.
    persist_observation(observation([]))
    assert list_incidents(status="OPEN") == []
    assert list_incidents(status="RESOLVED")[0]["id"] == first["id"]

    persist_observation(observation([pod_incident()]))
    reopened = list_incidents(status="OPEN")

    assert len(reopened) == 1
    assert reopened[0]["id"] != first["id"]


def test_distinct_resources_create_distinct_incidents():
    other = {
        **pod_incident(),
        "resource": "demo-web-another-pod",
    }

    persist_observation(observation([pod_incident(), other]))

    assert len(list_incidents(status="OPEN")) == 2


def test_fingerprints_include_namespace_and_resource_kind():
    from app.services.k8s_incident_sync import build_fingerprint

    pod = pod_incident()
    deployment = {
        "type": "deployment_unavailable_replicas",
        "resource": pod["resource"],
        "severity": "high",
        "evidence": ["Unavailable replicas"],
    }

    pod_fingerprint = build_fingerprint(pod, "kybernaut-lab")
    deployment_fingerprint = build_fingerprint(
        deployment, "kybernaut-lab"
    )
    another_namespace = build_fingerprint(pod, "another-namespace")

    assert pod_fingerprint != deployment_fingerprint
    assert pod_fingerprint != another_namespace



def test_invalid_observation_is_rejected_before_writes(monkeypatch):
    from app.services import k8s_incident_sync as sync

    def unexpected_write(*args, **kwargs):
        raise AssertionError("Database write attempted for invalid observation")

    monkeypatch.setattr(sync, "record_incident", unexpected_write)
    monkeypatch.setattr(sync, "list_incidents", unexpected_write)

    with pytest.raises(ValueError, match="incident count"):
        sync.persist_observation({
            "namespace": "kybernaut-lab",
            "incident_count": 1,
            "incidents": [],
            "deployments": [],
        })


def test_missing_deployments_is_rejected_before_writes(monkeypatch):
    from app.services import k8s_incident_sync as sync

    def unexpected_write(*args, **kwargs):
        raise AssertionError("Database write attempted for invalid observation")

    monkeypatch.setattr(sync, "record_incident", unexpected_write)
    monkeypatch.setattr(sync, "list_incidents", unexpected_write)

    with pytest.raises(ValueError, match="deployments"):
        sync.persist_observation({
            "namespace": "kybernaut-lab",
            "incident_count": 0,
            "incidents": [],
        })


def test_missing_expected_deployment_is_rejected_before_writes(monkeypatch):
    from app.services import k8s_incident_sync as sync

    def unexpected_write(*args, **kwargs):
        raise AssertionError("Database write attempted for invalid observation")

    monkeypatch.setattr(sync, "record_incident", unexpected_write)
    monkeypatch.setattr(sync, "list_incidents", unexpected_write)

    with pytest.raises(ValueError, match="Expected demo-web deployment missing"):
        sync.persist_observation({
            "namespace": "kybernaut-lab",
            "incident_count": 0,
            "incidents": [],
            "deployments": [],
        })
