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
