"""Integrated LangGraph incident-response workflow (guide step 13).

collect_evidence -> classify -> diagnose_rca -> plan -> policy
    -> [no action | request_approval | remediate] -> verify
    -> resolved | collect_evidence (bounded retry) | escalated

Human approval is durable: the graph persists an approval request and
ends (``awaiting_approval``). ``resume_after_decision`` later re-enters
the graph at ``recheck_policy`` -> ``remediate``. Policy is re-evaluated
at execution time against fresh evidence.

This module is additive: the advisory ``incident_workflow`` is unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import operator
import os
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.models.incident_contract import (
    IncidentContract,
    IncidentEvidence,
    IncidentTarget,
)
from app.services import audit_repository as audit
from app.services import action_claim_repository as claims
from app.services.incident_classifier import classify_incident
from app.services.incident_contract import normalize_source_state
from app.services.incident_workflow import (
    ALLOWED_SERVICES,
    analyze_root_cause,
    detect_and_diagnose,
)
from app.services.k8s_observer import (
    LAB_NAMESPACE,
    POD_EVENT_LOOKBACK,
    observe_namespace,
)
from app.services.k8s_remediation import execute_action
from app.services.policy_engine import (
    PolicyConfig,
    ProposedAction,
    evaluate_policy,
)
from app.services.runbook_retriever import resolve_runbook
from app.services.verification import verify_recovery

from app.services import remediation_catalog

logger = logging.getLogger(__name__)

KUBERNETES_SERVICE = "kubernetes-lab"
SUPPORTED_SERVICES = set(ALLOWED_SERVICES) | {KUBERNETES_SERVICE}
MAX_AUDIT_INCIDENTS = 50
MAX_AUDIT_EVIDENCE_PER_INCIDENT = 10
MAX_AUDIT_TEXT_LENGTH = 1000


class WorkflowNotStarted(ValueError):
    """A workflow failed during preflight, before graph execution began."""


class WorkflowExecutionAmbiguous(Exception):
    """Graph execution began but its durable outcome is uncertain."""


def max_attempts_default() -> int:
    try:
        return max(1, int(os.getenv("KYBERNAUT_MAX_REMEDIATION_ATTEMPTS", "2")))
    except ValueError:
        return 2


class ResponseState(TypedDict, total=False):
    incident_id: str
    workflow_claim_id: str
    action_claim_id: str | None
    duplicate_action_claim_id: str | None
    service: str
    requested_by: str
    monitor_handoff: bool
    occurrence_bound: bool
    force_approval: bool
    occurrence_id: int
    expected_occurrence_service: str
    expected_incident_type: str
    expected_fingerprint: str
    expected_occurrence_timestamp: str
    handoff_valid: bool
    handoff_error: str
    diagnosis: dict
    kubernetes: dict | None
    kubernetes_status: str
    telemetry_anomaly: dict
    rca: dict | None
    rca_status: str
    rca_error: str
    classification: dict
    runbook_resolution: dict
    contract: dict | None
    proposal: dict | None
    plan_note: str
    preferred_action: str | None
    remediation_options: list
    policy_decision: dict | None
    approval_id: str | None
    approval_decision: str | None
    approved_by: str | None
    execution: dict | None
    verification: dict | None
    attempts: int
    max_attempts: int
    status: str
    timeline: Annotated[list, operator.add]


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _step(node: str, detail: str) -> list[dict]:
    return [{"ts": _ts(), "node": node, "detail": detail}]


def _audit(state: ResponseState, event_type: str, summary: str, **kw) -> None:
    """Best-effort audit write for non-critical events."""
    try:
        details = kw.pop("details", None)
        if state.get("occurrence_id") is not None:
            details = {
                **(details if isinstance(details, dict) else {}),
                "incident_row_id": state["occurrence_id"],
            }
        audit.record_event(
            incident_ref=state["incident_id"], event_type=event_type,
            actor=kw.pop("actor", "kybernaut-workflow"),
            action_claim_id=kw.pop(
                "action_claim_id", state.get("action_claim_id")
            ),
            workflow_claim_id=kw.pop(
                "workflow_claim_id", state.get("workflow_claim_id")
            ),
            summary=summary, details=details, **kw,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Audit write failed for event %s", event_type)


def _audit_observation(kubernetes: Any) -> dict | None:
    if not isinstance(kubernetes, dict):
        return None

    incidents = kubernetes.get("incidents")
    deployments = kubernetes.get("deployments")
    incidents = incidents if isinstance(incidents, list) else []
    deployments = deployments if isinstance(deployments, list) else []
    bounded_incidents = []
    truncated = (
        len(incidents) > MAX_AUDIT_INCIDENTS
        or len(deployments) > 100
    )
    for incident in incidents[:MAX_AUDIT_INCIDENTS]:
        if not isinstance(incident, dict):
            truncated = True
            continue
        raw_evidence = incident.get("evidence")
        evidence = raw_evidence if isinstance(raw_evidence, list) else []
        bounded_evidence = [
            item[:MAX_AUDIT_TEXT_LENGTH]
            for item in evidence[:MAX_AUDIT_EVIDENCE_PER_INCIDENT]
            if isinstance(item, str)
        ]
        item_truncated = (
            len(evidence) > len(bounded_evidence)
            or any(
                isinstance(item, str) and len(item) > MAX_AUDIT_TEXT_LENGTH
                for item in evidence[:MAX_AUDIT_EVIDENCE_PER_INCIDENT]
            )
        )
        truncated = truncated or item_truncated
        bounded_incidents.append({
            key: incident.get(key)
            for key in (
                "source", "namespace", "type", "severity", "resource",
                "resource_kind", "resource_uid", "source_timestamp",
                "recommendation",
            )
        } | {
            "evidence": bounded_evidence,
            "truncated": item_truncated,
        })
    bounded_deployments = [
        {
            key: deployment.get(key)
            for key in (
                "name", "desired_replicas", "available_replicas",
                "ready_replicas",
            )
        }
        for deployment in deployments[:100]
        if isinstance(deployment, dict)
    ]
    return {
        "observed_at": kubernetes.get("observed_at"),
        "namespace": kubernetes.get("namespace"),
        "incident_count": kubernetes.get("incident_count", len(incidents)),
        "incidents": bounded_incidents,
        "deployments": bounded_deployments,
        "truncated": truncated,
    }


def _deployment_state(kubernetes: Any, deployment: str) -> dict | None:
    if not isinstance(kubernetes, dict):
        return None
    for item in kubernetes.get("deployments") or []:
        if isinstance(item, dict) and item.get("name") == deployment:
            return item
    return None


def _contract_evidence(
    kubernetes: Any,
    kubernetes_status: str,
    telemetry: Any,
    classification: Any,
) -> tuple[list[IncidentEvidence], bool]:
    evidence: list[IncidentEvidence] = []
    truncated = False
    if isinstance(kubernetes, dict):
        incidents = kubernetes.get("incidents")
        if isinstance(incidents, list):
            namespace = kubernetes.get("namespace")
            class_target = (
                classification.get("target")
                if isinstance(classification, dict) else None
            )
            if len(incidents) > MAX_AUDIT_INCIDENTS:
                truncated = True
            indexed_incidents = list(enumerate(incidents))
            if isinstance(class_target, dict):
                indexed_incidents.sort(
                    key=lambda item: not (
                        isinstance(item[1], dict)
                        and item[1].get("resource") == class_target.get("resource")
                        and item[1].get("resource_kind")
                        == class_target.get("resource_kind")
                        and item[1].get("resource_uid")
                        == class_target.get("resource_uid")
                    )
                )
            for index, incident in indexed_incidents[:MAX_AUDIT_INCIDENTS]:
                if not isinstance(incident, dict):
                    truncated = True
                    continue
                details = incident.get("evidence")
                details = (
                    [item for item in details if isinstance(item, str)]
                    if isinstance(details, list) else []
                )
                if (
                    len(details) > MAX_AUDIT_EVIDENCE_PER_INCIDENT
                    or any(
                        len(item) > MAX_AUDIT_TEXT_LENGTH
                        for item in details
                    )
                ):
                    truncated = True
                details = [
                    item[:MAX_AUDIT_TEXT_LENGTH]
                    for item in details[:MAX_AUDIT_EVIDENCE_PER_INCIDENT]
                ]
                evidence_id = incident.get("resource_uid")
                if not isinstance(evidence_id, str) or not evidence_id:
                    evidence_id = f"kubernetes-{index}"
                observed_at = incident.get("source_timestamp")
                resource = incident.get("resource")
                resource_kind = incident.get("resource_kind")
                valid_identity = (
                    isinstance(namespace, str)
                    and bool(namespace.strip())
                    and isinstance(resource, str)
                    and bool(resource.strip())
                    and resource_kind in {"Pod", "Deployment"}
                    and bool(details)
                )
                status = (
                    "available"
                    if kubernetes_status == "completed" and valid_identity
                    else "unavailable"
                    if kubernetes_status != "completed"
                    else "invalid"
                )
                target = None
                if (
                    isinstance(class_target, dict)
                    and isinstance(namespace, str)
                    and namespace.strip()
                    and namespace == class_target.get("namespace")
                    and resource == class_target.get("resource")
                    and resource_kind == class_target.get("resource_kind")
                    and incident.get("resource_uid")
                    == class_target.get("resource_uid")
                    and status == "available"
                ):
                    try:
                        target = IncidentTarget.model_validate(class_target)
                    except ValidationError:
                        status = "invalid"
                if observed_at is not None:
                    try:
                        # Normalize/validate here so malformed source times
                        # remain recorded as invalid evidence, not exceptions
                        # that hide the workflow result.
                        parsed = IncidentEvidence.model_validate({
                            "evidence_id": evidence_id,
                            "source": "kubernetes",
                            "status": status,
                            "observed_at": observed_at,
                            "target": target,
                            "details": details,
                        })
                    except ValidationError:
                        parsed = IncidentEvidence(
                            evidence_id=evidence_id,
                            source="kubernetes",
                            status="invalid",
                            target=None,
                            details=details + ["invalid_source_timestamp"],
                        )
                    evidence.append(parsed)
                else:
                    evidence.append(IncidentEvidence(
                        evidence_id=evidence_id,
                        source="kubernetes",
                        status=status,
                        target=target,
                        details=details,
                    ))
    if isinstance(telemetry, dict) and telemetry.get("status") == "evaluated":
        candidates = telemetry.get("actionable_candidates")
        if isinstance(candidates, list) and len(candidates) > (
            MAX_AUDIT_EVIDENCE_PER_INCIDENT
        ):
            truncated = True
        details = [
            item[:MAX_AUDIT_TEXT_LENGTH]
            for item in candidates[:MAX_AUDIT_EVIDENCE_PER_INCIDENT]
            if isinstance(item, str)
        ] if isinstance(candidates, list) else []
        if isinstance(candidates, list) and any(
            isinstance(item, str) and len(item) > MAX_AUDIT_TEXT_LENGTH
            for item in candidates
        ):
            truncated = True
        try:
            telemetry_evidence = IncidentEvidence(
                evidence_id="telemetry-anomaly",
                source="telemetry",
                status="available",
                observed_at=telemetry.get("observed_at"),
                details=details,
            )
        except ValidationError:
            telemetry_evidence = IncidentEvidence(
                evidence_id="telemetry-anomaly",
                source="telemetry",
                status="invalid",
                details=details + ["invalid_telemetry_observation"],
            )
        evidence.append(telemetry_evidence)
    return evidence, truncated


def _build_contract(state: ResponseState) -> IncidentContract:
    base = state.get("contract")
    if isinstance(base, dict):
        data = IncidentContract.model_validate(base).model_dump()
    else:
        diagnosis = state.get("diagnosis")
        diagnosis = diagnosis if isinstance(diagnosis, dict) else {}
        kubernetes = state.get("kubernetes")
        telemetry = state.get("telemetry_anomaly")
        k8s_completed = (
            state.get("kubernetes_status") == "completed"
            and isinstance(kubernetes, dict)
        )
        incidents = kubernetes.get("incidents") if k8s_completed else None
        k8s_valid = (
            k8s_completed
            and isinstance(kubernetes.get("namespace"), str)
            and bool(kubernetes["namespace"].strip())
            and isinstance(incidents, list)
            and all(isinstance(item, dict) for item in incidents)
        )
        k8s_status = (
            "available" if k8s_valid
            else "unavailable" if state.get("kubernetes_status") == "unavailable"
            else "invalid"
        )
        diagnosis_status = diagnosis.get("evidence_status")
        prometheus_status = (
            diagnosis_status
            if diagnosis_status in {"available", "unavailable", "stale", "invalid"}
            else "unavailable"
        )
        missing_diagnosis_evidence = diagnosis.get("missing_evidence")
        if isinstance(missing_diagnosis_evidence, list) and missing_diagnosis_evidence:
            if prometheus_status == "available":
                prometheus_status = "unavailable"
        elif (
            missing_diagnosis_evidence is not None
            and not isinstance(missing_diagnosis_evidence, list)
        ):
            prometheus_status = "invalid"
        telemetry_status = (
            "available" if isinstance(telemetry, dict)
            and telemetry.get("status") == "evaluated"
            else "unavailable"
        )
        kubernetes_observed_at = (
            kubernetes.get("observed_at") if isinstance(kubernetes, dict) else None
        )
        evidence_sources = [
            normalize_source_state(
                "prometheus", prometheus_status,
                diagnosis.get("observed_at"),
                reason=(
                    "diagnosis_evidence_incomplete"
                    if prometheus_status != "available" else None
                ),
                invalid_timestamp_reason="invalid_observation_time",
            ),
            normalize_source_state(
                "kubernetes", k8s_status, kubernetes_observed_at,
                None if k8s_valid else "observation_invalid_or_unavailable",
                invalid_timestamp_reason="invalid_observation_time",
            ),
            normalize_source_state(
                "telemetry", telemetry_status,
                telemetry.get("observed_at")
                if isinstance(telemetry, dict) else None,
                None if telemetry_status == "available"
                else "anomaly_evaluation_unavailable",
                invalid_timestamp_reason="invalid_observation_time",
            ),
        ]
        evidence, evidence_truncated = _contract_evidence(
            kubernetes, state.get("kubernetes_status", "invalid"),
            telemetry, state.get("classification"),
        )
        classification = state.get("classification")
        data = {
            "incident_id": state["incident_id"],
            "service": state["service"],
            "evidence_sources": evidence_sources,
            "evidence": evidence,
            "evidence_truncated": evidence_truncated,
            "classification": classification,
            "diagnosis_findings": [
                item for item in diagnosis.get("findings", [])
                if isinstance(item, dict)
            ] if isinstance(diagnosis.get("findings"), list) else [],
            "missing_evidence": [
                item for item in diagnosis.get("missing_evidence", [])
                if isinstance(item, str)
            ] if isinstance(diagnosis.get("missing_evidence"), list) else [],
        }

    diagnosis = state.get("diagnosis")
    diagnosis = diagnosis if isinstance(diagnosis, dict) else {}
    if diagnosis:
        findings = diagnosis.get("findings")
        missing = diagnosis.get("missing_evidence")
        data["diagnosis_findings"] = (
            [item for item in findings if isinstance(item, dict)]
            if isinstance(findings, list) else []
        )
        data["missing_evidence"] = (
            [item for item in missing if isinstance(item, str)]
            if isinstance(missing, list) else []
        )
    if state.get("classification") is not None:
        data["classification"] = state["classification"]
    if "proposal" in state:
        data["proposed_action"] = state.get("proposal")
    if "policy_decision" in state:
        data["policy_decision"] = state.get("policy_decision")
    if state.get("approval_id"):
        data["approval_id"] = state["approval_id"]
    approval_decision = state.get("approval_decision")
    if approval_decision in {"approved", "rejected"}:
        data["approval_status"] = approval_decision
    elif state.get("approval_id"):
        data["approval_status"] = "pending"
    if "execution" in state:
        data["execution"] = state.get("execution")
    verification = state.get("verification")
    if isinstance(verification, dict):
        if (
            isinstance(state.get("execution"), dict)
            and not state["execution"].get("executed")
        ):
            data["verification_status"] = "not_run"
        elif verification.get("recovered") is True:
            data["verification_status"] = "recovered"
        elif verification.get("recovered") is False:
            data["verification_status"] = "not_recovered"
        else:
            data["verification_status"] = "unavailable"
    try:
        data["audit_refs"] = [
            event["event_id"]
            for event in audit.list_events(state["incident_id"])
            if isinstance(event.get("event_id"), str)
        ]
    except Exception:  # noqa: BLE001 - references are supplemental
        logger.exception("Could not collect audit references for contract")
        data["audit_refs"] = []
    return IncidentContract.model_validate(data)


# ---------------------------------------------------------------- nodes

async def collect_evidence(state: ResponseState) -> dict:
    service = state["service"]
    refreshed = state.get("attempts", 0) > 0
    if service == KUBERNETES_SERVICE:
        try:
            kubernetes = await asyncio.to_thread(observe_namespace)
            status = "completed"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Kubernetes observation unavailable (%s)",
                           type(exc).__name__)
            kubernetes, status = None, "unavailable"
        update = {
            "diagnosis": {"service": service, "status": "not_applicable",
                          "findings": []},
            "telemetry_anomaly": {"status": "not_applicable"},
            "kubernetes": kubernetes, "kubernetes_status": status,
        }
    else:
        update = await detect_and_diagnose(state)  # reuse existing logic

    _audit(state, "evidence_refreshed" if refreshed else "evidence_collected",
           f"Evidence collected (kubernetes={update['kubernetes_status']})",
           details={
               "kubernetes_status": update["kubernetes_status"],
               "kubernetes": _audit_observation(update.get("kubernetes")),
           })
    return {**update, "contract": None,
            "timeline": _step("collect_evidence",
                              "fresh evidence" if refreshed else "evidence")}


def classify(state: ResponseState) -> dict:
    kubernetes = state.get("kubernetes")
    if state.get("occurrence_bound"):
        kubernetes = _scope_kubernetes_to_occurrence(state, kubernetes)
    result = classify_incident(
        state.get("diagnosis"), kubernetes,
        state.get("telemetry_anomaly"),
    )
    _audit(state, "incident_classified",
           f"Classified as {result['incident_type']} "
           f"(confidence {result['confidence']:.2f})",
           details=result)
    update = {"classification": result,
            "timeline": _step("classify", result["incident_type"])}
    if state.get("occurrence_bound"):
        update["kubernetes"] = kubernetes
    if state.get("monitor_handoff") or state.get("occurrence_bound"):
        from app.services import incident_repository

        if state.get("occurrence_bound"):
            valid = _bound_occurrence_matches(
                state, kubernetes, result
            )
        else:
            occurrence = incident_repository.get_incident_occurrence(
                state.get("occurrence_id", -1)
            )
            valid = (
                occurrence is not None
                and occurrence["status"] in {"OPEN", "REMEDIATING"}
                and occurrence["service"] == state.get("service")
                and occurrence["incident_type"]
                == state.get("expected_incident_type")
                and occurrence["fingerprint"]
                == state.get("expected_fingerprint")
                and result["incident_type"]
                == state.get("expected_incident_type")
            )
        update["handoff_valid"] = valid
        if not valid:
            update["handoff_error"] = (
                "occurrence_or_fresh_evidence_mismatch"
                if state.get("monitor_handoff")
                else "persisted_occurrence_evidence_mismatch"
            )
            _audit(
                state,
                (
                    "incident_handoff_evidence_rejected"
                    if state.get("monitor_handoff")
                    else "incident_occurrence_evidence_rejected"
                ),
                "Fresh evidence no longer supports the selected occurrence",
                outcome="rejected",
                details={
                    "expected_incident_type": state.get(
                        "expected_incident_type"
                    ),
                    "observed_incident_type": result["incident_type"],
                    "expected_fingerprint": state.get("expected_fingerprint"),
                },
            )
    return update


def close_handoff(state: ResponseState) -> dict:
    reason = state.get("handoff_error", "occurrence_inactive")
    _audit(
        state,
        "incident_handoff_closed",
        "Detection handoff closed without proposing remediation",
        outcome="no_action",
        details={"reason": reason},
    )
    return {
        "status": "stale_evidence",
        "timeline": _step("close_handoff", reason),
    }


def route_after_classify(state: ResponseState) -> str:
    if (
        state.get("monitor_handoff") or state.get("occurrence_bound")
    ) and not state.get("handoff_valid"):
        return "close_handoff"
    return "diagnose_rca"


async def diagnose_rca(state: ResponseState) -> dict:
    update = await analyze_root_cause(state)  # RAG + LLM, failure tolerant
    event_type = {
        "completed": "rca_completed",
        "invalid": "rca_invalid",
        "unavailable": "rca_unavailable",
    }.get(update["rca_status"], "rca_unavailable")
    _audit(state, event_type,
           f"RCA status: {update['rca_status']}",
           details={
               "status": update["rca_status"],
               "error_code": update.get("rca_error", ""),
           })
    return {**update, "timeline": _step("diagnose_rca", update["rca_status"])}


def plan(state: ResponseState) -> dict:
    cls = state.get("classification") or {}
    target = cls.get("target") or {}
    itype = cls.get("incident_type", "unknown")
    namespace = target.get("namespace")
    deployment = target.get("deployment")
    resource = target.get("resource")

    confidence = float(cls.get("confidence", 0.0))

    severity = cls.get("severity", "medium")
    severity = severity if severity in {"low", "medium", "high", "critical"} \
        else "medium"

    note = ""
    evidence = cls.get("evidence")
    evidence = evidence if isinstance(evidence, list) else []
    evidence_text = " ".join([itype, *(i for i in evidence if isinstance(i, str))])
    deployment_state = (
        _deployment_state(state.get("kubernetes"), deployment)
        if deployment else None
    )
    preferred = state.get("preferred_action")
    option_target = {**target, "deployment": deployment, "resource": resource}

    checks: dict[str, dict] = {}

    def _check(raw: dict) -> dict:
        if raw["action"] not in checks:
            checks[raw["action"]] = resolve_runbook(
                evidence_text,
                incident_type=itype,
                evidence=evidence,
                planned_action=raw["action"],
                deployment_state=deployment_state,
                allow_scaled_down_restore=(
                    itype == "disk_full"
                    and deployment == "sim-disk-fill"
                    and raw.get("target_replicas") == 1
                ),
            )
        return checks[raw["action"]]

    candidates = (
        remediation_catalog.candidate_actions(itype, option_target, deployment_state)
        if namespace and deployment else []
    )
    # Without an explicit operator choice only the primary action may be
    # selected; alternatives run only when the operator picked them.
    eligible = (
        candidates[:1] if not preferred
        else [c for c in candidates if c["action"] == preferred]
    )
    raw: dict | None = None
    resolution: dict | None = None
    for candidate in eligible:
        result = _check(candidate)
        if result["status"] == "applicable":
            raw, resolution = candidate, result
            break
    selected = raw["action"] if raw else None
    options = remediation_catalog.build_options(
        itype, option_target, deployment_state,
        runbook_check=_check if namespace and deployment else None,
        selected_action=selected,
    )

    if not namespace or not deployment:
        note = "no_kubernetes_target"
    elif not candidates:
        note = (
            "deployment_state_unavailable" if itype == "cpu_spike"
            else "no_action_for_classification"
        )
    elif preferred and not eligible:
        note = "preferred_action_not_available"
    elif raw is None:
        first = eligible[0]
        resolution = _check(first)
        note = resolution["status"]
        _audit(
            state,
            "runbook_resolution_rejected",
            f"No action proposed: runbook resolution {note}",
            details={
                key: value for key, value in resolution.items()
                if key != "documented_risks"
            },
        )
        return {
            "proposal": None,
            "plan_note": note,
            "runbook_resolution": resolution,
            "remediation_options": options,
            "timeline": _step("plan", f"none ({note})"),
        }

    if raw is None:
        _audit(state, "plan_empty", f"No remediation proposed: {note}")
        return {"proposal": None, "plan_note": note,
                "remediation_options": options,
                "timeline": _step("plan", f"none ({note})")}

    try:
        proposal = ProposedAction(
            namespace=namespace, deployment=deployment, confidence=confidence,
            severity=severity,
            rationale=(
                f"{itype} classified from cluster evidence; "
                f"runbook {resolution['runbook_id']} supports this proposal"
            ),
            source="rules", **raw,
        )
    except ValidationError as exc:
        _audit(state, "plan_invalid", "Proposal failed schema validation",
               details={"errors": exc.errors(include_url=False)})
        return {"proposal": None, "plan_note": "schema_validation_failed",
                "remediation_options": options,
                "timeline": _step("plan", "schema_validation_failed")}

    _audit(state, "action_proposed",
           f"Proposed {proposal.action} on {proposal.resource}",
           resource=proposal.deployment, details=proposal.model_dump())
    _audit(
        state,
        "runbook_resolution_matched",
        f"Matched runbook {resolution['runbook_id']}",
        resource=proposal.deployment,
        details=resolution,
    )
    return {"proposal": proposal.model_dump(), "plan_note": "",
            "runbook_resolution": resolution,
            "remediation_options": options,
            "timeline": _step("plan", proposal.action)}


def _nothing_serving(deployment: Any) -> bool:
    if not isinstance(deployment, dict):
        return False
    available = deployment.get("available_replicas")
    desired = deployment.get("desired_replicas")
    return (
        isinstance(available, int) and not isinstance(available, bool)
        and isinstance(desired, int) and not isinstance(desired, bool)
        and available == 0 and desired >= 1
    )


def _policy_context(state: ResponseState, proposal: ProposedAction,
                    config: PolicyConfig) -> dict:
    _, window_seconds = config.action_capacity_for_namespace(
        proposal.namespace
    )
    try:
        recent = claims.count_recent_action_claims(
            proposal.deployment,
            window_seconds,
            exclude_claim_id=state.get("action_claim_id"),
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not read recent action count")
        recent = None
    classification = state.get("classification")
    classification = classification if isinstance(classification, dict) else {}
    class_target = classification.get("target")
    class_target = class_target if isinstance(class_target, dict) else {}
    return {
        "deployment": _deployment_state(state.get("kubernetes"),
                                        proposal.deployment),
        "recent_action_count": recent,
        # An evicted/failed pod is not serving traffic, so recreating it
        # cannot reduce serving capacity (see blast_radius_restart).
        "target_not_serving": (
            class_target.get("resource_kind") == "Pod"
            and (
                classification.get("incident_type") == "disk_full"
                # ...and so is a pod of a deployment with NOTHING serving
                # (e.g. crash loop with 0 available): no capacity to lose.
                or _nothing_serving(
                    _deployment_state(state.get("kubernetes"), proposal.deployment)
                )
            )
        ),
    }


def _logical_action_key(
    state: ResponseState, proposal: ProposedAction
) -> str:
    classification = state.get("classification")
    classification = classification if isinstance(classification, dict) else {}
    target = classification.get("target")
    target = target if isinstance(target, dict) else {}
    identity = {
        "service": state["service"],
        "incident_type": classification.get("incident_type"),
        "namespace": proposal.namespace,
        "resource_kind": target.get("resource_kind"),
        "resource_identity": target.get("resource_uid") or proposal.resource,
        "deployment": proposal.deployment,
        "action": proposal.action,
        "resource": proposal.resource,
        "target_replicas": proposal.target_replicas,
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _handoff_occurrence_is_active(state: ResponseState) -> bool:
    if not isinstance(state.get("occurrence_id"), int):
        return True
    from app.services import incident_repository

    occurrence = incident_repository.get_incident_occurrence(
        state.get("occurrence_id", -1)
    )
    return bool(
        occurrence is not None
        and occurrence["status"] in {"OPEN", "REMEDIATING"}
        and occurrence["service"] == state.get("service")
        and occurrence["incident_type"]
        == state.get("expected_incident_type")
        and occurrence["fingerprint"]
        == state.get("expected_fingerprint")
    )


def _scope_kubernetes_to_occurrence(
    state: ResponseState,
    kubernetes: Any,
) -> Any:
    """Keep unrelated cluster incidents out of a persisted incident diagnosis."""
    if not isinstance(kubernetes, dict):
        return kubernetes
    observation = {
        key: value for key, value in kubernetes.items()
        if key != "_persistent_disk_fill_evictions"
    }
    incidents = kubernetes.get("incidents")
    if not isinstance(incidents, list):
        return {**observation, "incidents": []}

    from app.services import incident_repository

    occurrence_id = state.get("occurrence_id")
    occurrence = (
        incident_repository.get_incident_occurrence(occurrence_id)
        if isinstance(occurrence_id, int) else None
    )
    try:
        source_evidence = json.loads(
            occurrence.get("source_evidence_json") or "null"
        ) if occurrence is not None else None
    except (TypeError, json.JSONDecodeError):
        source_evidence = None
    if not isinstance(source_evidence, dict):
        return {**observation, "incidents": []}
    expected_uid = source_evidence.get("resource_uid")
    if expected_uid is not None and (
        not isinstance(expected_uid, str) or not expected_uid
    ):
        return {**observation, "incidents": []}

    expected_fingerprint = state.get("expected_fingerprint")
    matching = []
    persistent_evictions = kubernetes.get(
        "_persistent_disk_fill_evictions"
    )
    scoped_candidates = [
        *incidents,
        *(
            persistent_evictions
            if isinstance(persistent_evictions, list) else []
        ),
    ]
    for item in scoped_candidates:
        if not isinstance(item, dict):
            continue
        resource = item.get("resource")
        resource_kind = item.get("resource_kind")
        incident_type = item.get("type")
        namespace = item.get("namespace", LAB_NAMESPACE)
        if (
            not isinstance(resource, str)
            or not isinstance(resource_kind, str)
            or not isinstance(incident_type, str)
            or namespace != LAB_NAMESPACE
        ):
            continue
        fingerprint = (
            f"k8s:{LAB_NAMESPACE}:{resource_kind}:{resource}:{incident_type}"
        )
        if (
            fingerprint == expected_fingerprint
            and (
                expected_uid is None
                or item.get("resource_uid") == expected_uid
            )
        ):
            matching.append(item)
    return {**observation, "incidents": matching}


def _bound_occurrence_matches(
    state: ResponseState,
    kubernetes: Any,
    classification: Any,
) -> bool:
    """Require fresh evidence to identify the exact persisted Kubernetes event."""
    from app.services import incident_repository

    occurrence_id = state.get("occurrence_id")
    if not isinstance(occurrence_id, int) or not _handoff_occurrence_is_active(
        state
    ):
        return False
    occurrence = incident_repository.get_incident_occurrence(occurrence_id)
    if occurrence is None or occurrence.get("service") != KUBERNETES_SERVICE:
        return False
    # Point-in-time events (e.g. an eviction) must still carry the exact
    # persisted timestamp. A failure that is STILL IN EFFECT (crash loop, image
    # pull failure, OOM restarts) is identified by its exact pod instead (name +
    # uid + fingerprint, checked below), because its timestamp legitimately
    # changes while it keeps failing.
    occurrence_timestamp_matches = (
        occurrence.get("source_timestamp")
        == state.get("expected_occurrence_timestamp")
    )
    try:
        persisted_evidence = json.loads(
            occurrence.get("source_evidence_json") or "null"
        )
    except (TypeError, json.JSONDecodeError):
        persisted_evidence = None
    persisted_uid = (
        persisted_evidence.get("resource_uid")
        if isinstance(persisted_evidence, dict) else None
    )
    expected_timestamp = occurrence.get("source_timestamp")
    if not isinstance(expected_timestamp, str):
        return False
    try:
        expected_time = datetime.fromisoformat(
            expected_timestamp.replace("Z", "+00:00")
        )
    except ValueError:
        return False
    if expected_time.tzinfo is None or expected_time.utcoffset() is None:
        return False
    expected_time = expected_time.astimezone(timezone.utc)
    if not isinstance(kubernetes, dict) or kubernetes.get("namespace") != LAB_NAMESPACE:
        return False
    observed_at = kubernetes.get("observed_at")
    if not isinstance(observed_at, str):
        return False
    try:
        observed_time = datetime.fromisoformat(
            observed_at.replace("Z", "+00:00")
        )
    except ValueError:
        return False
    if observed_time.tzinfo is None or observed_time.utcoffset() is None:
        return False
    observed_time = observed_time.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    if observed_time > now or now - observed_time > POD_EVENT_LOOKBACK:
        return False

    incidents = kubernetes.get("incidents")
    if not isinstance(incidents, list):
        return False
    matched = []
    for item in incidents:
        if not isinstance(item, dict):
            continue
        resource = item.get("resource")
        resource_kind = item.get("resource_kind")
        incident_type = item.get("type")
        if (
            not isinstance(resource, str)
            or not isinstance(resource_kind, str)
            or not isinstance(incident_type, str)
            or item.get("namespace", LAB_NAMESPACE) != LAB_NAMESPACE
        ):
            continue
        fingerprint = (
            f"k8s:{LAB_NAMESPACE}:{resource_kind}:{resource}:{incident_type}"
        )
        if fingerprint != state.get("expected_fingerprint"):
            continue
        if item.get("ongoing") is True:
            # Same exact pod as the persisted occurrence, still failing now.
            item_uid = item.get("resource_uid")
            if resource_kind == "Pod" and (
                not isinstance(item_uid, str) or not item_uid
            ):
                continue  # a pod is only ever identified by its uid
            if persisted_uid != item_uid:
                continue  # (a Deployment has no uid: both sides must be None)
            matched.append(item)
            continue
        if not occurrence_timestamp_matches:
            continue
        source_timestamp = item.get("source_timestamp")
        if not isinstance(source_timestamp, str):
            continue
        try:
            source_time = datetime.fromisoformat(
                source_timestamp.replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if source_time.tzinfo is None or source_time.utcoffset() is None:
            continue
        source_time = source_time.astimezone(timezone.utc)
        if (
            source_time != expected_time
            or source_time > observed_time
            or source_time > now
            or now - source_time > POD_EVENT_LOOKBACK
        ):
            continue
        matched.append(item)
    if len(matched) != 1:
        return False

    target = classification.get("target") if isinstance(
        classification, dict
    ) else None
    item = matched[0]
    deployment = target.get("deployment") if isinstance(target, dict) else None
    resource = item.get("resource")
    resource_kind = item.get("resource_kind")
    supported_deployments = PolicyConfig.from_env().allowed_deployments
    if (
        not isinstance(deployment, str)
        or deployment not in supported_deployments
        or (
            resource_kind == "Pod"
            and not resource.startswith(deployment + "-")
        )
        or (
            resource_kind == "Deployment"
            and resource != deployment
        )
    ):
        return False
    return bool(
        isinstance(target, dict)
        and classification.get("incident_type") != "unknown"
        and target.get("namespace") == LAB_NAMESPACE
        and target.get("resource") == resource
        and target.get("resource_kind") == resource_kind
        and target.get("resource_uid") == item.get("resource_uid")
    )


async def policy(state: ResponseState) -> dict:
    policy_state = state
    if state.get("occurrence_bound"):
        try:
            kubernetes = await asyncio.to_thread(observe_namespace)
            kubernetes = _scope_kubernetes_to_occurrence(
                state, kubernetes
            )
            fresh_classification = classify_incident(
                None, kubernetes, {"status": "not_applicable"}
            )
        except Exception as exc:  # noqa: BLE001 - fail closed
            logger.exception("Could not revalidate persisted occurrence")
            kubernetes, fresh_classification = None, {"incident_type": "unknown"}
            error = type(exc).__name__
        else:
            error = "fresh_evidence_mismatch"
        previous_target = (state.get("classification") or {}).get("target")
        fresh_target = fresh_classification.get("target")
        target_matches = (
            isinstance(previous_target, dict)
            and isinstance(fresh_target, dict)
            and all(
                previous_target.get(key) == fresh_target.get(key)
                for key in (
                    "namespace", "resource", "resource_kind", "resource_uid",
                    "deployment",
                )
            )
        )
        if (
            not _bound_occurrence_matches(
                state, kubernetes, fresh_classification
            )
            or not target_matches
        ):
            _audit(
                state,
                "incident_occurrence_evidence_rejected",
                "Policy blocked because fresh evidence no longer supports the selected occurrence",
                outcome="blocked",
                details={
                    "reason": error,
                    "observed_incident_type": fresh_classification.get(
                        "incident_type"
                    ),
                    "expected_fingerprint": state.get("expected_fingerprint"),
                },
            )
            return {
                "policy_decision": {
                    "allowed": False,
                    "requires_approval": False,
                    "auto_execute": False,
                    "reasons": ["incident_occurrence_or_evidence_changed"],
                },
                "kubernetes": kubernetes,
                "timeline": _step("policy", "fresh evidence mismatch"),
            }
        policy_state = {**state, "kubernetes": kubernetes}
    elif state.get("monitor_handoff"):
        try:
            fresh = await detect_and_diagnose(
                {"service": state["service"]}
            )
            fresh_classification = classify_incident(
                fresh.get("diagnosis"),
                fresh.get("kubernetes"),
                fresh.get("telemetry_anomaly"),
            )
        except Exception as exc:  # noqa: BLE001 - fail closed
            logger.exception("Could not revalidate monitor evidence")
            fresh, fresh_classification = {}, {"incident_type": "unknown"}
            error = type(exc).__name__
        else:
            error = "fresh_evidence_mismatch"
        previous_target = (state.get("classification") or {}).get("target")
        fresh_target = fresh_classification.get("target")
        target_matches = (
            isinstance(previous_target, dict)
            and isinstance(fresh_target, dict)
            and all(
                previous_target.get(key) == fresh_target.get(key)
                for key in (
                    "namespace", "resource", "resource_kind", "resource_uid",
                    "deployment",
                )
            )
        )
        proposal = state.get("proposal") or {}
        proposal_matches = (
            isinstance(fresh_target, dict)
            and proposal.get("namespace") == fresh_target.get("namespace")
            and proposal.get("resource") == fresh_target.get("resource")
            and proposal.get("deployment") == fresh_target.get("deployment")
        )
        if (
            not _handoff_occurrence_is_active(state)
            or fresh.get("kubernetes_status") != "completed"
            or fresh_classification.get("incident_type")
            != state.get("expected_incident_type")
            or not target_matches
            or not proposal_matches
        ):
            _audit(
                state,
                "incident_handoff_evidence_rejected",
                "Action authorization blocked because fresh evidence changed",
                outcome="blocked",
                details={
                    "reason": error,
                    "observed_incident_type": fresh_classification.get(
                        "incident_type"
                    ),
                },
            )
            return {
                "policy_decision": {
                    "allowed": False,
                    "requires_approval": False,
                    "auto_execute": False,
                    "reasons": ["incident_occurrence_or_evidence_changed"],
                },
                "kubernetes": fresh.get("kubernetes"),
                "timeline": _step("policy", "fresh evidence mismatch"),
            }
        policy_state = {
            **state,
            "kubernetes": fresh.get("kubernetes"),
            "telemetry_anomaly": fresh.get("telemetry_anomaly"),
        }
    proposal = ProposedAction(**state["proposal"])
    config = PolicyConfig.from_env()
    decision = evaluate_policy(
        proposal, _policy_context(policy_state, proposal, config), config)
    data = decision.to_dict()
    if (
        state.get("monitor_handoff") or state.get("force_approval")
    ) and data["allowed"]:
        data["auto_execute"] = False
        data["requires_approval"] = True
        data["rules"].append({
            "id": (
                "monitor_requires_human_approval"
                if state.get("monitor_handoff")
                else "operator_initiated_requires_human_approval"
            ),
            "passed": True,
            "detail": (
                "monitor identity cannot authorize execution"
                if state.get("monitor_handoff")
                else "operator-initiated Kubernetes investigations require approval"
            ),
        })
    runbook_resolution = state.get("runbook_resolution") or {}
    if data["allowed"] and runbook_resolution.get("approval_required") is True:
        data["auto_execute"] = False
        data["requires_approval"] = True
        for item in data["rules"]:
            if item.get("id") == "approval_required":
                item.update({
                    "passed": True,
                    "detail": "runbook requires human approval",
                })
                break
    update: dict = {}
    if decision.allowed:
        max_actions, window_seconds = config.action_capacity_for_namespace(
            proposal.namespace
        )
        reservation = claims.reserve_action_claim(
            workflow_claim_id=state["workflow_claim_id"],
            attempt_no=state.get("attempts", 0),
            logical_key=_logical_action_key(state, proposal),
            deployment=proposal.deployment,
            action=proposal.model_dump(mode="json"),
            policy=data,
            max_actions=max_actions,
            window_seconds=window_seconds,
            auto_authorized=data["auto_execute"],
            incident_occurrence_id=(
                state.get("occurrence_id")
                if isinstance(state.get("occurrence_id"), int)
                else None
            ),
            expected_service=(
                state.get(
                    "expected_occurrence_service",
                    state.get("service") if state.get("monitor_handoff") else None,
                )
                if isinstance(state.get("occurrence_id"), int) else None
            ),
            expected_incident_type=(
                state.get("expected_incident_type")
                if isinstance(state.get("occurrence_id"), int) else None
            ),
            expected_fingerprint=(
                state.get("expected_fingerprint")
                if isinstance(state.get("occurrence_id"), int) else None
            ),
        )
        if reservation["allowed"]:
            update["action_claim_id"] = reservation["action_claim_id"]
        else:
            reason = reservation["reason"]
            data["allowed"] = False
            data["requires_approval"] = False
            data["auto_execute"] = False
            data["reasons"].append(reason)
            data["rules"].append({
                "id": "durable_action_claim",
                "passed": False,
                "detail": reason,
            })
            update["duplicate_action_claim_id"] = reservation.get(
                "duplicate_claim_id"
            )
    _audit(policy_state, "policy_evaluated",
           "Policy " + ("allowed" if data["allowed"] else "denied")
           + ("; approval required" if data["requires_approval"] else ""),
           resource=proposal.deployment, policy_decision=data,
           outcome="allowed" if data["allowed"] else "denied",
           action_claim_id=update.get("action_claim_id"),
           details={
               "duplicate_action_claim_id": update.get(
                   "duplicate_action_claim_id"
               ),
           } if update.get("duplicate_action_claim_id") else None)
    return {**update, "kubernetes": policy_state.get("kubernetes"),
            "policy_decision": data,
            "timeline": _step("policy", "allowed" if decision.allowed
                              else "denied")}


def request_approval(state: ResponseState) -> dict:
    occurrence_active = True
    if isinstance(state.get("occurrence_id"), int):
        from app.services import incident_repository

        occurrence = incident_repository.get_incident_occurrence(
            state["occurrence_id"]
        )
        occurrence_active = bool(
            occurrence is not None
            and occurrence["status"] in {"OPEN", "REMEDIATING"}
            and occurrence["service"]
            == state.get(
                "expected_occurrence_service",
                state.get("service") if state.get("monitor_handoff") else None,
            )
            and occurrence["incident_type"]
            == state.get("expected_incident_type")
            and occurrence["fingerprint"]
            == state.get("expected_fingerprint")
        )
    if isinstance(state.get("occurrence_id"), int) and not occurrence_active:
        claims.transition_action_claim(
            state["action_claim_id"], "RESERVED", "BLOCKED"
        )
        _audit(
            state,
            "incident_handoff_occurrence_inactive",
            "Approval request blocked because the occurrence is no longer active",
            outcome="blocked",
        )
        return {
            "status": "blocked",
            "timeline": _step("request_approval", "incident occurrence inactive"),
        }
    try:
        contract = _build_contract(state)
    except ValidationError:
        logger.exception("Contract validation failed before approval")
        action_claim_id = state.get("action_claim_id")
        if isinstance(action_claim_id, str) and action_claim_id:
            try:
                action_claim = claims.get_action_claim(action_claim_id)
                if action_claim and action_claim["status"] == "RESERVED":
                    transitioned = claims.transition_action_claim(
                        action_claim_id, "RESERVED", "BLOCKED"
                    )
                    if not transitioned:
                        logger.warning(
                            "Reserved action claim %s changed before it "
                            "could be blocked",
                            action_claim_id,
                        )
            except Exception:  # noqa: BLE001 - approval remains blocked
                logger.exception(
                    "Could not block reserved action claim after contract "
                    "validation failure"
                )
        _audit(
            state,
            "contract_validation_failed",
            "Approval blocked because the incident contract is invalid",
            outcome="blocked",
        )
        return {
            "contract": None,
            "contract_status": "invalid",
            "contract_error": "validation_failed",
            "status": "blocked",
            "timeline": _step("request_approval", "contract_invalid"),
        }
    approval = audit.create_approval(
        incident_ref=state["incident_id"],
        proposed_action=state["proposal"],
        action_claim_id=state["action_claim_id"],
        context={
            "service": state["service"],
            "attempts": state.get("attempts", 0),
            "max_attempts": state.get("max_attempts", max_attempts_default()),
            "classification": state.get("classification"),
            "runbook_resolution": state.get("runbook_resolution"),
            "policy": state.get("policy_decision"),
            "contract": contract.model_dump(mode="json"),
            "monitor_handoff": state.get("monitor_handoff", False),
            "occurrence_bound": state.get("occurrence_bound", False),
            "force_approval": state.get("force_approval", False),
            "occurrence_id": state.get("occurrence_id"),
            "expected_occurrence_service": state.get(
                "expected_occurrence_service"
            ),
            "expected_occurrence_service": state.get(
                "expected_occurrence_service"
            ),
            "expected_incident_type": state.get("expected_incident_type"),
            "expected_fingerprint": state.get("expected_fingerprint"),
            "expected_occurrence_timestamp": state.get(
                "expected_occurrence_timestamp"
            ),
        },
        requested_by=state.get("requested_by", "system"),
    )
    return {"approval_id": approval["approval_id"],
            "contract": contract.model_dump(mode="json"),
            "status": "awaiting_approval",
            "timeline": _step("request_approval", approval["approval_id"])}


async def recheck_policy(state: ResponseState) -> dict:
    """On resume: re-validate the stored proposal against fresh state."""
    try:
        proposal = ProposedAction(**state["proposal"])
        if state.get("monitor_handoff"):
            fresh = await detect_and_diagnose(
                {"service": state["service"]}
            )
            fresh_classification = classify_incident(
                fresh.get("diagnosis"),
                fresh.get("kubernetes"),
                fresh.get("telemetry_anomaly"),
            )
            if (
                not _handoff_occurrence_is_active(state)
                or fresh.get("kubernetes_status") != "completed"
                or fresh_classification.get("incident_type")
                != state.get("expected_incident_type")
            ):
                raise ValueError("handoff_evidence_no_longer_matches")
            original_target = (state.get("classification") or {}).get("target")
            fresh_target = fresh_classification.get("target")
            if not isinstance(original_target, dict) or not isinstance(
                fresh_target, dict
            ) or any(
                original_target.get(key) != fresh_target.get(key)
                for key in (
                    "namespace", "resource", "resource_kind", "resource_uid",
                    "deployment",
                )
            ):
                raise ValueError("handoff_target_changed")
            kubernetes = fresh.get("kubernetes")
        else:
            if isinstance(state.get("occurrence_id"), int):
                from app.services import incident_repository

                occurrence = incident_repository.get_incident_occurrence(
                    state["occurrence_id"]
                )
                if (
                    occurrence is None
                    or occurrence["status"] not in {"OPEN", "REMEDIATING"}
                    or occurrence["service"]
                    != state.get("expected_occurrence_service")
                    or occurrence["incident_type"]
                    != state.get("expected_incident_type")
                    or occurrence["fingerprint"]
                    != state.get("expected_fingerprint")
                ):
                    raise ValueError("incident_occurrence_no_longer_active")
            if state["service"] == KUBERNETES_SERVICE:
                kubernetes = await asyncio.to_thread(observe_namespace)
                fresh = {
                    "diagnosis": None,
                    "telemetry_anomaly": None,
                    "kubernetes": kubernetes,
                }
            else:
                fresh = await detect_and_diagnose(
                    {"service": state["service"]}
                )
                if fresh.get("kubernetes_status") != "completed":
                    raise ValueError("fresh_kubernetes_evidence_unavailable")
                kubernetes = fresh.get("kubernetes")

            if state.get("occurrence_bound"):
                kubernetes = _scope_kubernetes_to_occurrence(
                    state, kubernetes
                )
            fresh_classification = classify_incident(
                fresh.get("diagnosis"),
                kubernetes,
                fresh.get("telemetry_anomaly"),
            )
            original_classification = state.get("classification") or {}
            original_target = original_classification.get("target")
            fresh_target = fresh_classification.get("target")
            if (
                fresh_classification.get("incident_type")
                != original_classification.get("incident_type")
                or not isinstance(original_target, dict)
                or not isinstance(fresh_target, dict)
                or any(
                    original_target.get(key) != fresh_target.get(key)
                    for key in (
                        "namespace", "resource", "resource_kind",
                        "resource_uid", "deployment",
                    )
                )
                or (
                    state.get("occurrence_bound")
                    and not _bound_occurrence_matches(
                        state, kubernetes, fresh_classification
                    )
                )
                or proposal.namespace != fresh_target.get("namespace")
                or (
                    proposal.target_pod is not None
                    and (
                        proposal.target_pod != fresh_target.get("resource")
                        or proposal.target_pod_uid
                        != fresh_target.get("resource_uid")
                    )
                )
                or not (
                    proposal.resource == fresh_target.get("resource")
                    # Deployment-level actions (scale / rollback / set_image /
                    # restore_command) name the deployment, not the pod.
                    or proposal.resource == proposal.deployment
                    == fresh_target.get("deployment")
                )
                or proposal.deployment != fresh_target.get("deployment")
            ):
                raise ValueError("fresh_incident_evidence_no_longer_matches")
    except Exception as exc:  # noqa: BLE001 - fail closed
        if state.get("action_claim_id"):
            claims.transition_action_claim(
                state["action_claim_id"], "APPROVED", "BLOCKED"
            )
        cause = str(exc) if isinstance(exc, ValueError) and str(exc) else type(exc).__name__
        logger.warning("Policy recheck failed: %s", cause)
        _audit(state, "policy_recheck_failed",
               f"Could not re-validate: {cause}",
               outcome="blocked", details={"cause": cause})
        return {"policy_decision": {"allowed": False,
                                    "reasons": ["recheck_failed", cause]},
                "timeline": _step("recheck_policy", "failed")}

    config = PolicyConfig.from_env()
    state_with_k8s = {**state, "kubernetes": kubernetes}
    classification = state.get("classification") or {}
    evidence = classification.get("evidence")
    evidence = evidence if isinstance(evidence, list) else []
    itype = classification.get("incident_type", "unknown")
    runbook_resolution = resolve_runbook(
        " ".join([
            itype,
            *(item for item in evidence if isinstance(item, str)),
        ]),
        incident_type=itype,
        evidence=evidence,
        planned_action=proposal.action,
        deployment_state=_deployment_state(kubernetes, proposal.deployment),
        allow_scaled_down_restore=(
            classification.get("incident_type") == "disk_full"
            and proposal.deployment == "sim-disk-fill"
            and proposal.target_replicas == 1
        ),
    )
    if runbook_resolution["status"] != "applicable":
        if state.get("action_claim_id"):
            claims.transition_action_claim(
                state["action_claim_id"], "APPROVED", "BLOCKED"
            )
        _audit(
            state,
            "runbook_recheck_failed",
            "Approved action blocked because runbook requirements no longer pass",
            outcome="blocked",
            details=runbook_resolution,
        )
        return {
            "policy_decision": {
                "allowed": False,
                "reasons": [runbook_resolution["reason"]],
            },
            "runbook_resolution": runbook_resolution,
            "kubernetes": kubernetes,
            "timeline": _step("recheck_policy", "runbook_requirements_failed"),
        }

    decision = evaluate_policy(
        proposal, _policy_context(state_with_k8s, proposal, config), config)
    data = decision.to_dict()
    _audit(
        state,
        "runbook_requirements_rechecked",
        f"Runbook requirements revalidated for {runbook_resolution['runbook_id']}",
        resource=proposal.deployment,
        approval_id=state.get("approval_id"),
        outcome="applicable",
        details={
            "runbook_id": runbook_resolution["runbook_id"],
            "status": runbook_resolution["status"],
            "prerequisites": runbook_resolution["prerequisites"],
        },
    )
    if not decision.allowed and state.get("action_claim_id"):
        claims.transition_action_claim(
            state["action_claim_id"], "APPROVED", "BLOCKED"
        )
    _audit(state, "policy_rechecked",
           "Policy re-evaluated before execution: "
           + ("allowed" if decision.allowed else "denied"),
           resource=proposal.deployment, policy_decision=data,
           approval_id=state.get("approval_id"))
    return {"policy_decision": data, "runbook_resolution": runbook_resolution,
            "kubernetes": kubernetes,
            "timeline": _step("recheck_policy",
                              "allowed" if decision.allowed else "denied")}


def remediate(state: ResponseState) -> dict:
    proposal = ProposedAction(**state["proposal"])
    approved_by = state.get("approved_by")
    actor = approved_by or "kybernaut-auto"
    claim_status = "APPROVED" if approved_by else "AUTO_AUTHORIZED"
    execution_audit = {
        "incident_ref": state["incident_id"],
        "event_type": "remediation_started",
        "actor": actor,
        "summary": (
            f"Execution authorization granted; starting "
            f"{proposal.action} attempt on {proposal.resource}"
        ),
        "resource": proposal.deployment,
        "policy_decision": state.get("policy_decision"),
        "approval_id": state.get("approval_id"),
        "action_claim_id": state.get("action_claim_id"),
        "workflow_claim_id": state.get("workflow_claim_id"),
        "outcome": "authorized",
        "details": {
            "execution_boundary": "authorized_attempt_initiation",
            **(
                {"incident_row_id": state["occurrence_id"]}
                if isinstance(state.get("occurrence_id"), int) else {}
            ),
        },
    }
    try:
        if isinstance(state.get("occurrence_id"), int):
            authorized = claims.authorize_action_execution(
                state["action_claim_id"],
                claim_status,
                incident_occurrence_id=state["occurrence_id"],
                expected_service=state["expected_occurrence_service"],
                expected_incident_type=state["expected_incident_type"],
                expected_fingerprint=state["expected_fingerprint"],
                require_handoff=state.get("monitor_handoff", False),
                actor=actor,
                approval_id=state.get("approval_id"),
                policy_decision=state.get("policy_decision"),
                audit_incident_ref=state.get("incident_id"),
            )
        else:
            authorized = claims.transition_action_claim(
                state["action_claim_id"], claim_status, "EXECUTING",
                audit_event=execution_audit,
            )
    except Exception:  # noqa: BLE001
        logger.exception(
            "Execution authorization audit failed; refusing to execute"
        )
        return {
            "execution": {
                "executed": False,
                "ok": False,
                "error": "audit_unavailable",
            },
            "attempts": state.get("attempts", 0) + 1,
            "timeline": _step("remediate", "blocked_audit_unavailable"),
        }
    if not authorized:
        occurrence_active = _handoff_occurrence_is_active(state)
        if not isinstance(state.get("occurrence_id"), int):
            _audit(state, "action_claim_unavailable",
                   "Execution blocked because its action claim is unavailable",
                   outcome="blocked")
        return {
            "execution": {
                "executed": False,
                "ok": False,
                "error": (
                    "incident_occurrence_inactive"
                    if isinstance(state.get("occurrence_id"), int)
                    and not occurrence_active
                    else "action_claim_unavailable"
                ),
            },
            "attempts": state.get("attempts", 0) + 1,
            "timeline": _step("remediate", "blocked_action_claim"),
        }
    try:
        result = execute_action(proposal)
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("executed"), bool)
            or not isinstance(result.get("ok"), bool)
        ):
            raise ValueError("Invalid remediation execution result")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Remediation execution outcome is unknown")
        try:
            claims.transition_action_claim(
                state["action_claim_id"],
                "EXECUTING",
                "EXECUTION_UNKNOWN",
                execution={"error": type(exc).__name__},
                audit_event={
                    **execution_audit,
                    "event_type": "remediation_execution_unknown",
                    "summary": "External execution outcome is unknown",
                    "outcome": "unknown",
                    "details": {
                        **execution_audit["details"],
                        "error_category": type(exc).__name__,
                    },
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "Could not persist unknown execution outcome; claim remains "
                "non-replayable"
            )
        return {
            "execution": {
                "executed": True,
                "ok": False,
                "error": "execution_outcome_unknown",
            },
            "attempts": state.get("attempts", 0) + 1,
            "timeline": _step("remediate", "execution_outcome_unknown"),
        }

    mode = result.get("mode")
    executed = result["executed"]
    ok = result["ok"]
    valid_mode = isinstance(mode, str) and mode in {
        "live", "dry_run", "simulated",
    }
    contradictory = (
        not valid_mode
        or (mode == "simulated" and executed)
        or (mode == "dry_run" and executed)
        or (mode == "live" and not executed and ok)
    )
    if contradictory:
        invalid_result = {
            **result,
            "executed": False,
            "ok": False,
            "error": "invalid_execution_result",
        }
        try:
            claims.transition_action_claim(
                state["action_claim_id"],
                "EXECUTING",
                "EXECUTION_UNKNOWN",
                execution=invalid_result,
                audit_event={
                    **execution_audit,
                    "event_type": "remediation_result_invalid",
                    "summary": (
                        "Executor returned a contradictory or unsupported "
                        "result"
                    ),
                    "outcome": "invalid",
                    "details": {**execution_audit["details"], **invalid_result},
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not persist invalid execution outcome")
        return {
            "execution": invalid_result,
            "attempts": state.get("attempts", 0) + 1,
            "timeline": _step("remediate", "invalid_execution_result"),
        }

    if result.get("outcome_unknown") is True:
        terminal_status = "EXECUTION_UNKNOWN"
    elif mode == "simulated" and ok:
        terminal_status = "SIMULATED"
    elif mode == "simulated":
        terminal_status = "SIMULATION_FAILED"
    elif not executed and ok:
        terminal_status = "DRY_RUN_COMPLETE"
    elif not executed:
        terminal_status = "EXECUTION_FAILED"
    elif ok:
        terminal_status = "COMPLETED"
    else:
        terminal_status = "EXECUTION_UNKNOWN"
    if result.get("outcome_unknown") is True:
        event = "remediation_execution_unknown"
    elif mode == "simulated":
        event = (
            "remediation_simulated"
            if result["ok"] else "remediation_simulation_failed"
        )
    elif mode == "dry_run":
        event = "remediation_dry_run" if result["ok"] else "remediation_failed"
    else:
        event = (
            "remediation_executed"
            if result["executed"] and result["ok"]
            else "remediation_failed"
        )
    try:
        recorded = claims.transition_action_claim(
            state["action_claim_id"],
            "EXECUTING",
            terminal_status,
            execution=result,
            audit_event={
                **execution_audit,
                "event_type": event,
                "summary": (
                    f"{proposal.action}: execution outcome unknown"
                    if result.get("outcome_unknown") is True
                    else f"{proposal.action}: {mode}, ok={result['ok']}"
                ),
                "outcome": (
                    "unknown" if result.get("outcome_unknown") is True
                    else "ok" if result["ok"] else "failed"
                ),
                "details": {**execution_audit["details"], **result},
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not persist remediation claim outcome")
        recorded = False
    if not recorded:
        result = {
            **result,
            "ok": False,
            "error": "action_claim_outcome_unavailable",
        }
    return {"execution": result, "attempts": state.get("attempts", 0) + 1,
            "timeline": _step("remediate", event)}


async def verify(state: ResponseState) -> dict:
    proposal = ProposedAction(**state["proposal"])
    classification = state.get("classification") or {}
    target = classification.get("target") if isinstance(classification, dict) else None
    target_resource = target.get("resource") if isinstance(target, dict) else None
    target_incident_type = state.get("expected_incident_type")
    verification_scope = (
        {
            "target_resource": target_resource,
            "target_incident_type": target_incident_type,
        }
        if proposal.action in {"restore_command", "cleanup_evicted_pod"}
        and proposal.deployment == "sim-disk-fill"
        and target_incident_type == "pod_failed"
        and isinstance(target_resource, str)
        and target_resource.startswith("sim-disk-fill-")
        else {}
    )
    verification = await verify_recovery(
        deployment=proposal.deployment, observe=observe_namespace,
        **verification_scope,
        attempts=int(os.getenv("KYBERNAUT_VERIFY_ATTEMPTS", "5")),
        interval_seconds=float(os.getenv("KYBERNAUT_VERIFY_INTERVAL", "3")),
    )
    try:
        persisted = claims.record_action_verification(
            state["action_claim_id"],
            verification,
            audit_event={
                "incident_ref": state["incident_id"],
                "event_type": "verification_completed",
                "actor": state.get("approved_by") or "kybernaut-workflow",
                "summary": f"Verification: {verification['reason']}",
                "resource": proposal.deployment,
                "approval_id": state.get("approval_id"),
                "action_claim_id": state.get("action_claim_id"),
                "workflow_claim_id": state.get("workflow_claim_id"),
                "outcome": (
                    "recovered" if verification.get("recovered") is True
                    else "not_recovered"
                ),
                "details": {
                    **verification,
                    **(
                        {"incident_row_id": state["occurrence_id"]}
                        if isinstance(state.get("occurrence_id"), int) else {}
                    ),
                },
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not persist recovery verification")
        persisted = False
    if not persisted:
        verification = {
            **verification,
            "recovered": False,
            "persistence_failed": True,
            "reason": "verification_record_unavailable",
        }
    return {"verification": verification,
            "timeline": _step("verify", verification["reason"])}


def resolve(state: ResponseState) -> dict:
    execution = state.get("execution") or {}
    verification = state.get("verification") or {}
    if not (
        execution.get("mode") == "live"
        and execution.get("executed") is True
        and execution.get("ok") is True
        and verification.get("recovered") is True
    ):
        _audit(
            state,
            "resolution_blocked",
            "Incident resolution requires successful live execution and verification",
            outcome="blocked",
        )
        return {
            "status": "escalated",
            "timeline": _step("resolve", "live_execution_and_verification_required"),
        }
    if not isinstance(state.get("occurrence_id"), int):
        _audit(state, "incident_recovery_verified",
               "Recovery verified from fresh telemetry", outcome="resolved")
    return {"status": "resolved", "timeline": _step("resolve", "resolved")}


def escalate(state: ResponseState) -> dict:
    reason = "execution_failed" if (state.get("execution") or {}).get("ok") \
        is False else "verification_failed_attempts_exhausted"
    _audit(state, "escalated", f"Escalated to a human operator: {reason}",
           outcome="escalated")
    return {"status": "escalated", "timeline": _step("escalate", reason)}


def close_no_action(state: ResponseState) -> dict:
    decision = state.get("policy_decision")
    if state.get("proposal") is None:
        status, why = "no_action", state.get("plan_note", "no_proposal")
    else:
        status = "blocked"
        why = "; ".join((decision or {}).get("reasons", [])) or "policy_denied"
    _audit(state, "closed_without_action", f"{status}: {why}", outcome=status)
    return {"status": status, "timeline": _step("close", status)}


def close_dry_run(state: ResponseState) -> dict:
    _audit(state, "dry_run_complete",
           "Dry-run only; nothing executed, recovery NOT verified",
           outcome="dry_run")
    return {"status": "dry_run_complete",
            "verification": {"recovered": False, "reason": "not_executed"},
            "timeline": _step("close", "dry_run_complete")}


def close_simulation(state: ResponseState) -> dict:
    _audit(
        state,
        "simulation_complete",
        "Simulation only; no Kubernetes action executed or recovery verified",
        outcome="simulated",
    )
    return {
        "status": "simulation_complete",
        "verification": {"recovered": False, "reason": "simulation_not_executed"},
        "timeline": _step("close", "simulation_complete"),
    }


# -------------------------------------------------------------- routing

def route_start(state: ResponseState) -> str:
    return "recheck_policy" if state.get("approval_decision") == "approved" \
        else "collect_evidence"


def route_after_plan(state: ResponseState) -> str:
    return "policy" if state.get("proposal") else "close_no_action"


def route_after_policy(state: ResponseState) -> str:
    d = state.get("policy_decision") or {}
    if not d.get("allowed"):
        return "close_no_action"
    return "remediate" if d.get("auto_execute") else "request_approval"


def route_after_recheck(state: ResponseState) -> str:
    return "remediate" if (state.get("policy_decision") or {}).get("allowed") \
        else "close_no_action"


def route_after_remediate(state: ResponseState) -> str:
    ex = state.get("execution") or {}
    if not ex.get("ok"):
        return "escalate"
    if ex.get("mode") == "simulated":
        return "close_simulation"
    if ex.get("mode") == "live" and ex.get("executed") is True:
        return "verify"
    return "close_dry_run"


def route_after_verify(state: ResponseState) -> str:
    execution = state.get("execution") or {}
    if not (
        execution.get("mode") == "live"
        and execution.get("executed") is True
        and execution.get("ok") is True
    ):
        return "escalate"
    if (state.get("verification") or {}).get("persistence_failed"):
        return "escalate"
    if (state.get("verification") or {}).get("recovered"):
        return "resolve"
    if state.get("attempts", 0) < state.get("max_attempts",
                                            max_attempts_default()):
        return "collect_evidence"  # fresh evidence -> new plan -> new approval
    return "escalate"


def build_response_graph():
    g = StateGraph(ResponseState)
    for name, fn in [
        ("collect_evidence", collect_evidence), ("classify", classify),
        ("close_handoff", close_handoff),
        ("diagnose_rca", diagnose_rca), ("plan", plan), ("policy", policy),
        ("request_approval", request_approval),
        ("recheck_policy", recheck_policy), ("remediate", remediate),
        ("verify", verify), ("resolve", resolve), ("escalate", escalate),
        ("close_no_action", close_no_action), ("close_dry_run", close_dry_run),
        ("close_simulation", close_simulation),
    ]:
        g.add_node(name, fn)

    g.add_conditional_edges(START, route_start,
                            ["collect_evidence", "recheck_policy"])
    g.add_edge("collect_evidence", "classify")
    g.add_conditional_edges(
        "classify",
        route_after_classify,
        ["close_handoff", "diagnose_rca"],
    )
    g.add_edge("diagnose_rca", "plan")
    g.add_conditional_edges("plan", route_after_plan,
                            ["policy", "close_no_action"])
    g.add_conditional_edges("policy", route_after_policy,
                            ["close_no_action", "remediate", "request_approval"])
    g.add_edge("request_approval", END)
    g.add_conditional_edges("recheck_policy", route_after_recheck,
                            ["remediate", "close_no_action"])
    g.add_conditional_edges("remediate", route_after_remediate,
                            ["escalate", "verify", "close_dry_run",
                             "close_simulation"])
    g.add_conditional_edges("verify", route_after_verify,
                            ["resolve", "collect_evidence", "escalate"])
    for terminal in (
        "resolve", "escalate", "close_no_action", "close_dry_run",
        "close_simulation", "close_handoff",
    ):
        g.add_edge(terminal, END)
    return g.compile()


response_graph = build_response_graph()


def _summary(result: dict) -> dict:
    summary = {k: result.get(k) for k in (
        "incident_id", "workflow_claim_id", "action_claim_id",
        "duplicate_action_claim_id", "service", "status",
        "classification", "proposal",
        "runbook_resolution", "plan_note", "remediation_options",
        "policy_decision", "approval_id", "execution",
        "verification", "rca_status", "attempts", "timeline",
        "occurrence_id", "rca")}
    if result.get("contract_status") == "invalid":
        summary["contract"] = None
        summary["contract_status"] = "invalid"
        summary["contract_error"] = (
            result.get("contract_error") or "validation_failed"
        )
        return summary
    try:
        summary["contract"] = _build_contract(result).model_dump(mode="json")
    except ValidationError:
        logger.exception("Could not build response incident contract")
        summary["contract"] = None
        summary["contract_status"] = "invalid"
        summary["contract_error"] = "validation_failed"
        return summary
    summary["contract_status"] = "valid"
    summary["contract_error"] = ""
    return summary


def _condition_cleared_workflow_response(claim_id: str) -> dict | None:
    claim = claims.get_workflow_claim(claim_id)
    if (
        claim is None
        or claim["status"] != "COMPLETED"
        or not claim["response_json"]
    ):
        return None
    response = json.loads(claim["response_json"])
    return response if response.get("status") == "condition_cleared" else None


async def run_response_workflow(
    service: str, *, incident_id: str | None = None,
    requested_by: str = "system",
    workflow_claim_id: str | None = None,
    monitor_handoff: bool = False,
    occurrence_bound: bool = False,
    force_approval: bool = False,
    occurrence_id: int | None = None,
    expected_incident_type: str | None = None,
    expected_fingerprint: str | None = None,
    preferred_action: str | None = None,
) -> dict:
    if service not in SUPPORTED_SERVICES:
        raise WorkflowNotStarted("Unsupported service")
    if occurrence_bound and (
        service != KUBERNETES_SERVICE or not incident_id or monitor_handoff
    ):
        raise WorkflowNotStarted("Invalid persisted Kubernetes occurrence")
    if occurrence_bound:
        force_approval = True
    if monitor_handoff and (
        requested_by != "kybernaut-monitor"
        or not isinstance(occurrence_id, int)
        or not expected_incident_type
        or not expected_fingerprint
    ):
        raise WorkflowNotStarted("Invalid internal handoff identity")
    incident_ref = incident_id or f"INC-{uuid.uuid4().hex[:8]}"
    if workflow_claim_id is None:
        request_claim, created, _ = claims.create_workflow_claim(
            operator_id=requested_by,
            service=service,
            idempotency_key=f"internal-{uuid.uuid4().hex}",
            incident_ref=incident_ref,
            request_correlation=incident_id,
        )
        if not created:
            raise RuntimeError("Could not create workflow claim")
        workflow_claim_id = request_claim["claim_id"]
        incident_ref = request_claim["incident_ref"]
    else:
        request_claim = claims.get_workflow_claim(workflow_claim_id)
        if (
            request_claim is None
            or request_claim["status"] != "RUNNING"
            or request_claim["operator_id"] != requested_by
            or request_claim["service"] != service
        ):
            raise WorkflowNotStarted("Workflow claim is not available")
        incident_ref = request_claim["incident_ref"]
    expected_occurrence_service = None
    expected_occurrence_timestamp = None
    if monitor_handoff:
        expected_occurrence_service = service
    elif isinstance(occurrence_id, int):
        from app.services import incident_repository

        occurrence = incident_repository.get_incident_occurrence(occurrence_id)
        if occurrence is not None:
            expected_occurrence_service = occurrence["service"]
            expected_incident_type = (
                expected_incident_type or occurrence["incident_type"]
            )
            expected_fingerprint = (
                expected_fingerprint or occurrence["fingerprint"]
            )
            expected_occurrence_timestamp = occurrence.get("source_timestamp")
            if occurrence_bound:
                try:
                    source_evidence = json.loads(
                        occurrence.get("source_evidence_json") or "null"
                    )
                except (TypeError, json.JSONDecodeError):
                    source_evidence = None
                if (
                    occurrence.get("status") not in {"OPEN", "REMEDIATING"}
                    or occurrence.get("service") != KUBERNETES_SERVICE
                    or not isinstance(source_evidence, dict)
                    or source_evidence.get("source") != "kubernetes"
                    or source_evidence.get("namespace") != LAB_NAMESPACE
                    or not isinstance(expected_occurrence_timestamp, str)
                    or source_evidence.get("source_timestamp")
                    != expected_occurrence_timestamp
                ):
                    raise WorkflowNotStarted(
                        "Persisted Kubernetes evidence is unavailable"
                    )
    else:
        from app.services import incident_repository

        occurrence = incident_repository.get_latest_occurrence_by_incident_id(
            incident_ref
        )
        if occurrence is not None:
            if occurrence["status"] not in {"OPEN", "REMEDIATING"}:
                raise WorkflowNotStarted("Incident occurrence is not active")
            if occurrence_bound:
                try:
                    source_evidence = json.loads(
                        occurrence.get("source_evidence_json") or "null"
                    )
                except (TypeError, json.JSONDecodeError):
                    source_evidence = None
                if (
                    occurrence.get("service") != KUBERNETES_SERVICE
                    or not isinstance(source_evidence, dict)
                    or source_evidence.get("source") != "kubernetes"
                    or source_evidence.get("namespace") != LAB_NAMESPACE
                    or not isinstance(occurrence.get("source_timestamp"), str)
                    or source_evidence.get("source_timestamp")
                    != occurrence.get("source_timestamp")
                ):
                    raise WorkflowNotStarted(
                        "Persisted Kubernetes evidence is unavailable"
                    )
            if not claims.bind_workflow_claim_occurrence(
                workflow_claim_id,
                incident_ref=incident_ref,
                incident_occurrence_id=int(occurrence["id"]),
            ):
                raise WorkflowNotStarted(
                    "Incident occurrence changed before workflow binding"
                )
            occurrence_id = int(occurrence["id"])
            expected_occurrence_service = occurrence["service"]
            expected_incident_type = occurrence["incident_type"]
            expected_fingerprint = occurrence["fingerprint"]
            expected_occurrence_timestamp = occurrence.get("source_timestamp")
    if occurrence_bound and (
        not isinstance(occurrence_id, int)
        or not isinstance(expected_occurrence_timestamp, str)
    ):
        raise WorkflowNotStarted("Persisted Kubernetes occurrence is unavailable")
    try:
        result = await response_graph.ainvoke(
            {"service": service,
             "incident_id": incident_ref,
             "workflow_claim_id": workflow_claim_id,
             "requested_by": requested_by, "attempts": 0,
             "max_attempts": max_attempts_default(), "timeline": [],
             "monitor_handoff": monitor_handoff,
             "occurrence_bound": occurrence_bound,
             "force_approval": force_approval,
             "preferred_action": preferred_action,
             "occurrence_id": occurrence_id,
             "expected_occurrence_service": expected_occurrence_service,
             "expected_incident_type": expected_incident_type,
             "expected_fingerprint": expected_fingerprint,
             "expected_occurrence_timestamp": expected_occurrence_timestamp},
            config={"recursion_limit": 60},
        )
    except Exception as exc:
        persisted_response = _condition_cleared_workflow_response(
            workflow_claim_id
        )
        if persisted_response is not None:
            return persisted_response
        claims.mark_workflow_claim_reconciliation_required(workflow_claim_id)
        raise WorkflowExecutionAmbiguous(
            "Response graph execution outcome is uncertain"
        ) from exc
    response = _summary(result)
    if isinstance(occurrence_id, int) and response.get("status") == "resolved":
        from app.services import incident_repository

        action_claim_id = response.get("action_claim_id")
        if not isinstance(action_claim_id, str) or not (
            incident_repository.finalize_verified_recovery(
                occurrence_id=occurrence_id,
                workflow_claim_id=workflow_claim_id,
                action_claim_id=action_claim_id,
                response=response,
            )
        ):
            claims.mark_workflow_claim_reconciliation_required(
                workflow_claim_id
            )
            raise WorkflowExecutionAmbiguous(
                "Could not atomically persist verified recovery"
            )
        return response
    if not claims.finish_workflow_claim(workflow_claim_id, response):
        persisted_response = _condition_cleared_workflow_response(
            workflow_claim_id
        )
        if persisted_response is not None:
            return persisted_response
        claims.mark_workflow_claim_reconciliation_required(workflow_claim_id)
        raise WorkflowExecutionAmbiguous(
            "Could not persist response workflow outcome"
        )
    return response


async def resume_after_decision(
    approval_id: str, decision: str, decided_by: str, reason: str = ""
) -> dict:
    """Record a human decision and, if approved, continue the workflow."""
    approval = audit.decide_approval(approval_id, decision, decided_by, reason)
    ref = approval["incident_ref"]
    ctx = approval.get("context") or {}

    if decision != "approved":
        contract = _build_contract({
            "incident_id": ref,
            "service": ctx.get("service", KUBERNETES_SERVICE),
            "contract": ctx.get("contract"),
            "approval_id": approval_id,
            "approval_decision": "rejected",
        })
        result = {
            "incident_id": ref,
            "workflow_claim_id": (
                action_claim["workflow_claim_id"]
                if (action_claim := claims.get_action_claim(
                    approval.get("action_claim_id") or ""
                ))
                else None
            ),
            "action_claim_id": approval.get("action_claim_id"),
            "status": "rejected",
            "approval_id": approval_id,
            "contract": contract.model_dump(mode="json"),
        }
        if result["workflow_claim_id"]:
            claims.complete_workflow_claim_after_decision(
                result["workflow_claim_id"], result
            )
            if ctx.get("monitor_handoff") and isinstance(
                ctx.get("occurrence_id"), int
            ):
                from app.services import incident_handoff_repository as handoffs

                handoffs.mark_workflow_decision_complete(
                    ctx["occurrence_id"], result["workflow_claim_id"]
                )
        return result

    action_claim = (
        claims.get_action_claim(approval["action_claim_id"])
        if approval.get("action_claim_id") else None
    )
    workflow_claim_id = (
        action_claim["workflow_claim_id"]
        if action_claim else f"approval-{approval_id}"
    )
    try:
        result = await response_graph.ainvoke(
            {"service": ctx.get("service", KUBERNETES_SERVICE),
             "incident_id": ref, "requested_by": approval["requested_by"],
             "workflow_claim_id": workflow_claim_id,
             "action_claim_id": approval.get("action_claim_id"),
             "proposal": approval["proposed_action"],
             "classification": ctx.get("classification"),
             "runbook_resolution": ctx.get("runbook_resolution"),
             "contract": ctx.get("contract"),
             "approval_id": approval_id, "approval_decision": "approved",
             "approved_by": decided_by, "attempts": ctx.get("attempts", 0),
             "max_attempts": ctx.get("max_attempts", max_attempts_default()),
             "monitor_handoff": ctx.get("monitor_handoff", False),
             "occurrence_bound": ctx.get("occurrence_bound", False),
             "force_approval": ctx.get("force_approval", False),
             "occurrence_id": ctx.get("occurrence_id"),
             "expected_occurrence_service": ctx.get(
                 "expected_occurrence_service"
             ),
             "expected_incident_type": ctx.get("expected_incident_type"),
             "expected_fingerprint": ctx.get("expected_fingerprint"),
             "expected_occurrence_timestamp": ctx.get(
                 "expected_occurrence_timestamp"
             ),
             "timeline": []},
            config={"recursion_limit": 60},
        )
    except Exception:
        persisted_response = _condition_cleared_workflow_response(
            workflow_claim_id
        )
        if persisted_response is not None:
            return persisted_response
        claims.mark_workflow_claim_reconciliation_required(workflow_claim_id)
        if ctx.get("monitor_handoff") and isinstance(
            ctx.get("occurrence_id"), int
        ):
            from app.services import incident_handoff_repository as handoffs

            handoffs.mark_handoff_reconciliation_required(
                ctx["occurrence_id"], workflow_claim_id,
                "approval_resume_ambiguous",
            )
        raise
    response = _summary(result)
    if action_claim:
        if (
            isinstance(ctx.get("occurrence_id"), int)
            and response.get("status") == "resolved"
        ):
            from app.services import incident_repository

            if not (
                isinstance(ctx.get("occurrence_id"), int)
                and incident_repository.finalize_verified_recovery(
                    occurrence_id=ctx["occurrence_id"],
                    workflow_claim_id=action_claim["workflow_claim_id"],
                    action_claim_id=action_claim["action_claim_id"],
                    response=response,
                )
            ):
                claims.mark_workflow_claim_reconciliation_required(
                    action_claim["workflow_claim_id"]
                )
                raise WorkflowExecutionAmbiguous(
                    "Could not atomically persist verified recovery"
                )
            return response
        completed = claims.complete_workflow_claim_after_decision(
            action_claim["workflow_claim_id"], response
        )
        if not completed:
            persisted_response = _condition_cleared_workflow_response(
                action_claim["workflow_claim_id"]
            )
            if persisted_response is not None:
                return persisted_response
        if ctx.get("monitor_handoff") and isinstance(
            ctx.get("occurrence_id"), int
        ):
            from app.services import incident_handoff_repository as handoffs

            handoffs.mark_workflow_decision_complete(
                ctx["occurrence_id"], action_claim["workflow_claim_id"]
            )
    return response