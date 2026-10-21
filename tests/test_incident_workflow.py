import pytest

from app.services import incident_workflow as workflow

def test_unknown_monitoring_data_requires_investigation():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "unknown",
            "findings": [
                {"type": "monitoring_data_unavailable"}
            ],
        },
        "rca": None,
    })

    assert result["policy"]["action"] == "investigate_monitoring"
    assert result["policy"]["automatic_execution"] is False

def test_service_outage_requires_approval():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "down",
            "findings": [{"type": "service_unavailable"}],
        },
        "rca": None,
    })

    assert result["policy"]["action"] == "request_approval"
    assert result["policy"]["requires_human_approval"] is True
    assert result["policy"]["remediation_executed"] is False

def test_rca_cannot_enable_automatic_execution():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "up",
            "findings": [{"type": "no_detected_anomaly"}],
        },
        "rca": {
            "requires_human_approval": False,
            "recommended_action": "continue_monitoring",
        },
    })

    assert result["policy"]["automatic_execution"] is False
    assert result["policy"]["remediation_executed"] is False

@pytest.mark.asyncio
async def test_workflow_rejects_unknown_service():
    with pytest.raises(ValueError, match="Unsupported service"):
        await workflow.run_incident_workflow("unknown-service")

@pytest.mark.asyncio
async def test_workflow_runs_without_groq(monkeypatch):
    async def fake_diagnosis(service):
        return {
            "service": service,
            "status": "up",
            "findings": [{"type": "no_detected_anomaly"}],
        }

    async def unavailable_rca(diagnosis):
        raise RuntimeError(
            "GROQ_API_KEY is not configured; RCA generation is unavailable."
        )

    monkeypatch.setattr(workflow, "diagnose_service", fake_diagnosis)
    monkeypatch.setattr(workflow, "generate_rca", unavailable_rca)

    result = await workflow.run_incident_workflow("order-service")

    assert result["rca"] is None
    assert result["rca_status"] == "unavailable"
    assert result["policy"]["action"] == "continue_monitoring"
    assert result["policy"]["automatic_execution"] is False

def test_kubernetes_high_severity_requires_approval():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "up",
            "findings": [{"type": "no_detected_anomaly"}],
        },
        "kubernetes": {
            "incidents": [
                {"type": "pod_container_failure", "severity": "high"}
            ]
        },
        "rca": None,
    })

    assert result["policy"]["action"] == "request_approval"
    assert result["policy"]["priority"] == "critical"
    assert result["policy"]["requires_human_approval"] is True
    assert result["policy"]["automatic_execution"] is False
    assert result["policy"]["remediation_executed"] is False

def test_healthy_kubernetes_does_not_force_approval():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "up",
            "findings": [{"type": "no_detected_anomaly"}],
        },
        "kubernetes": {"incidents": []},
        "rca": None,
    })

    assert result["policy"]["action"] == "continue_monitoring"
    assert result["policy"]["automatic_execution"] is False

@pytest.mark.asyncio
async def test_workflow_preserves_kubernetes_success_status(monkeypatch):
    async def fake_diagnosis(service):
        return {
            "service": service,
            "status": "up",
            "findings": [{"type": "no_detected_anomaly"}],
        }

    def fake_observer():
        return {
            "namespace": "kybernaut-lab",
            "incident_count": 0,
            "incidents": [],
            "deployments": [
                {
                    "name": "demo-web",
                    "desired_replicas": 2,
                    "available_replicas": 2,
                    "ready_replicas": 2,
                }
            ],
        }

    async def unavailable_rca(diagnosis):
        raise RuntimeError(
            "GROQ_API_KEY is not configured; RCA generation is unavailable."
        )

    monkeypatch.setattr(workflow, "diagnose_service", fake_diagnosis)
    monkeypatch.setattr(workflow, "observe_namespace", fake_observer)
    monkeypatch.setattr(workflow, "generate_rca", unavailable_rca)

    result = await workflow.run_incident_workflow("order-service")

    assert result["kubernetes_status"] == "completed"
    assert result["kubernetes"]["incident_count"] == 0
    assert result["rca_status"] == "unavailable"
    assert result["policy"]["automatic_execution"] is False

@pytest.mark.asyncio
async def test_workflow_reports_kubernetes_unavailable(monkeypatch):
    async def fake_diagnosis(service):
        return {
            "service": service,
            "status": "up",
            "findings": [{"type": "no_detected_anomaly"}],
        }

    def failed_observer():
        raise RuntimeError("simulated Kubernetes access failure")

    async def unavailable_rca(diagnosis):
        raise RuntimeError(
            "GROQ_API_KEY is not configured; RCA generation is unavailable."
        )

    monkeypatch.setattr(workflow, "diagnose_service", fake_diagnosis)
    monkeypatch.setattr(workflow, "observe_namespace", failed_observer)
    monkeypatch.setattr(workflow, "generate_rca", unavailable_rca)

    result = await workflow.run_incident_workflow("order-service")

    assert result["kubernetes_status"] == "unavailable"
    assert result["kubernetes"] is None
    assert result["policy"]["automatic_execution"] is False
    assert result["policy"]["remediation_executed"] is False

@pytest.mark.asyncio
async def test_malformed_rca_does_not_stop_policy_enforcement(monkeypatch):
    """An LLM parsing/provider error must not abort the workflow."""

    async def fake_diagnosis(service):
        return {
            "service": service,
            "status": "up",
            "findings": [{"type": "no_detected_anomaly"}],
        }

    def fake_observer():
        return {
            "namespace": "kybernaut-lab",
            "incident_count": 1,
            "incidents": [
                {
                    "type": "pod_container_failure",
                    "severity": "high",
                    "resource": "demo-web-test",
                }
            ],
            "deployments": [],
        }

    async def malformed_rca(diagnosis):
        raise ValueError("Simulated malformed structured LLM output")

    monkeypatch.setattr(workflow, "diagnose_service", fake_diagnosis)
    monkeypatch.setattr(workflow, "observe_namespace", fake_observer)
    monkeypatch.setattr(workflow, "generate_rca", malformed_rca)

    result = await workflow.run_incident_workflow("order-service")

    assert result["rca"] is None
    assert result["rca_status"] == "unavailable"
    assert result["rca_error"] == "ValueError"
    assert result["policy"]["action"] == "request_approval"
    assert result["policy"]["priority"] == "critical"
    assert result["policy"]["requires_human_approval"] is True
    assert result["policy"]["automatic_execution"] is False
    assert result["policy"]["remediation_executed"] is False

@pytest.mark.asyncio
async def test_rca_provider_error_does_not_escalate_healthy_system(monkeypatch):
    """An RCA error alone must not turn a healthy system into an incident."""

    async def fake_diagnosis(service):
        return {
            "service": service,
            "status": "up",
            "findings": [{"type": "no_detected_anomaly"}],
        }

    def fake_observer():
        return {
            "namespace": "kybernaut-lab",
            "incident_count": 0,
            "incidents": [],
            "deployments": [],
        }

    async def failed_rca(diagnosis):
        raise RuntimeError("Simulated provider failure")

    monkeypatch.setattr(workflow, "diagnose_service", fake_diagnosis)
    monkeypatch.setattr(workflow, "observe_namespace", fake_observer)
    monkeypatch.setattr(workflow, "generate_rca", failed_rca)

    result = await workflow.run_incident_workflow("order-service")

    assert result["rca_status"] == "unavailable"
    assert result["policy"]["action"] == "continue_monitoring"
    assert result["policy"]["automatic_execution"] is False
    assert result["policy"]["remediation_executed"] is False

def test_statistical_memory_candidate_does_not_escalate_without_operational_rule():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "up",
            "findings": [{"type": "no_detected_anomaly"}],
        },
        "telemetry_anomaly": {
            "status": "evaluated",
            "actionable_candidates": ["memory_bytes"],
            "informational_deviations": [],
            "operational_findings": [],
        },
        "rca": None,
    })

    policy = result["policy"]
    assert policy["action"] == "continue_monitoring"
    assert policy["priority"] == "low"
    assert policy["automatic_execution"] is False
    assert policy["remediation_executed"] is False

def test_informational_telemetry_does_not_escalate():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "up",
            "findings": [{"type": "no_detected_anomaly"}],
        },
        "telemetry_anomaly": {
            "status": "evaluated",
            "actionable_candidates": [],
            "informational_deviations": ["cpu_cores_used"],
        },
        "rca": None,
    })

    assert result["policy"]["action"] == "continue_monitoring"
    assert result["policy"]["priority"] == "low"

def test_unavailable_telemetry_does_not_escalate():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "up",
            "findings": [{"type": "no_detected_anomaly"}],
        },
        "telemetry_anomaly": {
            "status": "unavailable",
            "reason": "incomplete_telemetry",
        },
        "rca": None,
    })

    assert result["policy"]["action"] == "continue_monitoring"
    assert result["policy"]["automatic_execution"] is False

def test_statistical_memory_candidate_does_not_escalate_without_operational_rule():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "up",
            "findings": [{"type": "no_detected_anomaly"}],
        },
        "telemetry_anomaly": {
            "status": "evaluated",
            "actionable_candidates": ["memory_bytes"],
            "operational_findings": [],
        },
        "rca": None,
    })

    policy = result["policy"]
    assert policy["action"] == "continue_monitoring"
    assert policy["priority"] == "low"
    assert policy["automatic_execution"] is False
    assert policy["remediation_executed"] is False

def test_critical_outage_overrides_actionable_telemetry():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "down",
            "findings": [{"type": "service_unavailable"}],
        },
        "telemetry_anomaly": {
            "status": "evaluated",
            "actionable_candidates": ["memory_bytes"],
        },
        "rca": None,
    })

    assert result["policy"]["action"] == "request_approval"
    assert result["policy"]["priority"] == "critical"
    assert result["policy"]["requires_human_approval"] is True
    assert result["policy"]["automatic_execution"] is False
    assert result["policy"]["remediation_executed"] is False

@pytest.mark.asyncio
async def test_runbook_retrieval_failure_does_not_block_rca(monkeypatch):
    """RCA should still run when optional runbook retrieval fails."""

    # retrieve_runbooks is called through asyncio.to_thread, so this
    # replacement must be synchronous rather than an async coroutine.
    def failed_retrieval(*args, **kwargs):
        raise RuntimeError("Synthetic runbook retrieval failure")

    async def successful_rca(evidence):
        assert "service_diagnosis" in evidence
        assert "runbook_guidance" in evidence
        assert evidence["runbook_guidance"] == []

        return {
            "root_cause": "No confirmed failure; continue monitoring.",
            "confidence": 0.8,
            "evidence": ["Service diagnosis reports healthy status."],
            "recommended_action": "continue_monitoring",
            "requires_human_approval": False,
        }

    async def fake_diagnosis(service):
        return {
            "service": service,
            "status": "up",
            "findings": [{"type": "no_detected_anomaly"}],
        }

    monkeypatch.setattr(workflow, "retrieve_runbooks", failed_retrieval)
    monkeypatch.setattr(workflow, "generate_rca", successful_rca)
    monkeypatch.setattr(workflow, "diagnose_service", fake_diagnosis)

    result = await workflow.run_incident_workflow("payment-service")

    assert result["rca_status"] == "completed"
    assert result["rca"] is not None
    assert result["policy"]["automatic_execution"] is False
    assert result["policy"]["remediation_executed"] is False

def test_operational_latency_finding_triggers_investigation():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "up",
            "findings": [{"type": "no_detected_anomaly"}],
        },
        "telemetry_anomaly": {
            "status": "evaluated",
            "actionable_candidates": [],
            "informational_deviations": [],
            "operational_findings": [{
                "type": "high_latency",
                "severity": "warning",
                "value": 1.2,
                "threshold": 1.0,
            }],
        },
        "rca": None,
    })

    policy = result["policy"]
    assert policy["action"] == "investigate_latency"
    assert policy["priority"] == "medium"
    assert policy["automatic_execution"] is False
    assert policy["remediation_executed"] is False

def test_statistical_latency_below_operational_threshold_does_not_escalate():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "up",
            "findings": [{"type": "no_detected_anomaly"}],
        },
        "telemetry_anomaly": {
            "status": "evaluated",
            "actionable_candidates": ["p95_latency_seconds"],
            "informational_deviations": [],
            "operational_findings": [],
            "features": {
                "p95_latency_seconds": {
                    "value": 0.975,
                    "baseline_median": 0.0024193609,
                    "direction": "HIGH",
                    "deviation_score": 352515.37,
                    "classification": "actionable_candidate",
                }
            },
        },
        "rca": None,
    })

    policy = result["policy"]
    assert policy["action"] == "continue_monitoring"
    assert policy["priority"] == "low"
    assert policy["automatic_execution"] is False
    assert policy["remediation_executed"] is False
