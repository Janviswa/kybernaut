"""Explicit, read-only collection of lab Kubernetes cost evidence."""

from __future__ import annotations

import ipaddress
import hashlib
import json
import math
import re
import subprocess
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.services.k8s_observer import LAB_CONTEXT, LAB_NAMESPACE
from app.services.policy_engine import PolicyConfig
from app.services.prometheus_features import freshness_limits_from_environment

PROMETHEUS_TIMEOUT_SECONDS = 10.0
KUBERNETES_TIMEOUT_SECONDS = 10
CPU_QUANTITY = re.compile(
    r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(n|u|m)?$"
)
MEMORY_QUANTITY = re.compile(
    r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    r"(Ki|Mi|Gi|Ti|Pi|Ei|k|K|M|G|T|P|E)?$"
)
MEMORY_MULTIPLIERS = {
    None: Decimal(1),
    "k": Decimal(1000),
    "K": Decimal(1000),
    "M": Decimal(1000) ** 2,
    "G": Decimal(1000) ** 3,
    "T": Decimal(1000) ** 4,
    "P": Decimal(1000) ** 5,
    "E": Decimal(1000) ** 6,
    "Ki": Decimal(1024),
    "Mi": Decimal(1024) ** 2,
    "Gi": Decimal(1024) ** 3,
    "Ti": Decimal(1024) ** 4,
    "Pi": Decimal(1024) ** 5,
    "Ei": Decimal(1024) ** 6,
}


class CostTelemetryError(ValueError):
    """Raised when collection cannot produce identity-safe evidence."""


QUERY_KEYS = (
    "cpu",
    "memory",
    "cpu_source_timestamp",
    "memory_source_timestamp",
)
PROFILE_METRIC = re.compile(r"^[A-Za-z_:][A-Za-z0-9_:]{0,127}$")
PROFILE_LABEL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
PROFILE_REVIEWER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:@/-]{0,99}$")
PROFILE_HASH = re.compile(r"^[0-9a-f]{64}$")


def load_query_profile(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as profile_file:
            profile = json.load(profile_file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CostTelemetryError(
            "Operator-reviewed telemetry query profile is unreadable"
        ) from exc
    if not isinstance(profile, dict):
        raise CostTelemetryError("Telemetry query profile must be an object")
    return profile


def _validate_query_profile(
    profile: object,
    *,
    queries: dict[str, str],
    label_names: tuple[str, str, str],
) -> dict[str, Any]:
    if (
        not isinstance(profile, dict)
        or type(profile.get("schema_version")) is not int
        or profile["schema_version"] != 1
    ):
        raise CostTelemetryError(
            "A version 1 operator-reviewed query profile is required"
        )
    expected_profile_keys = {
        "schema_version",
        "reviewed_by",
        "reviewed_at",
        "identity_labels",
        "query_sha256",
        *QUERY_KEYS,
    }
    if set(profile) != expected_profile_keys:
        raise CostTelemetryError(
            "Query profile has missing or unsupported fields"
        )
    reviewer = profile.get("reviewed_by")
    if not isinstance(reviewer, str) or not PROFILE_REVIEWER.fullmatch(reviewer):
        raise CostTelemetryError("Query profile requires a valid reviewer identifier")
    reviewed_at_value = profile.get("reviewed_at")
    if not isinstance(reviewed_at_value, str):
        raise CostTelemetryError("Query profile requires a timezone-aware review time")
    try:
        reviewed_at = datetime.fromisoformat(reviewed_at_value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CostTelemetryError(
            "Query profile review time must be an ISO timestamp"
        ) from exc
    if reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None:
        raise CostTelemetryError("Query profile review time must include a timezone")
    if reviewed_at.astimezone(timezone.utc) > datetime.now(timezone.utc):
        raise CostTelemetryError("Query profile review time cannot be in the future")

    identity_labels = profile.get("identity_labels")
    expected_labels = dict(zip(("namespace", "pod", "container"), label_names))
    if identity_labels != expected_labels:
        raise CostTelemetryError(
            "Query profile identity labels do not match the collection labels"
        )
    fingerprints = profile.get("query_sha256")
    if not isinstance(fingerprints, dict) or set(fingerprints) != set(QUERY_KEYS):
        raise CostTelemetryError(
            "Query profile must fingerprint all four collection queries"
        )
    actual_fingerprints = {
        key: hashlib.sha256(query.encode("utf-8")).hexdigest()
        for key, query in queries.items()
    }
    if any(
        not isinstance(fingerprints.get(key), str)
        or not PROFILE_HASH.fullmatch(fingerprints[key])
        or fingerprints[key] != actual_fingerprints[key]
        for key in QUERY_KEYS
    ):
        raise CostTelemetryError(
            "Query profile fingerprints do not match the supplied PromQL"
        )

    metric_profiles = {
        "cpu": (profile.get("cpu"), "cores", "per_container_usage"),
        "memory": (profile.get("memory"), "bytes", "per_container_usage"),
        "cpu_source_timestamp": (
            profile.get("cpu_source_timestamp"),
            "unix_seconds",
            "raw_sample_timestamp",
        ),
        "memory_source_timestamp": (
            profile.get("memory_source_timestamp"),
            "unix_seconds",
            "raw_sample_timestamp",
        ),
    }
    checked: dict[str, Any] = {}
    for key, (entry, expected_unit, expected_semantics) in metric_profiles.items():
        if not isinstance(entry, dict):
            raise CostTelemetryError(f"Query profile section {key!r} is required")
        expected_entry_keys = {
            "source_metric",
            "unit",
            "aggregation" if key in {"cpu", "memory"} else "semantics",
        }
        if set(entry) != expected_entry_keys:
            raise CostTelemetryError(
                f"Query profile section {key!r} has missing or unsupported fields"
            )
        source_metric = entry.get("source_metric")
        if not isinstance(source_metric, str) or not PROFILE_METRIC.fullmatch(
            source_metric
        ):
            raise CostTelemetryError(
                f"Query profile section {key!r} requires a source metric identifier"
            )
        if entry.get("unit") != expected_unit:
            raise CostTelemetryError(
                f"Query profile section {key!r} must declare {expected_unit!r}"
            )
        semantics_key = "aggregation" if key in {"cpu", "memory"} else "semantics"
        if entry.get(semantics_key) != expected_semantics:
            raise CostTelemetryError(
                f"Query profile section {key!r} has unsupported semantics"
            )
        checked[key] = {
            "source_metric": source_metric,
            "unit": expected_unit,
            semantics_key: expected_semantics,
        }
    if (
        checked["cpu"]["source_metric"]
        != checked["cpu_source_timestamp"]["source_metric"]
        or checked["memory"]["source_metric"]
        != checked["memory_source_timestamp"]["source_metric"]
    ):
        raise CostTelemetryError(
            "Source-timestamp profile metric must match its utilization metric"
        )
    return {
        "reviewed_by": reviewer,
        "reviewed_at": reviewed_at.astimezone(timezone.utc).isoformat(),
        "identity_labels": expected_labels,
        "query_sha256": actual_fingerprints,
        "metrics": checked,
        "trust_boundary": (
            "Operator-reviewed declarations; PromQL semantics are not statically "
            "proven by the collector."
        ),
    }


def _quantity(value: object, pattern: re.Pattern, multipliers: dict) -> float:
    if not isinstance(value, str) or not (match := pattern.fullmatch(value)):
        raise CostTelemetryError("Kubernetes resource quantity is invalid")
    try:
        number = Decimal(match.group(1)) * multipliers[match.group(2)]
    except (InvalidOperation, KeyError) as exc:
        raise CostTelemetryError(
            "Kubernetes resource quantity is invalid"
        ) from exc
    result = float(number)
    if not math.isfinite(result) or result <= 0:
        raise CostTelemetryError(
            "Kubernetes resource quantity must be finite and positive"
        )
    return result


def parse_cpu_quantity(value: object) -> float:
    """Return a positive Kubernetes CPU request/limit in cores."""
    return _quantity(
        value,
        CPU_QUANTITY,
        {None: Decimal(1), "n": Decimal("1e-9"), "u": Decimal("1e-6"),
         "m": Decimal("1e-3")},
    )


def parse_memory_quantity(value: object) -> float:
    """Return a positive Kubernetes memory request/limit in bytes."""
    return _quantity(value, MEMORY_QUANTITY, MEMORY_MULTIPLIERS)


def _validate_prometheus_url(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname
    is_loopback = host == "localhost"
    if host and not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or not is_loopback
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise CostTelemetryError(
            "Prometheus URL must be a credential-free HTTP(S) loopback base URL"
        )
    return url.rstrip("/")


def _kubectl_lab_objects(
    runner=subprocess.run,
) -> tuple[list[dict], list[dict], list[dict]]:
    try:
        result = runner(
            [
                "kubectl", "--context", LAB_CONTEXT, "get",
                "deployments,replicasets,pods", "-n", LAB_NAMESPACE, "-o", "json",
            ],
            capture_output=True,
            text=True,
            timeout=KUBERNETES_TIMEOUT_SECONDS,
            check=True,
        )
        import json

        payload = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise CostTelemetryError(
            "Read-only Kubernetes lab discovery failed"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise CostTelemetryError("Kubernetes discovery returned an invalid list")
    deployments = []
    replicasets = []
    pods = []
    for item in payload["items"]:
        if not isinstance(item, dict):
            raise CostTelemetryError("Kubernetes discovery contained an invalid item")
        kind = item.get("kind")
        metadata = item.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("namespace") != LAB_NAMESPACE:
            raise CostTelemetryError(
                "Kubernetes discovery returned an out-of-scope namespace"
            )
        if kind == "Deployment":
            deployments.append(item)
        elif kind == "ReplicaSet":
            replicasets.append(item)
        elif kind == "Pod":
            pods.append(item)
    return deployments, replicasets, pods


def _has_controller_owner(resource: dict, *, kind: str, uid: str) -> bool:
    metadata = resource.get("metadata") or {}
    owners = metadata.get("ownerReferences")
    return isinstance(owners, list) and any(
        isinstance(owner, dict)
        and owner.get("kind") == kind
        and owner.get("uid") == uid
        and owner.get("controller") is True
        for owner in owners
    )


def _matches_selector(labels: object, selector: object) -> bool:
    if not isinstance(labels, dict) or not isinstance(selector, dict):
        return False
    match_labels = selector.get("matchLabels")
    expressions = selector.get("matchExpressions", [])
    if not isinstance(match_labels, dict) or not match_labels or expressions:
        return False
    return all(labels.get(key) == value for key, value in match_labels.items())


def discover_kubernetes_resources(
    workloads: tuple[str, ...] = ("demo-web",),
    *,
    runner=subprocess.run,
) -> dict[str, dict[str, Any]]:
    """Discover selected, policy-allowlisted deployments in the fixed lab."""
    allowed = PolicyConfig().allowed_deployments
    if not workloads or len(set(workloads)) != len(workloads):
        raise CostTelemetryError("Select one or more distinct lab workloads")
    if any(workload not in allowed for workload in workloads):
        raise CostTelemetryError("A requested workload is not policy-allowlisted")

    deployments, replicasets, pods = _kubectl_lab_objects(runner)
    result: dict[str, dict[str, Any]] = {}
    claimed_pods: set[tuple[str, str]] = set()
    for workload in workloads:
        deployment = next(
            (item for item in deployments
             if (item.get("metadata") or {}).get("name") == workload),
            None,
        )
        if deployment is None:
            raise CostTelemetryError(
                f"Allowlisted deployment {workload!r} was not found in the lab"
            )
        deployment_metadata = deployment.get("metadata") or {}
        if not all(
            isinstance(deployment_metadata.get(field), str)
            and deployment_metadata[field]
            for field in ("uid", "resourceVersion")
        ):
            raise CostTelemetryError(
                f"Deployment {workload!r} lacks stable Kubernetes identity"
            )
        deployment_uid = deployment_metadata["uid"]
        owned_replicaset_uids = set()
        for replicaset in replicasets:
            metadata = replicaset.get("metadata") or {}
            if (
                _has_controller_owner(
                    replicaset, kind="Deployment", uid=deployment_uid
                )
                and isinstance(metadata.get("uid"), str)
                and metadata["uid"]
            ):
                owned_replicaset_uids.add(metadata["uid"])
        spec = deployment.get("spec") or {}
        selector = spec.get("selector") or {}
        if selector.get("matchExpressions"):
            raise CostTelemetryError(
                f"Deployment {workload!r} uses an unsupported selector"
            )
        template_labels = (
            (spec.get("template") or {}).get("metadata") or {}
        ).get("labels") or {}
        if not _matches_selector(template_labels, selector):
            raise CostTelemetryError(
                f"Deployment {workload!r} has no safe matchLabels selector"
            )
        selected_pods = []
        for pod in pods:
            metadata = pod.get("metadata") or {}
            if metadata.get("deletionTimestamp"):
                continue
            pod_labels = metadata.get("labels") or {}
            if _matches_selector(pod_labels, selector):
                if not any(
                    _has_controller_owner(
                        pod, kind="ReplicaSet", uid=replicaset_uid
                    )
                    for replicaset_uid in owned_replicaset_uids
                ):
                    raise CostTelemetryError(
                        f"Deployment {workload!r} selector matched a pod "
                        "outside its controller ownership chain"
                    )
                selected_pods.append(pod)
        desired = spec.get("replicas", 1)
        if (
            isinstance(desired, bool)
            or not isinstance(desired, int)
            or desired < 1
            or len(selected_pods) != desired
        ):
            raise CostTelemetryError(
                f"Deployment {workload!r} does not have a stable, complete pod set"
            )
        container_keys: set[tuple[str, str]] = set()
        for pod in selected_pods:
            pod_metadata = pod.get("metadata") or {}
            pod_name = pod_metadata.get("name")
            spec_containers = (pod.get("spec") or {}).get("containers")
            if (
                not isinstance(pod_name, str)
                or not isinstance(spec_containers, list)
                or not spec_containers
            ):
                raise CostTelemetryError(
                    f"Deployment {workload!r} has incomplete pod metadata"
                )
            status = pod.get("status") or {}
            ready = any(
                condition.get("type") == "Ready"
                and condition.get("status") == "True"
                for condition in status.get("conditions", [])
                if isinstance(condition, dict)
            )
            if status.get("phase") != "Running" or not ready:
                raise CostTelemetryError(
                    f"Deployment {workload!r} contains a pod that is not Ready"
                )
            for container in spec_containers:
                container_name = container.get("name")
                if not isinstance(container_name, str) or not container_name:
                    raise CostTelemetryError(
                        f"Deployment {workload!r} has an unnamed container"
                    )
                key = (pod_name, container_name)
                if key in container_keys:
                    raise CostTelemetryError(
                        f"Deployment {workload!r} has duplicate container identity"
                    )
                container_keys.add(key)
        if claimed_pods.intersection(container_keys):
            raise CostTelemetryError(
                "Selected deployment selectors overlap pod/container identity"
            )
        claimed_pods.update(container_keys)
        cpu_requests = memory_requests = 0.0
        cpu_limits = memory_limits = 0.0
        limits_complete = True
        container_metadata = []
        for pod in selected_pods:
            for container in (pod.get("spec") or {}).get("containers", []):
                resources = container.get("resources") or {}
                requests = resources.get("requests") or {}
                if "cpu" not in requests or "memory" not in requests:
                    raise CostTelemetryError(
                        f"Deployment {workload!r} has containers without CPU/memory requests"
                    )
                cpu_request = parse_cpu_quantity(requests["cpu"])
                memory_request = parse_memory_quantity(requests["memory"])
                cpu_requests += cpu_request
                memory_requests += memory_request
                limits = resources.get("limits") or {}
                if "cpu" in limits and "memory" in limits:
                    cpu_limit = parse_cpu_quantity(limits["cpu"])
                    memory_limit = parse_memory_quantity(limits["memory"])
                    cpu_limits += cpu_limit
                    memory_limits += memory_limit
                else:
                    cpu_limit = memory_limit = None
                    limits_complete = False
                container_metadata.append({
                    "pod": (pod.get("metadata") or {}).get("name"),
                    "container": container["name"],
                    "cpu_request": requests["cpu"],
                    "memory_request": requests["memory"],
                    "cpu_limit": limits.get("cpu"),
                    "memory_limit": limits.get("memory"),
                    "cpu_request_cores": cpu_request,
                    "memory_request_bytes": memory_request,
                    "cpu_limit_cores": cpu_limit,
                    "memory_limit_bytes": memory_limit,
                })
        result[workload] = {
            "namespace": LAB_NAMESPACE,
            "deployment": workload,
            "deployment_uid": deployment_uid,
            "resource_version": deployment_metadata["resourceVersion"],
            "replicas": desired,
            "pods": sorted((pod, container) for pod, container in container_keys),
            "cpu_request_cores": cpu_requests,
            "memory_request_bytes": memory_requests,
            "cpu_limit_cores": cpu_limits if limits_complete else None,
            "memory_limit_bytes": memory_limits if limits_complete else None,
            "cpu_limits_complete": limits_complete,
            "containers": sorted(
                container_metadata,
                key=lambda item: (item["pod"], item["container"]),
            ),
        }
    return result


def _prometheus_matrix(
    client: httpx.Client,
    base_url: str,
    query: str,
    start: datetime,
    end: datetime,
    step_seconds: int,
) -> list[dict]:
    try:
        response = client.get(
            f"{base_url}/api/v1/query_range",
            params={
                "query": query,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": step_seconds,
            },
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise CostTelemetryError(
            "Prometheus range query failed"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("status") != "success"
        or not isinstance(payload.get("data"), dict)
        or payload["data"].get("resultType") != "matrix"
        or not isinstance(payload["data"].get("result"), list)
    ):
        raise CostTelemetryError("Prometheus returned an invalid range response")
    return payload["data"]["result"]


def _parse_matrix(
    series: list[dict],
    *,
    metric_name: str,
    namespace: str,
    label_names: tuple[str, str, str],
    allowed_pods: set[tuple[str, str]],
) -> dict[tuple[str, str, float], float]:
    points: dict[tuple[str, str, float], float] = {}
    for item in series:
        if not isinstance(item, dict) or not isinstance(item.get("metric"), dict):
            raise CostTelemetryError(f"Prometheus {metric_name} series is invalid")
        labels = item["metric"]
        namespace_value, pod, container = (labels.get(name) for name in label_names)
        if not all(isinstance(value, str) and value for value in (
            namespace_value, pod, container
        )):
            raise CostTelemetryError(
                f"Prometheus {metric_name} series lacks configured identity labels"
            )
        if namespace_value != namespace:
            raise CostTelemetryError(
                f"Prometheus {metric_name} returned an out-of-scope namespace"
            )
        if (pod, container) not in allowed_pods:
            raise CostTelemetryError(
                f"Prometheus {metric_name} returned an unmapped pod/container"
            )
        values = item.get("values")
        if not isinstance(values, list):
            raise CostTelemetryError(f"Prometheus {metric_name} values are invalid")
        for sample in values:
            if not isinstance(sample, list) or len(sample) != 2:
                raise CostTelemetryError(f"Prometheus {metric_name} sample is invalid")
            try:
                timestamp = float(sample[0])
                value = float(sample[1])
            except (TypeError, ValueError) as exc:
                raise CostTelemetryError(
                    f"Prometheus {metric_name} sample is not numeric"
                ) from exc
            if not math.isfinite(timestamp) or not math.isfinite(value) or value < 0:
                raise CostTelemetryError(
                    f"Prometheus {metric_name} sample is non-finite or negative"
                )
            key = (pod, container, round(timestamp, 3))
            if key in points:
                raise CostTelemetryError(
                    f"Prometheus {metric_name} returned duplicate samples"
                )
            points[key] = value
    return points


def collect_kubernetes_cost_evidence(
    *,
    period_start: str,
    period_end: str,
    sample_interval_minutes: int,
    prometheus_url: str,
    cpu_query: str,
    memory_query: str,
    cpu_source_timestamp_query: str,
    memory_source_timestamp_query: str,
    query_profile: dict[str, Any],
    namespace_label: str,
    pod_label: str,
    container_label: str,
    workloads: tuple[str, ...] = ("demo-web",),
    runner=subprocess.run,
    http_client_factory=httpx.Client,
) -> dict[str, Any]:
    """Collect range samples and deployment requests without mutating systems."""
    try:
        start = datetime.fromisoformat(period_start.replace("Z", "+00:00"))
        end = datetime.fromisoformat(period_end.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise CostTelemetryError("Collection period must use ISO timestamps") from exc
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise CostTelemetryError(
            "Collection period must be ordered and include timezones"
        )
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    if (
        isinstance(sample_interval_minutes, bool)
        or not isinstance(sample_interval_minutes, int)
        or sample_interval_minutes < 1
    ):
        raise CostTelemetryError("Sample interval must be a positive number of minutes")
    if not all(query.strip() for query in (
        cpu_query, memory_query, cpu_source_timestamp_query,
        memory_source_timestamp_query,
    )):
        raise CostTelemetryError(
            "Explicit CPU, memory, and source-timestamp PromQL queries are required"
        )
    label_names = (namespace_label, pod_label, container_label)
    if any(
        not isinstance(label, str) or not PROFILE_LABEL.fullmatch(label)
        for label in label_names
    ):
        raise CostTelemetryError("Prometheus identity label names are required")
    if len(set(label_names)) != 3:
        raise CostTelemetryError("Prometheus identity label names must be distinct")
    reviewed_profile = _validate_query_profile(
        query_profile,
        queries={
            "cpu": cpu_query,
            "memory": memory_query,
            "cpu_source_timestamp": cpu_source_timestamp_query,
            "memory_source_timestamp": memory_source_timestamp_query,
        },
        label_names=label_names,
    )
    base_url = _validate_prometheus_url(prometheus_url)
    resources = discover_kubernetes_resources(workloads, runner=runner)
    step = sample_interval_minutes * 60
    maximum_age, maximum_skew = freshness_limits_from_environment()
    try:
        with http_client_factory(timeout=PROMETHEUS_TIMEOUT_SECONDS) as client:
            cpu_series = _prometheus_matrix(
                client, base_url, cpu_query, start, end, step
            )
            memory_series = _prometheus_matrix(
                client, base_url, memory_query, start, end, step
            )
            cpu_timestamp_series = _prometheus_matrix(
                client, base_url, cpu_source_timestamp_query, start, end, step
            )
            memory_timestamp_series = _prometheus_matrix(
                client, base_url, memory_source_timestamp_query, start, end, step
            )
    except CostTelemetryError:
        raise
    except httpx.HTTPError as exc:
        raise CostTelemetryError("Prometheus collection could not be completed") from exc

    all_allowed_pods = {
        pair for metadata in resources.values() for pair in metadata["pods"]
    }
    points = {
        "cpu": _parse_matrix(
            cpu_series,
            metric_name="CPU",
            namespace=LAB_NAMESPACE,
            label_names=label_names,
            allowed_pods=all_allowed_pods,
        ),
        "memory": _parse_matrix(
            memory_series,
            metric_name="memory",
            namespace=LAB_NAMESPACE,
            label_names=label_names,
            allowed_pods=all_allowed_pods,
        ),
        "cpu_timestamp": _parse_matrix(
            cpu_timestamp_series,
            metric_name="CPU source timestamp",
            namespace=LAB_NAMESPACE,
            label_names=label_names,
            allowed_pods=all_allowed_pods,
        ),
        "memory_timestamp": _parse_matrix(
            memory_timestamp_series,
            metric_name="memory source timestamp",
            namespace=LAB_NAMESPACE,
            label_names=label_names,
            allowed_pods=all_allowed_pods,
        ),
    }
    observations = []
    expected_samples = math.ceil((end - start).total_seconds() / step)
    for workload, metadata in resources.items():
        allowed_pods = set(metadata["pods"])
        stale_samples = 0
        expected_containers = len(allowed_pods)
        for index in range(expected_samples):
            timestamp = round(start.timestamp() + index * step, 3)
            keys = {
                (pod, container, timestamp) for pod, container in allowed_pods
            }
            if not all(
                keys.issubset(points[name])
                for name in (
                    "cpu", "memory", "cpu_timestamp", "memory_timestamp"
                )
            ):
                continue
            ages = [
                timestamp - points[name][key]
                for name in ("cpu_timestamp", "memory_timestamp")
                for key in keys
            ]
            if any(
                age > maximum_age or age < -maximum_skew for age in ages
            ):
                stale_samples += 1
                continue
            observations.append({
                "timestamp": datetime.fromtimestamp(
                    timestamp, timezone.utc
                ).isoformat(),
                "resource": f"{LAB_NAMESPACE}/{workload}",
                "service": workload,
                "instance_type": "kubernetes-request",
                "importance": "standard",
                "cpu_utilization": sum(points["cpu"][key] for key in keys)
                / metadata["cpu_request_cores"],
                "memory_utilization": sum(points["memory"][key] for key in keys)
                / metadata["memory_request_bytes"],
            })
        metadata["expected_samples"] = expected_samples
        metadata["complete_samples"] = sum(
            row["service"] == workload for row in observations
        )
        metadata["expected_container_series"] = expected_containers
        metadata["stale_samples"] = stale_samples

    return {
        "observations": observations,
        "resources": {
            f"{LAB_NAMESPACE}/{workload}": metadata
            for workload, metadata in resources.items()
        },
        "collection": {
            "status": "completed",
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "namespace": LAB_NAMESPACE,
            "kubernetes_context": LAB_CONTEXT,
            "prometheus_url": base_url,
            "query_range_samples_are_evaluation_timestamps": True,
            "sample_interval_minutes": sample_interval_minutes,
            "expected_samples_per_resource": expected_samples,
            "maximum_sample_age_seconds": maximum_age,
            "maximum_future_skew_seconds": maximum_skew,
            "query_sha256": reviewed_profile["query_sha256"],
            "identity_labels": {
                "namespace": namespace_label,
                "pod": pod_label,
                "container": container_label,
            },
            "query_profile": reviewed_profile,
            "resource_coverage": {
                f"{LAB_NAMESPACE}/{workload}": {
                    "expected_samples": metadata["expected_samples"],
                    "complete_samples": metadata["complete_samples"],
                    "stale_samples": metadata["stale_samples"],
                }
                for workload, metadata in resources.items()
            },
            "query_text_saved": False,
        },
    }
