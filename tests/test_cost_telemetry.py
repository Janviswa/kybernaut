import hashlib
import json
import subprocess
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.cost_telemetry import (
    CostTelemetryError,
    collect_kubernetes_cost_evidence,
    discover_kubernetes_resources,
    load_query_profile,
    parse_cpu_quantity,
    parse_memory_quantity,
)

START = "2026-10-12T00:00:00+00:00"
END = "2026-10-12T02:00:00+00:00"
BASE_TIMESTAMP = 1791763200.0


def kubernetes_payload(*, cpu_request="500m", memory_request="128Mi"):
    container = {
        "name": "web",
        "resources": {
            "requests": {"cpu": cpu_request, "memory": memory_request},
            "limits": {"cpu": "1", "memory": "256Mi"},
        },
    }
    return {
        "items": [
            {
                "kind": "Deployment",
                "metadata": {
                    "name": "demo-web",
                    "namespace": "kybernaut-lab",
                    "uid": "uid-demo",
                    "resourceVersion": "11",
                },
                "spec": {
                    "replicas": 1,
                    "selector": {"matchLabels": {"app": "demo-web"}},
                    "template": {
                        "metadata": {"labels": {"app": "demo-web"}},
                    },
                },
            },
            {
                "kind": "ReplicaSet",
                "metadata": {
                    "name": "demo-web-abc",
                    "namespace": "kybernaut-lab",
                    "uid": "uid-rs-demo",
                    "ownerReferences": [{
                        "kind": "Deployment",
                        "uid": "uid-demo",
                        "controller": True,
                    }],
                },
            },
            {
                "kind": "Pod",
                "metadata": {
                    "name": "demo-web-abc",
                    "namespace": "kybernaut-lab",
                    "labels": {"app": "demo-web"},
                    "ownerReferences": [{
                        "kind": "ReplicaSet",
                        "uid": "uid-rs-demo",
                        "controller": True,
                    }],
                },
                "spec": {"containers": [container]},
                "status": {
                    "phase": "Running",
                    "conditions": [{"type": "Ready", "status": "True"}],
                },
            },
        ]
    }


def fake_runner(payload=None):
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        return SimpleNamespace(
            stdout=json.dumps(payload or kubernetes_payload())
        )

    return run, seen


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeHttpClient:
    def __init__(self, *, timeout):
        assert timeout > 0
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, url, *, params):
        self.calls.append((url, params))
        if params["query"] == "cpu-query":
            values = [[BASE_TIMESTAMP, "0.25"], [BASE_TIMESTAMP + 3600, "0.5"]]
        elif params["query"] == "memory-query":
            values = [
                [BASE_TIMESTAMP, str(64 * 1024 ** 2)],
                [BASE_TIMESTAMP + 3600, str(96 * 1024 ** 2)],
            ]
        elif params["query"] == "cpu-source-time":
            values = [[BASE_TIMESTAMP, str(BASE_TIMESTAMP)],
                      [BASE_TIMESTAMP + 3600, str(BASE_TIMESTAMP + 3600)]]
        else:
            values = [[BASE_TIMESTAMP, str(BASE_TIMESTAMP)],
                      [BASE_TIMESTAMP + 3600, str(BASE_TIMESTAMP + 3600)]]
        return FakeResponse({
            "status": "success",
            "data": {
                "resultType": "matrix",
                "result": [{
                    "metric": {
                        "namespace": "kybernaut-lab",
                        "pod": "demo-web-abc",
                        "container": "web",
                    },
                    "values": values,
                }],
            },
        })


def make_query_profile(options):
    queries = {
        "cpu": options["cpu_query"],
        "memory": options["memory_query"],
        "cpu_source_timestamp": options["cpu_source_timestamp_query"],
        "memory_source_timestamp": options["memory_source_timestamp_query"],
    }
    profile = {
        "schema_version": 1,
        "reviewed_by": "offline-test-owner",
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "identity_labels": {
            "namespace": options["namespace_label"],
            "pod": options["pod_label"],
            "container": options["container_label"],
        },
        "query_sha256": {
            name: hashlib.sha256(query.encode("utf-8")).hexdigest()
            for name, query in queries.items()
        },
        "cpu": {
            "source_metric": "test_cpu_metric",
            "unit": "cores",
            "aggregation": "per_container_usage",
        },
        "memory": {
            "source_metric": "test_memory_metric",
            "unit": "bytes",
            "aggregation": "per_container_usage",
        },
        "cpu_source_timestamp": {
            "source_metric": "test_cpu_metric",
            "unit": "unix_seconds",
            "semantics": "raw_sample_timestamp",
        },
        "memory_source_timestamp": {
            "source_metric": "test_memory_metric",
            "unit": "unix_seconds",
            "semantics": "raw_sample_timestamp",
        },
    }
    return profile


def collect(**overrides):
    runner, calls = fake_runner()
    options = {
        "period_start": START,
        "period_end": END,
        "sample_interval_minutes": 60,
        "prometheus_url": "http://localhost:9090",
        "cpu_query": "cpu-query",
        "memory_query": "memory-query",
        "cpu_source_timestamp_query": "cpu-source-time",
        "memory_source_timestamp_query": "memory-source-time",
        "namespace_label": "namespace",
        "pod_label": "pod",
        "container_label": "container",
        "runner": runner,
        "http_client_factory": FakeHttpClient,
    }
    options.update(overrides)
    options.setdefault("query_profile", make_query_profile(options))
    return collect_kubernetes_cost_evidence(**options), calls


def test_resource_quantity_parsers_support_kubernetes_units():
    assert parse_cpu_quantity("250m") == 0.25
    assert parse_cpu_quantity("2") == 2.0
    assert parse_memory_quantity("128Mi") == 128 * 1024 ** 2
    assert parse_memory_quantity("1G") == 1_000_000_000


@pytest.mark.parametrize("value", ["0", "-1", "one", None, True])
def test_cpu_quantity_rejects_non_positive_or_invalid_values(value):
    with pytest.raises(CostTelemetryError):
        parse_cpu_quantity(value)


def test_discovery_uses_fixed_lab_context_and_rejects_unallowlisted_workload():
    runner, calls = fake_runner()
    result = discover_kubernetes_resources(runner=runner)

    assert result["demo-web"]["cpu_request_cores"] == 0.5
    assert result["demo-web"]["memory_request_bytes"] == 128 * 1024 ** 2
    assert calls[0][0] == [
        "kubectl", "--context", "kind-kybernaut", "get",
        "deployments,replicasets,pods", "-n", "kybernaut-lab", "-o", "json",
    ]
    assert calls[0][1]["check"] is True
    with pytest.raises(CostTelemetryError, match="policy-allowlisted"):
        discover_kubernetes_resources(("outside-namespace-workload",), runner=runner)
    assert len(calls) == 1


def test_discovery_rejects_missing_requests_and_unstable_replica_set():
    runner, _ = fake_runner(kubernetes_payload(cpu_request="0"))
    with pytest.raises(CostTelemetryError, match="positive"):
        discover_kubernetes_resources(runner=runner)

    payload = kubernetes_payload()
    payload["items"][0]["spec"]["replicas"] = 2
    runner, _ = fake_runner(payload)
    with pytest.raises(CostTelemetryError, match="stable, complete pod set"):
        discover_kubernetes_resources(runner=runner)

    payload = kubernetes_payload()
    payload["items"][2]["status"]["conditions"][0]["status"] = "False"
    runner, _ = fake_runner(payload)
    with pytest.raises(CostTelemetryError, match="not Ready"):
        discover_kubernetes_resources(runner=runner)


def test_collection_aligns_cpu_memory_and_maps_correct_identity():
    result, calls = collect()
    samples = result["observations"]

    assert [sample["cpu_utilization"] for sample in samples] == [0.5, 1.0]
    assert [sample["memory_utilization"] for sample in samples] == [0.5, 0.75]
    assert result["resources"]["kybernaut-lab/demo-web"]["complete_samples"] == 2
    assert result["collection"]["kubernetes_context"] == "kind-kybernaut"
    assert result["collection"]["query_range_samples_are_evaluation_timestamps"]
    profile = result["collection"]["query_profile"]
    assert profile["reviewed_by"] == "offline-test-owner"
    assert profile["metrics"]["cpu"]["unit"] == "cores"
    assert profile["metrics"]["memory"]["unit"] == "bytes"
    assert "not statically proven" in profile["trust_boundary"]
    assert calls[0][0][3:6] == ["get", "deployments,replicasets,pods", "-n"]


def test_query_profile_rejects_unreviewed_or_mismatched_declarations():
    valid_profile = make_query_profile({
        "cpu_query": "cpu-query",
        "memory_query": "memory-query",
        "cpu_source_timestamp_query": "cpu-source-time",
        "memory_source_timestamp_query": "memory-source-time",
        "namespace_label": "namespace",
        "pod_label": "pod",
        "container_label": "container",
    })
    invalid_profiles = []
    wrong_fingerprint = json.loads(json.dumps(valid_profile))
    wrong_fingerprint["query_sha256"]["cpu"] = "0" * 64
    invalid_profiles.append((wrong_fingerprint, "fingerprints"))

    wrong_unit = json.loads(json.dumps(valid_profile))
    wrong_unit["cpu"]["unit"] = "millicores"
    invalid_profiles.append((wrong_unit, "must declare"))

    wrong_labels = json.loads(json.dumps(valid_profile))
    wrong_labels["identity_labels"]["container"] = "container_name"
    invalid_profiles.append((wrong_labels, "identity labels"))

    mismatched_source = json.loads(json.dumps(valid_profile))
    mismatched_source["cpu_source_timestamp"]["source_metric"] = "other_metric"
    invalid_profiles.append((mismatched_source, "must match"))

    for profile, message in invalid_profiles:
        with pytest.raises(CostTelemetryError, match=message):
            collect(query_profile=profile)


def test_query_profile_loader_rejects_invalid_file(tmp_path):
    profile_path = tmp_path / "profile.json"
    profile_path.write_text("{", encoding="utf-8")
    with pytest.raises(CostTelemetryError, match="profile is unreadable"):
        load_query_profile(str(profile_path))

    profile = make_query_profile({
        "cpu_query": "cpu-query",
        "memory_query": "memory-query",
        "cpu_source_timestamp_query": "cpu-source-time",
        "memory_source_timestamp_query": "memory-source-time",
        "namespace_label": "namespace",
        "pod_label": "pod",
        "container_label": "container",
    })
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    assert load_query_profile(str(profile_path)) == profile


def test_discovery_rejects_selector_matches_outside_deployment_ownership():
    payload = kubernetes_payload()
    payload["items"][2]["metadata"]["ownerReferences"][0]["uid"] = "other-replicaset"
    runner, _ = fake_runner(payload)

    with pytest.raises(CostTelemetryError, match="outside its controller ownership"):
        discover_kubernetes_resources(runner=runner)


def test_collection_rejects_identity_mismatch_and_incomplete_query_contract():
    class WrongIdentityClient(FakeHttpClient):
        def get(self, url, *, params):
            response = super().get(url, params=params)
            response.payload["data"]["result"][0]["metric"]["namespace"] = "default"
            return response

    runner, _ = fake_runner()
    with pytest.raises(CostTelemetryError, match="out-of-scope namespace"):
        collect(runner=runner, http_client_factory=WrongIdentityClient)

    with pytest.raises(CostTelemetryError, match="source-timestamp PromQL"):
        collect(cpu_query=" ")

    with pytest.raises(CostTelemetryError, match="credential-free"):
        collect(prometheus_url="http://user:password@prometheus.example.test")
    with pytest.raises(CostTelemetryError, match="loopback base URL"):
        collect(prometheus_url="https://prometheus.example.test")

    class UnknownPodClient(FakeHttpClient):
        def get(self, url, *, params):
            response = super().get(url, params=params)
            response.payload["data"]["result"][0]["metric"]["pod"] = "other-pod"
            return response

    runner, _ = fake_runner()
    with pytest.raises(CostTelemetryError, match="unmapped pod/container"):
        collect(runner=runner, http_client_factory=UnknownPodClient)


def test_prometheus_partial_metric_coverage_is_not_filled():
    class MissingMemoryClient(FakeHttpClient):
        def get(self, url, *, params):
            response = super().get(url, params=params)
            if params["query"] == "memory-query":
                response.payload["data"]["result"][0]["values"] = []
            return response

    runner, _ = fake_runner()
    result, _ = collect(runner=runner, http_client_factory=MissingMemoryClient)

    assert result["observations"] == []
    assert result["resources"]["kybernaut-lab/demo-web"]["complete_samples"] == 0


def test_stale_underlying_samples_are_excluded_and_counted():
    class StaleCpuClient(FakeHttpClient):
        def get(self, url, *, params):
            response = super().get(url, params=params)
            if params["query"] == "cpu-source-time":
                response.payload["data"]["result"][0]["values"] = [
                    [BASE_TIMESTAMP, str(BASE_TIMESTAMP - 60)],
                    [BASE_TIMESTAMP + 3600, str(BASE_TIMESTAMP + 3600 - 60)],
                ]
            return response

    runner, _ = fake_runner()
    result, _ = collect(runner=runner, http_client_factory=StaleCpuClient)

    assert result["observations"] == []
    assert result["resources"]["kybernaut-lab/demo-web"]["stale_samples"] == 2
    assert result["collection"]["maximum_sample_age_seconds"] == 30


def test_kubernetes_discovery_subprocess_failure_is_explicit():
    def fail(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("kubectl", 10)

    with pytest.raises(CostTelemetryError, match="discovery failed"):
        discover_kubernetes_resources(runner=fail)
