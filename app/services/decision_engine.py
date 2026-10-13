
from app.services.incident_diagnoser import diagnose_service


async def decide_action(service: str) -> dict:
    diagnosis = await diagnose_service(service)

    findings = diagnosis["findings"]
    finding_types = {item["type"] for item in findings}
    service_status = diagnosis["status"]

    if service_status == "unknown":
        decision = {
            "priority": "medium",
            "action": "investigate_monitoring",
            "reason": (
                f"Availability of {service} could not be confirmed "
                "because Prometheus returned no availability sample."
            ),
            "proposed_steps": [
                "Check the Prometheus target state",
                "Verify metric collection and scrape configuration",
                "Confirm application health before considering recovery",
            ],
            "automatic_execution": False,
        }

    elif "service_unavailable" in finding_types:
        decision = {
            "priority": "critical",
            "action": "request_approval",
            "reason": (
                f"{service} is reported down. "
                "Investigate container status and logs before recovery."
            ),
            "proposed_steps": [
                "Inspect Docker container status",
                "Review recent application logs",
                "Request approval before restarting the service",
            ],
            "automatic_execution": False,
        }

    elif "http_5xx_errors" in finding_types:
        decision = {
            "priority": "high",
            "action": "investigate_errors",
            "reason": (
                f"{service} is returning HTTP 5xx errors "
                "while the service remains reachable."
            ),
            "proposed_steps": [
                "Inspect application logs",
                "Identify the affected endpoint",
                "Check recent code or configuration changes",
            ],
            "automatic_execution": False,
        }

    elif "high_latency" in finding_types:
        decision = {
            "priority": "medium",
            "action": "investigate_latency",
            "reason": (
                f"{service} has elevated P95 request latency."
            ),
            "proposed_steps": [
                "Inspect slow endpoints",
                "Review resource usage",
                "Investigate database or dependency delays",
            ],
            "automatic_execution": False,
        }

    else:
        decision = {
            "priority": "low",
            "action": "continue_monitoring",
            "reason": "No configured anomaly rule requires intervention.",
            "proposed_steps": [
                "Continue collecting metrics",
                "Reassess if an anomaly appears",
            ],
            "automatic_execution": False,
        }

    return {
        "service": service,
        "diagnosis": diagnosis,
        "decision": decision,
    }
