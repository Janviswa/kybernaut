"""Deterministic incident classification into the benchmark categories.

Categories: cpu_spike, memory_leak, pod_crash, disk_full,
deployment_failure, unknown. Classification is evidence-based and
separate from any LLM RCA prediction (guide section 08 / prompt 8).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.models.incident_contract import IncidentClassification

INCIDENT_TYPES = (
    "cpu_spike", "memory_leak", "pod_crash", "disk_full",
    "deployment_failure", "unknown",
)


def _deployment_for_pod(pod: str, deployments: list[str]) -> str | None:
    matches = [d for d in deployments if pod.startswith(d + "-")]
    return max(matches, key=len) if matches else None


def _source_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _result(
    incident_type: str,
    confidence: float,
    severity: str,
    target: dict | None,
    evidence: list[str],
) -> dict:
    return IncidentClassification(
        incident_type=incident_type,
        confidence=confidence,
        severity=severity,
        target=target,
        evidence=evidence,
        method="rules",
    ).model_dump()


def classify_incident(
    diagnosis: Any, kubernetes: Any, telemetry: Any
) -> dict:
    """Classify only Kubernetes symptoms and supported telemetry candidates.

    Diagnosis findings intentionally do not select benchmark incident
    categories: service availability/5xx/latency findings are handled by
    deterministic decision policy, and should not be guessed into a
    Kubernetes failure category.
    """
    kubernetes = kubernetes if isinstance(kubernetes, dict) else {}
    telemetry = telemetry if isinstance(telemetry, dict) else {}

    namespace = kubernetes.get("namespace")
    if not isinstance(namespace, str) or not namespace.strip():
        namespace = None
    summaries = kubernetes.get("deployments")
    deployments = [
        d["name"] for d in (summaries if isinstance(summaries, list) else [])
        if isinstance(d, dict) and isinstance(d.get("name"), str)
    ]
    incidents = kubernetes.get("incidents")
    incidents = incidents if isinstance(incidents, list) else []

    candidates: list[dict] = []

    for inc in incidents:
        if not isinstance(inc, dict):
            continue
        raw_evidence = inc.get("evidence")
        evidence = [
            e.strip() for e in raw_evidence
            if isinstance(e, str) and e.strip()
        ] if isinstance(raw_evidence, list) else []
        if not evidence:
            continue
        text = " ".join(evidence).lower()
        resource = inc.get("resource")
        if not isinstance(resource, str) or not resource.strip() or not namespace:
            continue
        incident_namespace = inc.get("namespace")
        if incident_namespace is not None and (
            not isinstance(incident_namespace, str)
            or not incident_namespace.strip()
            or incident_namespace != namespace
        ):
            continue
        kind = inc.get("resource_kind")
        if kind not in {"Pod", "Deployment"}:
            continue
        severity = inc.get("severity", "medium")
        if severity not in {"low", "medium", "high", "critical"}:
            continue
        itype = inc.get("type")
        pod = resource if kind == "Pod" else None
        deployment = (
            _deployment_for_pod(resource, deployments) if pod
            else (resource if kind == "Deployment" else None)
        )
        raw_timestamp = inc.get("source_timestamp")
        source_timestamp = _source_timestamp(raw_timestamp)
        if raw_timestamp not in (None, "") and source_timestamp is None:
            continue
        base = {
            "namespace": namespace, "resource": resource,
            "resource_kind": kind, "deployment": deployment,
            "resource_uid": (
                inc.get("resource_uid")
                if kind == "Pod"
                and isinstance(inc.get("resource_uid"), str)
                and inc.get("resource_uid")
                else None
            ),
            "source_type": itype,
            "severity": severity,
            "evidence": evidence,
            "source_timestamp": source_timestamp,
        }

        if any(s in text for s in ("imagepullbackoff", "errimagepull")):
            candidates.append({**base, "incident_type": "deployment_failure",
                               "confidence": 0.9})
        elif "oomkilled" in text:
            candidates.append({**base, "incident_type": "memory_leak",
                               "confidence": 0.7})
        elif "evicted" in text and (
            any(s in text for s in ("ephemeral", "disk", "storage"))
            or (
                "emptydir" in text
                and any(s in text for s in ("exceed", "limit", "size"))
            )
        ):
            candidates.append({**base, "incident_type": "disk_full",
                               "confidence": 0.8})
        elif "crashloopbackoff" in text:
            candidates.append({**base, "incident_type": "pod_crash",
                               "confidence": 0.85})
        elif "back-off restarting" in text:
            candidates.append({**base, "incident_type": "pod_crash",
                               "confidence": 0.7})
        elif "restarted after termination reason error" in text:
            candidates.append({**base, "incident_type": "pod_crash",
                               "confidence": 0.75})
        elif itype == "pod_failed":
            candidates.append({**base, "incident_type": "pod_crash",
                               "confidence": 0.6})
        elif itype == "deployment_unavailable_replicas":
            candidates.append({**base, "incident_type": "deployment_failure",
                               "confidence": 0.6})

    if candidates:
        oldest = datetime.min.replace(tzinfo=timezone.utc)
        latest_timestamp = max(
            c["source_timestamp"] or oldest for c in candidates
        )
        latest = [
            c for c in candidates
            if (c["source_timestamp"] or oldest) == latest_timestamp
        ]
        pod_deployments = {
            candidate["deployment"]
            for candidate in latest
            if candidate["resource_kind"] == "Pod"
            and candidate["deployment"]
        }
        latest = [
            candidate for candidate in latest
            if not (
                candidate["source_type"] == "deployment_unavailable_replicas"
                and candidate["deployment"] in pod_deployments
                and any(
                    pod["deployment"] == candidate["deployment"]
                    and pod["incident_type"] != candidate["incident_type"]
                    for pod in latest
                    if pod["resource_kind"] == "Pod"
                )
            )
        ]
        latest_categories = {candidate["incident_type"] for candidate in latest}
        if len(latest_categories) > 1:
            return _result(
                "unknown",
                0.0,
                "low",
                None,
                list(dict.fromkeys(
                    item for candidate in latest
                    for item in candidate["evidence"]
                )),
            )
        best = max(latest, key=lambda c: c["confidence"])
        identities = {
            (
                candidate["namespace"],
                candidate["resource"],
                candidate["resource_kind"],
                candidate["resource_uid"],
                candidate["deployment"],
            )
            for candidate in latest
        }
        if len(identities) > 1:
            severity_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
            severity = max(
                latest,
                key=lambda candidate: severity_order[candidate["severity"]],
            )["severity"]
            return _result(
                best["incident_type"],
                best["confidence"],
                severity,
                None,
                list(dict.fromkeys(
                    item for candidate in latest
                    for item in candidate["evidence"]
                )),
            )
        return _result(
            best["incident_type"],
            best["confidence"],
            best["severity"],
            {
                "namespace": best["namespace"],
                "resource": best["resource"],
                "resource_kind": best["resource_kind"],
                "resource_uid": best["resource_uid"],
                "deployment": best["deployment"],
            },
            best["evidence"],
        )

    anomaly = telemetry.get("actionable_candidates")
    if telemetry.get("status") == "evaluated" and isinstance(anomaly, list):
        # A single statistical reading is not proof of a leak or spike, so
        # confidence stays low and no Kubernetes target is inferred.
        if "cpu_cores_used" in anomaly:
            return _telemetry_result("cpu_spike", "cpu_cores_used")
        if "memory_bytes" in anomaly:
            return _telemetry_result("memory_leak", "memory_bytes")

    return _result("unknown", 0.0, "low", None, [])


def _telemetry_result(incident_type: str, feature: str) -> dict:
    return _result(
        incident_type,
        0.5,
        "medium",
        None,
        [f"Statistical anomaly: {feature} is abnormally HIGH."],
    )
