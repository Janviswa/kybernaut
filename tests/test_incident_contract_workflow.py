from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

import app.database as database
from app.models.incident_contract import IncidentContract
from app.services import (
    incident_contract,
    incident_response_graph,
    incident_workflow as workflow,
)
from app.services.incident_classifier import classify_incident


OBSERVED_AT = "2026-10-22T10:00:00+00:00"


@pytest.fixture(autouse=True)
def initialized_isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "contract.db")
    database.initialize_database()


def state(**overrides):
    value = {
        "incident_id": "LEGACY-INC-1",
        "service": "order-service",
        "diagnosis": {
            "status": "up",
            "evidence_status": "available",
            "observed_at": OBSERVED_AT,
            "missing_evidence": [],
            "evidence": {
                "http_5xx_rate_per_second": 0,
                "p95_latency_seconds": 0.1,
            },
            "findings": [{
                "type": "no_detected_anomaly",
                "severity": "info",
                "evidence": "Synthetic healthy diagnosis.",
            }],
        },
        "telemetry_anomaly": {
            "status": "evaluated",
            "observed_at": OBSERVED_AT,
            "actionable_candidates": [],
        },
        "kubernetes_status": "completed",
        "kubernetes": {
            "observed_at": OBSERVED_AT,
            "namespace": "kybernaut-lab",
            "incidents": [{
                "type": "pod_container_failure",
                "resource": "demo-web-crash-1",
                "resource_kind": "Pod",
                "resource_uid": "pod-uid-1",
                "severity": "high",
                "source_timestamp": OBSERVED_AT,
                "evidence": ["Container terminated after CrashLoopBackOff."],
            }],
            "deployments": [{"name": "demo-web"}],
        },
        "policy": {
            "priority": "critical",
            "action": "request_approval",
            "requires_human_approval": True,
            "automatic_execution": False,
            "remediation_executed": False,
        },
        "rca": None,
    }
    value.update(overrides)
    return value


def test_legacy_state_maps_to_shared_contract_without_losing_identity():
    contract = incident_contract.build_incident_contract(state())

    assert isinstance(contract, IncidentContract)
    assert contract.incident_id == "LEGACY-INC-1"
    assert contract.classification.incident_type == "pod_crash"
    assert contract.classification.target.resource_uid == "pod-uid-1"
    assert contract.evidence_sources[0].status == "available"
    assert contract.evidence_sources[0].observed_at == datetime.fromisoformat(
        OBSERVED_AT
    )
    assert contract.policy_decision["action"] == "request_approval"
    assert not contract.evidence_truncated


@pytest.mark.parametrize(
    ("diagnosis_overrides", "expected_status", "expected_observed_at"),
    [
        ({}, "available", None),
        ({"observed_at": OBSERVED_AT}, "available", OBSERVED_AT),
        (
            {"evidence_status": "stale", "observed_at": OBSERVED_AT},
            "stale",
            OBSERVED_AT,
        ),
        (
            {"observed_at": "not-a-timestamp"},
            "invalid",
            None,
        ),
    ],
)
def test_workflow_mappers_normalize_prometheus_source_consistently(
    diagnosis_overrides, expected_status, expected_observed_at
):
    current = state()
    current["diagnosis"].pop("observed_at")
    current["diagnosis"].update(diagnosis_overrides)
    classification = classify_incident(
        current["diagnosis"],
        current["kubernetes"],
        current["telemetry_anomaly"],
    )
    response_contract = incident_response_graph._build_contract({
        **current,
        "classification": classification,
    })
    advisory_contract = incident_contract.build_incident_contract({
        **current,
        "classification": classification,
    })

    for contract in (response_contract, advisory_contract):
        prometheus = next(
            item for item in contract.evidence_sources
            if item.source == "prometheus"
        )
        assert prometheus.status == expected_status
        assert (
            prometheus.observed_at.isoformat()
            if prometheus.observed_at is not None else None
        ) == expected_observed_at


def test_missing_prometheus_timestamp_remains_unknown_freshness():
    current = state()
    current["diagnosis"].pop("observed_at")
    contract = incident_contract.build_incident_contract(current)
    prometheus = next(
        item for item in contract.evidence_sources
        if item.source == "prometheus"
    )

    assert prometheus.status == "available"
    assert prometheus.observed_at is None


@pytest.mark.parametrize(
    ("diagnosis", "expected_status"),
    [
        (
            {"status": "up", "findings": [{"type": "no_detected_anomaly"}]},
            "unavailable",
        ),
        (
            {
                "status": "up",
                "evidence_status": "stale",
                "observed_at": OBSERVED_AT,
                "findings": [],
            },
            "stale",
        ),
        (
            {
                "status": "up",
                "evidence_status": "available",
                "observed_at": "not-a-timestamp",
                "findings": [],
            },
            "invalid",
        ),
    ],
)
def test_missing_stale_and_malformed_diagnosis_remain_non_available(
    diagnosis, expected_status
):
    contract = incident_contract.build_incident_contract(
        state(diagnosis=diagnosis)
    )

    assert contract.evidence_sources[0].status == expected_status
    assert contract.evidence[0].status == expected_status


def test_malformed_kubernetes_timestamp_is_invalid_evidence():
    observation = state()["kubernetes"]
    observation["incidents"][0]["source_timestamp"] = "bad-time"

    contract = incident_contract.build_incident_contract(
        state(kubernetes=observation)
    )

    assert contract.classification.incident_type == "unknown"
    assert contract.classification.target is None
    assert contract.evidence[1].status == "invalid"
    assert contract.evidence[1].target is None


def test_malformed_diagnosis_and_telemetry_are_invalid_sources():
    diagnosis = state()["diagnosis"]
    diagnosis["findings"] = "not-a-findings-list"
    telemetry = {
        "status": "evaluated",
        "actionable_candidates": ["cpu_cores_used", 7],
    }

    contract = incident_contract.build_incident_contract(
        state(diagnosis=diagnosis, telemetry_anomaly=telemetry)
    )

    assert contract.evidence_sources[0].status == "invalid"
    assert contract.evidence_sources[2].status == "invalid"
    assert contract.evidence[0].status == "invalid"
    assert contract.evidence[-1].status == "invalid"


def test_conflicting_current_kubernetes_evidence_is_targetless():
    observation = state()["kubernetes"]
    observation["incidents"].append({
        "type": "kubernetes_warning_event",
        "resource": "demo-web-disk-1",
        "resource_kind": "Pod",
        "resource_uid": "pod-uid-2",
        "severity": "medium",
        "source_timestamp": OBSERVED_AT,
        "evidence": ["Evicted: ephemeral storage exceeded."],
    })

    contract = incident_contract.build_incident_contract(
        state(kubernetes=observation)
    )

    assert contract.classification.incident_type == "unknown"
    assert contract.classification.target is None
    assert all(item.target is None for item in contract.evidence[1:])


def test_contract_bounds_findings_and_preserves_target_evidence():
    current = state()
    current["diagnosis"]["findings"] = [
        {"type": "finding", "evidence": "x" * 1200}
        for _ in range(12)
    ]
    current["kubernetes"]["incidents"] = [
        {
            "type": "kubernetes_warning_event",
            "resource": f"demo-web-pod-{index}",
            "resource_kind": "Pod",
            "resource_uid": f"uid-{index}",
            "severity": "medium",
            "evidence": ["Synthetic event"],
        }
        for index in range(51)
    ] + [current["kubernetes"]["incidents"][0]]

    contract = incident_contract.build_incident_contract(current)

    assert contract.evidence_truncated is True
    assert len(contract.diagnosis_findings) == 10
    assert len(contract.diagnosis_findings[0]["evidence"]) == 1000
    assert any(
        item.target is not None and item.target.resource_uid == "pod-uid-1"
        for item in contract.evidence
    )


@pytest.mark.asyncio
async def test_advisory_workflow_adds_contract_without_removing_legacy_fields(
    monkeypatch,
):
    async def fake_diagnosis(service):
        return state()["diagnosis"]

    async def unavailable_rca(_evidence):
        raise RuntimeError("synthetic provider failure")

    monkeypatch.setattr(workflow, "diagnose_service", fake_diagnosis)
    monkeypatch.setattr(
        workflow, "evaluate_service_anomaly",
        lambda _service: _evaluated_anomaly(),
    )
    monkeypatch.setattr(
        workflow,
        "observe_namespace",
        lambda: state()["kubernetes"],
    )
    monkeypatch.setattr(workflow, "generate_rca", unavailable_rca)

    result = await workflow.run_incident_workflow("order-service")

    assert {
        "service", "diagnosis", "telemetry_anomaly", "kubernetes",
        "kubernetes_status", "rca", "rca_status", "rca_error", "policy",
    } <= result.keys()
    assert result["contract_status"] == "valid"
    assert result["contract"]["incident_id"].startswith("INC-")
    assert result["contract"]["classification"]["incident_type"] == "pod_crash"
    assert result["rca_status"] == "unavailable"
    assert result["policy"]["action"] == "request_approval"
    assert result["policy"]["automatic_execution"] is False


async def _evaluated_anomaly():
    return {
        "status": "evaluated",
        "observed_at": OBSERVED_AT,
        "actionable_candidates": [],
    }


@pytest.mark.asyncio
async def test_contract_validation_failure_does_not_abort_advisory_policy(
    monkeypatch,
):
    with pytest.raises(ValidationError) as error:
        IncidentContract.model_validate({})

    async def fake_diagnosis(_service):
        return {
            "status": "unknown",
            "evidence_status": "unavailable",
            "findings": [],
        }

    async def unavailable_rca(_evidence):
        raise RuntimeError("synthetic provider failure")

    def failed_contract(_state):
        raise error.value

    monkeypatch.setattr(workflow, "diagnose_service", fake_diagnosis)
    monkeypatch.setattr(
        workflow, "evaluate_service_anomaly",
        lambda _service: _evaluated_anomaly(),
    )
    monkeypatch.setattr(
        workflow, "observe_namespace",
        lambda: {
            "namespace": "kybernaut-lab",
            "incidents": [],
            "deployments": [],
        },
    )
    monkeypatch.setattr(workflow, "generate_rca", unavailable_rca)
    monkeypatch.setattr(workflow, "build_incident_contract", failed_contract)

    result = await workflow.run_incident_workflow("order-service")

    assert result["contract"] is None
    assert result["contract_status"] == "invalid"
    assert result["contract_error"] == "validation_failed"
    assert result["policy"]["action"] == "investigate_monitoring"
    assert result["policy"]["automatic_execution"] is False


@pytest.mark.asyncio
async def test_legacy_healthy_label_without_completeness_is_not_trusted(
    monkeypatch,
):
    async def incomplete_diagnosis(_service):
        return {
            "status": "up",
            "findings": [{
                "type": "no_detected_anomaly",
                "severity": "info",
                "evidence": "Legacy fixture with no completeness metadata.",
            }],
        }

    async def unavailable_rca(_evidence):
        raise RuntimeError("synthetic provider failure")

    monkeypatch.setattr(workflow, "diagnose_service", incomplete_diagnosis)
    monkeypatch.setattr(
        workflow, "evaluate_service_anomaly",
        lambda _service: _evaluated_anomaly(),
    )
    monkeypatch.setattr(
        workflow, "observe_namespace",
        lambda: {
            "observed_at": OBSERVED_AT,
            "namespace": "kybernaut-lab",
            "incidents": [],
            "deployments": [],
        },
    )
    monkeypatch.setattr(workflow, "generate_rca", unavailable_rca)

    result = await workflow.run_incident_workflow("order-service")

    assert result["contract"]["evidence_sources"][0]["status"] == (
        "unavailable"
    )
    assert result["policy"]["action"] == "investigate_monitoring"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "expected_source_status"),
    [
        ("telemetry_unavailable", "unavailable"),
        ("telemetry_malformed", "invalid"),
        ("kubernetes_unavailable", "unavailable"),
        ("kubernetes_malformed", "invalid"),
    ],
)
async def test_advisory_workflow_investigates_invalid_contract_source(
    monkeypatch, source, expected_source_status
):
    async def healthy_diagnosis(_service):
        return state()["diagnosis"]

    async def unavailable_anomaly(_service):
        if source == "telemetry_unavailable":
            return {"status": "unavailable", "reason": "test"}
        if source == "telemetry_malformed":
            return {
                "status": "evaluated",
                "actionable_candidates": ["cpu_cores_used", 7],
            }
        return await _evaluated_anomaly()

    def observe():
        if source == "kubernetes_unavailable":
            raise RuntimeError("synthetic observation unavailable")
        observation = state()["kubernetes"]
        observation["incidents"] = (
            [None] if source == "kubernetes_malformed" else []
        )
        return observation

    async def unavailable_rca(_evidence):
        raise RuntimeError("synthetic provider failure")

    monkeypatch.setattr(workflow, "diagnose_service", healthy_diagnosis)
    monkeypatch.setattr(
        workflow, "evaluate_service_anomaly", unavailable_anomaly
    )
    monkeypatch.setattr(workflow, "observe_namespace", observe)
    monkeypatch.setattr(workflow, "generate_rca", unavailable_rca)
    monkeypatch.setattr(
        workflow,
        "_persist_telemetry_incidents",
        lambda *_args: {"status": "skipped", "incident_ids": []},
    )

    result = await workflow.run_incident_workflow("order-service")

    expected_source = source.split("_", 1)[0]
    unavailable_source = next(
        item for item in result["contract"]["evidence_sources"]
        if item["source"] == expected_source
    )

    assert unavailable_source["status"] == expected_source_status
    assert result["policy"]["action"] == "investigate_monitoring"
    assert result["policy"]["automatic_execution"] is False
    assert result["policy"]["remediation_executed"] is False


def test_mixed_evidence_valid_high_kubernetes_finding_requires_approval():
    current = state(
        diagnosis={
            "status": "unknown",
            "evidence_status": "unavailable",
            "missing_evidence": ["service_availability"],
            "findings": [],
        },
        telemetry_anomaly={
            "status": "unavailable",
            "reason": "synthetic unavailable evidence",
        },
    )

    result = workflow.enforce_policy(current)

    assert result["policy"]["action"] == "request_approval"
    assert result["policy"]["requires_human_approval"] is True
    assert result["policy"]["automatic_execution"] is False
    assert result["policy"]["remediation_executed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_result", [None, [], "invalid"])
async def test_advisory_workflow_invalid_diagnosis_is_unknown_and_investigated(
    monkeypatch, invalid_result
):
    async def invalid_diagnosis(_service):
        return invalid_result

    async def unavailable_anomaly(_service):
        return {"status": "unavailable", "reason": "test"}

    async def unavailable_rca(_evidence):
        raise RuntimeError("synthetic provider failure")

    monkeypatch.setattr(workflow, "diagnose_service", invalid_diagnosis)
    monkeypatch.setattr(
        workflow, "evaluate_service_anomaly", unavailable_anomaly
    )
    monkeypatch.setattr(
        workflow,
        "observe_namespace",
        lambda: {
            **state()["kubernetes"],
            "incidents": [],
        },
    )
    monkeypatch.setattr(workflow, "generate_rca", unavailable_rca)
    monkeypatch.setattr(
        workflow,
        "_persist_telemetry_incidents",
        lambda *_args: {"status": "skipped", "incident_ids": []},
    )

    result = await workflow.run_incident_workflow("order-service")

    assert result["diagnosis"]["status"] == "unknown"
    assert result["diagnosis"]["evidence_status"] == "invalid"
    assert result["policy"]["action"] == "investigate_monitoring"
    assert result["policy"]["automatic_execution"] is False


def test_rca_request_cannot_override_deterministic_policy():
    policy = workflow.enforce_policy({
        "diagnosis": {
            "status": "up",
            "evidence_status": "available",
            "missing_evidence": [],
            "findings": [{"type": "no_detected_anomaly"}],
        },
        "kubernetes_status": "completed",
        "kubernetes": {
            "observed_at": OBSERVED_AT,
            "namespace": "kybernaut-lab",
            "incidents": [],
            "deployments": [],
        },
        "telemetry_anomaly": {
            "status": "evaluated",
            "observed_at": OBSERVED_AT,
            "actionable_candidates": [],
        },
        "rca": {
            "recommended_action": "request_approval",
            "requires_human_approval": True,
        },
    })["policy"]

    assert policy["action"] == "continue_monitoring"
    assert policy["automatic_execution"] is False
    assert policy["remediation_executed"] is False
