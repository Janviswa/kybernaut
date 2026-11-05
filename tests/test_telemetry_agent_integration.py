from datetime import datetime, timezone

import numpy as np
import pytest

import app.database as database
import app.services.anomaly_runtime as anomaly_runtime
import app.services.incident_response_graph as response_graph
import app.services.incident_workflow as incident_workflow
from app.services import audit_repository as audit
from app.services.anomaly_policy import DirectionAwareAnomalyPolicy
from app.services.incident_repository import list_incidents
from app.services.prometheus_features import FEATURE_NAMES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sample_kind", "expected_status", "expected_category", "expected_action"),
    [
        ("normal", "evaluated", "unknown", "no_action"),
        ("cpu_anomaly", "evaluated", "cpu_spike", "no_action"),
        ("incomplete", "unavailable", "unknown", "no_action"),
        ("stale", "unavailable", "unknown", "no_action"),
    ],
)
async def test_synthetic_telemetry_flows_through_agent_workflow_and_audit(
    tmp_path, monkeypatch, sample_kind, expected_status, expected_category,
    expected_action,
):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "audit.sqlite")
    monkeypatch.delenv("KYBERNAUT_POLICY_AUTO_REMEDIATION", raising=False)
    monkeypatch.setenv("KYBERNAUT_REMEDIATION_MODE", "dry_run")

    baseline_center = np.array([0.2, 0.095, 49_000_000.0, 0.02])
    baseline = np.tile(baseline_center, (40, 1))
    policy = DirectionAwareAnomalyPolicy(threshold=5.0)
    policy.fit(baseline.tolist())
    monkeypatch.setattr(
        anomaly_runtime, "_policies", {"order-service": policy}
    )
    monkeypatch.setattr(anomaly_runtime, "_initialization_error", None)

    samples = {
        "normal": {
            name: float(value)
            for name, value in zip(FEATURE_NAMES, baseline_center)
        },
        "cpu_anomaly": {
            **{
                name: float(value)
                for name, value in zip(FEATURE_NAMES, baseline_center)
            },
            "cpu_cores_used": 0.5,
        },
        "incomplete": {},
    }
    collected_at = datetime.now(timezone.utc).isoformat()

    async def collect_synthetic_features(_client):
        # The collector preserves source timestamps; stale or incomplete rows
        # are omitted before anomaly evaluation.
        return {
            "order-service": {
                **samples[sample_kind],
                "observed_at": collected_at,
                "sample_timestamps": {
                    name: collected_at for name in FEATURE_NAMES
                },
            }
        } if sample_kind not in {"incomplete", "stale"} else {}

    monkeypatch.setattr(
        anomaly_runtime, "collect_service_features",
        collect_synthetic_features,
    )
    runtime_results = []
    real_evaluate = anomaly_runtime.evaluate_service_anomaly

    async def capture_runtime_result(service):
        evaluation = await real_evaluate(service)
        runtime_results.append(evaluation)
        return evaluation

    monkeypatch.setattr(
        incident_workflow, "evaluate_service_anomaly",
        capture_runtime_result,
    )

    async def synthetic_diagnosis(_service):
        return {
            "service": "order-service",
            "status": "up",
            "findings": [{
                "type": "no_detected_anomaly",
                "severity": "info",
                "evidence": "Synthetic service diagnosis is healthy.",
            }],
        }

    monkeypatch.setattr(
        incident_workflow, "diagnose_service", synthetic_diagnosis
    )
    monkeypatch.setattr(
        incident_workflow, "observe_namespace",
        lambda: {
            "namespace": "kybernaut-lab",
            "observed_at": collected_at,
            "incidents": [],
            "incident_count": 0,
            "deployments": [],
        },
    )

    async def unavailable_rca(_state):
        return {
            "rca": None,
            "rca_status": "unavailable",
            "rca_error": "mocked_provider_boundary",
        }

    monkeypatch.setattr(
        response_graph, "analyze_root_cause", unavailable_rca
    )
    monkeypatch.setattr(
        response_graph, "response_graph",
        response_graph.build_response_graph(),
    )

    result = await response_graph.run_response_workflow(
        "order-service", incident_id=f"SYNTHETIC-{sample_kind}"
    )

    assert runtime_results[0]["status"] == expected_status
    if sample_kind == "cpu_anomaly":
        assert "cpu_cores_used" in runtime_results[0]["actionable_candidates"]
    else:
        assert not runtime_results[0].get("actionable_candidates", [])
    if sample_kind in {"incomplete", "stale"}:
        assert runtime_results[0]["collection_status"] == "unavailable"
        assert runtime_results[0]["evaluation_status"] == "not_applicable"
    else:
        assert runtime_results[0]["collection_status"] == "success"
        assert runtime_results[0]["evaluation_status"] == "success"
    assert result["classification"]["incident_type"] == expected_category
    assert result["status"] == expected_action
    assert result["proposal"] is None
    assert result["approval_id"] is None
    assert result["verification"] is None

    event_list = audit.list_events(f"SYNTHETIC-{sample_kind}")
    events = {event["event_type"]: event for event in event_list}
    assert "evidence_collected" in events
    assert "incident_classified" in events
    assert "plan_empty" in events
    assert "closed_without_action" in events
    assert events["incident_classified"]["details"]["incident_type"] == (
        expected_category
    )
    if sample_kind == "cpu_anomaly":
        assert "cpu_cores_used" in " ".join(
            events["incident_classified"]["details"]["evidence"]
        )
    assert "policy_evaluated" not in events
    assert "approval_requested" not in events
    assert "remediation_started" not in events
    assert "incident_recovery_verified" not in events


@pytest.mark.asyncio
async def test_normal_telemetry_never_fabricates_anomaly(monkeypatch):
    baseline = np.tile([0.2, 0.095, 49_000_000.0, 0.02], (40, 1))
    policy = DirectionAwareAnomalyPolicy(threshold=5.0)
    policy.fit(baseline.tolist())

    monkeypatch.setattr(
        anomaly_runtime, "_policies", {"order-service": policy}
    )
    monkeypatch.setattr(anomaly_runtime, "_initialization_error", None)

    async def collect_normal(_client):
        return {
            "order-service": dict(zip(FEATURE_NAMES, baseline[0].tolist()))
        }

    monkeypatch.setattr(
        anomaly_runtime, "collect_service_features", collect_normal
    )
    result = await anomaly_runtime.evaluate_service_anomaly("order-service")

    assert result["status"] == "evaluated"
    assert result["collection_status"] == "success"
    assert result["evaluation_status"] == "success"
    assert result["actionable_candidates"] == []
    assert result["informational_deviations"] == []


async def _run_persisting_workflow(monkeypatch, kind):
    baseline_center = np.array([0.2, 0.095, 49_000_000.0, 0.02])
    policy = DirectionAwareAnomalyPolicy(threshold=5.0)
    policy.fit(np.tile(baseline_center, (40, 1)).tolist())
    monkeypatch.setattr(
        anomaly_runtime, "_policies", {"order-service": policy}
    )
    monkeypatch.setattr(anomaly_runtime, "_initialization_error", None)

    values = dict(zip(FEATURE_NAMES, baseline_center.tolist()))
    if kind == "cpu":
        values["cpu_cores_used"] = 0.5
    elif kind == "memory":
        values["memory_bytes"] = 490_000_000.0
    observed_at = datetime.now(timezone.utc).isoformat()

    async def collect(_client):
        if kind == "error":
            raise RuntimeError("mocked Prometheus failure")
        if kind in {"missing", "stale"}:
            return {}
        return {
            "order-service": {
                **values,
                "observed_at": observed_at,
                "sample_timestamps": {
                    feature: observed_at for feature in FEATURE_NAMES
                },
            }
        }

    async def diagnosis(service):
        return {
            "service": service,
            "status": "up",
            "evidence_status": "available",
            "observed_at": observed_at,
            "missing_evidence": [],
            "findings": [],
        }

    async def unavailable_rca(_evidence):
        raise RuntimeError("mocked provider unavailable")

    monkeypatch.setattr(
        anomaly_runtime, "collect_service_features", collect
    )
    monkeypatch.setattr(incident_workflow, "diagnose_service", diagnosis)
    monkeypatch.setattr(
        incident_workflow,
        "observe_namespace",
        lambda: {
            "namespace": "kybernaut-lab",
            "observed_at": observed_at,
            "incidents": [],
            "incident_count": 0,
            "deployments": [],
        },
    )
    monkeypatch.setattr(
        incident_workflow, "retrieve_runbooks", lambda *_args: []
    )
    monkeypatch.setattr(incident_workflow, "generate_rca", unavailable_rca)

    import app.services.k8s_remediation as remediation

    def unexpected_remediation(*_args, **_kwargs):
        raise AssertionError("advisory telemetry must not remediate")

    monkeypatch.setattr(remediation, "execute_action", unexpected_remediation)
    return await incident_workflow.run_incident_workflow("order-service")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected_type", "expected_feature", "expected_unit"),
    [
        ("cpu", "cpu_spike", "cpu_cores_used", "CPU cores"),
        ("memory", "memory_leak", "memory_bytes", "bytes"),
    ],
)
async def test_application_anomaly_is_persisted_with_source_evidence(
    tmp_path, monkeypatch, kind, expected_type, expected_feature, expected_unit
):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "incidents.db")
    database.initialize_database()

    result = await _run_persisting_workflow(monkeypatch, kind)

    incidents = list_incidents()
    assert result["contract"]["classification"]["incident_type"] == expected_type
    assert result["telemetry_incident_persistence"]["status"] == "persisted"
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident["service"] == "order-service"
    assert incident["incident_type"] == expected_type
    assert incident["severity"] == "medium"
    assert incident["source_evidence"]["source"] == "prometheus"
    assert incident["source_evidence"]["service"] == "order-service"
    assert incident["source_evidence"]["incident_type"] == expected_type
    assert incident["source_evidence"]["metric"]["name"] == expected_feature
    assert incident["source_evidence"]["metric"]["unit"] == expected_unit
    assert expected_feature in incident["message"]
    assert expected_unit in incident["message"]
    source_timestamp = result["telemetry_anomaly"]["sample_timestamps"][
        expected_feature
    ]
    assert f"observed_at={source_timestamp}" in incident["message"]
    assert "confidence=0.5" in incident["message"]
    assert incident["status"] == "OPEN"
    assert result["policy"]["remediation_executed"] is False


@pytest.mark.asyncio
async def test_repeated_anomaly_updates_active_incident_and_healthy_sample_resolves(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "incidents.db")
    database.initialize_database()

    first = await _run_persisting_workflow(monkeypatch, "cpu")
    repeated = await _run_persisting_workflow(monkeypatch, "cpu")

    incidents = list_incidents()
    assert len(incidents) == 1
    assert incidents[0]["incident_id"] == first[
        "telemetry_incident_persistence"
    ]["incident_ids"][0]
    assert repeated["telemetry_incident_persistence"]["incident_ids"] == [
        incidents[0]["incident_id"]
    ]
    assert incidents[0]["status"] == "OPEN"

    healthy = await _run_persisting_workflow(monkeypatch, "healthy")
    assert healthy["telemetry_incident_persistence"]["status"] == "healthy"
    assert list_incidents()[0]["status"] == "RESOLVED"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing", "stale", "error"])
async def test_unavailable_or_stale_metrics_do_not_persist_or_resolve(
    tmp_path, monkeypatch, kind
):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "incidents.db")
    database.initialize_database()
    await _run_persisting_workflow(monkeypatch, "cpu")

    result = await _run_persisting_workflow(monkeypatch, kind)

    assert result["telemetry_anomaly"]["status"] == "unavailable"
    assert result["telemetry_incident_persistence"]["status"] == "skipped"
    assert len(list_incidents()) == 1
    assert list_incidents()[0]["status"] == "OPEN"
