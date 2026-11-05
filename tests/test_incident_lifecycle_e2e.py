import asyncio
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.jwt_auth as jwt_auth
import app.database as database
import app.services.anomaly_runtime as anomaly_runtime
import app.services.application_anomaly_monitor as monitor_module
import app.services.incident_response_graph as graph
import app.services.k8s_remediation as remediation
from app.services import audit_repository
from app.services.anomaly_detector import FEATURE_NAMES
from app.services.anomaly_policy import DirectionAwareAnomalyPolicy
from app.services.application_anomaly_monitor import ApplicationAnomalyMonitor
from app.services.incident_detector import make_incident_id
from app.services.incident_repository import list_incidents
from app.services import incident_repository
from scripts.demo_incident import synthetic_observation


def _operator_token(*, secret: str, operator_id: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": operator_id,
            "iss": "kybernaut-lifecycle-e2e",
            "aud": "kybernaut-lifecycle-e2e-api",
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "permissions": ["read", "remediation"],
        },
        secret,
        algorithm="HS256",
    )


def _configure_test_environment(tmp_path, monkeypatch):
    db_path = tmp_path / "incident-lifecycle-e2e.sqlite3"
    monkeypatch.setattr(database, "DATABASE_PATH", db_path)

    jwt_secret = uuid.uuid4().hex + uuid.uuid4().hex
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "true")
    monkeypatch.setenv("KYBERNAUT_JWT_SECRET", jwt_secret)
    monkeypatch.setenv("KYBERNAUT_JWT_ISSUER", "kybernaut-lifecycle-e2e")
    monkeypatch.setenv("KYBERNAUT_JWT_AUDIENCE", "kybernaut-lifecycle-e2e-api")
    monkeypatch.setenv("KYBERNAUT_REMEDIATION_MODE", "dry_run")
    monkeypatch.setenv("KYBERNAUT_POLICY_AUTO_REMEDIATION", "false")
    monkeypatch.setenv("KYBERNAUT_POLICY_MIN_CONFIDENCE", "0.6")
    monkeypatch.setenv("KYBERNAUT_POLICY_AUTO_CONFIDENCE", "0.9")
    monkeypatch.setenv("KYBERNAUT_POLICY_MAX_REPLICAS", "5")
    monkeypatch.setenv("KYBERNAUT_POLICY_MAX_ACTIONS", "3")
    monkeypatch.setenv("KYBERNAUT_POLICY_WINDOW_SECONDS", "3600")
    monkeypatch.setenv("KYBERNAUT_VERIFY_ATTEMPTS", "3")
    monkeypatch.setenv("KYBERNAUT_VERIFY_INTERVAL", "0")
    monkeypatch.setattr(jwt_auth, "JWT_SECRET", jwt_secret)
    monkeypatch.setattr(jwt_auth, "JWT_ISSUER", "kybernaut-lifecycle-e2e")
    monkeypatch.setattr(
        jwt_auth, "JWT_AUDIENCE", "kybernaut-lifecycle-e2e-api"
    )
    database.initialize_database()
    return db_path, jwt_secret


def _install_deterministic_detector(monkeypatch, *, memory_value):
    baseline = [
        [
            10.0 + index * 0.1,
            0.1 + index * 0.001,
            100.0 + index,
            0.1 + index * 0.001,
        ]
        for index in range(30)
    ]
    policy = DirectionAwareAnomalyPolicy(threshold=5.0)
    policy.fit(baseline)
    monkeypatch.setitem(anomaly_runtime._policies, "order-service", policy)

    sample_time = "2026-10-24T03:00:00+00:00"
    values = dict(zip(FEATURE_NAMES, baseline[15]))
    values["memory_bytes"] = memory_value
    features = {
        **values,
        "observed_at": sample_time,
        "sample_timestamps": {
            feature: sample_time for feature in FEATURE_NAMES
        },
    }

    async def collect_synthetic_features(_client):
        return {"order-service": features}

    monkeypatch.setattr(
        anomaly_runtime, "collect_service_features", collect_synthetic_features
    )


def _poll_synthetic_telemetry(
    tmp_path, monkeypatch, *, memory_value, expect_incident
):
    _configure_test_environment(tmp_path, monkeypatch)
    _install_deterministic_detector(monkeypatch, memory_value=memory_value)

    async def no_diagnoses(_service):
        return []

    monkeypatch.setattr(monitor_module, "process_active_incidents", no_diagnoses)
    monitor = ApplicationAnomalyMonitor(
        60,
        services=("order-service",),
        lock_path=tmp_path / "monitor.lock",
    )
    result = asyncio.run(monitor.poll_service("order-service"))
    assert result["status"] == "evaluated"
    if expect_incident:
        assert result["persistence"]["status"] == "persisted"
        assert len(result["persistence"]["incident_ids"]) == 1
        assert result["persistence"]["incident_ids"][0] == make_incident_id(
            "order-service", "memory_leak"
        )
    else:
        assert result["persistence"]["status"] == "healthy"
        assert result["persistence"]["incident_ids"] == []
    return result


def _install_workflow(
    monkeypatch,
    incident_id,
    observations,
    *,
    executor,
    max_attempts=2,
):
    if incident_repository.get_latest_occurrence_by_incident_id(
        incident_id
    ) is None:
        incident_repository.record_incident({
            "incident_id": incident_id,
            "service": "kubernetes-lab",
            "type": "pod_container_failure",
            "severity": "high",
            "message": "Synthetic persisted lab occurrence",
            "fingerprint": f"e2e:{incident_id}",
            "source_evidence": {
                "source": "kubernetes",
                "namespace": "kybernaut-lab",
                "type": "pod_container_failure",
                "resource": "demo-web-e2e-1",
                "resource_kind": "Pod",
                "resource_uid": "e2e-pod-uid",
                "evidence": ["Synthetic persisted test failure"],
            },
        })
    monkeypatch.setenv("KYBERNAUT_MAX_REMEDIATION_ATTEMPTS", str(max_attempts))
    observation_queue = list(observations)
    observed_labels = []

    def observe_synthetic_namespace():
        result = observation_queue.pop(0)
        observed_labels.append(result["synthetic_observation"])
        return result

    async def no_external_rca(_state):
        return {
            "rca": None,
            "rca_status": "unavailable",
            "rca_error": "External RCA intentionally excluded from this test.",
        }

    live_runner_calls = []

    def fail_if_live_runner_called(args):
        live_runner_calls.append(args)
        raise AssertionError("The real kubectl runner must not be invoked.")

    monkeypatch.setattr(graph, "observe_namespace", observe_synthetic_namespace)
    monkeypatch.setattr(graph, "analyze_root_cause", no_external_rca)
    monkeypatch.setattr(graph, "execute_action", executor)
    monkeypatch.setattr(remediation, "default_runner", fail_if_live_runner_called)
    monkeypatch.setattr(graph, "response_graph", graph.build_response_graph())

    from app.api import operations

    app = FastAPI()
    app.include_router(operations.router)
    return app, observation_queue, observed_labels, live_runner_calls


def _auth_headers(*, jwt_secret, operator_id, idempotency=False):
    headers = {
        "Authorization": (
            "Bearer " + _operator_token(
                secret=jwt_secret, operator_id=operator_id
            )
        ),
    }
    if idempotency:
        headers["Idempotency-Key"] = f"lifecycle-{uuid.uuid4().hex}"
    return headers


def _synthetic_failure_observation(incident_id, label):
    return {
        **synthetic_observation(incident_id),
        "synthetic_observation": label,
    }


def _synthetic_healthy_observation(label):
    return {
        "namespace": "kybernaut-lab",
        "observed_at": "2026-10-24T03:01:00+00:00",
        "incidents": [],
        "incident_count": 0,
        "deployments": [{
            "name": "demo-web",
            "desired_replicas": 2,
            "available_replicas": 2,
            "ready_replicas": 2,
        }],
        "synthetic_observation": label,
    }


def _simulated_execution(proposal, *, ok):
    return {
        "action": proposal.action,
        "namespace": proposal.namespace,
        "resource": proposal.resource,
        "deployment": proposal.deployment,
        "mode": "simulated",
        "simulated": True,
        "effect": "none",
        "executed": False,
        "ok": ok,
    }


def test_operator_execute_approves_and_runs_synthetic_memory_remediation(
    tmp_path, monkeypatch
):
    persistence = _poll_synthetic_telemetry(
        tmp_path, monkeypatch, memory_value=10000.0, expect_incident=True
    )
    assert persistence["status"] == "evaluated"
    assert persistence["persistence"]["status"] == "persisted"
    assert len(persistence["persistence"]["incident_ids"]) == 1
    telemetry_incident_id = persistence["persistence"]["incident_ids"][0]
    assert telemetry_incident_id == make_incident_id(
        "order-service", "memory_leak"
    )

    incidents = list_incidents()
    assert len(incidents) == 1
    assert incidents[0]["incident_id"] == telemetry_incident_id
    assert incidents[0]["incident_type"] == "memory_leak"
    source_evidence = incidents[0]["source_evidence"]
    assert source_evidence["source"] == "prometheus"
    assert source_evidence["metric"]["name"] == "memory_bytes"
    assert source_evidence["metric"]["classification"] == (
        "actionable_candidate"
    )
    incident_id = "inc-k8s-lifecycle-memory-test"

    failure = _synthetic_failure_observation(incident_id, "initial_failure")
    healthy_recheck = _synthetic_failure_observation(
        incident_id, "approval_recheck"
    )
    observations = [
        failure,
        healthy_recheck,
        _synthetic_healthy_observation("recovery_1"),
        _synthetic_healthy_observation("recovery_2"),
    ]
    executor_calls = []

    def simulated_executor(proposal):
        executor_calls.append(proposal.action)
        return _simulated_execution(proposal, ok=True)

    app, remaining, observed_labels, live_runner_calls = _install_workflow(
        monkeypatch, incident_id, observations, executor=simulated_executor
    )
    jwt_secret = jwt_auth.JWT_SECRET
    requester = "synthetic-requester"
    requester_headers = _auth_headers(
        jwt_secret=jwt_secret,
        operator_id=requester,
        idempotency=True,
    )

    with TestClient(app) as client:
        started = client.post(
            "/workflow/kubernetes-lab/respond",
            headers=requester_headers,
            json={"incident_id": incident_id},
        )
        assert started.status_code == 200
        pending = started.json()
        assert pending["status"] == "awaiting_approval"
        assert pending["classification"]["incident_type"] == "memory_leak"
        assert any(
            "OOMKilled" in evidence
            for evidence in pending["classification"]["evidence"]
        )
        assert pending["proposal"]["action"] == "restart_pod"
        assert pending["policy_decision"]["allowed"] is True
        assert pending["policy_decision"]["requires_approval"] is True
        assert pending["approval_id"]
        assert executor_calls == []

        approval = audit_repository.get_approval(pending["approval_id"])
        assert approval["status"] == "PENDING"
        assert approval["requested_by"] == requester

        completed_response = client.post(
            f"/approvals/{pending['approval_id']}/decision",
            headers=requester_headers,
            json={
                "decision": "approved",
                "reason": "Approved from the Console Execute action.",
            },
        )
        assert completed_response.status_code == 200
        completed = completed_response.json()

    assert completed["status"] == "simulation_complete"
    assert executor_calls == ["restart_pod"]
    assert completed["execution"]["mode"] == "simulated"
    assert completed["execution"]["simulated"] is True
    assert completed["execution"]["effect"] == "none"
    assert completed["execution"]["executed"] is False
    assert completed["verification"] == {
        "recovered": False,
        "reason": "simulation_not_executed",
    }
    assert (
        completed["contract"]["verification_status"] == "not_run"
    )
    assert "optimization" not in completed["execution"]
    assert "realized_savings" not in completed["execution"]
    assert observed_labels == [
        "initial_failure",
        "approval_recheck",
    ]
    assert [item["synthetic_observation"] for item in remaining] == [
        "recovery_1",
        "recovery_2",
    ]
    assert live_runner_calls == []

    approval = audit_repository.get_approval(pending["approval_id"])
    assert approval["status"] == "APPROVED"
    assert approval["decided_by"] == requester
    assert approval["requested_by"] == approval["decided_by"]

    with closing(database.get_connection()) as connection:
        action_claim = connection.execute(
            "SELECT * FROM action_claims WHERE action_claim_id = ?",
            (pending["action_claim_id"],),
        ).fetchone()
    assert action_claim is not None
    assert action_claim["status"] == "SIMULATED"
    assert action_claim["approval_id"] == pending["approval_id"]
    assert json.loads(action_claim["execution_json"])["simulated"] is True
    assert action_claim["verification_json"] is None

    with sqlite3.connect(database.DATABASE_PATH) as connection:
        events = connection.execute(
            "SELECT event_type, outcome, details FROM audit_events "
            "WHERE incident_ref = ? ORDER BY id",
            (incident_id,),
        ).fetchall()

    event_types = [row[0] for row in events]
    required_order = [
        "evidence_collected",
        "incident_classified",
        "action_proposed",
        "policy_evaluated",
        "approval_requested",
        "approval_approved",
        "remediation_started",
        "remediation_simulated",
        "simulation_complete",
    ]
    event_positions = [event_types.index(event) for event in required_order]
    assert event_positions == sorted(event_positions)

    execution_event = next(
        row for row in events if row[0] == "remediation_simulated"
    )
    execution_details = json.loads(execution_event[2])
    assert execution_details["mode"] == "simulated"
    assert execution_details["simulated"] is True
    assert execution_details["effect"] == "none"
    assert "remediation_executed" not in event_types
    assert "verification_completed" not in event_types
    assert "incident_recovery_verified" not in event_types


def test_healthy_synthetic_telemetry_does_not_create_incident(
    tmp_path, monkeypatch
):
    result = _poll_synthetic_telemetry(
        tmp_path, monkeypatch, memory_value=115.0, expect_incident=False
    )

    assert result["status"] == "evaluated"
    assert result["persistence"]["status"] == "healthy"
    assert result["persistence"]["incident_ids"] == []
    assert result["diagnoses"] == []
    assert list_incidents() == []


def test_simulated_executor_failure_never_claims_recovery(
    tmp_path, monkeypatch
):
    persistence = _poll_synthetic_telemetry(
        tmp_path, monkeypatch, memory_value=10000.0, expect_incident=True
    )
    incident_id = "inc-k8s-lifecycle-simulated-failure"
    executor_calls = []

    def failed_simulated_executor(proposal):
        executor_calls.append(proposal.action)
        return {
            **_simulated_execution(proposal, ok=False),
            "simulated": True,
            "effect": "none",
        }

    app, remaining, observed_labels, live_runner_calls = _install_workflow(
        monkeypatch,
        incident_id,
        [
            _synthetic_failure_observation(incident_id, "initial_failure"),
            _synthetic_failure_observation(incident_id, "approval_recheck"),
        ],
        executor=failed_simulated_executor,
    )
    jwt_secret = jwt_auth.JWT_SECRET

    with TestClient(app) as client:
        started = client.post(
            "/workflow/kubernetes-lab/respond",
            headers=_auth_headers(
                jwt_secret=jwt_secret,
                operator_id="failure-requester",
                idempotency=True,
            ),
            json={"incident_id": incident_id},
        )
        pending = started.json()
        assert started.status_code == 200
        assert pending["status"] == "awaiting_approval"
        assert executor_calls == []

        completed_response = client.post(
            f"/approvals/{pending['approval_id']}/decision",
            headers=_auth_headers(
                jwt_secret=jwt_secret,
                operator_id="failure-approver",
            ),
            json={"decision": "approved", "reason": "Synthetic test."},
        )
        assert completed_response.status_code == 200
        completed = completed_response.json()

    assert completed["status"] == "approved"
    assert completed["execution_status"] == "completed"
    assert completed["execution"]["status"] == "escalated"
    assert completed["execution"]["execution"]["mode"] == "simulated"
    assert completed["execution"]["execution"]["simulated"] is True
    assert completed["execution"]["execution"]["ok"] is False
    assert completed["execution"]["verification"] is None
    assert executor_calls == ["restart_pod"]
    assert observed_labels == ["initial_failure", "approval_recheck"]
    assert remaining == []
    assert live_runner_calls == []

    with sqlite3.connect(database.DATABASE_PATH) as connection:
        claim = connection.execute(
            "SELECT status, execution_json FROM action_claims "
            "WHERE action_claim_id = ?",
            (pending["action_claim_id"],),
        ).fetchone()
        events = connection.execute(
            "SELECT event_type, outcome FROM audit_events "
            "WHERE incident_ref = ? ORDER BY id",
            (incident_id,),
        ).fetchall()
    assert claim[0] == "SIMULATION_FAILED"
    assert json.loads(claim[1])["simulated"] is True
    assert ("remediation_simulation_failed", "failed") in events
    assert "verification_completed" not in [event[0] for event in events]
    assert "incident_recovery_verified" not in [event[0] for event in events]


def test_contradictory_simulated_execution_fails_closed(
    tmp_path, monkeypatch
):
    persistence = _poll_synthetic_telemetry(
        tmp_path, monkeypatch, memory_value=10000.0, expect_incident=True
    )
    incident_id = "inc-k8s-lifecycle-contradictory"

    def contradictory_executor(proposal):
        return {
            **_simulated_execution(proposal, ok=True),
            "executed": True,
        }

    app, remaining, observed_labels, live_runner_calls = _install_workflow(
        monkeypatch,
        incident_id,
        [
            _synthetic_failure_observation(incident_id, "initial_failure"),
            _synthetic_failure_observation(incident_id, "approval_recheck"),
        ],
        executor=contradictory_executor,
    )
    jwt_secret = jwt_auth.JWT_SECRET

    with TestClient(app) as client:
        started = client.post(
            "/workflow/kubernetes-lab/respond",
            headers=_auth_headers(
                jwt_secret=jwt_secret,
                operator_id="contradiction-requester",
                idempotency=True,
            ),
            json={"incident_id": incident_id},
        )
        pending = started.json()
        assert started.status_code == 200

        completed_response = client.post(
            f"/approvals/{pending['approval_id']}/decision",
            headers=_auth_headers(
                jwt_secret=jwt_secret,
                operator_id="contradiction-approver",
            ),
            json={"decision": "approved", "reason": "Synthetic test."},
        )
        assert completed_response.status_code == 200
        completed = completed_response.json()

    assert completed["status"] == "approved"
    assert completed["execution_status"] == "completed"
    assert completed["execution"]["status"] == "escalated"
    assert completed["execution"]["execution"]["mode"] == "simulated"
    assert completed["execution"]["execution"]["executed"] is False
    assert completed["execution"]["execution"]["ok"] is False
    assert (
        completed["execution"]["execution"]["error"]
        == "invalid_execution_result"
    )
    assert completed["execution"]["verification"] is None
    assert observed_labels == ["initial_failure", "approval_recheck"]
    assert remaining == []
    assert live_runner_calls == []

    with sqlite3.connect(database.DATABASE_PATH) as connection:
        claim = connection.execute(
            "SELECT status FROM action_claims WHERE action_claim_id = ?",
            (pending["action_claim_id"],),
        ).fetchone()
        events = connection.execute(
            "SELECT event_type FROM audit_events "
            "WHERE incident_ref = ? ORDER BY id",
            (incident_id,),
        ).fetchall()
    assert claim == ("EXECUTION_UNKNOWN",)
    event_types = [event[0] for event in events]
    assert "remediation_result_invalid" in event_types
    assert "verification_completed" not in event_types
    assert "incident_recovery_verified" not in event_types


def test_verification_failure_never_claims_recovery(
    tmp_path, monkeypatch
):
    persistence = _poll_synthetic_telemetry(
        tmp_path, monkeypatch, memory_value=10000.0, expect_incident=True
    )
    incident_id = "inc-k8s-lifecycle-verification-failure"

    def mocked_live_executor(proposal):
        return {
            "action": proposal.action,
            "namespace": proposal.namespace,
            "resource": proposal.resource,
            "deployment": proposal.deployment,
            "mode": "live",
            "executed": True,
            "ok": True,
        }

    unhealthy = {
        **_synthetic_failure_observation(incident_id, "verification_unhealthy"),
        "deployments": [{
            "name": "demo-web",
            "desired_replicas": 2,
            "available_replicas": 1,
            "ready_replicas": 1,
        }],
    }
    app, remaining, observed_labels, live_runner_calls = _install_workflow(
        monkeypatch,
        incident_id,
        [
            _synthetic_failure_observation(incident_id, "initial_failure"),
            _synthetic_failure_observation(incident_id, "approval_recheck"),
            unhealthy,
            unhealthy,
            unhealthy,
        ],
        executor=mocked_live_executor,
        max_attempts=1,
    )
    jwt_secret = jwt_auth.JWT_SECRET

    with TestClient(app) as client:
        started = client.post(
            "/workflow/kubernetes-lab/respond",
            headers=_auth_headers(
                jwt_secret=jwt_secret,
                operator_id="verification-requester",
                idempotency=True,
            ),
            json={"incident_id": incident_id},
        )
        pending = started.json()
        assert started.status_code == 200

        completed_response = client.post(
            f"/approvals/{pending['approval_id']}/decision",
            headers=_auth_headers(
                jwt_secret=jwt_secret,
                operator_id="verification-approver",
            ),
            json={"decision": "approved", "reason": "Synthetic test."},
        )
        assert completed_response.status_code == 200
        completed = completed_response.json()

    assert completed["status"] == "approved"
    assert completed["execution_status"] == "completed"
    assert completed["execution"]["status"] == "escalated"
    assert completed["execution"]["execution"]["mode"] == "live"
    assert completed["execution"]["verification"]["recovered"] is False
    assert completed["execution"]["verification"]["attempts_used"] == 3
    assert completed["execution"]["verification"]["consecutive_healthy"] == 0
    assert observed_labels == [
        "initial_failure",
        "approval_recheck",
        "verification_unhealthy",
        "verification_unhealthy",
        "verification_unhealthy",
    ]
    assert remaining == []
    assert live_runner_calls == []

    with sqlite3.connect(database.DATABASE_PATH) as connection:
        claim = connection.execute(
            "SELECT status, verification_json FROM action_claims "
            "WHERE action_claim_id = ?",
            (pending["action_claim_id"],),
        ).fetchone()
        events = connection.execute(
            "SELECT event_type, outcome, details FROM audit_events "
            "WHERE incident_ref = ? ORDER BY id",
            (incident_id,),
        ).fetchall()
    assert claim[0] == "VERIFICATION_FAILED"
    assert json.loads(claim[1])["recovered"] is False
    assert ("verification_completed", "not_recovered") in [
        (event[0], event[1]) for event in events
    ]
    assert "incident_recovery_verified" not in [event[0] for event in events]
