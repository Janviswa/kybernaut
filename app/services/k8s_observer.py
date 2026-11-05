"""Read-only Kubernetes incident observer for the Kybernaut lab."""

import json
import subprocess
from datetime import datetime, timedelta, timezone

LAB_NAMESPACE = "kybernaut-lab"
LAB_CONTEXT = "kind-kybernaut"
LAB_DEPLOYMENT = "demo-web"
POD_EVENT_LOOKBACK = timedelta(minutes=30)

POD_FAILURE_REASONS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerConfigError",
}


def _parse_timestamp(value: object) -> datetime | None:
    if isinstance(value, datetime):
        timestamp = value
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        return timestamp.astimezone(timezone.utc)
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _event_timestamp(event: dict) -> datetime | None:
    """Return the best available Kubernetes event timestamp."""
    series = event.get("series")
    candidates = [
        event.get("eventTime"),
        series.get("lastObservedTime") if isinstance(series, dict) else None,
        event.get("lastTimestamp"),
        event.get("firstTimestamp"),
        event.get("metadata", {}).get("creationTimestamp"),
    ]
    for value in candidates:
        timestamp = _parse_timestamp(value)
        if timestamp is not None:
            return timestamp
    return None


def _is_recent_event(event: dict, now: datetime) -> bool:
    timestamp = _event_timestamp(event)
    if timestamp is None:
        return False
    age = now - timestamp
    return timedelta(0) <= age <= POD_EVENT_LOOKBACK


def _is_recent_timestamp(value: object, now: datetime) -> bool:
    timestamp = _parse_timestamp(value)
    if timestamp is None:
        return False
    age = now - timestamp
    return timedelta(0) <= age <= POD_EVENT_LOOKBACK


def _pod_failure_timestamp(pod: dict, status: dict) -> datetime | None:
    metadata = pod.get("metadata") or {}
    timestamps = [metadata.get("creationTimestamp")]
    for condition in status.get("conditions", []):
        if isinstance(condition, dict):
            timestamps.append(condition.get("lastTransitionTime"))
    for container in status.get("containerStatuses", []):
        if not isinstance(container, dict):
            continue
        last_state = container.get("lastState") or {}
        terminated = last_state.get("terminated") or {}
        timestamps.append(terminated.get("finishedAt"))
    parsed = [
        timestamp for timestamp in (_parse_timestamp(value) for value in timestamps)
        if timestamp is not None
    ]
    return max(parsed) if parsed else None


def _kubectl_json(
    resource: str,
    *,
    timeout: int = 20,
    context: str | None = None,
) -> dict:
    """Read JSON from a fixed Kubernetes resource in the lab namespace."""
    if resource not in {"pods", "events", "deployments"}:
        raise ValueError("Unsupported Kubernetes resource")
    if context is not None and context != LAB_CONTEXT:
        raise ValueError("Unsupported Kubernetes context")

    command = ["kubectl"]
    if context is not None:
        command.extend(["--context", context])
    command.extend([
        "get", resource,
        "-n", LAB_NAMESPACE,
        "-o", "json",
    ])
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=True,
    )
    return json.loads(result.stdout)


def observe_namespace() -> dict:
    """Return structured incidents from pod states and Warning events."""
    pods_data = _kubectl_json("pods")
    events_data = _kubectl_json("events")
    deployments_data = _kubectl_json("deployments")

    incidents_by_key = {}
    persistent_disk_fill_evictions = []
    severity_rank = {"low": 1, "medium": 2, "high": 3}
    now = datetime.now(timezone.utc)

    def add_incident(
        incident_type, severity, resource, evidence, recommendation,
        resource_kind=None, resource_uid=None, source_timestamp=None,
        ongoing=False,
    ):
        uid = resource_uid if resource_kind == "Pod" else None
        key = (incident_type, resource, uid)
        incident = incidents_by_key.get(key)

        if resource_kind is None:
            if incident_type in {"pod_container_failure", "pod_failed"}:
                resource_kind = "Pod"
            elif incident_type == "deployment_unavailable_replicas":
                resource_kind = "Deployment"
            else:
                resource_kind = "Unknown"

        if incident is None:
            incident = {
                "source": "kubernetes",
                "namespace": LAB_NAMESPACE,
                "type": incident_type,
                "severity": severity,
                "resource": resource,
                "resource_kind": resource_kind,
                "evidence": [],
                "recommendation": recommendation,
                "source_timestamp": (
                    source_timestamp or now
                ).astimezone(timezone.utc).isoformat(),
            }
            if uid:
                incident["resource_uid"] = uid
            if ongoing:
                # A condition that is still in effect (waiting/crash-looping/OOM
                # restarts). Its identity is the pod incarnation, so its
                # timestamp must not change on every observation.
                incident["ongoing"] = True
            incidents_by_key[key] = incident
        elif severity_rank.get(severity, 0) > severity_rank.get(
            incident["severity"], 0
        ):
            incident["severity"] = severity
        if ongoing and incident is not None and not incident.get("ongoing"):
            incident["ongoing"] = True
            incident["source_timestamp"] = (
                source_timestamp or now
            ).astimezone(timezone.utc).isoformat()

        if evidence not in incident["evidence"]:
            incident["evidence"].append(evidence)

    for pod in pods_data.get("items", []):
        metadata = pod.get("metadata", {})
        status = pod.get("status", {})
        pod_name = metadata.get("name", "unknown-pod")
        # Stable for the lifetime of this pod (uid): used as the identity
        # timestamp of still-in-effect container failures.
        pod_incarnation = _parse_timestamp(metadata.get("creationTimestamp")) or now

        for container in status.get("containerStatuses", []):
            waiting = (
                container.get("state", {})
                .get("waiting", {})
            )
            reason = waiting.get("reason")

            if reason in POD_FAILURE_REASONS:
                add_incident(
                    "pod_container_failure",
                    "high",
                    pod_name,
                    f"Container {container.get('name', 'unknown')} "
                    f"is waiting with reason {reason}: "
                    f"{waiting.get('message', 'No message provided')}",
                    "Inspect pod events and container image/configuration.",
                    resource_kind="Pod",
                    resource_uid=metadata.get("uid"),
                    source_timestamp=pod_incarnation,
                    ongoing=True,
                )

            last_state = container.get("lastState") or {}
            terminated = last_state.get("terminated") or {}
            termination_reason = terminated.get("reason")
            if (
                termination_reason == "OOMKilled"
                and _is_recent_timestamp(terminated.get("finishedAt"), now)
            ):
                exit_code = terminated.get("exitCode")
                exit_detail = (
                    f", exit code {exit_code}"
                    if isinstance(exit_code, int) else ""
                )
                add_incident(
                    "pod_container_failure",
                    "high",
                    pod_name,
                    f"Container {container.get('name', 'unknown')} "
                    f"previously terminated with reason "
                    f"{termination_reason}{exit_detail}.",
                    "Inspect pod status, events, and previous container logs.",
                    resource_kind="Pod",
                    resource_uid=metadata.get("uid"),
                    source_timestamp=pod_incarnation,
                    ongoing=True,
                )
            elif (
                termination_reason == "Error"
                and isinstance(terminated.get("exitCode"), int)
                and terminated["exitCode"] != 0
                and isinstance(container.get("restartCount"), int)
                and container["restartCount"] > 0
                and _is_recent_timestamp(terminated.get("finishedAt"), now)
            ):
                add_incident(
                    "pod_container_failure",
                    "high",
                    pod_name,
                    f"Container {container.get('name', 'unknown')} restarted "
                    f"after termination reason Error "
                    f"(exit code {terminated['exitCode']}).",
                    "Inspect pod status, events, and previous container logs.",
                    resource_kind="Pod",
                    resource_uid=metadata.get("uid"),
                    source_timestamp=pod_incarnation,
                    ongoing=True,
                )

        if status.get("phase") == "Failed":
            failure_timestamp = _pod_failure_timestamp(pod, status)
            failure_message = status.get("message")
            persistent_disk_fill_eviction = (
                pod_name.startswith("sim-disk-fill-")
                and status.get("reason") == "Evicted"
                and isinstance(failure_message, str)
                and "emptydir" in failure_message.lower()
                and (
                    "exceed" in failure_message.lower()
                    or "limit" in failure_message.lower()
                )
            )
            failure_is_recent = (
                failure_timestamp is not None
                and _is_recent_timestamp(failure_timestamp, now)
            )
            if failure_is_recent or persistent_disk_fill_eviction:
                failure_evidence = [
                    f"Pod phase is Failed; reason: "
                    f"{status.get('reason', 'not specified')}"
                ]
                if persistent_disk_fill_eviction:
                    failure_evidence.append(
                        f"Evicted: {failure_message}"
                    )
                if persistent_disk_fill_eviction and not failure_is_recent:
                    persistent_disk_fill_evictions.append({
                        "source": "kubernetes",
                        "namespace": LAB_NAMESPACE,
                        "type": "pod_failed",
                        "severity": "high",
                        "resource": pod_name,
                        "resource_kind": "Pod",
                        "resource_uid": metadata.get("uid"),
                        "evidence": failure_evidence,
                        "source_timestamp": (
                            failure_timestamp.isoformat()
                            if failure_timestamp is not None else None
                        ),
                        "ongoing": True,
                    })
                    continue
                for evidence in failure_evidence:
                    add_incident(
                        "pod_failed",
                        "high",
                        pod_name,
                        evidence,
                        "Inspect pod status, events, and previous container logs.",
                        resource_kind="Pod",
                        resource_uid=metadata.get("uid"),
                        source_timestamp=failure_timestamp,
                        ongoing=persistent_disk_fill_eviction,
                    )

    # Ignore events whose referenced Pod or Deployment no longer exists.
    # Pod UIDs prevent a stale event from matching a replacement Pod.
    live_pods = {
        (
            pod.get("metadata", {}).get("name"),
            pod.get("metadata", {}).get("uid"),
        )
        for pod in pods_data.get("items", [])
    }
    live_deployments = {
        deployment.get("metadata", {}).get("name")
        for deployment in deployments_data.get("items", [])
    }

    for event in events_data.get("items", []):
        if event.get("type") != "Warning":
            continue
        event_timestamp = _event_timestamp(event)
        event_is_recent = (
            event_timestamp is not None
            and _is_recent_event(event, now)
        )
        if event_timestamp is not None and not event_is_recent:
            continue

        involved = event.get("involvedObject", {})
        kind = involved.get("kind")
        resource = involved.get("name", "unknown-resource")

        if kind == "Pod":
            event_uid = involved.get("uid")
            pod_exists = any(
                name == resource and (not event_uid or uid == event_uid)
                for name, uid in live_pods
            )
            recent_eviction = (
                event.get("reason") == "Evicted"
                and event_is_recent
            )
            if not pod_exists and not recent_eviction:
                continue
        elif kind == "Deployment" and resource not in live_deployments:
            continue

        reason = event.get("reason", "UnknownWarning")
        message = event.get("message", "No event message provided")

        severity = (
            "high" if reason in {"Failed", "BackOff", "FailedScheduling"}
            else "medium"
        )
        evidence = f"{reason}: {message}"
        recommendation = (
            "Inspect the Kubernetes event and affected resource before remediation."
        )

        # Attach Pod warning events to an existing failure incident for that pod.
        # Keep unrelated warnings and Deployment events as separate incidents.
        existing_pod_failure = None
        if kind == "Pod":
            for incident_type in ("pod_container_failure", "pod_failed"):
                for key, incident in incidents_by_key.items():
                    if (
                        key[0] == incident_type
                        and key[1] == resource
                        and (not event_uid or key[2] == event_uid)
                    ):
                        existing_pod_failure = incident
                        break
                if existing_pod_failure is not None:
                    break

        if existing_pod_failure is not None:
            if severity_rank.get(severity, 0) > severity_rank.get(
                existing_pod_failure["severity"], 0
            ):
                existing_pod_failure["severity"] = severity
            if evidence not in existing_pod_failure["evidence"]:
                existing_pod_failure["evidence"].append(evidence)
        else:
            add_incident(
                "kubernetes_warning_event",
                severity,
                resource,
                evidence,
                recommendation,
                resource_kind=kind if kind in {"Pod", "Deployment"} else None,
                resource_uid=event_uid if kind == "Pod" else None,
                source_timestamp=event_timestamp,
            )

    deployment_summaries = []
    for deployment in deployments_data.get("items", []):
        metadata = deployment.get("metadata", {})
        status = deployment.get("status", {})
        spec = deployment.get("spec", {})
        deployment_summaries.append({
            "name": metadata.get("name"),
            "desired_replicas": spec.get("replicas", 1),
            "available_replicas": status.get("availableReplicas", 0),
            "ready_replicas": status.get("readyReplicas", 0),
        })

        desired = spec.get("replicas", 1)
        available = status.get("availableReplicas", 0)

        if available < desired:
            name = metadata.get("name", "unknown-deployment")
            add_incident(
                "deployment_unavailable_replicas",
                "high",
                name,
                f"Deployment has {available}/{desired} available replicas.",
                "Inspect rollout status, pod conditions, and recent events.",
                ongoing=True,
            )
            # The unavailable replicas are a symptom. Carry the still-failing
            # pods' own evidence (CrashLoopBackOff, ImagePullBackOff, OOMKilled)
            # on the deployment incident so it is classified by its real cause
            # and matches the same runbooks as the pod-level incident.
            for pod_incident in list(incidents_by_key.values()):
                if (
                    pod_incident.get("resource_kind") == "Pod"
                    and pod_incident.get("ongoing")
                    and str(pod_incident.get("resource", "")).startswith(f"{name}-")
                ):
                    for line in pod_incident.get("evidence", []):
                        add_incident(
                            "deployment_unavailable_replicas", "high", name,
                            f"Pod {pod_incident['resource']}: {line}",
                            "Inspect rollout status, pod conditions, and recent events.",
                            ongoing=True,
                        )

    incidents = list(incidents_by_key.values())

    return {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "namespace": LAB_NAMESPACE,
        "incident_count": len(incidents),
        "incidents": incidents,
        "_persistent_disk_fill_evictions": persistent_disk_fill_evictions,
        "deployments": deployment_summaries,
    }