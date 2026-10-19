import json
import subprocess

import pytest

from app.services import k8s_observer as observer


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
