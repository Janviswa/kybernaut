"""Persist successful Kubernetes observations in the incident repository."""

import hashlib
from collections.abc import Mapping

from app.services.incident_repository import (
    list_incidents,
    record_incident,
    resolve_incident_fingerprint,
)

KUBERNETES_SERVICE = "kubernetes-lab"
FINGERPRINT_PREFIX = "k8s:"


def build_fingerprint(incident: dict, namespace: str) -> str:
    """Build a stable identity for a Kubernetes resource incident."""
    incident_type = incident["type"]
    resource = incident["resource"]

    resource_kind = incident.get("resource_kind")
    if not resource_kind:
        if incident_type in {"pod_container_failure", "pod_failed"}:
            resource_kind = "Pod"
        elif incident_type == "deployment_unavailable_replicas":
            resource_kind = "Deployment"
        else:
            resource_kind = "Unknown"

    return (
        f"{FINGERPRINT_PREFIX}{namespace}:"
        f"{resource_kind}:{resource}:{incident_type}"
    )


def validate_observation(observation: dict) -> None:
    """Reject malformed or incomplete observation payloads before writes."""
    if not isinstance(observation, Mapping):
        raise ValueError("Observation must be a mapping")

    namespace = observation.get("namespace")
    if not isinstance(namespace, str) or not namespace.strip():
        raise ValueError("Observation namespace is missing")

    if namespace != "kybernaut-lab":
        raise ValueError("Unexpected Kubernetes namespace")

    incidents = observation.get("incidents")
    deployments = observation.get("deployments")

    if not isinstance(incidents, list):
        raise ValueError("Observation incidents must be a list")

    if not isinstance(deployments, list):
        raise ValueError("Observation deployments must be a list")

    if observation.get("incident_count") != len(incidents):
        raise ValueError("Observation incident count is inconsistent")

    deployment_names = {
        deployment.get("name")
        for deployment in deployments
        if isinstance(deployment, Mapping)
    }
    if "demo-web" not in deployment_names:
        raise ValueError(
            "Expected demo-web deployment missing from observation"
        )

    for incident in incidents:
        if not isinstance(incident, Mapping):
            raise ValueError("Incident must be a mapping")

        for field in ("type", "resource", "severity"):
            value = incident.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Incident field {field} is missing")

        if incident["severity"] not in {"low", "medium", "high"}:
            raise ValueError("Incident severity is invalid")

        evidence = incident.get("evidence")
        if not isinstance(evidence, list) or not all(
            isinstance(item, str) for item in evidence
        ):
            raise ValueError("Incident evidence must be a list of strings")

    for deployment in deployments:
        if not isinstance(deployment, Mapping):
            raise ValueError("Deployment must be a mapping")
        if not isinstance(deployment.get("name"), str) or not deployment["name"]:
            raise ValueError("Deployment name is missing")


def persist_observation(observation: dict) -> dict:
    """Validate and persist a complete successful observation."""
    validate_observation(observation)

    namespace = observation["namespace"]
    observed_incidents = observation["incidents"]
    active_fingerprints = set()
    persisted = []

    for incident in observed_incidents:
        fingerprint = build_fingerprint(incident, namespace)
        active_fingerprints.add(fingerprint)

        digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12]
        evidence = incident.get("evidence", [])
        message = "; ".join(evidence) or incident.get(
            "recommendation", "Kubernetes incident detected"
        )

        record = record_incident({
            "incident_id": f"inc-k8s-{digest}",
            "service": KUBERNETES_SERVICE,
            "type": incident["type"],
            "severity": incident["severity"],
            "message": message,
            "fingerprint": fingerprint,
        })
        persisted.append(record)

    # Only resolve previously active Kubernetes records that are absent from
    # this successfully completed observation. Other services are untouched.
    for record in list_incidents():
        fingerprint = record.get("fingerprint")

        if (
            record.get("service") == KUBERNETES_SERVICE
            and record.get("status") in {"OPEN", "REMEDIATING"}
            and fingerprint
            and fingerprint.startswith(FINGERPRINT_PREFIX)
            and fingerprint not in active_fingerprints
        ):
            resolve_incident_fingerprint(fingerprint)

    return {
        "persisted_count": len(persisted),
        "active_fingerprints": sorted(active_fingerprints),
    }
