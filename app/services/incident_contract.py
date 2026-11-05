"""Adapters from workflow evidence into the shared incident contract."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from app.models.incident_contract import (
    EvidenceSourceState,
    IncidentClassification,
    IncidentContract,
    IncidentEvidence,
    IncidentTarget,
)
from app.services.incident_classifier import classify_incident
from app.services import audit_repository as audit

MAX_EVIDENCE_ITEMS = 50
MAX_EVIDENCE_DETAILS = 10
MAX_EVIDENCE_TEXT = 1000
logger = logging.getLogger(__name__)


def normalize_source_state(
    source: str,
    status: str,
    observed_at: Any = None,
    reason: str | None = None,
    *,
    invalid_timestamp_reason: str = "invalid_observation_timestamp",
) -> EvidenceSourceState:
    if status not in {"available", "unavailable", "stale", "invalid"}:
        status = "invalid"
    try:
        return EvidenceSourceState(
            source=source,
            status=status,
            observed_at=observed_at,
            reason=reason,
        )
    except ValidationError:
        return EvidenceSourceState(
            source=source,
            status="invalid",
            reason=invalid_timestamp_reason,
        )


def _diagnosis_status(diagnosis: dict) -> str:
    status = diagnosis.get("evidence_status")
    missing = diagnosis.get("missing_evidence")
    if status is not None and status not in {
        "available", "unavailable", "stale", "invalid",
    }:
        return "invalid"
    if status in {"stale", "invalid"}:
        return status
    if (
        ("findings" in diagnosis and not isinstance(diagnosis["findings"], list))
        or (
            "evidence" in diagnosis
            and not isinstance(diagnosis["evidence"], dict)
        )
        or (
            isinstance(missing, list)
            and any(not isinstance(item, str) for item in missing)
        )
    ):
        return "invalid"
    if missing is not None and (
        not isinstance(missing, list) or bool(missing)
    ):
        return "unavailable" if isinstance(missing, list) else "invalid"
    if status is not None:
        return status
    if diagnosis.get("status") in {"up", "down"}:
        # Legacy diagnosis implementations do not expose metric completeness.
        return "unavailable"
    return "unavailable"


def _prometheus_details(diagnosis: dict) -> tuple[list[str], bool]:
    values = diagnosis.get("evidence")
    if isinstance(values, dict):
        details = [
            f"{key}={value}"
            for key, value in values.items()
            if isinstance(key, str) and value is not None
            and isinstance(value, (str, int, float))
        ]
        return _bounded_details(details)
    findings = diagnosis.get("findings")
    if not isinstance(findings, list):
        return [], findings is not None
    details = [
        item["evidence"]
        for item in findings
        if isinstance(item, dict) and isinstance(item.get("evidence"), str)
    ]
    return _bounded_details(details)


def _bounded_findings(values: Any) -> tuple[list[dict], bool]:
    if not isinstance(values, list):
        return [], values is not None
    findings: list[dict] = []
    truncated = len(values) > MAX_EVIDENCE_DETAILS
    for item in values[:MAX_EVIDENCE_DETAILS]:
        if not isinstance(item, dict):
            truncated = True
            continue
        normalized: dict[str, Any] = {}
        for key, value in item.items():
            if not isinstance(key, str):
                truncated = True
            elif isinstance(value, str):
                normalized[key] = value[:MAX_EVIDENCE_TEXT]
                truncated = truncated or len(value) > MAX_EVIDENCE_TEXT
            elif value is None or isinstance(value, (bool, int, float)):
                normalized[key] = value
            else:
                truncated = True
        findings.append(normalized)
    return findings, truncated


def _bounded_details(values: Any) -> tuple[list[str], bool]:
    if not isinstance(values, list):
        return [], values is not None
    strings = [value for value in values if isinstance(value, str)]
    truncated = (
        len(strings) > MAX_EVIDENCE_DETAILS
        or any(len(value) > MAX_EVIDENCE_TEXT for value in strings)
        or len(strings) != len(values)
    )
    return [
        value[:MAX_EVIDENCE_TEXT]
        for value in strings[:MAX_EVIDENCE_DETAILS]
    ], truncated


def _target_for_incident(
    incident: dict,
    namespace: Any,
    classification: IncidentClassification | None,
) -> IncidentTarget | None:
    if classification is None or classification.target is None:
        return None
    candidate = classification.target
    if (
        incident.get("resource") != candidate.resource
        or incident.get("resource_kind") != candidate.resource_kind
        or namespace != candidate.namespace
        or incident.get("resource_uid") != candidate.resource_uid
    ):
        return None
    return candidate


def _kubernetes_evidence(
    kubernetes: Any,
    kubernetes_status: str,
    classification: IncidentClassification | None,
) -> tuple[list[IncidentEvidence], str, Any, bool]:
    if kubernetes_status == "unavailable" or kubernetes is None:
        return [], "unavailable", None, False
    if not isinstance(kubernetes, dict):
        return [], "invalid", None, False
    namespace = kubernetes.get("namespace")
    incidents = kubernetes.get("incidents")
    if (
        kubernetes_status != "completed"
        or not isinstance(namespace, str)
        or not namespace.strip()
        or not isinstance(incidents, list)
        or any(not isinstance(item, dict) for item in incidents)
    ):
        return [], "invalid", None, False

    k8s_source = normalize_source_state(
        "kubernetes",
        "available",
        kubernetes.get("observed_at"),
    )
    observed_at = k8s_source.observed_at
    evidence: list[IncidentEvidence] = []
    truncated = len(incidents) > MAX_EVIDENCE_ITEMS
    indexed_incidents = list(enumerate(incidents))
    if classification is not None and classification.target is not None:
        target = classification.target
        indexed_incidents.sort(
            key=lambda pair: not (
                pair[1].get("resource") == target.resource
                and pair[1].get("resource_kind") == target.resource_kind
                and pair[1].get("resource_uid") == target.resource_uid
            )
        )
    for index, incident in indexed_incidents[:MAX_EVIDENCE_ITEMS]:
        details, details_truncated = _bounded_details(incident.get("evidence"))
        truncated = truncated or details_truncated
        resource = incident.get("resource")
        kind = incident.get("resource_kind")
        valid_identity = (
            isinstance(resource, str)
            and bool(resource.strip())
            and kind in {"Pod", "Deployment"}
            and bool(details)
        )
        source_time = incident.get("source_timestamp")
        status = "available" if valid_identity else "invalid"
        target = (
            _target_for_incident(incident, namespace, classification)
            if valid_identity else None
        )
        evidence_id = incident.get("resource_uid")
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            evidence_id = f"kubernetes-{index}"
        try:
            evidence.append(IncidentEvidence(
                evidence_id=evidence_id,
                source="kubernetes",
                status=(
                    status if source_time is None else
                    normalize_source_state(
                        "kubernetes", status, source_time
                    ).status
                ),
                observed_at=source_time,
                target=target,
                details=details,
            ))
        except ValidationError:
            evidence.append(IncidentEvidence(
                evidence_id=evidence_id,
                source="kubernetes",
                status="invalid",
                target=None,
                details=details + ["invalid_source_timestamp_or_identity"],
            ))

    return evidence, k8s_source.status, observed_at, truncated


def build_incident_contract(
    state: dict[str, Any],
    *,
    incident_id: str | None = None,
) -> IncidentContract:
    """Validate a workflow snapshot without changing its policy semantics."""
    existing = state.get("contract")
    if isinstance(existing, dict):
        data = IncidentContract.model_validate(existing).model_dump(mode="json")
        if "policy_decision" in state:
            data["policy_decision"] = state.get("policy_decision")
        elif "policy" in state:
            data["policy_decision"] = state.get("policy")
        if state.get("approval_id"):
            data["approval_id"] = state["approval_id"]
        if state.get("approval_decision") in {"approved", "rejected"}:
            data["approval_status"] = state["approval_decision"]
        elif state.get("approval_id"):
            data["approval_status"] = "pending"
        if "execution" in state:
            data["execution"] = state.get("execution")
        verification = state.get("verification")
        if isinstance(verification, dict):
            execution = state.get("execution")
            if isinstance(execution, dict) and execution.get("executed") is False:
                data["verification_status"] = "not_run"
            elif verification.get("recovered") is True:
                data["verification_status"] = "recovered"
            elif verification.get("recovered") is False:
                data["verification_status"] = "not_recovered"
            else:
                data["verification_status"] = "unavailable"
        return _validate_contract_with_audit_refs(data, state)

    service = state.get("service")
    if not isinstance(service, str) or not service.strip():
        raise ValueError("Workflow state requires a non-empty service")

    correlation_id = incident_id or state.get("incident_id") or (
        f"INC-{uuid4().hex[:12]}"
    )
    diagnosis = state.get("diagnosis")
    diagnosis = diagnosis if isinstance(diagnosis, dict) else {}
    kubernetes = state.get("kubernetes")
    telemetry = state.get("telemetry_anomaly")
    classification_data = state.get("classification")
    if classification_data is None:
        classification_data = classify_incident(
            diagnosis, kubernetes, telemetry
        )
    classification = IncidentClassification.model_validate(classification_data)

    diagnosis_status = _diagnosis_status(diagnosis)
    prometheus_source = normalize_source_state(
        "prometheus",
        diagnosis_status,
        diagnosis.get("observed_at"),
        "diagnosis_incomplete"
        if diagnosis_status != "available" else None,
    )
    diagnosis_status = prometheus_source.status
    prometheus_details, prometheus_truncated = _prometheus_details(diagnosis)
    diagnosis_findings, findings_truncated = _bounded_findings(
        diagnosis.get("findings")
    )
    missing = diagnosis.get("missing_evidence")
    if isinstance(missing, list):
        missing_evidence = [item for item in missing if isinstance(item, str)]
        if len(missing_evidence) > MAX_EVIDENCE_DETAILS:
            missing_evidence = missing_evidence[:MAX_EVIDENCE_DETAILS]
            findings_truncated = True
    elif missing is None:
        missing_evidence = []
    else:
        diagnosis_status = "invalid"
        missing_evidence = ["invalid_missing_evidence"]

    k8s_evidence, k8s_status, k8s_observed_at, k8s_truncated = (
        _kubernetes_evidence(
            kubernetes,
            state.get("kubernetes_status", "unavailable"),
            classification,
        )
    )
    telemetry_status = (
        "available" if isinstance(telemetry, dict)
        and telemetry.get("status") == "evaluated"
        else "unavailable"
    )
    telemetry_candidates = (
        telemetry.get("actionable_candidates")
        if isinstance(telemetry, dict) else None
    )
    if telemetry_status == "available" and (
        not isinstance(telemetry_candidates, list)
        or any(not isinstance(item, str) for item in telemetry_candidates)
    ):
        telemetry_status = "invalid"
    telemetry_source = normalize_source_state(
        "telemetry",
        telemetry_status,
        telemetry.get("observed_at")
        if isinstance(telemetry, dict) else None,
        "evaluation_unavailable"
        if telemetry_status != "available" else None,
    )
    telemetry_status = telemetry_source.status
    telemetry_details, telemetry_truncated = _bounded_details(
        telemetry_candidates
    )
    evidence = [
        IncidentEvidence(
            evidence_id="prometheus-diagnosis",
            source="prometheus",
            status=diagnosis_status,
            observed_at=prometheus_source.observed_at,
            details=prometheus_details,
        ),
        *k8s_evidence,
        IncidentEvidence(
            evidence_id="telemetry-anomaly",
            source="telemetry",
            status=telemetry_status,
            observed_at=telemetry_source.observed_at,
            details=telemetry_details,
        ),
    ]
    policy = state.get("policy")
    if "policy_decision" in state:
        policy = state.get("policy_decision")
    if policy is not None and not isinstance(policy, dict):
        policy = None

    data = {
        "incident_id": correlation_id,
        "service": service,
        "evidence_sources": [
            prometheus_source,
            normalize_source_state(
                "kubernetes",
                k8s_status,
                k8s_observed_at,
                "observation_unavailable"
                if k8s_status == "unavailable" else None,
            ),
            telemetry_source,
        ],
        "evidence": evidence,
        "evidence_truncated": (
            prometheus_truncated or findings_truncated
            or k8s_truncated or telemetry_truncated
        ),
        "classification": classification,
        "diagnosis_findings": diagnosis_findings,
        "missing_evidence": missing_evidence,
        "policy_decision": policy,
    }
    if state.get("proposal") is not None:
        data["proposed_action"] = state["proposal"]
    if state.get("approval_id"):
        data["approval_id"] = state["approval_id"]
    if state.get("approval_decision") in {"approved", "rejected"}:
        data["approval_status"] = state["approval_decision"]
    elif state.get("approval_id"):
        data["approval_status"] = "pending"
    if "execution" in state:
        data["execution"] = state.get("execution")
    verification = state.get("verification")
    if isinstance(verification, dict):
        execution = state.get("execution")
        if isinstance(execution, dict) and execution.get("executed") is False:
            data["verification_status"] = "not_run"
        elif verification.get("recovered") is True:
            data["verification_status"] = "recovered"
        elif verification.get("recovered") is False:
            data["verification_status"] = "not_recovered"
        else:
            data["verification_status"] = "unavailable"
    return _validate_contract_with_audit_refs(data, state)


def _validate_contract_with_audit_refs(
    data: dict[str, Any], state: dict[str, Any]
) -> IncidentContract:
    incident_id = data.get("incident_id")
    if isinstance(incident_id, str) and (
        "policy_decision" in state or "approval_id" in state
    ):
        try:
            data["audit_refs"] = [
                event["event_id"]
                for event in audit.list_events(incident_id)
                if isinstance(event.get("event_id"), str)
            ]
        except Exception:  # noqa: BLE001 - references are supplemental
            logger.exception("Could not collect workflow audit references")
            data["audit_refs"] = []
    return IncidentContract.model_validate(data)
