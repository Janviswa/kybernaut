import pytest
from pydantic import ValidationError

import app.database as database
from app.services import incident_workflow as workflow
from app.services.rca_agent import RCAConfigurationError

POLICY_OBSERVED_AT = "2026-10-22T10:00:00+00:00"


@pytest.fixture(autouse=True)
def initialized_isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "workflow.db")
    database.initialize_database()


def _high_kubernetes_observation(severity="high"):
    return {
        "observed_at": POLICY_OBSERVED_AT,
        "namespace": "kybernaut-lab",
        "incidents": [{
            "type": "pod_container_failure",
            "severity": severity,
            "resource": "demo-web-pod-1",
            "resource_kind": "Pod",
            "resource_uid": "pod-uid-1",
            "source_timestamp": POLICY_OBSERVED_AT,
            "evidence": ["Container waiting: CrashLoopBackOff"],
        }],
        "deployments": [{"name": "demo-web"}],
    }


def _enforce_policy_with_complete_fixture(state):
    complete = dict(state)
    diagnosis = complete.get("diagnosis")
    if isinstance(diagnosis, dict):
        diagnosis = dict(diagnosis)
        diagnosis.setdefault("evidence_status", "available")
        diagnosis.setdefault("missing_evidence", [])
        if (
            diagnosis.get("status") == "down"
            and any(
                isinstance(item, dict)
                and item.get("type") == "service_unavailable"
                for item in diagnosis.get("findings", [])
            )
        ):
            diagnosis.setdefault("evidence_timestamps", {
                "service_availability": {
                    "timestamp": POLICY_OBSERVED_AT,
                    "provenance": "prometheus_sample_timestamp",
                },
            })
        complete["diagnosis"] = diagnosis

    telemetry = complete.get("telemetry_anomaly")
    if telemetry is None:
        complete["telemetry_anomaly"] = {
            "status": "evaluated",
            "observed_at": POLICY_OBSERVED_AT,
            "actionable_candidates": [],
        }
    elif isinstance(telemetry, dict) and telemetry.get("status") == "evaluated":
        telemetry = dict(telemetry)
        telemetry.setdefault("observed_at", POLICY_OBSERVED_AT)
        telemetry.setdefault("actionable_candidates", [])
        complete["telemetry_anomaly"] = telemetry

    kubernetes = complete.get("kubernetes")
    if kubernetes is None:
        kubernetes = {"incidents": []}
    if isinstance(kubernetes, dict):
        kubernetes = dict(kubernetes)
        if kubernetes.get("incidents") == []:
            kubernetes.setdefault("namespace", "kybernaut-lab")
            kubernetes.setdefault("observed_at", POLICY_OBSERVED_AT)
        complete["kubernetes"] = kubernetes
        complete.setdefault("kubernetes_status", "completed")

    return workflow.enforce_policy(complete)


@pytest.fixture
def isolated_anomaly_evaluation(monkeypatch):
    async def unavailable_anomaly(_service):
        return {"status": "unavailable", "reason": "test"}

    monkeypatch.setattr(
        workflow, "evaluate_service_anomaly", unavailable_anomaly
    )


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
    assert "availability is unknown" in result["policy"]["reason"]
    assert result["policy"]["automatic_execution"] is False


def test_complete_evidence_with_no_actionable_findings_explains_monitoring():
    result = _enforce_policy_with_complete_fixture({
        "diagnosis": {
            "status": "up",
            "findings": [{"type": "no_detected_anomaly"}],
        },
        "rca": None,
    })

    assert result["policy"]["action"] == "continue_monitoring"
    assert result["policy"]["reason"] == (
        "No configured intervention threshold is met; continue monitoring."
    )
    assert result["policy"]["automatic_execution"] is False

def test_service_outage_requires_approval():
    result = _enforce_policy_with_complete_fixture({
        "diagnosis": {
            "status": "down",
            "findings": [{
                "type": "service_unavailable",
                "severity": "critical",
            }],
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
async def test_workflow_runs_without_groq(
    monkeypatch, isolated_anomaly_evaluation
):
    async def no_anomaly(_service):
        return {
            "status": "evaluated",
            "observed_at": POLICY_OBSERVED_AT,
            "actionable_candidates": [],
        }

    async def fake_diagnosis(service):
        return {
            "service": service,
            "status": "up",
            "evidence_status": "available",
            "missing_evidence": [],
            "findings": [{"type": "no_detected_anomaly"}],
        }

    async def unavailable_rca(diagnosis):
        raise RuntimeError(
            "GROQ_API_KEY is not configured; RCA generation is unavailable."
        )

    monkeypatch.setattr(workflow, "diagnose_service", fake_diagnosis)
    monkeypatch.setattr(workflow, "evaluate_service_anomaly", no_anomaly)
    monkeypatch.setattr(workflow, "generate_rca", unavailable_rca)
    monkeypatch.setattr(
        workflow,
        "observe_namespace",
        lambda: {
            "observed_at": POLICY_OBSERVED_AT,
            "namespace": "kybernaut-lab",
            "incidents": [],
            "deployments": [],
        },
    )

    result = await workflow.run_incident_workflow("order-service")

    assert result["rca"] is None
    assert result["rca_status"] == "unavailable"
    assert result["policy"]["action"] == "continue_monitoring"
    assert result["policy"]["automatic_execution"] is False


@pytest.mark.asyncio
async def test_invalid_rca_output_is_marked_invalid_and_policy_continues(
    monkeypatch,
):
    async def invalid_rca(_evidence):
        raise ValidationError.from_exception_data(
            "RCAResult",
            [{
                "type": "float_parsing",
                "loc": ("confidence",),
                "input": "not-a-number",
            }],
        )

    monkeypatch.setattr(workflow, "generate_rca", invalid_rca)
    update = await workflow.analyze_root_cause({
        "diagnosis": {"status": "up"},
        "kubernetes": {"incidents": []},
    })

    assert update == {
        "rca": None,
        "rca_status": "invalid",
        "rca_error": "invalid_structured_output",
    }
    policy = _enforce_policy_with_complete_fixture({
        "diagnosis": {
            "status": "up",
            "evidence_status": "available",
            "missing_evidence": [],
            "findings": [{"type": "no_detected_anomaly"}],
        },
        "rca": {"recommended_action": "request_approval"},
    })["policy"]
    assert policy["action"] == "continue_monitoring"
    assert policy["automatic_execution"] is False


@pytest.mark.asyncio
async def test_rca_provider_error_content_is_not_logged(monkeypatch, caplog):
    marker = "synthetic-provider-response-secret"

    async def provider_failure(_evidence):
        raise RuntimeError(marker)

    monkeypatch.setattr(workflow, "generate_rca", provider_failure)
    result = await workflow.analyze_root_cause({
        "diagnosis": {"status": "unknown", "evidence": marker},
    })

    assert result["rca_status"] == "unavailable"
    assert result["rca_error"] == "provider_error"
    assert marker not in caplog.text


@pytest.mark.asyncio
async def test_rca_failure_codes_are_sanitized(monkeypatch, caplog):
    timeout_error = type("APITimeoutError", (RuntimeError,), {})

    async def timeout(_evidence):
        raise timeout_error("synthetic provider timeout details")

    monkeypatch.setattr(workflow, "generate_rca", timeout)
    timed_out = await workflow.analyze_root_cause({
        "diagnosis": {"status": "unknown"},
    })
    assert timed_out["rca_error"] == "provider_timeout"

    async def invalid_config(_evidence):
        raise RCAConfigurationError("rca_timeout_configuration_invalid")

    monkeypatch.setattr(workflow, "generate_rca", invalid_config)
    unavailable = await workflow.analyze_root_cause({
        "diagnosis": {"status": "unknown"},
    })
    assert unavailable["rca_error"] == "rca_timeout_configuration_invalid"
    assert "synthetic provider timeout details" not in caplog.text


def test_kubernetes_high_severity_requires_approval():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "up",
            "evidence_status": "available",
            "missing_evidence": [],
            "findings": [{"type": "no_detected_anomaly"}],
        },
        "kubernetes": _high_kubernetes_observation(),
        "kubernetes_status": "completed",
        "telemetry_anomaly": {
            "status": "unavailable",
            "reason": "synthetic incomplete evidence",
        },
    })

    assert result["policy"]["action"] == "request_approval"
    assert result["policy"]["priority"] == "critical"
    assert result["policy"]["requires_human_approval"] is True
    assert result["policy"]["automatic_execution"] is False
    assert result["policy"]["remediation_executed"] is False

def test_healthy_kubernetes_does_not_force_approval():
    result = _enforce_policy_with_complete_fixture({
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
async def test_workflow_preserves_kubernetes_success_status(
    monkeypatch, isolated_anomaly_evaluation
):
    async def fake_diagnosis(service):
        return {
            "service": service,
            "status": "up",
            "evidence_status": "available",
            "missing_evidence": [],
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
async def test_workflow_reports_kubernetes_unavailable(
    monkeypatch, isolated_anomaly_evaluation
):
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
async def test_malformed_rca_does_not_stop_policy_enforcement(
    monkeypatch, isolated_anomaly_evaluation
):
    """An LLM parsing/provider error must not abort the workflow."""

    async def fake_diagnosis(service):
        return {
            "service": service,
            "status": "up",
            "findings": [{"type": "no_detected_anomaly"}],
        }

    def fake_observer():
        return {
            "observed_at": POLICY_OBSERVED_AT,
            "namespace": "kybernaut-lab",
            "incident_count": 1,
            "incidents": [
                {
                    "type": "pod_container_failure",
                    "severity": "high",
                    "resource": "demo-web-test",
                    "resource_kind": "Pod",
                    "resource_uid": "demo-web-test-uid",
                    "source_timestamp": POLICY_OBSERVED_AT,
                    "evidence": ["Container waiting: CrashLoopBackOff"],
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
    assert result["rca_error"] == "provider_error"
    assert result["policy"]["action"] == "request_approval"
    assert result["policy"]["priority"] == "critical"
    assert result["policy"]["requires_human_approval"] is True
    assert result["policy"]["automatic_execution"] is False
    assert result["policy"]["remediation_executed"] is False

@pytest.mark.asyncio
async def test_rca_provider_error_does_not_escalate_healthy_system(
    monkeypatch, isolated_anomaly_evaluation
):
    """An RCA error alone must not turn a healthy system into an incident."""

    async def no_anomaly(_service):
        return {
            "status": "evaluated",
            "observed_at": POLICY_OBSERVED_AT,
            "actionable_candidates": [],
        }

    async def fake_diagnosis(service):
        return {
            "service": service,
            "status": "up",
            "evidence_status": "available",
            "missing_evidence": [],
            "findings": [{"type": "no_detected_anomaly"}],
        }

    def fake_observer():
        return {
            "observed_at": POLICY_OBSERVED_AT,
            "namespace": "kybernaut-lab",
            "incident_count": 0,
            "incidents": [],
            "deployments": [],
        }

    async def failed_rca(diagnosis):
        raise RuntimeError("Simulated provider failure")

    monkeypatch.setattr(workflow, "diagnose_service", fake_diagnosis)
    monkeypatch.setattr(workflow, "evaluate_service_anomaly", no_anomaly)
    monkeypatch.setattr(workflow, "observe_namespace", fake_observer)
    monkeypatch.setattr(workflow, "generate_rca", failed_rca)

    result = await workflow.run_incident_workflow("order-service")

    assert result["rca_status"] == "unavailable"
    assert result["policy"]["action"] == "continue_monitoring"
    assert result["policy"]["automatic_execution"] is False
    assert result["policy"]["remediation_executed"] is False

def test_statistical_memory_candidate_does_not_escalate_without_operational_rule():
    result = _enforce_policy_with_complete_fixture({
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
    result = _enforce_policy_with_complete_fixture({
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

def test_unavailable_telemetry_requires_monitoring_investigation():
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

    assert result["policy"]["action"] == "investigate_monitoring"
    assert result["policy"]["priority"] == "medium"
    assert result["policy"]["automatic_execution"] is False


def test_memory_candidate_without_informational_deviations_does_not_escalate():
    result = _enforce_policy_with_complete_fixture({
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
    result = _enforce_policy_with_complete_fixture({
        "diagnosis": {
            "status": "down",
            "findings": [{
                "type": "service_unavailable",
                "severity": "critical",
            }],
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
async def test_runbook_retrieval_failure_does_not_block_rca(
    monkeypatch, isolated_anomaly_evaluation
):
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
    result = _enforce_policy_with_complete_fixture({
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
    result = _enforce_policy_with_complete_fixture({
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

@pytest.mark.asyncio
async def test_graph_executes_nodes_in_order(monkeypatch):
    execution_order = []

    async def fake_diagnosis(service):
        execution_order.append("diagnose")
        return {
            "service": service,
            "status": "up",
            "findings": [{"type": "no_detected_anomaly"}],
        }

    async def fake_anomaly(service):
        return {"status": "unavailable", "reason": "test"}

    def fake_observer():
        return {"incidents": [], "deployments": []}

    def fake_runbooks(query, top_k):
        return []

    async def fake_rca(evidence):
        execution_order.append("root_cause_analysis")
        return {
            "root_cause": "No confirmed failure",
            "confidence": 0.8,
            "evidence": ["Service is healthy"],
            "recommended_action": "continue_monitoring",
            "requires_human_approval": False,
        }

    original_policy = workflow.enforce_policy

    def tracked_policy(state):
        execution_order.append("policy")
        return original_policy(state)

    monkeypatch.setattr(workflow, "diagnose_service", fake_diagnosis)
    monkeypatch.setattr(workflow, "evaluate_service_anomaly", fake_anomaly)
    monkeypatch.setattr(workflow, "observe_namespace", fake_observer)
    monkeypatch.setattr(workflow, "retrieve_runbooks", fake_runbooks)
    monkeypatch.setattr(workflow, "generate_rca", fake_rca)
    monkeypatch.setattr(workflow, "enforce_policy", tracked_policy)

    graph = workflow.build_incident_graph()
    result = await graph.ainvoke({"service": "order-service"})

    assert execution_order == [
        "diagnose",
        "root_cause_analysis",
        "policy",
    ]
    assert result["policy"]["automatic_execution"] is False
    assert result["policy"]["remediation_executed"] is False

@pytest.mark.asyncio
async def test_graph_handles_unexpected_diagnosis_failure(
    monkeypatch, isolated_anomaly_evaluation
):
    async def failed_diagnosis(service):
        raise RuntimeError("Simulated diagnosis failure")

    monkeypatch.setattr(workflow, "diagnose_service", failed_diagnosis)
    monkeypatch.setattr(
        workflow,
        "observe_namespace",
        lambda: {"incidents": [], "deployments": []},
    )

    graph = workflow.build_incident_graph()
    result = await graph.ainvoke({"service": "order-service"})

    assert result["diagnosis"]["status"] == "unknown"
    assert result["policy"]["action"] == "investigate_monitoring"
    assert result["policy"]["automatic_execution"] is False
    assert result["policy"]["remediation_executed"] is False

@pytest.mark.asyncio
async def test_diagnosis_failure_continues_with_unknown_status(monkeypatch):
    async def failed_diagnosis(service):
        raise RuntimeError("Simulated Prometheus failure")

    async def fake_anomaly(service):
        return {"status": "unavailable", "reason": "test"}

    def fake_observer():
        return {"incidents": [], "deployments": []}

    def fake_runbooks(query, top_k):
        return []

    async def fake_rca(evidence):
        assert evidence["service_diagnosis"]["status"] == "unknown"
        return {
            "root_cause": "Diagnosis unavailable",
            "confidence": 0.2,
            "evidence": ["Prometheus diagnosis failed"],
            "recommended_action": "investigate_monitoring",
            "requires_human_approval": False,
        }

    monkeypatch.setattr(workflow, "diagnose_service", failed_diagnosis)
    monkeypatch.setattr(workflow, "evaluate_service_anomaly", fake_anomaly)
    monkeypatch.setattr(workflow, "observe_namespace", fake_observer)
    monkeypatch.setattr(workflow, "retrieve_runbooks", fake_runbooks)
    monkeypatch.setattr(workflow, "generate_rca", fake_rca)

    result = await workflow.run_incident_workflow("order-service")

    assert result["diagnosis"]["status"] == "unknown"
    assert result["kubernetes_status"] == "completed"
    assert result["rca_status"] == "completed"
    assert result["policy"]["action"] == "investigate_monitoring"
    assert result["policy"]["automatic_execution"] is False
    assert result["policy"]["remediation_executed"] is False

def test_malformed_diagnosis_findings_do_not_crash_policy():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "unknown",
            "findings": None,
        },
        "kubernetes": {"incidents": []},
        "telemetry_anomaly": {
            "status": "unavailable",
            "reason": "test",
        },
        "rca": None,
    })

    policy = result["policy"]
    assert policy["action"] == "investigate_monitoring"
    assert policy["priority"] == "medium"
    assert policy["automatic_execution"] is False
    assert policy["remediation_executed"] is False

def test_malformed_kubernetes_incidents_do_not_crash_policy():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "up",
            "findings": [{"type": "no_detected_anomaly"}],
        },
        "kubernetes": {
            "incidents": [None, "invalid-incident"],
        },
        "telemetry_anomaly": {
            "status": "unavailable",
            "reason": "test",
        },
        "rca": None,
    })

    policy = result["policy"]
    assert policy["action"] == "investigate_monitoring"
    assert policy["priority"] == "medium"
    assert policy["automatic_execution"] is False
    assert policy["remediation_executed"] is False


def test_malformed_telemetry_does_not_crash_policy():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "up",
            "findings": [{"type": "no_detected_anomaly"}],
        },
        "kubernetes": {"incidents": []},
        "telemetry_anomaly": "invalid-telemetry",
        "rca": None,
    })

    policy = result["policy"]
    assert policy["action"] == "investigate_monitoring"
    assert policy["priority"] == "medium"
    assert policy["automatic_execution"] is False
    assert policy["remediation_executed"] is False


def test_malformed_telemetry_findings_require_investigation():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "up",
            "findings": [{"type": "no_detected_anomaly"}],
        },
        "telemetry_anomaly": {
            "status": "evaluated",
            "operational_findings": ["invalid-finding"],
        },
    })

    assert result["policy"]["action"] == "investigate_monitoring"
    assert result["policy"]["automatic_execution"] is False


def test_critical_kubernetes_incident_requires_approval():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "up",
            "evidence_status": "available",
            "missing_evidence": [],
            "findings": [{"type": "no_detected_anomaly"}],
        },
        "kubernetes": _high_kubernetes_observation("critical"),
        "kubernetes_status": "completed",
        "telemetry_anomaly": {
            "status": "evaluated",
            "observed_at": POLICY_OBSERVED_AT,
            "actionable_candidates": [],
        },
    })

    assert result["policy"]["action"] == "request_approval"
    assert result["policy"]["priority"] == "critical"
    assert result["policy"]["requires_human_approval"] is True
    assert result["policy"]["automatic_execution"] is False


def test_malformed_diagnosis_findings_require_investigation():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "up",
            "evidence_status": "available",
            "missing_evidence": [],
            "findings": ["invalid-finding"],
        },
        "telemetry_anomaly": {"status": "evaluated"},
    })

    assert result["policy"]["action"] == "investigate_monitoring"
    assert result["policy"]["automatic_execution"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_result", [None, [], "invalid"])
async def test_non_dictionary_diagnosis_result_becomes_unknown(
    monkeypatch, invalid_result
):
    async def invalid_diagnosis(_service):
        return invalid_result

    async def unavailable_anomaly(_service):
        return {"status": "unavailable", "reason": "test"}

    monkeypatch.setattr(workflow, "diagnose_service", invalid_diagnosis)
    monkeypatch.setattr(
        workflow, "evaluate_service_anomaly", unavailable_anomaly
    )
    monkeypatch.setattr(
        workflow, "observe_namespace",
        lambda: {"incidents": [], "deployments": []},
    )

    result = await workflow.detect_and_diagnose({"service": "order-service"})

    assert result["diagnosis"]["status"] == "unknown"
    assert result["diagnosis"]["evidence_status"] == "invalid"
    assert result["diagnosis"]["reason"] == "invalid_diagnosis_result"
    policy = workflow.enforce_policy({
        **result,
        "diagnosis": result["diagnosis"],
    })["policy"]
    assert policy["action"] == "investigate_monitoring"


def test_incomplete_prometheus_evidence_never_selects_healthy_policy():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "up",
            "evidence_status": "unavailable",
            "missing_evidence": ["http_5xx_rate"],
            "findings": [{"type": "no_detected_anomaly"}],
        },
        "kubernetes": {"incidents": []},
        "telemetry_anomaly": {"status": "evaluated"},
        "rca": None,
    })

    assert result["policy"]["action"] == "investigate_monitoring"
    assert result["policy"]["automatic_execution"] is False
    assert result["policy"]["remediation_executed"] is False


def test_stale_prometheus_evidence_requires_monitoring_investigation():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "up",
            "evidence_status": "stale",
            "findings": [{"type": "no_detected_anomaly"}],
        },
        "kubernetes": {"incidents": []},
        "telemetry_anomaly": {"status": "evaluated"},
        "rca": None,
    })

    assert result["policy"]["action"] == "investigate_monitoring"


def test_missing_prometheus_evidence_cannot_be_overridden_by_healthy_finding():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "up",
            "missing_evidence": ["http_5xx_rate"],
            "findings": [{"type": "no_detected_anomaly"}],
        },
        "kubernetes": {"incidents": []},
        "telemetry_anomaly": {"status": "evaluated"},
        "rca": None,
    })

    assert result["policy"]["action"] == "investigate_monitoring"


def test_missing_telemetry_cannot_be_interpreted_as_healthy():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "up",
            "evidence_status": "available",
            "missing_evidence": [],
            "findings": [{"type": "no_detected_anomaly", "severity": "info"}],
        },
        "kubernetes_status": "completed",
        "kubernetes": {
            "observed_at": POLICY_OBSERVED_AT,
            "namespace": "kybernaut-lab",
            "incidents": [],
            "deployments": [],
        },
    })

    assert result["policy"]["action"] == "investigate_monitoring"
    assert result["policy"]["automatic_execution"] is False


def test_contradictory_high_kubernetes_evidence_has_no_target_or_approval():
    observation = _high_kubernetes_observation()
    observation["incidents"].append({
        "type": "pod_container_failure",
        "severity": "critical",
        "resource": "demo-web-pod-2",
        "resource_kind": "Pod",
        "resource_uid": "pod-uid-2",
        "source_timestamp": POLICY_OBSERVED_AT,
        "evidence": ["Container terminated with reason OOMKilled."],
    })
    diagnosis = {
        "status": "up",
        "evidence_status": "unavailable",
        "missing_evidence": ["http_5xx_rate"],
        "findings": [{"type": "no_detected_anomaly", "severity": "info"}],
    }

    result = workflow.enforce_policy({
        "diagnosis": diagnosis,
        "kubernetes_status": "completed",
        "kubernetes": observation,
        "telemetry_anomaly": {
            "status": "unavailable",
            "reason": "synthetic source outage",
        },
    })
    classification = workflow.classify_incident(
        diagnosis,
        observation,
        {"status": "unavailable"},
    )

    assert classification["incident_type"] == "unknown"
    assert classification["target"] is None
    assert result["policy"]["action"] == "investigate_monitoring"
    assert result["policy"]["requires_human_approval"] is False
    assert result["policy"]["automatic_execution"] is False


def test_unrecognized_high_severity_kubernetes_record_cannot_request_approval():
    result = workflow.enforce_policy({
        "diagnosis": {
            "status": "up",
            "evidence_status": "available",
            "missing_evidence": [],
            "findings": [{"type": "no_detected_anomaly", "severity": "info"}],
        },
        "kubernetes_status": "completed",
        "kubernetes": {
            "observed_at": POLICY_OBSERVED_AT,
            "namespace": "kybernaut-lab",
            "incidents": [{
                "severity": "critical",
                "resource": "unknown-resource",
                "resource_kind": "Pod",
                "evidence": ["Unrecognized event"],
            }],
            "deployments": [],
        },
        "telemetry_anomaly": {
            "status": "evaluated",
            "observed_at": POLICY_OBSERVED_AT,
            "actionable_candidates": [],
        },
    })

    assert result["policy"]["action"] == "investigate_monitoring"
    assert result["policy"]["requires_human_approval"] is False
    assert result["policy"]["automatic_execution"] is False
