import app.database as database
import pytest

from app.services.incident_repository import list_incidents, record_incident
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


def test_empty_lab_observation_does_not_resolve_prometheus_incident():
    record_incident({
        "incident_id": "inc-inventory-memory",
        "service": "inventory-service",
        "type": "memory_leak",
        "severity": "medium",
        "message": "Prometheus memory anomaly",
        "fingerprint": "prometheus:inventory-service:memory_leak",
        "source_evidence": {"source": "prometheus"},
    })

    persist_observation(observation([]))

    records = list_incidents(status="OPEN")
    assert len(records) == 1
    assert records[0]["incident_id"] == "inc-inventory-memory"
    assert records[0]["fingerprint"] == (
        "prometheus:inventory-service:memory_leak"
    )


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


def test_persisted_kubernetes_incident_retains_source_evidence():
    from datetime import datetime, timezone

    observed_at = datetime.now(timezone.utc).isoformat()
    incident = {
        **pod_incident(),
        "resource_kind": "Pod",
        "resource_uid": "pod-uid-1",
        "source_timestamp": observed_at,
    }

    persist_observation({
        **observation([incident]),
        "observed_at": observed_at,
    })

    saved = list_incidents(status="OPEN")[0]
    assert saved["source_timestamp"] == observed_at
    assert saved["source_evidence"] == {
        "source": "kubernetes",
        "namespace": "kybernaut-lab",
        "type": "pod_container_failure",
        "resource": "demo-web-bad",
        "resource_kind": "Pod",
        "resource_uid": "pod-uid-1",
        "workload": "demo-web",
        "incident_category": "deployment_failure",
        "source_timestamp": observed_at,
        "observed_at": observed_at,
        "evidence": ["ImagePullBackOff detected"],
        "recommendation": "Inspect the Pod.",
    }


def test_persisted_simulator_incidents_show_distinct_categories_and_workloads():
    incidents = [
        {
            "type": "pod_failed",
            "resource": "sim-disk-fill-1234567890-abcde",
            "resource_kind": "Pod",
            "severity": "high",
            "evidence": [
                "Evicted: usage of EmptyDir ephemeral storage exceeds limit"
            ],
        },
        {
            "type": "pod_container_failure",
            "resource": "sim-memory-leak-1234567890-abcde",
            "resource_kind": "Pod",
            "severity": "high",
            "evidence": ["Container terminated with reason OOMKilled"],
        },
        {
            "type": "pod_container_failure",
            "resource": "sim-pod-crash-1234567890-abcde",
            "resource_kind": "Pod",
            "severity": "high",
            "evidence": ["Container is in CrashLoopBackOff"],
        },
        {
            "type": "deployment_unavailable_replicas",
            "resource": "demo-web",
            "resource_kind": "Deployment",
            "severity": "high",
            "evidence": ["ImagePullBackOff: unable to pull image"],
        },
    ]
    observation_data = observation(incidents)
    observation_data["deployments"].extend([
        {
            "name": name,
            "desired_replicas": 1,
            "available_replicas": 0,
            "ready_replicas": 0,
        }
        for name in ("sim-disk-fill", "sim-memory-leak", "sim-pod-crash")
    ])

    persist_observation(observation_data)

    saved = {
        item["source_evidence"]["resource"]: item["source_evidence"]
        for item in list_incidents(status="OPEN")
    }
    assert {
        saved[resource]["incident_category"]
        for resource in saved
    } == {"disk_full", "memory_leak", "pod_crash", "deployment_failure"}
    assert saved["sim-disk-fill-1234567890-abcde"]["workload"] == "sim-disk-fill"
    assert saved["sim-memory-leak-1234567890-abcde"]["workload"] == "sim-memory-leak"
    assert saved["sim-pod-crash-1234567890-abcde"]["workload"] == "sim-pod-crash"
    assert saved["demo-web"]["workload"] == "demo-web"



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
