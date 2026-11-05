"""LangGraph orchestration for safe, advisory incident response."""

import asyncio
import logging
import math
from datetime import datetime
from typing import Any, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from app.models.incident_contract import IncidentContract
from app.services import incident_repository
from app.services.incident_diagnoser import diagnose_service
from app.services.incident_classifier import classify_incident
from app.services.incident_contract import build_incident_contract
from app.services.incident_detector import make_incident_id
from app.services.k8s_observer import observe_namespace
from app.services.rca_agent import RCAConfigurationError, generate_rca
from app.services.anomaly_runtime import evaluate_service_anomaly
from app.services.runbook_retriever import retrieve_runbooks

ALLOWED_SERVICES = {
    "order-service",
    "payment-service",
    "user-service",
    "inventory-service",
}

logger = logging.getLogger(__name__)


class IncidentState(TypedDict, total=False):
    incident_id: str
    service: str
    diagnosis: dict[str, Any]
    kubernetes: dict[str, Any] | None
    kubernetes_status: str
    telemetry_anomaly: dict[str, Any]
    rca: dict[str, Any] | None
    rca_status: str
    policy: dict[str, Any]
    classification: dict[str, Any]
    contract: dict[str, Any] | None
    contract_status: str
    contract_error: str
    telemetry_incident_persistence: dict[str, Any]
    error: str
    rca_error: str


class TelemetryIncidentPersistenceError(RuntimeError):
    """Sanitized failure raised when advisory incident persistence fails."""


def _has_timezone_aware_timestamp(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return timestamp.tzinfo is not None and timestamp.utcoffset() is not None


def _valid_service_outage(diagnosis: dict, findings: list[Any]) -> bool:
    if (
        diagnosis.get("status") != "down"
        or diagnosis.get("evidence_status") not in {"available", "unavailable"}
    ):
        return False
    if not any(
        isinstance(item, dict)
        and item.get("type") == "service_unavailable"
        and item.get("severity") == "critical"
        for item in findings
    ):
        return False
    missing = diagnosis.get("missing_evidence")
    if isinstance(missing, list) and "service_availability" in missing:
        return False
    availability = diagnosis.get("evidence_timestamps")
    availability = (
        availability.get("service_availability")
        if isinstance(availability, dict) else None
    )
    return (
        isinstance(availability, dict)
        and availability.get("provenance") == "prometheus_sample_timestamp"
        and _has_timezone_aware_timestamp(availability.get("timestamp"))
    )


def _valid_kubernetes_incident(item: Any, namespace: str) -> bool:
    if not isinstance(item, dict):
        return False
    evidence = item.get("evidence")
    source_timestamp = item.get("source_timestamp")
    return (
        isinstance(item.get("type"), str)
        and bool(item["type"].strip())
        and isinstance(item.get("resource"), str)
        and bool(item["resource"].strip())
        and item.get("resource_kind") in {"Pod", "Deployment"}
        and item.get("severity") in {"low", "medium", "high", "critical"}
        and (
            "namespace" not in item
            or item.get("namespace") == namespace
        )
        and isinstance(evidence, list)
        and any(isinstance(value, str) and value.strip() for value in evidence)
        and (
            source_timestamp in (None, "")
            or _has_timezone_aware_timestamp(source_timestamp)
        )
    )


def _valid_high_kubernetes_finding(
    kubernetes: Any,
    kubernetes_status: str,
    diagnosis: dict,
    telemetry: Any,
) -> bool:
    if (
        kubernetes_status != "completed"
        or not isinstance(kubernetes, dict)
        or not isinstance(kubernetes.get("namespace"), str)
        or not kubernetes["namespace"].strip()
        or not _has_timezone_aware_timestamp(kubernetes.get("observed_at"))
        or not isinstance(kubernetes.get("incidents"), list)
        or not all(
            _valid_kubernetes_incident(item, kubernetes["namespace"])
            for item in kubernetes["incidents"]
        )
    ):
        return False

    classification = classify_incident(diagnosis, kubernetes, telemetry)
    target = classification.get("target")
    selected_evidence = classification.get("evidence")
    if not isinstance(target, dict) or not isinstance(selected_evidence, list):
        return False
    selected_evidence = set(selected_evidence)
    for incident in kubernetes["incidents"]:
        if not isinstance(incident, dict):
            continue
        evidence = incident.get("evidence")
        if not isinstance(evidence, list):
            continue
        evidence = {
            item.strip() for item in evidence
            if isinstance(item, str) and item.strip()
        }
        if (
            incident.get("severity") in {"high", "critical"}
            and incident.get("namespace", kubernetes["namespace"])
            == target.get("namespace")
            and incident.get("resource") == target.get("resource")
            and incident.get("resource_kind") == target.get("resource_kind")
            and incident.get("resource_uid") == target.get("resource_uid")
            and bool(evidence.intersection(selected_evidence))
        ):
            return True
    return False


async def detect_and_diagnose(state: IncidentState) -> dict:
    """Collect service, telemetry-anomaly, and Kubernetes evidence."""
    service = state["service"]
    try:
        diagnosis = await diagnose_service(service)
    except Exception as exc:
        logger.warning(
            "Service diagnosis failed for %s: %s",
            service,
            type(exc).__name__,
        )
        diagnosis = {
            "service": service,
            "status": "unknown",
            "findings": [],
            "reason": "diagnosis_failed",
        }
    if not isinstance(diagnosis, dict):
        logger.warning(
            "Service diagnosis returned an invalid result for %s (%s)",
            service,
            type(diagnosis).__name__,
        )
        diagnosis = {
            "service": service,
            "status": "unknown",
            "evidence_status": "invalid",
            "findings": [],
            "missing_evidence": ["diagnosis_result_invalid"],
            "reason": "invalid_diagnosis_result",
        }
    # Anomaly evaluation is advisory and must not block diagnosis.
    try:
        telemetry_anomaly = await evaluate_service_anomaly(service)
    except Exception as exc:
        logger.warning(
            "Unexpected anomaly evaluation failure for %s (%s)",
            service,
            type(exc).__name__,
        )
        telemetry_anomaly = {
            "status": "unavailable",
            "reason": "evaluation_failed",
        }

    # Preserve service diagnosis if Kubernetes is temporarily unavailable.
    try:
        kubernetes = await asyncio.to_thread(observe_namespace)
        kubernetes_status = "completed"
    except Exception as exc:
        logger.warning(
            "Kubernetes observation unavailable (%s)",
            type(exc).__name__,
        )
        kubernetes = None
        kubernetes_status = "unavailable"

    return {
        "diagnosis": diagnosis,
        "telemetry_anomaly": telemetry_anomaly,
        "kubernetes": kubernetes,
        "kubernetes_status": kubernetes_status,
    }


def validate_contract(state: IncidentState) -> dict:
    """Build a canonical contract without gating the advisory workflow."""
    telemetry = state.get("telemetry_anomaly")
    try:
        persistence = _persist_telemetry_incidents(
            state.get("service", ""), telemetry
        )
    except Exception as exc:
        logger.error(
            "Telemetry incident persistence failed for %s (%s)",
            state.get("service", "unknown"),
            type(exc).__name__,
        )
        raise TelemetryIncidentPersistenceError(
            "Telemetry incident persistence failed"
        ) from exc
    classification = classify_incident(
        state.get("diagnosis"),
        state.get("kubernetes"),
        telemetry,
    )
    state_with_contract = {
        **state,
        "incident_id": state.get("incident_id") or f"INC-{uuid4().hex[:12]}",
        "classification": classification,
    }
    try:
        contract = build_incident_contract(state_with_contract)
    except ValidationError:
        logger.exception("Incident contract validation failed")
        return {
            "incident_id": state_with_contract["incident_id"],
            "classification": classification,
            "telemetry_incident_persistence": persistence,
            "contract": None,
            "contract_status": "invalid",
            "contract_error": "validation_failed",
        }
    return {
        "incident_id": state_with_contract["incident_id"],
        "classification": classification,
        "telemetry_incident_persistence": persistence,
        "contract": contract.model_dump(mode="json"),
        "contract_status": "valid",
        "contract_error": "",
    }


def _persist_telemetry_incidents(
    service: str, telemetry: Any
) -> dict[str, Any]:
    if not isinstance(telemetry, dict) or telemetry.get("status") != "evaluated":
        return {
            "status": "skipped",
            "reason": "evaluation_unavailable",
            "incident_ids": [],
        }

    observed_at = telemetry.get("observed_at")
    if not isinstance(observed_at, str):
        return {
            "status": "skipped",
            "reason": "observation_timestamp_unavailable",
            "incident_ids": [],
        }
    try:
        parsed_timestamp = datetime.fromisoformat(
            observed_at.replace("Z", "+00:00")
        )
    except ValueError:
        parsed_timestamp = None
    if (
        parsed_timestamp is None
        or parsed_timestamp.tzinfo is None
        or parsed_timestamp.utcoffset() is None
    ):
        return {
            "status": "skipped",
            "reason": "invalid_observation_timestamp",
            "incident_ids": [],
        }

    actionable = telemetry.get("actionable_candidates")
    if not isinstance(actionable, list) or not all(
        isinstance(feature, str) for feature in actionable
    ):
        return {
            "status": "skipped",
            "reason": "invalid_anomaly_candidates",
            "incident_ids": [],
        }

    feature_categories = {
        "cpu_cores_used": ("cpu_spike", "CPU cores"),
        "memory_bytes": ("memory_leak", "bytes"),
    }
    supported_candidates = set(actionable).intersection(feature_categories)
    feature_results = telemetry.get("features")
    feature_results = feature_results if isinstance(feature_results, dict) else {}
    sample_timestamps = telemetry.get("sample_timestamps")
    sample_timestamps = (
        sample_timestamps if isinstance(sample_timestamps, dict) else {}
    )

    records = []
    persisted_candidates = set()
    for feature in sorted(supported_candidates):
        detail = feature_results.get(feature)
        if not isinstance(detail, dict):
            continue
        value = detail.get("value")
        baseline = detail.get("baseline_median")
        deviation = detail.get("deviation_score")
        threshold = telemetry.get("threshold")
        numeric_values = (value, baseline, deviation, threshold)
        if any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(item)
            for item in numeric_values
        ):
            continue

        source_timestamp = sample_timestamps.get(feature, observed_at)
        if not isinstance(source_timestamp, str):
            continue
        try:
            parsed_source_timestamp = datetime.fromisoformat(
                source_timestamp.replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if (
            parsed_source_timestamp.tzinfo is None
            or parsed_source_timestamp.utcoffset() is None
        ):
            continue

        category, unit = feature_categories[feature]
        feature_telemetry = {
            **telemetry,
            "actionable_candidates": [feature],
        }
        classification = classify_incident(
            None, None, feature_telemetry
        )
        if classification["incident_type"] != category:
            continue
        fingerprint = f"prometheus:{service}:{category}"
        message = (
            f"Prometheus anomaly: {feature}={value} {unit}; "
            f"baseline_median={baseline} {unit}; "
            f"deviation_score={deviation}; threshold={threshold}; "
            f"confidence={classification['confidence']}; "
            f"observed_at={source_timestamp}"
        )
        record = incident_repository.record_incident({
            "incident_id": make_incident_id(service, category),
            "service": service,
            "type": category,
            "severity": classification["severity"],
            "message": message,
            "fingerprint": fingerprint,
            "source_timestamp": source_timestamp,
            "source_evidence": {
                "schema_version": 1,
                "source": "prometheus",
                "service": service,
                "incident_type": category,
                "observed_at": observed_at,
                "source_timestamp": source_timestamp,
                "metric": {
                    "name": feature,
                    "value": value,
                    "unit": unit,
                    "baseline_median": baseline,
                    "deviation_score": deviation,
                    "threshold": threshold,
                    "direction": detail.get("direction"),
                    "classification": detail.get("classification"),
                },
            },
        }, enqueue_handoff=True)
        records.append(record)
        persisted_candidates.add(feature)

    all_candidates_supported = set(actionable).issubset(feature_categories)
    can_reconcile = (
        all_candidates_supported
        and persisted_candidates == supported_candidates
    )
    if can_reconcile:
        active_categories = {
            feature_categories[feature][0] for feature in persisted_candidates
        }
        for category, _ in feature_categories.values():
            if category not in active_categories:
                incident_repository.resolve_incident_fingerprint(
                    f"prometheus:{service}:{category}"
                )

    if records:
        status = "persisted"
    elif can_reconcile:
        status = "healthy"
    else:
        status = "no_supported_anomaly"
    return {
        "status": status,
        "reason": None if can_reconcile else "candidate_not_persisted",
        "incident_ids": [record["incident_id"] for record in records],
        "occurrences": [
            {
                "incident_row_id": int(record["id"]),
                "incident_id": record["incident_id"],
                "incident_type": record["incident_type"],
                "fingerprint": record["fingerprint"],
            }
            for record in records
        ],
    }


def attach_policy_to_contract(state: IncidentState) -> dict:
    contract = state.get("contract")
    if not isinstance(contract, dict):
        return {}
    try:
        validated = IncidentContract.model_validate({
            **contract,
            "policy_decision": state.get("policy"),
        })
    except ValidationError:
        logger.exception("Incident contract policy update failed")
        return {
            "contract": None,
            "contract_status": "invalid",
            "contract_error": "policy_contract_validation_failed",
        }
    return {
        "contract": validated.model_dump(mode="json"),
        "contract_status": "valid",
    }


async def analyze_root_cause(state: IncidentState) -> dict:
    """Run advisory RCA using evidence and optional runbook guidance."""
    evidence = {
        "service_diagnosis": state["diagnosis"],
        "kubernetes_observations": state.get("kubernetes"),
        "telemetry_anomaly": state.get("telemetry_anomaly"),
    }

    retrieval_query = str({
        "diagnosis": state["diagnosis"],
        "telemetry_anomaly": state.get("telemetry_anomaly"),
        "kubernetes": state.get("kubernetes"),
    })

    # A caller may supply matches retrieved from the incident's original
    # evidence; otherwise preserve the existing fresh-workflow behavior.
    supplied_runbooks = state.get("runbook_guidance")
    runbooks = supplied_runbooks if isinstance(supplied_runbooks, list) else None

    # Retrieval is helpful, but RCA can continue without it.
    try:
        if runbooks is None:
            runbooks = await asyncio.to_thread(
                retrieve_runbooks,
                retrieval_query,
                2,
            )
        evidence["runbook_guidance"] = [
            {
                "source": item["source"],
                "content": item["content"],
                "matched_terms": item["matched_terms"],
            }
            for item in runbooks
        ]
    except Exception as exc:
        logger.warning(
            "Runbook retrieval unavailable (%s); continuing without guidance",
            type(exc).__name__,
        )
        evidence["runbook_guidance"] = []

    # Keep LLM failure handling separate from retrieval failure handling.
    try:
        rca = await generate_rca(evidence)
        return {
            "rca": rca,
            "rca_status": "completed",
            "rca_error": "",
        }
    except ValidationError:
        logger.warning("RCA returned invalid structured output")
        return {
            "rca": None,
            "rca_status": "invalid",
            "rca_error": "invalid_structured_output",
        }
    except RCAConfigurationError as exc:
        logger.warning("RCA configuration unavailable (%s)", exc.code)
        return {
            "rca": None,
            "rca_status": "unavailable",
            "rca_error": exc.code,
        }
    except Exception as exc:
        error_name = type(exc).__name__
        error_code = {
            "APITimeoutError": "provider_timeout",
            "TimeoutError": "provider_timeout",
            "APIConnectionError": "provider_connection_error",
            "ConnectError": "provider_connection_error",
            "RateLimitError": "provider_rate_limited",
            "InternalServerError": "provider_server_error",
            "ModuleNotFoundError": "provider_unavailable",
        }.get(error_name, "provider_error")
        logger.warning(
            "RCA unavailable; continuing with evidence-based policy (%s)",
            error_code,
        )
        return {
            "rca": None,
            "rca_status": "unavailable",
            "rca_error": error_code,
        }


def enforce_policy(state: IncidentState) -> dict:
    """Make deterministic policy decisions; never execute remediation."""
    raw_diagnosis = state.get("diagnosis")
    diagnosis_valid = isinstance(raw_diagnosis, dict)
    diagnosis = raw_diagnosis if diagnosis_valid else {
        "status": "unknown",
        "evidence_status": "invalid",
        "findings": [],
        "missing_evidence": ["diagnosis_result_invalid"],
    }
    raw_findings = diagnosis.get("findings")
    if not isinstance(raw_findings, list):
        raw_findings = []

    malformed_findings = (
        "findings" not in diagnosis
        or not isinstance(diagnosis.get("findings"), list)
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("type"), str)
            for item in raw_findings
        )
    )
    findings = {
        item.get("type")
        for item in raw_findings
        if isinstance(item, dict) and isinstance(item.get("type"), str)
    }
    missing_evidence = diagnosis.get("missing_evidence")
    diagnosis_incomplete = (
        not diagnosis_valid
        or malformed_findings
        or diagnosis.get("evidence_status") != "available"
        or (
            isinstance(missing_evidence, list)
            and (
                bool(missing_evidence)
                or any(not isinstance(item, str) for item in missing_evidence)
            )
        )
        or (
            missing_evidence is not None
            and not isinstance(missing_evidence, list)
        )
    )
    contract = state.get("contract")
    contract_sources = contract.get("evidence_sources") if isinstance(
        contract, dict
    ) else []
    contract_evidence = (
        contract.get("evidence") if isinstance(contract, dict) else []
    )
    contract_sources = (
        contract_sources if isinstance(contract_sources, list) else []
    )
    contract_evidence = (
        contract_evidence if isinstance(contract_evidence, list) else []
    )
    contract_incomplete = state.get("contract_status") == "invalid"
    contract_sources_incomplete = any(
        isinstance(item, dict)
        and item.get("status") in {"unavailable", "stale", "invalid"}
        for item in contract_sources + contract_evidence
    )

    kubernetes = state.get("kubernetes")
    kubernetes_valid = (
        state.get("kubernetes_status") == "completed"
        and isinstance(kubernetes, dict)
        and isinstance(kubernetes.get("namespace"), str)
        and bool(kubernetes["namespace"].strip())
        and _has_timezone_aware_timestamp(kubernetes.get("observed_at"))
        and isinstance(kubernetes.get("incidents"), list)
        and all(
            _valid_kubernetes_incident(item, kubernetes["namespace"])
            for item in kubernetes.get("incidents", [])
        )
    )
    kubernetes_incomplete = (
        not kubernetes_valid
    )
    k8s_incidents = (
        kubernetes.get("incidents", [])
        if kubernetes_valid else []
    )

    high_severity_k8s = _valid_high_kubernetes_finding(
        kubernetes,
        state.get("kubernetes_status", "unavailable"),
        diagnosis,
        state.get("telemetry_anomaly"),
    )
    raw_telemetry = state.get("telemetry_anomaly")
    telemetry_valid = (
        isinstance(raw_telemetry, dict)
        and raw_telemetry.get("status") == "evaluated"
        and _has_timezone_aware_timestamp(raw_telemetry.get("observed_at"))
        and isinstance(raw_telemetry.get("actionable_candidates"), list)
        and all(
            isinstance(item, str)
            for item in raw_telemetry["actionable_candidates"]
        )
        and (
            "operational_findings" not in raw_telemetry
            or (
                isinstance(raw_telemetry["operational_findings"], list)
                and all(
                    isinstance(item, dict)
                    and isinstance(item.get("type"), str)
                    for item in raw_telemetry["operational_findings"]
                )
            )
        )
    )
    telemetry_incomplete = not telemetry_valid
    telemetry = raw_telemetry if isinstance(raw_telemetry, dict) else {}

    operational_findings = telemetry.get("operational_findings", [])
    valid_service_outage = _valid_service_outage(diagnosis, raw_findings)
    untrusted_high_k8s = any(
        item.get("severity") in {"high", "critical"}
        for item in k8s_incidents
    ) and not high_severity_k8s

    if valid_service_outage or high_severity_k8s:
        action = "request_approval"
        priority = "critical"
        reason = (
            "Fresh evidence confirms a supported outage or high-severity "
            "Kubernetes incident; human approval is required before action."
        )
    elif (
        diagnosis_incomplete
        or contract_incomplete
        or contract_sources_incomplete
        or kubernetes_incomplete
        or telemetry_incomplete
        or untrusted_high_k8s
        or "monitoring_data_unavailable" in findings
    ):
        action = "investigate_monitoring"
        priority = "medium"
        reason = (
            "Service availability is unknown or required monitoring evidence "
            "is unavailable; confirm fresh monitoring evidence before "
            "considering remediation."
            if diagnosis.get("status") == "unknown"
            or "monitoring_data_unavailable" in findings
            else
            "Required diagnosis, telemetry, contract, or Kubernetes evidence "
            "is incomplete or invalid; continue investigating before "
            "considering remediation."
        )
    elif "http_5xx_errors" in findings:
        action = "investigate_errors"
        priority = "high"
        reason = (
            "HTTP 5xx errors were observed; investigate the affected requests "
            "before considering remediation."
        )
    elif (
        telemetry.get("status") == "evaluated"
        and isinstance(operational_findings, list)
        and any(
            item.get("type") == "high_latency"
            for item in operational_findings
            if isinstance(item, dict)
        )
    ):
        action = "investigate_latency"
        priority = "medium"
        reason = (
            "Fresh evaluated telemetry reports high latency without a "
            "qualifying outage; continue investigation."
        )
    elif diagnosis.get("status") == "unknown":
        action = "investigate_monitoring"
        priority = "medium"
        reason = (
            "Service availability is unknown; confirm fresh monitoring "
            "evidence before considering remediation."
        )
    elif "high_latency" in findings:
        action = "investigate_latency"
        priority = "medium"
        reason = (
            "Diagnosis indicates high latency; investigate further without "
            "executing remediation."
        )
    else:
        action = "continue_monitoring"
        priority = "low"
        reason = "No configured intervention threshold is met; continue monitoring."

    return {
        "policy": {
            "priority": priority,
            "action": action,
            "reason": reason,
            "requires_human_approval": action == "request_approval",
            "automatic_execution": False,
            "remediation_executed": False,
        }
    }


def build_incident_graph():
    """Build and compile the incident-response state graph."""
    graph = StateGraph(IncidentState)

    graph.add_node("diagnose", detect_and_diagnose)
    graph.add_node("validate_contract", validate_contract)
    graph.add_node("root_cause_analysis", analyze_root_cause)
    graph.add_node("policy", enforce_policy)
    graph.add_node("attach_policy", attach_policy_to_contract)

    graph.add_edge(START, "diagnose")
    graph.add_edge("diagnose", "validate_contract")
    graph.add_edge("validate_contract", "root_cause_analysis")
    graph.add_edge("root_cause_analysis", "policy")
    graph.add_edge("policy", "attach_policy")
    graph.add_edge("attach_policy", END)

    return graph.compile()


incident_graph = build_incident_graph()


async def run_incident_workflow(service: str) -> dict:
    """Run the advisory workflow for an allowlisted service."""
    if service not in ALLOWED_SERVICES:
        raise ValueError("Unsupported service")

    result = await incident_graph.ainvoke({
        "service": service,
        "incident_id": f"INC-{uuid4().hex[:12]}",
    })

    return {
        "service": service,
        "diagnosis": result["diagnosis"],
        "telemetry_anomaly": result.get(
            "telemetry_anomaly",
            {
                "status": "unavailable",
                "reason": "not_evaluated",
            },
        ),
        "telemetry_incident_persistence": result.get(
            "telemetry_incident_persistence",
            {"status": "skipped", "reason": "not_evaluated",
             "incident_ids": []},
        ),
        "kubernetes": result.get("kubernetes"),
        "kubernetes_status": result.get(
            "kubernetes_status", "unavailable"
        ),
        "rca": result.get("rca"),
        "rca_status": result.get("rca_status", "unavailable"),
        "rca_error": result.get("rca_error", ""),
        "policy": result["policy"],
        "contract": result.get("contract"),
        "contract_status": result.get("contract_status", "invalid"),
        "contract_error": result.get("contract_error", ""),
    }
