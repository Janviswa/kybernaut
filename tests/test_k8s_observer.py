import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from app.services import failure_scenarios as simulator
from app.services import k8s_observer as observer
from app.services.incident_classifier import classify_incident


def test_rejects_unsupported_resource():
    with pytest.raises(ValueError, match="Unsupported"):
        observer._kubectl_json("secrets")


def test_detects_bad_image_and_warning_event(monkeypatch):
    fixtures = {
        "pods": {
            "items": [{
                "metadata": {"name": "demo-web-bad"},
                "status": {
                    "phase": "Pending",
                    "containerStatuses": [{
                        "name": "nginx",
                        "state": {
                            "waiting": {
                                "reason": "ImagePullBackOff",
                                "message": "Image not found",
                            }
                        },
                    }],
                },
            }]
        },
        "events": {
            "items": [{
                "type": "Warning",
                "reason": "Failed",
                "message": "Failed to pull image",
                "involvedObject": {"name": "demo-web-bad"},
            }]
        },
        "deployments": {
            "items": [{
                "metadata": {"name": "demo-web"},
                "spec": {"replicas": 2},
                "status": {"availableReplicas": 1, "readyReplicas": 1},
            }]
        },
    }

    def fake_kubectl(resource):
        return fixtures[resource]

    monkeypatch.setattr(observer, "_kubectl_json", fake_kubectl)

    result = observer.observe_namespace()
    types = {item["type"] for item in result["incidents"]}

    assert "pod_container_failure" in types
    assert "kubernetes_warning_event" in types
    assert "deployment_unavailable_replicas" in types
    assert result["incident_count"] == len(result["incidents"])


def test_detects_the_dedicated_pod_crash_simulator(monkeypatch):
    fixtures = {
        "pods": {
            "items": [{
                "metadata": {
                    "name": "sim-pod-crash-abc",
                    "uid": "sim-pod-crash-uid",
                },
                "status": {
                    "phase": "Running",
                    "containerStatuses": [{
                        "name": "crash",
                        "state": {
                            "waiting": {
                                "reason": "CrashLoopBackOff",
                                "message": "back-off restarting failed container",
                            }
                        },
                    }],
                },
            }]
        },
        "events": {"items": []},
        "deployments": {"items": [{
            "metadata": {"name": "sim-pod-crash"},
            "spec": {"replicas": 1},
            "status": {"availableReplicas": 0, "readyReplicas": 0},
        }]},
    }
    monkeypatch.setattr(
        observer, "_kubectl_json", lambda resource: fixtures[resource]
    )

    result = observer.observe_namespace()

    assert any(
        incident["type"] == "pod_container_failure"
        and incident["resource"] == "sim-pod-crash-abc"
        for incident in result["incidents"]
    )


def test_only_warning_events_create_event_incidents(monkeypatch):
    fixtures = {
        "pods": {"items": []},
        "events": {
            "items": [{
                "type": "Normal",
                "reason": "Started",
                "message": "Container started",
                "involvedObject": {"name": "demo-web"},
            }]
        },
        "deployments": {"items": []},
    }

    monkeypatch.setattr(
        observer, "_kubectl_json", lambda resource: fixtures[resource]
    )

    result = observer.observe_namespace()
    assert result["incident_count"] == 0


def test_kubectl_failure_is_not_silently_ignored(monkeypatch):
    def failed_command(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args[0], stderr="cluster unavailable")

    monkeypatch.setattr(subprocess, "run", failed_command)

    with pytest.raises(subprocess.CalledProcessError):
        observer._kubectl_json("pods")


def test_groups_pod_failure_and_warning_events(monkeypatch):
    fixtures = {
        "pods": {
            "items": [{
                "metadata": {
                    "name": "demo-web-bad",
                    "uid": "pod-uid-1",
                },
                "status": {
                    "phase": "Pending",
                    "containerStatuses": [{
                        "name": "nginx",
                        "state": {
                            "waiting": {
                                "reason": "ErrImagePull",
                                "message": "Image not found",
                            }
                        },
                    }],
                },
            }]
        },
        "events": {
            "items": [
                {
                    "type": "Warning",
                    "reason": "Failed",
                    "message": "Failed to pull image",
                    "involvedObject": {
                        "kind": "Pod",
                        "name": "demo-web-bad",
                        "uid": "pod-uid-1",
                    },
                },
                {
                    "type": "Warning",
                    "reason": "BackOff",
                    "message": "Back-off pulling image",
                    "involvedObject": {
                        "kind": "Pod",
                        "name": "demo-web-bad",
                        "uid": "pod-uid-1",
                    },
                },
            ]
        },
        "deployments": {
            "items": [{
                "metadata": {"name": "demo-web"},
                "spec": {"replicas": 2},
                "status": {"availableReplicas": 2, "readyReplicas": 2},
            }]
        },
    }

    monkeypatch.setattr(
        observer, "_kubectl_json", lambda resource: fixtures[resource]
    )

    result = observer.observe_namespace()

    pod_incidents = [
        item for item in result["incidents"]
        if item["resource"] == "demo-web-bad"
    ]

    assert len(pod_incidents) == 1
    assert pod_incidents[0]["type"] == "pod_container_failure"
    assert pod_incidents[0]["resource_kind"] == "Pod"
    assert pod_incidents[0]["severity"] == "high"
    assert len(pod_incidents[0]["evidence"]) == 3
    assert any(
        "Failed to pull image" in evidence
        for evidence in pod_incidents[0]["evidence"]
    )
    assert any(
        "BackOff" in evidence
        for evidence in pod_incidents[0]["evidence"]
    )
    assert result["incident_count"] == len(result["incidents"])


def test_grouped_incident_keeps_highest_severity(monkeypatch):
    fixtures = {
        "pods": {"items": []},
        "events": {
            "items": [
                {
                    "type": "Warning",
                    "reason": "Unhealthy",
                    "message": "Readiness probe failed",
                    "involvedObject": {
                        "kind": "Deployment",
                        "name": "demo-web",
                    },
                },
                {
                    "type": "Warning",
                    "reason": "Failed",
                    "message": "Deployment operation failed",
                    "involvedObject": {
                        "kind": "Deployment",
                        "name": "demo-web",
                    },
                },
            ]
        },
        "deployments": {
            "items": [{
                "metadata": {"name": "demo-web"},
                "spec": {"replicas": 2},
                "status": {"availableReplicas": 2, "readyReplicas": 2},
            }]
        },
    }

    monkeypatch.setattr(
        observer, "_kubectl_json", lambda resource: fixtures[resource]
    )

    result = observer.observe_namespace()
    deployment_events = [
        item for item in result["incidents"]
        if item["type"] == "kubernetes_warning_event"
    ]

    assert len(deployment_events) == 1
    assert deployment_events[0]["severity"] == "high"
    assert len(deployment_events[0]["evidence"]) == 2


def test_eviction_event_preserves_ephemeral_storage_evidence(monkeypatch):
    from app.services.incident_classifier import classify_incident

    fixtures = {
        "pods": {
            "items": [{
                "metadata": {
                    "name": "sim-disk-fill-abc",
                    "uid": "disk-pod-uid",
                    "creationTimestamp": datetime.now(
                        timezone.utc
                    ).isoformat(),
                },
                "status": {
                    "phase": "Failed",
                    "reason": "Evicted",
                },
            }]
        },
        "events": {
            "items": [{
                "type": "Warning",
                "reason": "Evicted",
                "message": (
                    "The node was low on resource: "
                    "ephemeral-storage. Container exceeded its limit."
                ),
                "involvedObject": {
                    "kind": "Pod",
                    "name": "sim-disk-fill-abc",
                    "uid": "disk-pod-uid",
                },
            }]
        },
        "deployments": {"items": []},
    }

    monkeypatch.setattr(
        observer, "_kubectl_json", lambda resource: fixtures[resource]
    )

    observed = observer.observe_namespace()

    eviction = next(
        incident for incident in observed["incidents"]
        if incident["type"] == "pod_failed"
    )

    assert any(
        "ephemeral-storage" in evidence.lower()
        for evidence in eviction["evidence"]
    )

    classified = classify_incident({}, observed, {})
    assert classified["incident_type"] == "disk_full"


def test_persistent_disk_fill_eviction_remains_current_after_event_ages_out(
    monkeypatch,
):
    old_timestamp = (
        datetime.now(timezone.utc) - timedelta(days=2)
    ).isoformat()
    fixtures = {
        "pods": {
            "items": [{
                "metadata": {
                    "name": "sim-disk-fill-5dc94cc998-t9gqv",
                    "uid": "evicted-pod-uid",
                    "creationTimestamp": old_timestamp,
                },
                "status": {
                    "phase": "Failed",
                    "reason": "Evicted",
                    "message": (
                        'Usage of EmptyDir volume "scratch" exceeds the '
                        'limit "40Mi".'
                    ),
                    "conditions": [{
                        "type": "Ready",
                        "status": "False",
                        "lastTransitionTime": old_timestamp,
                    }],
                },
            }]
        },
        "events": {"items": []},
        "deployments": {"items": [{
            "metadata": {"name": "sim-disk-fill"},
            "spec": {"replicas": 1},
            "status": {"availableReplicas": 1, "readyReplicas": 1},
        }]},
    }
    monkeypatch.setattr(
        observer, "_kubectl_json", lambda resource: fixtures[resource]
    )

    observed = observer.observe_namespace()
    evictions = observed["_persistent_disk_fill_evictions"]
    assert len(evictions) == 1
    eviction = evictions[0]

    assert eviction["ongoing"] is True
    assert eviction["resource_uid"] == "evicted-pod-uid"
    assert any("EmptyDir" in evidence for evidence in eviction["evidence"])
    assert not any(
        incident["type"] == "pod_failed"
        for incident in observed["incidents"]
    )


def test_recent_eviction_for_deleted_pod_is_retained_and_classified(monkeypatch):
    from app.services.incident_classifier import classify_incident

    fixtures = {
        "pods": {"items": []},
        "events": {
            "items": [{
                "type": "Warning",
                "reason": "Evicted",
                "message": (
                    'Usage of EmptyDir volume "scratch" exceeds the limit "40Mi".'
                ),
                "lastTimestamp": datetime.now(timezone.utc).isoformat(),
                "involvedObject": {
                    "kind": "Pod",
                    "name": "sim-disk-fill-deleted",
                    "uid": "deleted-disk-pod-uid",
                },
            }]
        },
        "deployments": {"items": []},
    }
    monkeypatch.setattr(
        observer, "_kubectl_json", lambda resource: fixtures[resource]
    )

    observed = observer.observe_namespace()
    classified = classify_incident({}, observed, {})

    assert observed["incident_count"] == 1
    assert observed["incidents"][0]["resource_uid"] == "deleted-disk-pod-uid"
    assert classified["incident_type"] == "disk_full"
    assert classified["confidence"] == 0.8


def test_stale_eviction_for_deleted_pod_is_ignored(monkeypatch):
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    fixtures = {
        "pods": {"items": []},
        "events": {
            "items": [{
                "type": "Warning",
                "reason": "Evicted",
                "message": "ephemeral-storage limit exceeded",
                "metadata": {"creationTimestamp": stale},
                "involvedObject": {
                    "kind": "Pod",
                    "name": "sim-disk-fill-old",
                    "uid": "old-disk-pod-uid",
                },
            }]
        },
        "deployments": {"items": []},
    }
    monkeypatch.setattr(
        observer, "_kubectl_json", lambda resource: fixtures[resource]
    )

    observed = observer.observe_namespace()

    assert observed["incident_count"] == 0


def test_stale_failed_pod_is_not_reported_indefinitely(monkeypatch):
    old_time = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat()
    fixtures = {
        "pods": {
            "items": [{
                "metadata": {
                    "name": "sim-disk-fill-old",
                    "uid": "old-pod-uid",
                    "creationTimestamp": old_time,
                },
                "status": {"phase": "Failed", "reason": "Evicted"},
            }]
        },
        "events": {"items": []},
        "deployments": {"items": []},
    }
    monkeypatch.setattr(
        observer, "_kubectl_json", lambda resource: fixtures[resource]
    )

    observed = observer.observe_namespace()

    assert observed["incident_count"] == 0


def test_stale_warning_for_existing_pod_is_ignored(monkeypatch):
    old_time = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat()
    fixtures = {
        "pods": {
            "items": [{
                "metadata": {"name": "demo-web-old-warning", "uid": "pod-uid"},
                "status": {"phase": "Running"},
            }]
        },
        "events": {
            "items": [{
                "type": "Warning",
                "reason": "BackOff",
                "message": "old warning",
                "lastTimestamp": old_time,
                "involvedObject": {
                    "kind": "Pod",
                    "name": "demo-web-old-warning",
                    "uid": "pod-uid",
                },
            }]
        },
        "deployments": {"items": []},
    }
    monkeypatch.setattr(
        observer, "_kubectl_json", lambda resource: fixtures[resource]
    )

    observed = observer.observe_namespace()

    assert observed["incident_count"] == 0


def test_recent_non_eviction_warning_for_deleted_pod_is_ignored(monkeypatch):
    fixtures = {
        "pods": {"items": []},
        "events": {
            "items": [{
                "type": "Warning",
                "reason": "Failed",
                "message": "Failed to pull image",
                "eventTime": datetime.now(timezone.utc).isoformat(),
                "involvedObject": {
                    "kind": "Pod",
                    "name": "demo-web-deleted",
                    "uid": "deleted-pod-uid",
                },
            }]
        },
        "deployments": {"items": []},
    }
    monkeypatch.setattr(
        observer, "_kubectl_json", lambda resource: fixtures[resource]
    )

    observed = observer.observe_namespace()

    assert observed["incident_count"] == 0


def test_recent_event_for_old_pod_uid_is_not_attached_to_replacement(
        monkeypatch):
    fixtures = {
        "pods": {
            "items": [{
                "metadata": {
                    "name": "sim-disk-fill-reused",
                    "uid": "replacement-pod-uid",
                },
                "status": {"phase": "Running"},
            }]
        },
        "events": {
            "items": [{
                "type": "Warning",
                "reason": "Evicted",
                "message": "ephemeral-storage limit exceeded",
                "eventTime": datetime.now(timezone.utc).isoformat(),
                "involvedObject": {
                    "kind": "Pod",
                    "name": "sim-disk-fill-reused",
                    "uid": "old-pod-uid",
                },
            }]
        },
        "deployments": {"items": []},
    }
    monkeypatch.setattr(
        observer, "_kubectl_json", lambda resource: fixtures[resource]
    )

    observed = observer.observe_namespace()

    assert observed["incident_count"] == 1
    assert observed["incidents"][0]["type"] == "kubernetes_warning_event"
    assert observed["incidents"][0]["resource_uid"] == "old-pod-uid"


def test_previous_oom_killed_termination_is_classified_as_memory_leak(
        monkeypatch):
    from app.services.incident_classifier import classify_incident

    fixtures = {
        "pods": {
            "items": [{
                "metadata": {
                    "name": "sim-memory-leak-abc",
                    "uid": "memory-pod-uid",
                },
                "status": {
                    "phase": "Running",
                    "containerStatuses": [{
                        "name": "leak",
                        "state": {
                            "waiting": {
                                "reason": "CrashLoopBackOff",
                                "message": "back-off restarting failed container",
                            }
                        },
                        "lastState": {
                            "terminated": {
                                "reason": "OOMKilled",
                                "exitCode": 137,
                                "finishedAt": datetime.now(
                                    timezone.utc
                                ).isoformat(),
                            }
                        },
                    }],
                },
            }]
        },
        "events": {"items": []},
        "deployments": {
            "items": [{
                "metadata": {"name": "sim-memory-leak"},
                "spec": {"replicas": 1},
                "status": {"availableReplicas": 0, "readyReplicas": 0},
            }]
        },
    }
    monkeypatch.setattr(
        observer, "_kubectl_json", lambda resource: fixtures[resource]
    )

    observed = observer.observe_namespace()
    classified = classify_incident({}, observed, {})

    evidence = " ".join(observed["incidents"][0]["evidence"])
    assert "OOMKilled" in evidence
    assert classified["incident_type"] == "memory_leak"


def test_recent_nonzero_container_restart_is_classified_as_pod_crash(
        monkeypatch):
    fixtures = {
        "pods": {
            "items": [{
                "metadata": {
                    "name": "demo-web-crashed",
                    "uid": "crashed-pod-uid",
                },
                "status": {
                    "phase": "Running",
                    "containerStatuses": [{
                        "name": "nginx",
                        "state": {"running": {}},
                        "lastState": {
                            "terminated": {
                                "reason": "Error",
                                "exitCode": 137,
                                "finishedAt": datetime.now(
                                    timezone.utc
                                ).isoformat(),
                            }
                        },
                        "restartCount": 1,
                    }],
                },
            }]
        },
        "events": {"items": []},
        "deployments": {
            "items": [{
                "metadata": {"name": "demo-web"},
                "spec": {"replicas": 2},
                "status": {"availableReplicas": 2, "readyReplicas": 2},
            }]
        },
    }
    monkeypatch.setattr(
        observer, "_kubectl_json", lambda resource: fixtures[resource]
    )

    observed = observer.observe_namespace()
    classified = classify_incident({}, observed, {})

    assert "restarted after termination reason Error" in (
        observed["incidents"][0]["evidence"][0]
    )
    assert classified["incident_type"] == "pod_crash"
    assert classified["confidence"] == 0.75


@pytest.mark.parametrize(("reason", "expected_type"), [
    ("Error", "pod_crash"),
    ("OOMKilled", "memory_leak"),
    ("ContainerCannotRun", "unknown"),
])
def test_synthetic_crash_evidence_contract_across_stages(
    monkeypatch, reason, expected_type,
):
    pod_uid = "synthetic-pod-uid"
    finished_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    pod_status = {
        "metadata": {"name": "demo-web-synthetic", "uid": pod_uid},
        "status": {
            "phase": "Running",
            "containerStatuses": [{
                "name": "nginx",
                "state": {"running": {}},
                "lastState": {"terminated": {
                    "reason": reason,
                    "exitCode": 137,
                    "finishedAt": finished_at.isoformat(),
                }},
                "restartCount": 1,
            }],
        },
    }

    def fake_runner(args, *, timeout):
        return subprocess.CompletedProcess(
            args, 0, json.dumps(pod_status), "",
        )

    injection_started_at = finished_at - timedelta(seconds=1)
    simulator_evidence = simulator._pod_crash_evidence(
        fake_runner,
        {
            "name": "demo-web-synthetic",
            "uid": pod_uid,
            "container": "nginx",
            "restart_counts": {"nginx": 0},
            "last_terminations": {"nginx": None},
        },
        injection_started_at,
        timeout=1,
    )
    assert simulator_evidence is not None
    assert simulator_evidence["pod_uid"] == pod_uid
    assert simulator_evidence["container"] == "nginx"

    fixtures = {
        "pods": {"items": [pod_status]},
        "events": {"items": []},
        "deployments": {
            "items": [{
                "metadata": {"name": "demo-web"},
                "spec": {"replicas": 2},
                "status": {"availableReplicas": 2, "readyReplicas": 2},
            }]
        },
    }
    monkeypatch.setattr(
        observer, "_kubectl_json", lambda resource: fixtures[resource]
    )
    observed = observer.observe_namespace()
    classified = classify_incident({}, observed, {})

    if expected_type == "unknown":
        assert observed["incident_count"] == 0
        assert classified["incident_type"] == "unknown"
    else:
        assert observed["incidents"][0]["resource_uid"] == pod_uid
        assert classified["incident_type"] == expected_type


def test_stale_oom_termination_does_not_create_active_incident(monkeypatch):
    old_finish = (
        datetime.now(timezone.utc) - timedelta(hours=1)
    ).isoformat()
    fixtures = {
        "pods": {
            "items": [{
                "metadata": {"name": "demo-web-healthy", "uid": "pod-uid"},
                "status": {
                    "phase": "Running",
                    "containerStatuses": [{
                        "name": "nginx",
                        "state": {"running": {}},
                        "lastState": {
                            "terminated": {
                                "reason": "OOMKilled",
                                "exitCode": 137,
                                "finishedAt": old_finish,
                            }
                        },
                    }],
                },
            }]
        },
        "events": {"items": []},
        "deployments": {
            "items": [{
                "metadata": {"name": "demo-web"},
                "spec": {"replicas": 1},
                "status": {"availableReplicas": 1, "readyReplicas": 1},
            }]
        },
    }
    monkeypatch.setattr(
        observer, "_kubectl_json", lambda resource: fixtures[resource]
    )

    observed = observer.observe_namespace()

    assert observed["incident_count"] == 0
