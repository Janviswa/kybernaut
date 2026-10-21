"""LangGraph orchestration for safe, advisory incident response."""

import asyncio
import logging
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.services.incident_diagnoser import diagnose_service
from app.services.k8s_observer import observe_namespace
from app.services.rca_agent import generate_rca
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
    service: str
    diagnosis: dict[str, Any]
    kubernetes: dict[str, Any] | None
    kubernetes_status: str
    telemetry_anomaly: dict[str, Any]
    rca: dict[str, Any] | None
    rca_status: str
    policy: dict[str, Any]
    error: str
    rca_error: str


async def detect_and_diagnose(state: IncidentState) -> dict:
    """Collect service, telemetry-anomaly, and Kubernetes evidence."""
    service = state["service"]
    diagnosis = await diagnose_service(service)

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

    # Retrieval is helpful, but RCA can continue without it.
    try:
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
    except Exception as exc:
        logger.warning(
            "RCA unavailable; continuing with evidence-based policy (%s)",
            type(exc).__name__,
        )
        return {
            "rca": None,
            "rca_status": "unavailable",
            "rca_error": type(exc).__name__,
        }


def enforce_policy(state: IncidentState) -> dict:
    """Make deterministic policy decisions; never execute remediation."""
    diagnosis = state["diagnosis"]
    findings = {
        item.get("type") for item in diagnosis.get("findings", [])
    }

    kubernetes = state.get("kubernetes") or {}
    k8s_incidents = kubernetes.get("incidents", [])

    high_severity_k8s = any(
        item.get("severity") == "high" for item in k8s_incidents
    )

    telemetry = state.get("telemetry_anomaly") or {}
    operational_findings = telemetry.get("operational_findings", [])

    if "service_unavailable" in findings or high_severity_k8s:
        action = "request_approval"
        priority = "critical"
    elif "http_5xx_errors" in findings:
        action = "investigate_errors"
        priority = "high"
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
    elif diagnosis.get("status") == "unknown":
        action = "investigate_monitoring"
        priority = "medium"
    elif "high_latency" in findings:
        action = "investigate_latency"
        priority = "medium"
    else:
        action = "continue_monitoring"
        priority = "low"

    rca = state.get("rca")
    if rca and rca.get("requires_human_approval"):
        action = "request_approval"

    return {
        "policy": {
            "priority": priority,
            "action": action,
            "requires_human_approval": action == "request_approval",
            "automatic_execution": False,
            "remediation_executed": False,
        }
    }


def build_incident_graph():
    """Build and compile the incident-response state graph."""
    graph = StateGraph(IncidentState)

    graph.add_node("diagnose", detect_and_diagnose)
    graph.add_node("root_cause_analysis", analyze_root_cause)
    graph.add_node("policy", enforce_policy)

    graph.add_edge(START, "diagnose")
    graph.add_edge("diagnose", "root_cause_analysis")
    graph.add_edge("root_cause_analysis", "policy")
    graph.add_edge("policy", END)

    return graph.compile()


incident_graph = build_incident_graph()


async def run_incident_workflow(service: str) -> dict:
    """Run the advisory workflow for an allowlisted service."""
    if service not in ALLOWED_SERVICES:
        raise ValueError("Unsupported service")

    result = await incident_graph.ainvoke({"service": service})

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
        "kubernetes": result.get("kubernetes"),
        "kubernetes_status": result.get(
            "kubernetes_status", "unavailable"
        ),
        "rca": result.get("rca"),
        "rca_status": result.get("rca_status", "unavailable"),
        "rca_error": result.get("rca_error", ""),
        "policy": result["policy"],
    }
