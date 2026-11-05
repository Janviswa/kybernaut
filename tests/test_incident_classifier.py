from app.services.incident_classifier import classify_incident


def k8s(evidence, itype="pod_container_failure", resource="demo-web-abc-1",
        kind="Pod", severity="high"):
    return {"namespace": "kybernaut-lab",
            "deployments": [{"name": "demo-web"}],
            "incidents": [{"type": itype, "resource": resource,
                           "resource_kind": kind, "severity": severity,
                           "evidence": evidence}]}


def test_image_pull_is_deployment_failure():
    r = classify_incident({}, k8s(["waiting with reason ImagePullBackOff"]), {})
    assert r["incident_type"] == "deployment_failure"
    assert r["target"]["deployment"] == "demo-web"


def test_crashloop_is_pod_crash_and_oom_is_memory():
    r = classify_incident({}, k8s(["reason CrashLoopBackOff"]), {})
    assert r["incident_type"] == "pod_crash"
    r = classify_incident({}, k8s(["CrashLoopBackOff OOMKilled"]), {})
    assert r["incident_type"] == "memory_leak" and r["confidence"] < 0.9


def test_eviction_for_ephemeral_storage_is_disk_full():
    r = classify_incident(
        {}, k8s(["Evicted: ephemeral local storage usage exceeds"],
                itype="pod_failed"), {})
    assert r["incident_type"] == "disk_full"


def test_emptydir_size_limit_eviction_is_disk_full():
    r = classify_incident(
        {},
        k8s([
            'Evicted: Usage of EmptyDir volume "scratch" exceeds the limit "40Mi".'
        ], itype="kubernetes_warning_event"),
        {},
    )
    assert r["incident_type"] == "disk_full"
    assert r["confidence"] == 0.8


def test_newer_oom_evidence_outranks_historical_disk_event():
    observations = {
        "namespace": "kybernaut-lab",
        "deployments": [{"name": "sim-memory-leak"}],
        "incidents": [
            {
                "type": "kubernetes_warning_event",
                "resource": "sim-disk-fill-old",
                "resource_kind": "Pod",
                "severity": "medium",
                "source_timestamp": "2026-10-22T04:23:02+00:00",
                "evidence": [
                    'Evicted: Usage of EmptyDir volume "scratch" exceeds the limit "40Mi".'
                ],
            },
            {
                "type": "pod_container_failure",
                "resource": "sim-memory-leak-current",
                "resource_kind": "Pod",
                "severity": "high",
                "source_timestamp": "2026-10-22T04:50:06+00:00",
                "evidence": ["previously terminated with reason OOMKilled"],
            },
        ],
    }

    result = classify_incident({}, observations, {})

    assert result["incident_type"] == "memory_leak"


def test_telemetry_only_is_low_confidence_without_target():
    tele = {"status": "evaluated", "actionable_candidates": ["cpu_cores_used"]}
    r = classify_incident({}, None, tele)
    assert r["incident_type"] == "cpu_spike"
    assert r["confidence"] <= 0.5 and r["target"] is None


def test_healthy_or_malformed_is_unknown():
    assert classify_incident({}, {"incidents": []}, {})["incident_type"] == "unknown"
    assert classify_incident(None, "junk", 5)["incident_type"] == "unknown"


def test_target_requires_kubernetes_namespace_and_valid_timestamp():
    observation = k8s(
        ["reason CrashLoopBackOff"],
        resource="demo-web-pod",
    )
    assert classify_incident(
        {}, {**observation, "namespace": " "}, {}
    )["incident_type"] == "unknown"

    incident = observation["incidents"][0]
    incident["resource_uid"] = "pod-uid"
    result = classify_incident({}, observation, {})
    assert result["target"]["resource_uid"] == "pod-uid"

    incident["source_timestamp"] = "not-a-timestamp"
    assert classify_incident({}, observation, {})["incident_type"] == "unknown"


def test_conflicting_categories_at_same_timestamp_are_unknown():
    timestamp = "2026-10-22T06:00:00+00:00"
    observation = {
        "namespace": "kybernaut-lab",
        "deployments": [{"name": "demo-web"}],
        "incidents": [
            {
                "type": "pod_container_failure",
                "resource": "demo-web-crash-1",
                "resource_kind": "Pod",
                "severity": "high",
                "source_timestamp": timestamp,
                "evidence": ["CrashLoopBackOff"],
            },
            {
                "type": "kubernetes_warning_event",
                "resource": "demo-web-disk-1",
                "resource_kind": "Pod",
                "severity": "medium",
                "source_timestamp": timestamp,
                "evidence": ["Evicted: ephemeral storage exceeded"],
            },
        ],
    }

    result = classify_incident({}, observation, {})

    assert result["incident_type"] == "unknown"
    assert result["target"] is None


def test_conflicting_namespace_evidence_cannot_establish_a_target():
    observation = k8s(["reason CrashLoopBackOff"])
    observation["incidents"][0]["namespace"] = "other-namespace"

    result = classify_incident({}, observation, {})

    assert result["incident_type"] == "unknown"
    assert result["target"] is None


def test_same_category_on_multiple_targets_is_not_actionable():
    timestamp = "2026-10-22T06:00:00+00:00"
    observation = {
        "namespace": "kybernaut-lab",
        "deployments": [{"name": "demo-web"}],
        "incidents": [
            {
                "type": "pod_container_failure",
                "resource": pod,
                "resource_kind": "Pod",
                "severity": "high",
                "source_timestamp": timestamp,
                "evidence": ["CrashLoopBackOff"],
            }
            for pod in ("demo-web-one", "demo-web-two")
        ],
    }

    result = classify_incident({}, observation, {})

    assert result["incident_type"] == "pod_crash"
    assert result["target"] is None


def test_diagnosis_findings_do_not_override_evidence_classification():
    observation = k8s(["reason CrashLoopBackOff"])

    result = classify_incident(
        {"findings": [{"type": "service_unavailable"}]},
        observation,
        {},
    )

    assert result["incident_type"] == "pod_crash"
