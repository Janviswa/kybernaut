"""Read-only diagnosis for persisted Prometheus anomaly incidents."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services import audit_repository, incident_repository
from app.services.anomaly_runtime import EXPECTED_SERVICES
from app.services.incident_classifier import classify_incident
from app.services.incident_workflow import analyze_root_cause, enforce_policy
from app.services.k8s_observer import LAB_NAMESPACE, observe_namespace
from app.services.prometheus_features import freshness_limits_from_environment
from app.services.runbook_retriever import retrieve_runbooks

WORKLOAD_MAPPING_PATH = (
    Path(__file__).resolve().parents[2]
    / "config"
    / "application_workload_map.json"
)
SUPPORTED_SERVICES = frozenset(EXPECTED_SERVICES)
FEATURE_UNITS = {
    "cpu_cores_used": ("cpu_spike", "CPU cores"),
    "memory_bytes": ("memory_leak", "bytes"),
}
PROCESSING_TIMEOUT_SECONDS = 300
logger = logging.getLogger(__name__)


class PersistedEvidenceError(ValueError):
    """A stored anomaly snapshot cannot be safely interpreted."""


def _material_digest(incident: dict) -> str:
    source_evidence = incident.get("source_evidence")
    metric = source_evidence.get("metric") if isinstance(
        source_evidence, dict
    ) else None
    try:
        mapping_config = json.loads(
            WORKLOAD_MAPPING_PATH.read_text(encoding="utf-8")
        )
        services = (
            mapping_config.get("services")
            if isinstance(mapping_config, dict) else None
        )
        mapping_identity = (
            services.get(incident.get("service"))
            if isinstance(services, dict) else None
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        mapping_identity = "mapping_unavailable"
    identity = {
        "row_id": incident.get("id"),
        "incident_id": incident.get("incident_id"),
        "service": incident.get("service"),
        "incident_type": incident.get("incident_type"),
        "severity": incident.get("severity"),
        "fingerprint": incident.get("fingerprint"),
        "source_evidence": (
            {
                key: source_evidence.get(key)
                for key in (
                    "source", "service", "incident_type", "observed_at",
                    "source_timestamp",
                )
            }
            | {
                "metric": metric,
            }
            if isinstance(source_evidence, dict) else None
        ),
        "trusted_mapping": mapping_identity,
    }
    encoded = json.dumps(
        identity, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _telemetry_from_incident(incident: dict) -> tuple[dict, dict]:
    source = incident.get("source_evidence")
    if not isinstance(source, dict):
        raise PersistedEvidenceError("persisted_evidence_missing")
    if source.get("_decode_error") == "invalid_json":
        raise PersistedEvidenceError("persisted_evidence_invalid_json")
    service = incident.get("service")
    if (
        source.get("schema_version") != 1
        or source.get("source") != "prometheus"
        or source.get("service") != service
        or not isinstance(service, str)
        or service not in SUPPORTED_SERVICES
        or source.get("incident_type") != incident.get("incident_type")
    ):
        raise PersistedEvidenceError("persisted_evidence_identity_invalid")

    metric = source.get("metric")
    if not isinstance(metric, dict):
        raise PersistedEvidenceError("persisted_metric_evidence_missing")
    name = metric.get("name")
    expected_type_unit = FEATURE_UNITS.get(name)
    if (
        expected_type_unit is None
        or expected_type_unit[0] != incident.get("incident_type")
        or metric.get("unit") != expected_type_unit[1]
    ):
        raise PersistedEvidenceError("persisted_metric_identity_invalid")
    for key in ("value", "baseline_median", "deviation_score", "threshold"):
        value = metric.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise PersistedEvidenceError("persisted_metric_value_invalid")
    if (
        metric.get("direction") != "HIGH"
        or metric.get("classification") != "actionable_candidate"
    ):
        raise PersistedEvidenceError("persisted_metric_classification_invalid")

    observed_at = source.get("observed_at")
    source_timestamp = source.get("source_timestamp")
    if not isinstance(observed_at, str) or not isinstance(source_timestamp, str):
        raise PersistedEvidenceError("persisted_evidence_timestamp_missing")
    parsed_timestamps = []
    for timestamp in (observed_at, source_timestamp):
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PersistedEvidenceError(
                "persisted_evidence_timestamp_invalid"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise PersistedEvidenceError(
                "persisted_evidence_timestamp_invalid"
            )
        parsed_timestamps.append(parsed.astimezone(timezone.utc))
    try:
        max_age, max_skew = freshness_limits_from_environment()
    except ValueError as exc:
        raise PersistedEvidenceError(
            "prometheus_freshness_configuration_invalid"
        ) from exc
    now = datetime.now(timezone.utc)
    ages = [(now - timestamp).total_seconds() for timestamp in parsed_timestamps]
    if any(age < 0 for age in ages):
        raise PersistedEvidenceError("persisted_evidence_timestamp_future")
    if any(age > max_age for age in ages):
        raise PersistedEvidenceError("persisted_evidence_stale")
    if abs((parsed_timestamps[0] - parsed_timestamps[1]).total_seconds()) > max_skew:
        raise PersistedEvidenceError("persisted_evidence_timestamp_skew")
    telemetry = {
        "status": "evaluated",
        "observed_at": observed_at,
        "sample_timestamps": {name: source_timestamp},
        "threshold": metric["threshold"],
        "actionable_candidates": [name],
        "features": {name: metric},
        "operational_findings": [],
    }
    return telemetry, source


def _trusted_mapping(service: str) -> tuple[dict | None, str | None]:
    try:
        config = json.loads(WORKLOAD_MAPPING_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "trusted_workload_mapping_missing"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "trusted_workload_mapping_invalid"
    services = config.get("services") if isinstance(config, dict) else None
    targets = services.get(service) if isinstance(services, dict) else None
    if not isinstance(targets, list) or not targets:
        return None, "trusted_workload_mapping_missing"
    if len(targets) != 1:
        return None, "trusted_workload_mapping_ambiguous"
    target = targets[0]
    if (
        not isinstance(target, dict)
        or target.get("namespace") != LAB_NAMESPACE
        or not isinstance(target.get("deployment"), str)
        or re.fullmatch(r"[a-z0-9]([-a-z0-9.]*[a-z0-9])?", target["deployment"])
        is None
    ):
        return None, "trusted_workload_mapping_invalid"
    return target, None


def _scope_kubernetes_observation(
    observation: Any, target: dict
) -> tuple[dict | None, str | None]:
    if not isinstance(observation, dict):
        return None, "kubernetes_observation_invalid"
    if observation.get("namespace") != target["namespace"]:
        return None, "kubernetes_namespace_mismatch"
    deployments = observation.get("deployments")
    if not isinstance(deployments, list):
        return None, "kubernetes_deployment_evidence_missing"
    matches = [
        item for item in deployments
        if isinstance(item, dict) and item.get("name") == target["deployment"]
    ]
    if len(matches) != 1:
        return None, (
            "mapped_workload_ambiguous"
            if len(matches) > 1 else "mapped_workload_not_observed"
        )

    raw_incidents = observation.get("incidents")
    if not isinstance(raw_incidents, list):
        return None, "kubernetes_incident_evidence_invalid"
    scoped_incidents = []
    pod_evidence_omitted = False
    for item in raw_incidents:
        if not isinstance(item, dict):
            continue
        resource = item.get("resource")
        kind = item.get("resource_kind")
        if (
            item.get("namespace") == target["namespace"]
            and kind == "Deployment"
            and resource == target["deployment"]
        ):
            scoped_incidents.append(item)
        elif kind == "Pod":
            pod_evidence_omitted = True
    return {
        "namespace": target["namespace"],
        "observed_at": observation.get("observed_at"),
        "deployments": matches,
        "incidents": scoped_incidents,
        "incident_count": len(scoped_incidents),
    }, "mapped_pod_ownership_unverified" if pod_evidence_omitted else None


async def _diagnose(incident: dict) -> tuple[str, dict]:
    telemetry, source = _telemetry_from_incident(incident)
    classification = classify_incident(None, None, telemetry)
    limitations = []
    mapped_target, mapping_error = _trusted_mapping(incident["service"])
    kubernetes = None
    target_reference = None
    if mapping_error:
        limitations.append(mapping_error)
    else:
        try:
            observation = await asyncio.to_thread(observe_namespace)
        except Exception as exc:
            logger.warning(
                "Read-only Kubernetes diagnosis failed service=%s error=%s",
                incident["service"],
                type(exc).__name__,
            )
            limitations.append("kubernetes_observation_unavailable")
        else:
            kubernetes, observation_error = _scope_kubernetes_observation(
                observation, mapped_target
            )
            if observation_error:
                limitations.append(observation_error)
            if kubernetes is not None:
                target_reference = {
                    "namespace": mapped_target["namespace"],
                    "deployment": mapped_target["deployment"],
                }

    evidence_text = (
        f"{incident['incident_type']} {incident['service']} "
        f"{source['metric']['name']} {source['metric']['value']} "
        f"{source['metric']['unit']} baseline "
        f"{source['metric']['baseline_median']} deviation "
        f"{source['metric']['deviation_score']} threshold "
        f"{source['metric']['threshold']} source_timestamp "
        f"{source['source_timestamp']}"
    )
    runbooks = await asyncio.to_thread(
        retrieve_runbooks, evidence_text, 3
    )
    runbook_matches = [
        {
            "source": item.get("source"),
            "score": item.get("score"),
            "matched_terms": item.get("matched_terms", []),
        }
        for item in runbooks
        if isinstance(item, dict)
    ]
    if runbooks and not runbook_matches:
        limitations.append("runbook_results_invalid")
    diagnosis = {
        "service": incident["service"],
        "status": "unknown",
        "evidence_status": "available",
        "observed_at": source["source_timestamp"],
        "missing_evidence": [],
        "evidence": {
            source["metric"]["name"]: source["metric"]["value"],
        },
        "findings": [{
            "type": "persisted_prometheus_anomaly",
            "severity": incident["severity"].lower(),
            "evidence": evidence_text,
            "recommendation": "Investigate the recorded telemetry anomaly.",
        }],
    }
    rca_update = await analyze_root_cause({
        "service": incident["service"],
        "diagnosis": diagnosis,
        "telemetry_anomaly": telemetry,
        "kubernetes": kubernetes,
        "runbook_guidance": runbooks,
    })
    rca_status = rca_update.get("rca_status")
    rca_error = rca_update.get("rca_error") or ""
    if rca_status != "completed":
        limitations.append(f"root_cause_analysis_{rca_status or 'unavailable'}")

    policy = enforce_policy({
        "diagnosis": diagnosis,
        "telemetry_anomaly": telemetry,
        "kubernetes": kubernetes,
        "kubernetes_status": (
            "completed" if kubernetes is not None else "unavailable"
        ),
    })["policy"]
    rca = rca_update.get("rca")
    recommendation = (
        rca.get("recommended_action")
        if isinstance(rca, dict) else None
    )
    result = {
        "status": (
            "failed" if rca_status != "completed"
            else "limited" if limitations else "completed"
        ),
        "incident_row_id": incident["id"],
        "incident_id": incident["incident_id"],
        "incident_type": incident["incident_type"],
        "service": incident["service"],
        "source_evidence": source,
        "evidence_references": [{
            "source": "prometheus",
            "service": source["service"],
            "metric": source["metric"]["name"],
            "observed_at": source["observed_at"],
            "source_timestamp": source["source_timestamp"],
        }],
        "kubernetes_evidence": kubernetes,
        "classification": classification,
        "confidence": classification["confidence"],
        "diagnosis": diagnosis,
        "root_cause_analysis": rca if rca_status == "completed" else None,
        "root_cause_analysis_status": rca_status or "unavailable",
        "root_cause_analysis_error": rca_error,
        "runbook_match": runbook_matches[0] if runbook_matches else None,
        "runbook_matches": runbook_matches,
        "trusted_target": target_reference,
        "limitations": limitations,
        "policy_recommendation": policy,
        "proposed_action": {
            "kind": "advisory",
            "recommendation": recommendation or policy["action"],
            "policy_action": policy["action"],
            "execution_status": "not_submitted",
        },
        "approval_status": "not_requested",
        "verification_status": "not_run",
    }
    return result["status"], result


async def process_incident(incident: dict) -> dict:
    """Diagnose one persisted incident once per materially changed snapshot."""
    digest = _material_digest(incident)
    claim = incident_repository.claim_incident_diagnosis(
        incident["id"], digest,
        processing_timeout_seconds=PROCESSING_TIMEOUT_SECONDS,
    )
    if not claim["claimed"]:
        stored_result = incident.get("diagnosis")
        stored_status = incident.get("diagnosis_status")
        if (
            claim.get("reason") in {"unchanged", "retry_backoff"}
            and isinstance(stored_result, dict)
            and stored_status in {"completed", "limited", "failed"}
        ):
            limitations = stored_result.get("limitations", [])
            limitations = limitations if isinstance(limitations, list) else []
            if (
                "audit_persistence_failed" in limitations
                or stored_result.get("audit_status") == "failed"
            ):
                audit_status = "failed"
            elif stored_result.get("audit_event_id") is not None:
                audit_status = "success"
            else:
                audit_status = "unknown"
            reason = (
                "audit_persistence_failed"
                if audit_status == "failed"
                else stored_result.get("error_code")
                or stored_result.get("root_cause_analysis_error")
                or (limitations[0] if limitations else None)
            )
            return {
                "incident_id": incident.get("incident_id"),
                "status": stored_status,
                "reason": reason,
                "retry_state": (
                    claim["reason"]
                    if claim["reason"] == "retry_backoff" else None
                ),
                "limitations": limitations,
                "audit_status": audit_status,
            }
        return {
            "incident_id": incident.get("incident_id"),
            "status": "skipped",
            "reason": claim["reason"],
            "audit_status": (
                "pending"
                if claim.get("reason") == "in_progress"
                else "not_applicable"
            ),
        }
    attempts = claim["attempts"]
    try:
        status, result = await _diagnose(incident)
    except asyncio.CancelledError:
        result = {
            "status": "failed",
            "incident_row_id": incident["id"],
            "incident_id": incident.get("incident_id"),
            "limitations": ["diagnosis_cancelled"],
        }
        incident_repository.finish_incident_diagnosis(
            incident["id"], digest, "failed", result, attempts=attempts
        )
        raise
    except PersistedEvidenceError as exc:
        status = "limited"
        result = {
            "status": status,
            "incident_row_id": incident["id"],
            "incident_id": incident.get("incident_id"),
            "service": incident.get("service"),
            "limitations": [str(exc)],
            "confidence": 0.0,
            "proposed_action": None,
            "approval_status": "not_requested",
            "verification_status": "not_run",
        }
    except Exception as exc:
        logger.warning(
            "Automatic incident diagnosis failed incident=%s error=%s",
            incident.get("incident_id"),
            type(exc).__name__,
        )
        status = "failed"
        source = incident.get("source_evidence")
        metric = source.get("metric") if isinstance(source, dict) else None
        evidence_references = []
        if isinstance(source, dict) and isinstance(metric, dict):
            evidence_references.append({
                "source": source.get("source"),
                "service": source.get("service"),
                "metric": metric.get("name"),
                "observed_at": source.get("observed_at"),
                "source_timestamp": source.get("source_timestamp"),
            })
        result = {
            "status": status,
            "incident_row_id": incident["id"],
            "incident_id": incident.get("incident_id"),
            "service": incident.get("service"),
            "source_evidence": source,
            "evidence_references": evidence_references,
            "classification": None,
            "confidence": 0.0,
            "runbook_match": None,
            "limitations": ["diagnosis_stage_failed"],
            "error_code": type(exc).__name__,
            "proposed_action": None,
            "approval_status": "not_requested",
            "verification_status": "not_run",
        }

    try:
        event = audit_repository.record_event(
            incident_ref=incident["incident_id"],
            event_type=f"automatic_diagnosis_{status}",
            actor="kybernaut-auto-diagnosis",
            summary=f"Read-only incident diagnosis {status}",
            resource=incident.get("service"),
            outcome=status,
            details={
                "incident_row_id": incident["id"],
                "evidence_digest": digest,
                "diagnosis_status": status,
                "limitations": result.get("limitations", []),
            },
        )
        result["audit_event_id"] = event["event_id"]
        result["audit_status"] = "success"
    except Exception as exc:
        logger.warning(
            "Automatic diagnosis audit write failed incident=%s error=%s",
            incident.get("incident_id"),
            type(exc).__name__,
        )
        status = "failed"
        result["status"] = status
        result["limitations"] = [
            *result.get("limitations", []),
            "audit_persistence_failed",
        ]
        result["error_code"] = "AuditPersistenceError"
        result["audit_status"] = "failed"

    incident_repository.finish_incident_diagnosis(
        incident["id"], digest, status, result, attempts=attempts
    )
    return {
        "incident_id": incident.get("incident_id"),
        "status": status,
        "limitations": result.get("limitations", []),
        "audit_status": result.get("audit_status", "unknown"),
        "error_code": result.get("error_code"),
        "root_cause_analysis_error": result.get(
            "root_cause_analysis_error"
        ),
    }


async def process_active_incidents(service: str) -> list[dict]:
    """Process open monitor incidents without invoking response or remediation."""
    if service not in SUPPORTED_SERVICES:
        raise ValueError("Unsupported application service")
    incidents = incident_repository.list_incidents()
    relevant = [
        item for item in incidents
        if item.get("service") == service
        and item.get("status") in {"OPEN", "REMEDIATING"}
        and isinstance(item.get("fingerprint"), str)
        and item["fingerprint"].startswith(f"prometheus:{service}:")
    ]
    results = []
    for incident in relevant:
        results.append(await process_incident(incident))
    return results
