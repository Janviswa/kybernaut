"""LangGraph orchestration for safe, advisory incident response."""

import logging
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.services.incident_diagnoser import diagnose_service
from app.services.k8s_observer import observe_namespace
from app.services.rca_agent import generate_rca


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
    kubernetes: dict[str, Any]
    kubernetes_status: str
    rca: dict[str, Any] | None
    rca_status: str
    policy: dict[str, Any]
    error: str
    rca_error: str


async def detect_and_diagnose(state: IncidentState) -> dict:
    """Collect service and Kubernetes evidence."""
    diagnosis = await diagnose_service(state["service"])

    try:
        kubernetes = await __import__("asyncio").to_thread(
            observe_namespace
        )
        kubernetes_status = "completed"
    except Exception:
        # Preserve service diagnosis if Kubernetes is temporarily unavailable.
        kubernetes = None
        kubernetes_status = "unavailable"

    return {
        "diagnosis": diagnosis,
        "kubernetes": kubernetes,
        "kubernetes_status": kubernetes_status,
    }


async def analyze_root_cause(state: IncidentState) -> dict:
    """Run advisory RCA without allowing provider failures to stop policy."""
    evidence = {
        "service_diagnosis": state["diagnosis"],
        "kubernetes_observations": state.get("kubernetes"),
    }

    try:
        rca = await generate_rca(evidence)
        return {
            "rca": rca,
            "rca_status": "completed",
            "rca_error": "",
        }
    except Exception as exc:
        # Preserve the workflow and let deterministic policy evaluate evidence.
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

    if "service_unavailable" in findings or high_severity_k8s:
        action = "request_approval"
        priority = "critical"
    elif diagnosis.get("status") == "unknown":
        action = "investigate_monitoring"
        priority = "medium"
    elif "http_5xx_errors" in findings:
        action = "investigate_errors"
        priority = "high"
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
        "kubernetes": result.get("kubernetes"),
        "kubernetes_status": result.get("kubernetes_status", "unavailable"),
        "rca": result.get("rca"),
        "rca_status": result.get("rca_status", "unavailable"),
        "rca_error": result.get("rca_error", ""),
        "policy": result["policy"],
    }
