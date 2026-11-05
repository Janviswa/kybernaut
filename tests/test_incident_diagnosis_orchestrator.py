import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import app.database as database
from app.services import audit_repository
from app.services.incident_diagnosis_orchestrator import (
    process_active_incidents,
    process_incident,
)
from app.services.incident_repository import list_incidents, record_incident
from app.services import incident_diagnosis_orchestrator as orchestrator


@pytest.fixture
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "incidents.db")
    database.initialize_database()
    monkeypatch.setenv("KYBERNAUT_PROMETHEUS_MAX_SAMPLE_AGE_SECONDS", "30")
    monkeypatch.setenv("KYBERNAUT_PROMETHEUS_MAX_SAMPLE_SKEW_SECONDS", "10")
    mapping_path = tmp_path / "application_workload_map.json"
    mapping_path.write_text(
        json.dumps({"services": {}}), encoding="utf-8"
    )
    monkeypatch.setattr(orchestrator, "WORKLOAD_MAPPING_PATH", mapping_path)
    return mapping_path


def _source_evidence(*, value=0.5, observed_at=None, source_timestamp=None):
    stamp = observed_at or datetime.now(timezone.utc).isoformat()
    sample = source_timestamp or stamp
    return {
        "schema_version": 1,
        "source": "prometheus",
        "service": "order-service",
        "incident_type": "cpu_spike",
        "observed_at": stamp,
        "source_timestamp": sample,
        "metric": {
            "name": "cpu_cores_used",
            "value": value,
            "unit": "CPU cores",
            "baseline_median": 0.02,
            "deviation_score": 8.0,
            "threshold": 5.0,
            "direction": "HIGH",
            "classification": "actionable_candidate",
        },
    }


def _incident(source_evidence=None):
    return record_incident({
        "incident_id": "inc-order-cpu",
        "service": "order-service",
        "type": "cpu_spike",
        "severity": "medium",
        "message": "Prometheus CPU anomaly",
        "fingerprint": "prometheus:order-service:cpu_spike",
        "source_timestamp": (
            source_evidence or _source_evidence()
        )["source_timestamp"],
        "source_evidence": source_evidence or _source_evidence(),
    })


def _success_rca():
    async def analyze(state):
        assert state["diagnosis"]["service"] == "order-service"
        return {
            "rca": {
                "root_cause": "Recorded CPU usage exceeded its baseline.",
                "confidence": 0.7,
                "evidence": ["cpu_cores_used=0.5"],
                "recommended_action": "investigate_further",
                "requires_human_approval": True,
            },
            "rca_status": "completed",
            "rca_error": "",
        }

    return analyze


def _successful_runbooks(*_args):
    return [{
        "source": "cpu-runbook.md",
        "content": "Investigate sustained CPU usage against baseline.",
        "score": 0.8,
        "matched_terms": ["cpu", "baseline"],
    }]


def _guard_remediation(monkeypatch):
    import app.services.k8s_remediation as remediation

    executor_calls = []

    def unexpected_executor(*args, **kwargs):
        executor_calls.append((args, kwargs))
        raise AssertionError("Read-only diagnosis must never execute actions")

    monkeypatch.setattr(remediation, "execute_action", unexpected_executor)
    monkeypatch.setenv("KYBERNAUT_REMEDIATION_MODE", "live")
    return executor_calls


@pytest.mark.asyncio
async def test_diagnosis_uses_original_evidence_and_only_trusted_target(
    isolated_database, monkeypatch
):
    timestamp = datetime.now(timezone.utc).isoformat()
    original = _source_evidence(
        value=0.75, observed_at=timestamp, source_timestamp=timestamp
    )
    incident = _incident(original)
    isolated_database.write_text(
        json.dumps({
            "services": {
                "order-service": [{
                    "namespace": "kybernaut-lab",
                    "deployment": "orders",
                }]
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        orchestrator, "retrieve_runbooks", _successful_runbooks
    )
    rca_inputs = []

    async def capture_rca(state):
        rca_inputs.append(state)
        return await _success_rca()(state)

    monkeypatch.setattr(orchestrator, "analyze_root_cause", capture_rca)
    monkeypatch.setenv("KYBERNAUT_REMEDIATION_MODE", "live")
    import app.services.k8s_remediation as remediation

    executor_calls = []

    def unexpected_executor(*args, **kwargs):
        executor_calls.append((args, kwargs))
        raise AssertionError("Automatic diagnosis must never execute actions")

    monkeypatch.setattr(remediation, "execute_action", unexpected_executor)
    monkeypatch.setattr(
        orchestrator,
        "observe_namespace",
        lambda: {
            "namespace": "kybernaut-lab",
            "observed_at": timestamp,
            "deployments": [
                {"name": "orders", "desired_replicas": 2,
                 "available_replicas": 2}
            ],
            "incidents": [
                {"resource": "orders-123", "resource_kind": "Pod",
                 "evidence": ["matching workload symptom"]},
                {"resource": "other-123", "resource_kind": "Pod",
                 "evidence": ["unrelated symptom"]},
            ],
        },
    )

    result = await process_incident(list_incidents()[0])

    assert result["status"] == "limited"
    saved = list_incidents()[0]
    diagnosis = saved["diagnosis"]
    assert saved["source_evidence"] == original
    assert diagnosis["source_evidence"] == original
    assert diagnosis["evidence_references"] == [{
        "source": "prometheus",
        "service": "order-service",
        "metric": "cpu_cores_used",
        "observed_at": timestamp,
        "source_timestamp": timestamp,
    }]
    assert diagnosis["classification"]["incident_type"] == "cpu_spike"
    assert diagnosis["confidence"] == 0.5
    assert diagnosis["trusted_target"] == {
        "namespace": "kybernaut-lab",
        "deployment": "orders",
    }
    assert rca_inputs[0]["kubernetes"]["incidents"] == []
    assert "mapped_pod_ownership_unverified" in diagnosis["limitations"]
    assert diagnosis["runbook_match"]["source"] == "cpu-runbook.md"
    assert diagnosis["proposed_action"]["execution_status"] == "not_submitted"
    assert diagnosis["approval_status"] == "not_requested"
    assert diagnosis["verification_status"] == "not_run"
    assert diagnosis["policy_recommendation"]["action"] == "investigate_monitoring"
    assert diagnosis["policy_recommendation"]["automatic_execution"] is False
    assert "availability is unknown" in diagnosis["policy_recommendation"]["reason"]
    assert executor_calls == []
    assert saved["diagnosis_status"] == "limited"
    events = audit_repository.list_events("inc-order-cpu")
    assert [event["event_type"] for event in events] == [
        "automatic_diagnosis_limited"
    ]


@pytest.mark.asyncio
async def test_missing_or_ambiguous_mapping_limits_target_investigation(
    isolated_database, monkeypatch
):
    _incident()
    monkeypatch.setattr(
        orchestrator, "retrieve_runbooks", _successful_runbooks
    )
    monkeypatch.setattr(
        orchestrator, "analyze_root_cause", _success_rca()
    )

    def should_not_observe():
        raise AssertionError("Kubernetes must not be queried without one mapping")

    monkeypatch.setattr(orchestrator, "observe_namespace", should_not_observe)
    missing = await process_active_incidents("order-service")
    assert missing[0]["status"] == "limited"
    assert "trusted_workload_mapping_missing" in missing[0]["limitations"]

    isolated_database.write_text(
        json.dumps({
            "services": {
                "order-service": [
                    {"namespace": "kybernaut-lab", "deployment": "orders"},
                    {"namespace": "kybernaut-lab", "deployment": "orders-v2"},
                ]
            }
        }),
        encoding="utf-8",
    )
    ambiguous = await process_active_incidents("order-service")
    assert ambiguous[0]["status"] == "limited"
    assert "trusted_workload_mapping_ambiguous" in ambiguous[0]["limitations"]


@pytest.mark.parametrize(
    ("mapping_content", "expected_limitation"),
    [
        ("{", "trusted_workload_mapping_invalid"),
        (
            json.dumps({
                "services": {
                    "order-service": [{
                        "namespace": "other-namespace",
                        "deployment": "orders",
                    }]
                }
            }),
            "trusted_workload_mapping_invalid",
        ),
        (
            json.dumps({
                "services": {
                    "order-service": [{
                        "namespace": "kybernaut-lab",
                        "deployment": "Orders",
                    }]
                }
            }),
            "trusted_workload_mapping_invalid",
        ),
        (
            json.dumps({"services": {"order-service": [None]}}),
            "trusted_workload_mapping_invalid",
        ),
    ],
    ids=[
        "malformed-json",
        "wrong-namespace",
        "invalid-deployment-name",
        "malformed-target-shape",
    ],
)
@pytest.mark.asyncio
async def test_invalid_mapping_fails_closed_without_observation_or_action(
    isolated_database, monkeypatch, mapping_content, expected_limitation
):
    _incident()
    isolated_database.write_text(mapping_content, encoding="utf-8")
    monkeypatch.setattr(
        orchestrator, "retrieve_runbooks", _successful_runbooks
    )
    monkeypatch.setattr(
        orchestrator, "analyze_root_cause", _success_rca()
    )
    executor_calls = _guard_remediation(monkeypatch)
    observer_calls = []

    def unexpected_observation():
        observer_calls.append(True)
        raise AssertionError("Invalid mappings must not query Kubernetes")

    monkeypatch.setattr(
        orchestrator, "observe_namespace", unexpected_observation
    )

    result = await process_active_incidents("order-service")

    assert result[0]["status"] == "limited"
    assert expected_limitation in result[0]["limitations"]
    diagnosis = list_incidents()[0]["diagnosis"]
    assert diagnosis["trusted_target"] is None
    assert diagnosis["proposed_action"]["execution_status"] == "not_submitted"
    assert observer_calls == []
    assert executor_calls == []


@pytest.mark.parametrize(
    ("timestamp_delta", "expected_limitation"),
    [
        (timedelta(seconds=-60), "persisted_evidence_stale"),
        (timedelta(seconds=5), "persisted_evidence_timestamp_future"),
    ],
    ids=["stale", "future"],
)
@pytest.mark.asyncio
async def test_stale_or_future_persisted_evidence_is_limited(
    isolated_database, monkeypatch, timestamp_delta, expected_limitation
):
    timestamp = datetime.now(timezone.utc) + timestamp_delta
    _incident(_source_evidence(
        observed_at=timestamp.isoformat(),
        source_timestamp=timestamp.isoformat(),
    ))
    result = await process_incident(list_incidents()[0])

    assert result["status"] == "limited"
    assert expected_limitation in result["limitations"]
    assert list_incidents()[0]["diagnosis_status"] == "limited"


@pytest.mark.asyncio
async def test_persisted_evidence_timestamp_skew_uses_existing_limit(
    isolated_database,
):
    now = datetime.now(timezone.utc)
    _incident(_source_evidence(
        observed_at=now.isoformat(),
        source_timestamp=(now - timedelta(seconds=11)).isoformat(),
    ))

    result = await process_incident(list_incidents()[0])

    assert result["status"] == "limited"
    assert "persisted_evidence_timestamp_skew" in result["limitations"]


@pytest.mark.parametrize(
    ("deployments", "expected_limitation"),
    [
        ([], "mapped_workload_not_observed"),
        (
            [
                {"name": "orders", "desired_replicas": 1,
                 "available_replicas": 1},
                {"name": "orders", "desired_replicas": 1,
                 "available_replicas": 1},
            ],
            "mapped_workload_ambiguous",
        ),
    ],
    ids=["zero-matches", "multiple-matches"],
)
@pytest.mark.asyncio
async def test_observed_mapping_requires_exactly_one_deployment(
    isolated_database, monkeypatch, deployments, expected_limitation
):
    _incident()
    isolated_database.write_text(
        json.dumps({
            "services": {
                "order-service": [{
                    "namespace": "kybernaut-lab",
                    "deployment": "orders",
                }]
            }
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        orchestrator, "retrieve_runbooks", _successful_runbooks
    )
    monkeypatch.setattr(
        orchestrator, "analyze_root_cause", _success_rca()
    )
    executor_calls = _guard_remediation(monkeypatch)
    observer_calls = []

    def mocked_observation():
        observer_calls.append(True)
        return {
            "namespace": "kybernaut-lab",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "deployments": deployments,
            "incidents": [],
        }

    monkeypatch.setattr(
        orchestrator, "observe_namespace", mocked_observation
    )

    result = await process_active_incidents("order-service")

    assert result[0]["status"] == "limited"
    assert expected_limitation in result[0]["limitations"]
    diagnosis = list_incidents()[0]["diagnosis"]
    assert diagnosis["trusted_target"] is None
    assert diagnosis["proposed_action"]["execution_status"] == "not_submitted"
    assert len(observer_calls) == 1
    assert executor_calls == []


@pytest.mark.asyncio
async def test_unchanged_evidence_is_not_reprocessed_but_material_change_is(
    isolated_database, monkeypatch
):
    incident = _incident()
    calls = 0

    async def analyze(_state):
        nonlocal calls
        calls += 1
        return await _success_rca()({
            "diagnosis": {"service": "order-service"}
        })

    monkeypatch.setattr(orchestrator, "retrieve_runbooks", _successful_runbooks)
    monkeypatch.setattr(orchestrator, "analyze_root_cause", analyze)
    first = await process_incident(list_incidents()[0])
    repeated = await process_incident(list_incidents()[0])
    assert repeated["incident_id"] == "inc-order-cpu"
    assert repeated["status"] == "limited"
    assert repeated["audit_status"] == "success"
    assert repeated["reason"] == "trusted_workload_mapping_missing"
    assert repeated["retry_state"] is None
    assert calls == 1

    unchanged = list_incidents()[0]
    refreshed_at = datetime.now(timezone.utc).isoformat()
    refreshed = _source_evidence(
        value=0.5,
        observed_at=refreshed_at,
        source_timestamp=refreshed_at,
    )
    record_incident({
        **incident,
        "type": incident["incident_type"],
        "source_evidence": refreshed,
        "source_timestamp": refreshed["source_timestamp"],
    })
    refreshed_result = await process_incident(list_incidents()[0])
    assert first["status"] == "limited"
    assert refreshed_result["status"] == "limited"
    assert calls == 2

    changed_at = datetime.now(timezone.utc).isoformat()
    changed = _source_evidence(
        value=0.8,
        observed_at=changed_at,
        source_timestamp=changed_at,
    )
    record_incident({
        **incident,
        "type": incident["incident_type"],
        "source_evidence": changed,
        "source_timestamp": changed["source_timestamp"],
    })
    changed_result = await process_incident(list_incidents()[0])
    assert changed_result["status"] == "limited"
    assert calls == 3
    assert unchanged["diagnosis"]["source_evidence"]["source_timestamp"] != (
        list_incidents()[0]["source_evidence"]["source_timestamp"]
    )


@pytest.mark.asyncio
async def test_failed_diagnosis_retries_after_backoff_and_audits(
    isolated_database, monkeypatch
):
    incident = _incident()
    calls = 0

    async def flaky_rca(_state):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary diagnosis failure")
        return await _success_rca()({
            "diagnosis": {"service": "order-service"}
        })

    monkeypatch.setattr(orchestrator, "retrieve_runbooks", _successful_runbooks)
    monkeypatch.setattr(orchestrator, "analyze_root_cause", flaky_rca)
    failed = await process_incident(list_incidents()[0])
    assert failed["status"] == "failed"
    assert failed["audit_status"] == "success"
    backoff = await process_incident(list_incidents()[0])
    assert backoff["status"] == "failed"
    assert backoff["retry_state"] == "retry_backoff"
    assert backoff["audit_status"] == "success"
    assert backoff["reason"] == "RuntimeError"
    with database.get_connection() as connection:
        connection.execute(
            "UPDATE incident_diagnoses SET next_retry_at = ?",
            ("2000-01-01T00:00:00+00:00",),
        )

    retried = await process_incident(list_incidents()[0])
    assert retried["status"] == "limited"
    assert calls == 2
    assert list_incidents()[0]["diagnosis_status"] == "limited"
    assert len(audit_repository.list_events(incident["incident_id"])) == 2


@pytest.mark.asyncio
async def test_audit_persistence_failure_is_reported_without_remediation(
    isolated_database, monkeypatch
):
    incident = _incident()
    monkeypatch.setattr(orchestrator, "retrieve_runbooks", _successful_runbooks)
    monkeypatch.setattr(orchestrator, "analyze_root_cause", _success_rca())
    executor_calls = _guard_remediation(monkeypatch)

    def fail_audit(**_kwargs):
        raise RuntimeError("synthetic audit storage failure")

    monkeypatch.setattr(audit_repository, "record_event", fail_audit)

    result = await process_incident(list_incidents()[0])
    saved = list_incidents()[0]

    assert result["status"] == "failed"
    assert result["audit_status"] == "failed"
    assert "audit_persistence_failed" in result["limitations"]
    assert saved["diagnosis_status"] == "failed"
    assert saved["diagnosis"]["audit_status"] == "failed"
    assert saved["diagnosis"]["proposed_action"]["execution_status"] == (
        "not_submitted"
    )
    assert executor_calls == []


@pytest.mark.asyncio
async def test_unavailable_rca_preserves_classification_and_runbook(
    isolated_database, monkeypatch
):
    _incident()

    async def unavailable_rca(_state):
        return {
            "rca": None,
            "rca_status": "unavailable",
            "rca_error": "ProviderUnavailable",
        }

    monkeypatch.setattr(orchestrator, "retrieve_runbooks", _successful_runbooks)
    monkeypatch.setattr(orchestrator, "analyze_root_cause", unavailable_rca)

    result = await process_active_incidents("order-service")
    diagnosis = list_incidents()[0]["diagnosis"]

    assert result[0]["status"] == "failed"
    assert diagnosis["classification"]["incident_type"] == "cpu_spike"
    assert diagnosis["confidence"] == 0.5
    assert diagnosis["runbook_match"]["source"] == "cpu-runbook.md"
    assert diagnosis["root_cause_analysis"] is None
    assert diagnosis["root_cause_analysis_status"] == "unavailable"
    assert diagnosis["root_cause_analysis_error"] == "ProviderUnavailable"
    assert diagnosis["proposed_action"]["execution_status"] == "not_submitted"


@pytest.mark.asyncio
async def test_missing_evidence_is_limited_and_live_executor_is_never_called(
    isolated_database, monkeypatch
):
    incident = _incident()
    with database.get_connection() as connection:
        connection.execute(
            "UPDATE incidents SET source_evidence_json = NULL WHERE id = ?",
            (incident["id"],),
        )
    monkeypatch.setenv("KYBERNAUT_REMEDIATION_MODE", "live")
    import app.services.k8s_remediation as remediation

    def unexpected_executor(*_args, **_kwargs):
        raise AssertionError("Read-only diagnosis must not execute remediation")

    monkeypatch.setattr(remediation, "execute_action", unexpected_executor)
    result = await process_active_incidents("order-service")

    assert result[0]["status"] == "limited"
    assert "persisted_evidence_missing" in result[0]["limitations"]
    assert list_incidents()[0]["diagnosis_status"] == "limited"
    assert audit_repository.list_events("inc-order-cpu")[0]["event_type"] == (
        "automatic_diagnosis_limited"
    )


@pytest.mark.asyncio
async def test_malformed_persisted_evidence_is_safely_reported(
    isolated_database, monkeypatch
):
    incident = _incident()
    with database.get_connection() as connection:
        connection.execute(
            "UPDATE incidents SET source_evidence_json = ? WHERE id = ?",
            ("{", incident["id"]),
        )

    result = await process_active_incidents("order-service")

    assert result[0]["status"] == "limited"
    assert "persisted_evidence_invalid_json" in result[0]["limitations"]
    assert list_incidents()[0]["diagnosis_status"] == "limited"


@pytest.mark.asyncio
async def test_concurrent_diagnosis_claims_allow_only_one_worker(
    isolated_database,
):
    incident = _incident()
    digest = orchestrator._material_digest(list_incidents()[0])

    claims = await asyncio.gather(
        asyncio.to_thread(
            orchestrator.incident_repository.claim_incident_diagnosis,
            incident["id"],
            digest,
        ),
        asyncio.to_thread(
            orchestrator.incident_repository.claim_incident_diagnosis,
            incident["id"],
            digest,
        ),
    )

    assert sum(claim["claimed"] for claim in claims) == 1
