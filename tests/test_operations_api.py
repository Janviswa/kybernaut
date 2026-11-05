from datetime import datetime, timedelta, timezone
import json
import logging

import pytest
from pydantic import ValidationError

import app.database as database
import app.api.operations as operations
import app.services.incident_workflow as incident_workflow
from app.services import action_claim_repository as claims
from app.services import audit_repository as audit
from app.services import incident_handoff_repository as handoffs
from app.services import incident_repository
from app.services.incident_repository import list_incidents
from tests.conftest import create_operator_token


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "api.db")


def auth(perms=("read", "remediation")):
    return {"Authorization": "Bearer " + create_operator_token(
        permissions=list(perms))}


def _actionable_telemetry():
    observed_at = datetime.now(timezone.utc).isoformat()
    return {
        "status": "evaluated",
        "observed_at": observed_at,
        "actionable_candidates": ["cpu_cores_used"],
        "threshold": 5.0,
        "sample_timestamps": {"cpu_cores_used": observed_at},
        "features": {
            "cpu_cores_used": {
                "value": 0.5,
                "baseline_median": 0.2,
                "deviation_score": 6.0,
                "direction": "high",
                "classification": "anomaly",
            },
        },
    }


def _incomplete_telemetry():
    observed_at = datetime.now(timezone.utc).isoformat()
    return {
        "status": "evaluated",
        "observed_at": observed_at,
        "actionable_candidates": ["cpu_cores_used"],
        "threshold": 5.0,
        "sample_timestamps": {"cpu_cores_used": observed_at},
        "features": {},
    }


def _persisted_kubernetes_incident(
    incident_id="inc-k8s-api-test",
    *,
    resource="demo-web-api-test",
    source_timestamp=None,
    resource_kind="Pod",
    incident_type="pod_container_failure",
):
    observed_at = source_timestamp or datetime.now(timezone.utc).isoformat()
    evidence = {
        "source": "kubernetes",
        "namespace": "kybernaut-lab",
        "type": incident_type,
        "resource": resource,
        "resource_kind": resource_kind,
        "resource_uid": (
            "api-test-pod-uid" if resource_kind == "Pod" else None
        ),
        "source_timestamp": source_timestamp,
        "observed_at": observed_at,
        "evidence": ["Container waiting: CrashLoopBackOff"],
        "recommendation": "Inspect the pod.",
    }
    return incident_repository.record_incident({
        "incident_id": incident_id,
        "service": "kubernetes-lab",
        "type": incident_type,
        "severity": "high",
        "message": "Container waiting: CrashLoopBackOff",
        "fingerprint": (
            f"k8s:kybernaut-lab:{resource_kind}:{resource}:{incident_type}"
        ),
        "source_timestamp": source_timestamp,
        "source_evidence": evidence,
    })


def _mock_advisory_evidence(monkeypatch, telemetry):
    async def diagnosis(service):
        return {
            "service": service,
            "status": "unknown",
            "evidence_status": "unavailable",
            "findings": [],
        }

    async def anomaly(_service):
        return telemetry

    async def unavailable_rca(_evidence):
        raise RuntimeError("synthetic RCA provider unavailable")

    monkeypatch.setattr(incident_workflow, "diagnose_service", diagnosis)
    monkeypatch.setattr(
        incident_workflow, "evaluate_service_anomaly", anomaly
    )
    monkeypatch.setattr(
        incident_workflow,
        "observe_namespace",
        lambda: {
            "namespace": "kybernaut-lab",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "incidents": [],
            "incident_count": 0,
            "deployments": [],
        },
    )
    monkeypatch.setattr(
        incident_workflow, "retrieve_runbooks", lambda *_args: []
    )
    monkeypatch.setattr(incident_workflow, "generate_rca", unavailable_rca)


def test_routes_require_authentication(api_client):
    for method, path in [("get", "/approvals"), ("get", "/audit")]:
        assert getattr(api_client, method)(path).status_code == 401


@pytest.mark.parametrize(
    ("headers", "expected_status"),
    [
        ({}, 401),
        (auth(("remediation",)), 403),
    ],
    ids=["unauthenticated", "missing-read-permission"],
)
def test_kubernetes_investigation_rejects_unauthorized_before_work(
    api_client, monkeypatch, headers, expected_status
):
    _persisted_kubernetes_incident(source_timestamp=datetime.now(
        timezone.utc
    ).isoformat())
    calls = []

    async def guarded_workflow(*args, **kwargs):
        calls.append((args, kwargs))
        return {"unexpected": True}

    monkeypatch.setattr(operations, "run_response_workflow", guarded_workflow)
    response = api_client.post(
        "/incidents/inc-k8s-api-test/investigate",
        headers={**headers, "Idempotency-Key": "k8s-auth-check"},
    )

    assert response.status_code == expected_status
    assert calls == []


def test_kubernetes_investigation_binds_occurrence_and_forces_hitl(
    api_client, monkeypatch
):
    occurrence = _persisted_kubernetes_incident(
        source_timestamp=datetime.now(timezone.utc).isoformat()
    )
    calls = []

    async def workflow(service, **kwargs):
        calls.append((service, kwargs))
        return {
            "incident_id": occurrence["incident_id"],
            "status": "awaiting_approval",
            "approval_id": "approval-test",
        }

    monkeypatch.setattr(operations, "run_response_workflow", workflow)
    response = api_client.post(
        f"/incidents/{occurrence['incident_id']}/investigate",
        headers={
            **auth(("read",)),
            "Idempotency-Key": "k8s-investigate-api-test",
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "awaiting_approval"
    assert calls == [(
        "kubernetes-lab",
        {
            "incident_id": occurrence["incident_id"],
            "requested_by": "test-operator",
            "workflow_claim_id": calls[0][1]["workflow_claim_id"],
            "occurrence_bound": True,
            "force_approval": True,
            "require_active_occurrence": True,
        },
    )]


def test_kubernetes_investigation_replays_existing_approval_without_rerunning(
    api_client, monkeypatch
):
    occurrence = _persisted_kubernetes_incident(
        "inc-k8s-existing-approval",
        source_timestamp=datetime.now(timezone.utc).isoformat(),
    )
    idempotency_key = "console-k8s-inc-k8s-existing-approval"
    claim, created, conflict = claims.create_workflow_claim(
        operator_id="test-operator",
        service="kubernetes-lab",
        idempotency_key=idempotency_key,
        incident_ref=occurrence["incident_id"],
        request_correlation=occurrence["incident_id"],
        incident_occurrence_id=occurrence["id"],
    )
    assert created and not conflict
    approval = audit.create_approval(
        incident_ref=occurrence["incident_id"],
        proposed_action={"action": "restart_pod", "deployment": "demo-web"},
        context={},
        requested_by="test-operator",
    )
    expected = {
        "incident_id": occurrence["incident_id"],
        "status": "awaiting_approval",
        "approval_id": approval["approval_id"],
    }
    assert claims.finish_workflow_claim(claim["claim_id"], expected)
    workflow_calls = []

    async def forbidden_workflow(*args, **kwargs):
        workflow_calls.append((args, kwargs))
        raise AssertionError("an idempotent replay must not rerun the graph")

    monkeypatch.setattr(operations, "run_response_workflow", forbidden_workflow)
    response = api_client.post(
        f"/incidents/{occurrence['incident_id']}/investigate",
        headers={
            **auth(("read",)),
            "Idempotency-Key": idempotency_key,
        },
    )

    assert response.status_code == 200, response.json()
    assert response.json() == expected
    assert workflow_calls == []
    assert len(audit.list_approvals("PENDING")) == 1


def test_kubernetes_investigation_finishes_claim_when_graph_does_not_start(
    api_client, monkeypatch
):
    occurrence = _persisted_kubernetes_incident(
        source_timestamp=datetime.now(timezone.utc).isoformat()
    )
    workflow_claim_ids = []

    async def workflow(*args, workflow_claim_id, **kwargs):
        workflow_claim_ids.append(workflow_claim_id)
        raise operations.WorkflowNotStarted("Persisted evidence is stale")

    monkeypatch.setattr(operations, "run_response_workflow", workflow)
    response = api_client.post(
        f"/incidents/{occurrence['incident_id']}/investigate",
        headers={
            **auth(("read",)),
            "Idempotency-Key": "k8s-investigate-stale-start",
        },
    )

    assert response.status_code == 409
    assert workflow_claim_ids
    claim = claims.get_workflow_claim(workflow_claim_ids[0])
    assert claim["status"] == "COMPLETED"
    assert json.loads(claim["response_json"])["status"] == "blocked"


def test_kubernetes_investigation_endpoint_runs_existing_response_graph(
    api_client, monkeypatch
):
    from app.services import incident_response_graph as response_graph

    observed_at = datetime.now(timezone.utc).isoformat()
    occurrence = _persisted_kubernetes_incident(
        "inc-k8s-api-graph",
        source_timestamp=observed_at,
    )
    observation = {
        "namespace": "kybernaut-lab",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "incidents": [{
            "namespace": "kybernaut-lab",
            "type": "pod_container_failure",
            "severity": "high",
            "resource": "demo-web-api-test",
            "resource_kind": "Pod",
            "resource_uid": "api-test-pod-uid",
            "source_timestamp": observed_at,
            "evidence": ["Container waiting: CrashLoopBackOff"],
        }],
        "incident_count": 1,
        "deployments": [{
            "name": "demo-web",
            "desired_replicas": 2,
            "available_replicas": 2,
            "ready_replicas": 2,
        }],
    }

    async def unavailable_rca(_state):
        return {
            "rca": None,
            "rca_status": "unavailable",
            "rca_error": "test_provider_unavailable",
        }

    monkeypatch.setattr(response_graph, "observe_namespace", lambda: observation)
    monkeypatch.setattr(response_graph, "analyze_root_cause", unavailable_rca)
    monkeypatch.setattr(
        response_graph, "response_graph", response_graph.build_response_graph()
    )

    response = api_client.post(
        f"/incidents/{occurrence['incident_id']}/investigate",
        headers={
            **auth(("read",)),
            "Idempotency-Key": "k8s-investigate-api-graph",
        },
    )

    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "awaiting_approval"
    assert result["incident_id"] == occurrence["incident_id"]
    assert result["occurrence_id"] == occurrence["id"]
    assert result["rca_status"] == "unavailable"
    assert result["policy_decision"]["requires_approval"] is True
    assert result["policy_decision"]["auto_execute"] is False
    assert result["approval_id"]
    assert "approval_requested" in {
        event["event_type"]
        for event in audit.list_events(occurrence["incident_id"])
    }


def test_kubernetes_investigation_accepts_allowlisted_crash_simulator_deployment(
    api_client, monkeypatch
):
    observed_at = datetime.now(timezone.utc).isoformat()
    occurrence = _persisted_kubernetes_incident(
        "inc-k8s-sim-pod-crash-deployment",
        resource="sim-pod-crash",
        source_timestamp=observed_at,
        resource_kind="Deployment",
        incident_type="deployment_unavailable_replicas",
    )
    workflow_calls = []

    async def workflow(*args, **kwargs):
        workflow_calls.append((args, kwargs))
        return {
            "status": "investigated",
            "incident_id": occurrence["incident_id"],
        }

    monkeypatch.setattr(operations, "run_response_workflow", workflow)
    response = api_client.post(
        f"/incidents/{occurrence['incident_id']}/investigate",
        headers={
            **auth(("read",)),
            "Idempotency-Key": "k8s-sim-pod-crash-investigate",
        },
    )

    assert response.status_code == 200
    assert response.json()["incident_id"] == occurrence["incident_id"]
    assert workflow_calls[0][0] == ("kubernetes-lab",)
    assert workflow_calls[0][1]["force_approval"] is True
    assert workflow_calls[0][1]["require_active_occurrence"] is True


def test_kubernetes_investigation_rejects_unsupported_workload(
    api_client, monkeypatch
):
    occurrence = _persisted_kubernetes_incident(
        resource="unmapped-app-pod",
        source_timestamp=datetime.now(timezone.utc).isoformat(),
    )
    calls = []

    async def guarded_workflow(*args, **kwargs):
        calls.append((args, kwargs))
        return {"unexpected": True}

    monkeypatch.setattr(operations, "run_response_workflow", guarded_workflow)
    response = api_client.post(
        f"/incidents/{occurrence['incident_id']}/investigate",
        headers={
            **auth(("read",)),
            "Idempotency-Key": "k8s-unmapped-target",
        },
    )

    assert response.status_code == 400
    assert "outside the supported lab allowlist" in response.json()["detail"]
    assert calls == []


def test_kubernetes_investigation_rejects_legacy_record_without_timestamp(
    api_client, monkeypatch
):
    occurrence = _persisted_kubernetes_incident()
    calls = []

    async def guarded_workflow(*args, **kwargs):
        calls.append((args, kwargs))
        return {"unexpected": True}

    monkeypatch.setattr(operations, "run_response_workflow", guarded_workflow)
    response = api_client.post(
        f"/incidents/{occurrence['incident_id']}/investigate",
        headers={
            **auth(("read",)),
            "Idempotency-Key": "k8s-no-timestamp",
        },
    )

    assert response.status_code == 409
    assert "timestamped Kubernetes evidence" in response.json()["detail"]
    assert calls == []


@pytest.mark.parametrize(
    ("credential_kind", "expected_status"),
    [
        ("missing", 401),
        ("invalid", 401),
        ("without_read", 403),
    ],
    ids=["missing", "invalid", "without-read"],
)
def test_incident_history_rejects_unauthorized_before_retrieval(
    api_client, monkeypatch, credential_kind, expected_status
):
    import app.main as main

    calls = []

    def guarded_list_incidents(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    monkeypatch.setattr(main, "list_incidents", guarded_list_incidents)

    headers = {}
    if credential_kind == "invalid":
        headers["Authorization"] = "Bearer invalid-token"
    elif credential_kind == "without_read":
        headers.update(auth(("remediation",)))

    response = api_client.get(
        "/incidents/history",
        headers=headers,
    )

    assert response.status_code == expected_status
    assert calls == []


@pytest.mark.parametrize(
    ("credential_kind", "expected_status"),
    [
        ("missing", 401),
        ("insufficient", 403),
        ("expired", 401),
    ],
    ids=["missing", "insufficient-permission", "expired"],
)
def test_response_workflow_rejects_unauthorized_before_work(
    api_client, monkeypatch, credential_kind, expected_status
):
    calls = []

    async def guarded_workflow(*args, **kwargs):
        calls.append((args, kwargs))
        return {"unexpected": True}

    monkeypatch.setattr(operations, "run_response_workflow", guarded_workflow)

    headers = {"Idempotency-Key": "auth-regression"}
    if credential_kind == "insufficient":
        headers.update(auth(("read",)))
    elif credential_kind == "expired":
        token = create_operator_token(
            permissions=["remediation"],
            expires_delta=timedelta(seconds=-10),
        )
        headers["Authorization"] = f"Bearer {token}"

    response = api_client.post(
        "/workflow/order-service/respond",
        headers=headers,
        json={"incident_id": "INC-auth-regression"},
    )

    assert response.status_code == expected_status
    assert calls == []


@pytest.mark.parametrize(
    ("credential_kind", "expected_status"),
    [
        ("missing", 401),
        ("invalid", 401),
        ("without_read", 403),
        ("expired", 401),
    ],
    ids=["missing", "invalid", "without-read", "expired"],
)
def test_advisory_get_rejects_unauthorized_before_work(
    api_client, monkeypatch, credential_kind, expected_status
):
    import app.main as main

    calls = []

    async def guarded_workflow(service):
        calls.append(service)
        return {"unexpected": True}

    monkeypatch.setattr(main, "run_incident_workflow", guarded_workflow)

    headers = {}
    if credential_kind == "invalid":
        headers = {"Authorization": "Bearer invalid-token"}
    elif credential_kind == "without_read":
        headers = auth(("remediation",))
    elif credential_kind == "expired":
        token = create_operator_token(
            permissions=["read"],
            expires_delta=timedelta(seconds=-10),
        )
        headers = {"Authorization": f"Bearer {token}"}

    response = api_client.get(
        "/workflow/order-service",
        headers=headers,
    )

    assert response.status_code == expected_status
    assert calls == []


def test_decision_requires_remediation_permission_but_not_execution_token(api_client):
    _persisted_kubernetes_incident(
        incident_id="I",
        source_timestamp=datetime.now(timezone.utc).isoformat(),
    )
    a = audit.create_approval(incident_ref="I", proposed_action={},
                              context={}, requested_by="x")
    url = f"/approvals/{a['approval_id']}/decision"
    body = {"decision": "rejected"}
    assert api_client.post(url, json=body, headers=auth(("read",))).status_code == 403
    ok = api_client.post(url, json=body, headers=auth())
    assert ok.status_code == 200 and ok.json()["status"] == "rejected"
    again = api_client.post(url, json=body, headers=auth())
    assert again.status_code == 409


def test_approval_list_audits_and_preserves_requests_for_inactive_incidents(
    api_client,
):
    occurrence = _persisted_kubernetes_incident(
        incident_id="inc-resolved-approval",
        source_timestamp=datetime.now(timezone.utc).isoformat(),
    )
    with database.get_connection() as connection:
        connection.execute(
            "UPDATE incidents SET status = 'RESOLVED' WHERE id = ?",
            (occurrence["id"],),
        )
    resolved = audit.create_approval(
        incident_ref=occurrence["incident_id"],
        proposed_action={"action": "restart_pod"},
        context={},
        requested_by="operator-a",
    )
    missing = audit.create_approval(
        incident_ref="inc-missing-approval",
        proposed_action={"action": "restart_pod"},
        context={},
        requested_by="operator-b",
    )

    pending = api_client.get("/approvals?status=PENDING", headers=auth())
    cancelled = api_client.get("/approvals?status=CANCELLED", headers=auth())

    assert pending.status_code == 200
    assert pending.json()["approvals"] == []
    assert cancelled.status_code == 200
    preserved = {
        item["approval_id"]: item for item in cancelled.json()["approvals"]
    }
    assert preserved[resolved["approval_id"]]["decision_reason"] == (
        "incident_resolved"
    )
    assert preserved[missing["approval_id"]]["decision_reason"] == (
        "incident_not_found"
    )
    assert {
        event["event_type"]
        for event in audit.list_events(occurrence["incident_id"])
    } == {"approval_requested", "approval_cancelled"}


def test_rejected_approval_decision_does_not_start_execution(
    api_client, monkeypatch
):
    _persisted_kubernetes_incident(
        incident_id="inc-reject-without-execution",
        source_timestamp=datetime.now(timezone.utc).isoformat(),
    )
    approval = audit.create_approval(
        incident_ref="inc-reject-without-execution",
        proposed_action={},
        context={},
        requested_by="requester",
    )
    calls = []

    async def guarded_execute(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("decision must not start execution")

    monkeypatch.setattr(
        operations, "resume_approved_after_decision", guarded_execute
    )

    response = api_client.post(
        f"/approvals/{approval['approval_id']}/decision",
        json={"decision": "rejected", "reason": "not safe"},
        headers=auth(),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert calls == []


def test_approved_decision_dispatches_without_shared_token(api_client, monkeypatch):
    incident_id = "inc-approved-awaiting-execution"
    _persisted_kubernetes_incident(
        incident_id=incident_id,
        source_timestamp=datetime.now(timezone.utc).isoformat(),
    )
    workflow_claim, created, conflict = claims.create_workflow_claim(
        operator_id="requester",
        service="kubernetes-lab",
        idempotency_key="separate-execution-test",
        incident_ref=incident_id,
    )
    assert created and not conflict
    proposal = {"action": "restart_pod", "deployment": "demo-web"}
    policy = {"allowed": True, "requires_approval": True}
    action_claim = claims.reserve_action_claim(
        workflow_claim_id=workflow_claim["claim_id"],
        attempt_no=0,
        logical_key="separate-execution-test",
        deployment="demo-web",
        action=proposal,
        policy=policy,
        max_actions=2,
        window_seconds=3600,
        auto_authorized=False,
    )
    approval = audit.create_approval(
        incident_ref=incident_id,
        proposed_action=proposal,
        context={"policy": policy},
        requested_by="requester",
        action_claim_id=action_claim["action_claim_id"],
    )
    assert claims.finish_workflow_claim(
        workflow_claim["claim_id"], {"status": "awaiting_approval"}
    )
    calls = []

    async def execute(*args, **kwargs):
        calls.append((args, kwargs))
        return {"status": "dry_run_complete"}

    monkeypatch.setattr(operations, "resume_after_decision", execute)
    response = api_client.post(
        f"/approvals/{approval['approval_id']}/decision",
        json={"decision": "approved", "reason": "reviewed"},
        headers=auth(),
    )

    assert response.status_code == 200
    assert response.json() == {"status": "dry_run_complete"}
    assert calls == [(
        (
            approval["approval_id"],
            "approved",
            "test-operator",
            "reviewed",
        ),
        {},
    )]


def test_separate_execution_endpoint_is_not_registered(api_client):
    response = api_client.post(
        "/approvals/unknown/execute",
        headers=auth(),
    )
    assert response.status_code == 404


def test_execution_rejects_pending_approval(api_client):
    _persisted_kubernetes_incident(
        incident_id="inc-pending-execution",
        source_timestamp=datetime.now(timezone.utc).isoformat(),
    )
    approval = audit.create_approval(
        incident_ref="inc-pending-execution",
        proposed_action={},
        context={},
        requested_by="requester",
    )
    response = api_client.post(
        f"/approvals/{approval['approval_id']}/execute",
        headers=auth(),
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Approval must be approved before execution"
    )


def test_trusted_owner_can_approve_own_request_and_execution_starts(
    api_client, monkeypatch
):
    incident_id = "OWNER-SELF-APPROVAL"
    _persisted_kubernetes_incident(
        incident_id=incident_id,
        source_timestamp=datetime.now(timezone.utc).isoformat(),
    )
    workflow_claim, created, conflict = claims.create_workflow_claim(
        operator_id="test-operator",
        service="kubernetes-lab",
        idempotency_key="owner-self-approval",
        incident_ref=incident_id,
    )
    assert created and not conflict
    proposal = {"action": "restart_pod", "deployment": "demo-web"}
    policy = {"allowed": True, "requires_approval": True}
    action_claim = claims.reserve_action_claim(
        workflow_claim_id=workflow_claim["claim_id"],
        attempt_no=0,
        logical_key="owner-self-approval",
        deployment="demo-web",
        action=proposal,
        policy=policy,
        max_actions=2,
        window_seconds=3600,
        auto_authorized=False,
    )
    approval = audit.create_approval(
        incident_ref=incident_id,
        proposed_action=proposal,
        context={"policy": policy},
        requested_by="test-operator",
        action_claim_id=action_claim["action_claim_id"],
    )
    assert claims.finish_workflow_claim(
        workflow_claim["claim_id"], {"status": "awaiting_approval"}
    )
    executions = []

    async def execute(approval_id, operator_id):
        executions.append((approval_id, operator_id))
        return {"status": "dry_run_complete"}

    monkeypatch.setattr(
        operations, "resume_approved_after_decision", execute
    )
    response = api_client.post(
        f"/approvals/{approval['approval_id']}/decision",
        json={"decision": "approved", "reason": "owner reviewed"},
        headers=auth(("read", "remediation", "owner")),
    )

    assert response.status_code == 200
    assert response.json()["execution"]["status"] == "dry_run_complete"
    assert audit.get_approval(approval["approval_id"])["status"] == "APPROVED"
    assert claims.get_action_claim(action_claim["action_claim_id"])["status"] == (
        "APPROVED"
    )
    assert executions == [(approval["approval_id"], "test-operator")]


def test_unknown_approval_is_404_and_bad_status_is_400(api_client):
    assert api_client.post(
        "/approvals/apr-nope/decision",
        json={"decision": "approved"},
        headers=auth(),
    ).status_code == 404
    assert api_client.get("/approvals?status=bogus", headers=auth()).status_code == 400


def test_audit_and_timeline(api_client):
    incident_repository.record_incident({
        "incident_id": "INC-1",
        "service": "order-service",
        "type": "high_latency",
        "severity": "medium",
        "message": "Latency observation",
        "fingerprint": "timeline:INC-1",
    })
    audit.record_event(incident_ref="INC-1", event_type="a", actor="x", summary="s")
    assert len(api_client.get("/audit", headers=auth()).json()["events"]) == 1
    assert api_client.get("/incidents/INC-1/timeline",
                          headers=auth()).status_code == 200
    incident_repository.record_incident({
        "incident_id": "INC-EMPTY",
        "service": "order-service",
        "type": "high_latency",
        "severity": "medium",
        "message": "Observation without audit actions",
        "fingerprint": "timeline:INC-EMPTY",
    })
    empty = api_client.get(
        "/incidents/INC-EMPTY/timeline",
        headers=auth(),
    )
    assert empty.json()["events"] == []
    assert api_client.get("/incidents/NOPE/timeline",
                          headers=auth()).status_code == 404


def test_lifecycle_metrics_use_latest_operational_state_and_global_approval_count(
    api_client,
):
    active = _persisted_kubernetes_incident(
        incident_id="inc-lifecycle-active",
        resource="demo-web-lifecycle-active",
    )
    old_occurrence = _persisted_kubernetes_incident(
        incident_id="inc-lifecycle-resolved",
        resource="demo-web-lifecycle-old",
    )
    with database.get_connection() as connection:
        connection.execute(
            "UPDATE incidents SET status = 'RESOLVED' WHERE id = ?",
            (old_occurrence["id"],),
        )
    latest_occurrence = _persisted_kubernetes_incident(
        incident_id="inc-lifecycle-resolved",
        resource="demo-web-lifecycle-latest",
    )
    with database.get_connection() as connection:
        connection.execute(
            "UPDATE incidents SET status = 'RESOLVED' WHERE id = ?",
            (latest_occurrence["id"],),
        )
    incident_repository.record_incident({
        "incident_id": "DEMO-lifecycle",
        "service": "demo-synthetic",
        "type": "pod_crash",
        "severity": "medium",
        "message": "Synthetic demo record",
        "fingerprint": "demo:lifecycle",
        "source_evidence": {"simulation": "demo"},
    })

    approvals = []
    for index in range(105):
        approvals.append(audit.create_approval(
            incident_ref=f"inc-lifecycle-approval-{index}",
            proposed_action={
                "action": "restart_pod",
                "resource": f"pod-{index}",
            },
            context={},
            requested_by="operator-a",
        ))
    audit.decide_approval(
        approvals[0]["approval_id"], "rejected", "operator-b"
    )

    response = api_client.get("/metrics/incidents", headers=auth(("read",)))

    assert response.status_code == 200
    assert response.json()["active_incidents"] == 1
    assert response.json()["pending_approvals"] == 104
    assert active["status"] == "OPEN"


def test_audit_api_honors_limit_and_orders_by_timestamp(api_client):
    for event_type in ("older", "newer", "middle"):
        audit.record_event(
            incident_ref="INC-LIMIT",
            event_type=event_type,
            actor="operator",
            summary=event_type,
        )
    with database.get_connection() as conn:
        conn.executemany(
            "UPDATE audit_events SET timestamp = ? WHERE event_type = ?",
            [
                ("2026-10-27T10:00:00+00:00", "older"),
                ("2026-10-27T13:00:00+00:00", "newer"),
                ("2026-10-27T12:00:00+00:00", "middle"),
            ],
        )

    response = api_client.get(
        "/audit?incident_id=INC-LIMIT&limit=2",
        headers=auth(("read",)),
    )

    assert response.status_code == 200
    assert [event["event_type"] for event in response.json()["events"]] == [
        "newer",
        "middle",
    ]


def test_audit_incident_filter_returns_only_exact_associations(api_client):
    audit.record_event(
        incident_ref="INC-AUDIT-ONE",
        event_type="matching",
        actor="operator",
        summary="matching event",
    )
    audit.record_event(
        incident_ref="INC-AUDIT-TWO",
        event_type="unrelated",
        actor="operator",
        summary="unrelated event",
    )

    matching = api_client.get(
        "/audit?incident_id=INC-AUDIT-ONE&source=operational",
        headers=auth(("read",)),
    )
    unknown = api_client.get(
        "/audit?incident_id=invalid-123&source=operational",
        headers=auth(("read",)),
    )

    assert matching.status_code == unknown.status_code == 200
    assert [event["event_type"] for event in matching.json()["events"]] == [
        "matching"
    ]
    assert unknown.json()["events"] == []


def test_audit_source_filter_keeps_demo_events_separate(api_client):
    incident_repository.record_incident({
        "incident_id": "INC-OP-AUDIT",
        "service": "order-service",
        "type": "operational_test",
        "severity": "low",
        "message": "Operational audit test",
    })
    incident_repository.record_incident({
        "incident_id": "DEMO-AUDIT",
        "service": "order-service",
        "type": "simulated_test",
        "severity": "low",
        "message": "Demo audit test",
        "source_evidence": {"simulation": "demo"},
    })
    audit.record_event(
        incident_ref="INC-OP-AUDIT",
        event_type="operational_event",
        actor="operator",
        summary="operational",
    )
    audit.record_event(
        incident_ref="DEMO-AUDIT",
        event_type="simulated_event",
        actor="operator",
        summary="simulated",
    )

    operational = api_client.get(
        "/audit?source=operational&limit=1",
        headers=auth(("read",)),
    )
    demo = api_client.get(
        "/audit?source=demo&limit=1",
        headers=auth(("read",)),
    )

    assert operational.status_code == demo.status_code == 200
    assert [event["incident_ref"] for event in operational.json()["events"]] == [
        "INC-OP-AUDIT"
    ]
    assert [event["incident_ref"] for event in demo.json()["events"]] == [
        "DEMO-AUDIT"
    ]


@pytest.mark.parametrize("limit", ["0", "1001"])
def test_audit_api_rejects_out_of_range_limit(api_client, limit):
    response = api_client.get(f"/audit?limit={limit}", headers=auth(("read",)))

    assert response.status_code == 422


def test_configured_services_is_authenticated_and_does_not_run_health(
    api_client, monkeypatch
):
    import app.main as main

    calls = []

    async def unexpected_diagnosis(service):
        calls.append(service)
        raise AssertionError("configured services must not run health checks")

    monkeypatch.setattr(main, "diagnose_service", unexpected_diagnosis)

    assert api_client.get("/services").status_code == 401
    response = api_client.get("/services", headers=auth(("read",)))

    assert response.status_code == 200
    assert response.json()["services"] == [
        "inventory-service",
        "order-service",
        "payment-service",
        "user-service",
    ]
    assert calls == []


@pytest.mark.parametrize(
    "service",
    ["order-service", "payment-service", "user-service", "inventory-service"],
)
def test_health_route_accepts_every_configured_service(
    api_client, monkeypatch, service
):
    import app.main as main

    async def diagnosis(name):
        return {
            "service": name,
            "status": "unknown",
            "evidence_status": "unavailable",
            "findings": [],
        }

    monkeypatch.setattr(main, "diagnose_service", diagnosis)

    response = api_client.get(
        f"/service-health/{service}",
        headers=auth(("read",)),
    )

    assert response.status_code == 200
    assert response.json()["service"] == service
    assert response.json()["health_status"] == "unknown"


def test_unsupported_service_is_400(api_client):
    r = api_client.post(
        "/workflow/evil/respond",
        headers=auth(),
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "Unsupported service"


@pytest.mark.parametrize(
    "incident_id",
    ["", "contains spaces", "line\nbreak", "é", "x" * 65],
)
def test_workflow_rejects_malformed_correlation_id(api_client, incident_id):
    response = api_client.post(
        "/workflow/kubernetes-lab/respond",
        headers=auth(),
        json={"incident_id": incident_id},
    )

    assert response.status_code == 422


@pytest.mark.parametrize("body", [[], "not-an-object", {"incident_id": 12}])
def test_workflow_rejects_malformed_request_body(api_client, body):
    response = api_client.post(
        "/workflow/kubernetes-lab/respond",
        headers=auth(),
        json=body,
    )

    assert response.status_code == 422


def test_workflow_rejects_invalid_operator_token(api_client, monkeypatch):
    calls = []

    async def guarded_workflow(*args, **kwargs):
        calls.append((args, kwargs))
        return {"unexpected": True}

    monkeypatch.setattr(operations, "run_response_workflow", guarded_workflow)

    response = api_client.post(
        "/workflow/kubernetes-lab/respond",
        headers={"Authorization": "Bearer invalid-token"},
    )

    assert response.status_code == 401
    assert calls == []


def test_workflow_requires_remediation_permission_before_claim_or_start(
    api_client, monkeypatch
):
    claim_calls = []
    workflow_calls = []

    def unexpected_claim(*args, **kwargs):
        claim_calls.append((args, kwargs))
        raise AssertionError("workflow claim should not be created")

    async def unexpected_workflow(*args, **kwargs):
        workflow_calls.append((args, kwargs))
        raise AssertionError("workflow should not start")

    monkeypatch.setattr(claims, "create_workflow_claim", unexpected_claim)
    monkeypatch.setattr(operations, "run_response_workflow", unexpected_workflow)
    response = api_client.post(
        "/workflow/kubernetes-lab/respond",
        headers={
            **auth(("read",)),
            "Idempotency-Key": "key-remediation-required",
        },
        json={"incident_id": "INC-approval-required"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Operator lacks remediation permission"
    assert claim_calls == []
    assert workflow_calls == []


def test_workflow_propagates_valid_correlation_and_authenticated_operator(
    api_client, monkeypatch
):
    calls = []

    async def fake_workflow(
        service, *, incident_id, requested_by, workflow_claim_id, **_kwargs
    ):
        result = {
            "incident_id": incident_id,
            "workflow_claim_id": workflow_claim_id,
            "status": "no_action",
        }
        calls.append((service, incident_id, requested_by, workflow_claim_id))
        assert claims.finish_workflow_claim(workflow_claim_id, result)
        return result

    monkeypatch.setattr(operations, "run_response_workflow", fake_workflow)
    response = api_client.post(
        "/workflow/kubernetes-lab/respond",
        headers={
            **auth(),
            "Idempotency-Key": "key-propagate",
        },
        json={"incident_id": "INC-operator.test:1"},
    )

    assert response.status_code == 200
    assert response.json()["incident_id"] == "INC-operator.test:1"
    assert calls[0][:3] == (
        "kubernetes-lab", "INC-operator.test:1", "test-operator"
    )
    assert response.json()["workflow_claim_id"] == calls[0][3]


def test_workflow_value_error_is_generic_service_unavailable(
    api_client, monkeypatch, caplog
):
    secret = "synthetic-provider-response-secret"

    async def fail_workflow(*_args, **_kwargs):
        raise ValueError(secret)

    monkeypatch.setattr(operations, "run_response_workflow", fail_workflow)
    with caplog.at_level(logging.ERROR, logger=operations.__name__):
        response = api_client.post(
            "/workflow/kubernetes-lab/respond",
            headers={
                **auth(),
                "Idempotency-Key": "key-error",
            },
        )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Incident response workflow is temporarily unavailable."
    )
    assert secret not in response.text
    assert "exception_type=ValueError" in caplog.text
    assert secret not in caplog.text


def test_repeated_workflow_requests_replay_original_result(api_client, monkeypatch):
    calls = []

    async def fake_workflow(
        service, *, incident_id, requested_by, workflow_claim_id, **_kwargs
    ):
        calls.append(workflow_claim_id)
        result = {
            "incident_id": incident_id,
            "workflow_claim_id": workflow_claim_id,
            "status": "no_action",
        }
        assert claims.finish_workflow_claim(workflow_claim_id, result)
        return result

    monkeypatch.setattr(operations, "run_response_workflow", fake_workflow)
    request = {
        "headers": {
            **auth(),
            "Idempotency-Key": "key-repeat",
        },
        "json": {"incident_id": "INC-repeat"},
    }

    first = api_client.post("/workflow/kubernetes-lab/respond", **request)
    second = api_client.post("/workflow/kubernetes-lab/respond", **request)

    assert first.status_code == second.status_code == 200
    assert len(calls) == 1
    assert first.json() == second.json()


def test_idempotency_key_cannot_be_reused_for_different_correlation(
    api_client, monkeypatch
):
    async def fake_workflow(
        service, *, incident_id, requested_by, workflow_claim_id, **_kwargs
    ):
        result = {"incident_id": incident_id, "status": "no_action"}
        assert claims.finish_workflow_claim(workflow_claim_id, result)
        return result

    monkeypatch.setattr(operations, "run_response_workflow", fake_workflow)
    headers = {
        **auth(),
        "Idempotency-Key": "key-conflict",
    }
    first = api_client.post(
        "/workflow/kubernetes-lab/respond",
        headers=headers,
        json={"incident_id": "INC-A"},
    )
    second = api_client.post(
        "/workflow/kubernetes-lab/respond",
        headers=headers,
        json={"incident_id": "INC-B"},
    )

    assert first.status_code == 200
    assert second.status_code == 409


def test_missing_or_invalid_idempotency_key_is_rejected(api_client):
    for key in (None, "", "key with spaces", "x" * 129):
        headers = {**auth()}
        if key is not None:
            headers["Idempotency-Key"] = key
        response = api_client.post(
            "/workflow/kubernetes-lab/respond", headers=headers
        )
        assert response.status_code == 400


def test_unresolved_idempotency_claim_is_not_restarted(api_client, monkeypatch):
    calls = []

    async def interrupted(
        service, *, incident_id, requested_by, workflow_claim_id, **_kwargs
    ):
        calls.append(workflow_claim_id)
        raise RuntimeError("synthetic interrupted workflow")

    monkeypatch.setattr(operations, "run_response_workflow", interrupted)
    request = {
        "headers": {
            **auth(),
            "Idempotency-Key": "key-interrupted",
        },
        "json": {"incident_id": "INC-interrupted"},
    }
    first = api_client.post("/workflow/kubernetes-lab/respond", **request)
    second = api_client.post("/workflow/kubernetes-lab/respond", **request)

    assert first.status_code == 503
    assert second.status_code == 409
    assert len(calls) == 1


def test_advisory_get_persists_occurrence_and_pending_handoff_without_dispatch(
    api_client, monkeypatch
):
    _mock_advisory_evidence(monkeypatch, _actionable_telemetry())
    dispatch_calls = []
    execution_calls = []

    async def unexpected_dispatch(*args, **kwargs):
        dispatch_calls.append((args, kwargs))

    def unexpected_execution(*args, **kwargs):
        execution_calls.append((args, kwargs))

    import app.services.application_anomaly_monitor as monitor
    import app.services.incident_response_graph as response_graph
    import app.services.k8s_remediation as remediation

    monkeypatch.setattr(monitor, "dispatch_pending_handoffs", unexpected_dispatch)
    monkeypatch.setattr(response_graph, "execute_action", unexpected_execution)
    monkeypatch.setattr(remediation, "execute_action", unexpected_execution)

    response = api_client.get(
        "/workflow/order-service",
        headers=auth(("read",)),
    )

    assert response.status_code == 200
    assert response.json()["telemetry_incident_persistence"]["status"] == (
        "persisted"
    )
    occurrences = list_incidents(status="OPEN")
    pending_handoffs = handoffs.list_handoffs()
    assert len(occurrences) == 1
    occurrence = occurrences[0]
    assert occurrence["service"] == "order-service"
    assert occurrence["incident_type"] == "cpu_spike"
    assert occurrence["fingerprint"] == (
        "prometheus:order-service:cpu_spike"
    )
    assert len(pending_handoffs) == 1
    assert pending_handoffs[0]["incident_row_id"] == occurrence["id"]
    assert pending_handoffs[0]["incident_id"] == occurrence["incident_id"]
    assert pending_handoffs[0]["fingerprint"] == occurrence["fingerprint"]
    assert pending_handoffs[0]["idempotency_key"] == (
        f"incident-occurrence-{occurrence['id']}"
    )
    assert pending_handoffs[0]["status"] == "PENDING"
    assert pending_handoffs[0]["attempts"] == 0
    assert pending_handoffs[0]["workflow_claim_id"] is None
    assert dispatch_calls == []
    assert execution_calls == []


def test_advisory_persistence_failure_returns_sanitized_503_without_remediation(
    api_client, monkeypatch, caplog
):
    observed_at = datetime.now(timezone.utc).isoformat()
    telemetry = {
        "status": "evaluated",
        "observed_at": observed_at,
        "actionable_candidates": ["cpu_cores_used", "memory_bytes"],
        "threshold": 5.0,
        "sample_timestamps": {
            "cpu_cores_used": observed_at,
            "memory_bytes": observed_at,
        },
        "features": {
            feature: {
                "value": 2.0,
                "baseline_median": 0.5,
                "deviation_score": 6.0,
                "direction": "high",
                "classification": "anomaly",
            }
            for feature in ("cpu_cores_used", "memory_bytes")
        },
    }
    _mock_advisory_evidence(monkeypatch, telemetry)
    record_incident = incident_repository.record_incident
    persisted_features = []
    secret_error = "synthetic-database-detail-secret"

    def fail_after_first_write(data, *, enqueue_handoff=False):
        persisted_features.append(data["source_evidence"]["metric"]["name"])
        if len(persisted_features) == 1:
            return record_incident(
                data,
                enqueue_handoff=enqueue_handoff,
            )
        raise ValueError(secret_error)

    monkeypatch.setattr(
        incident_repository, "record_incident", fail_after_first_write
    )
    dispatch_calls = []
    execution_calls = []

    async def unexpected_dispatch(*args, **kwargs):
        dispatch_calls.append((args, kwargs))

    def unexpected_execution(*args, **kwargs):
        execution_calls.append((args, kwargs))

    import app.services.application_anomaly_monitor as monitor
    import app.services.incident_response_graph as response_graph
    import app.services.k8s_remediation as remediation

    monkeypatch.setattr(monitor, "dispatch_pending_handoffs", unexpected_dispatch)
    monkeypatch.setattr(response_graph, "execute_action", unexpected_execution)
    monkeypatch.setattr(remediation, "execute_action", unexpected_execution)

    response = api_client.get(
        "/workflow/order-service",
        headers=auth(("read",)),
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Incident workflow is temporarily unavailable."
    }
    assert secret_error not in response.text
    assert secret_error not in caplog.text
    assert "Telemetry incident persistence failed for order-service (ValueError)" in (
        caplog.text
    )
    assert persisted_features == ["cpu_cores_used", "memory_bytes"]
    assert len(list_incidents(status="OPEN")) == 1
    assert len(handoffs.list_handoffs()) == 1
    assert dispatch_calls == []
    assert execution_calls == []


def test_advisory_persistence_failure_before_first_write_returns_sanitized_503(
    api_client, monkeypatch, caplog
):
    _mock_advisory_evidence(monkeypatch, _actionable_telemetry())
    secret_error = "synthetic-database-detail-secret"

    def fail_before_write(*_args, **_kwargs):
        raise ValueError(secret_error)

    monkeypatch.setattr(
        incident_repository, "record_incident", fail_before_write
    )

    response = api_client.get(
        "/workflow/order-service",
        headers=auth(("read",)),
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Incident workflow is temporarily unavailable."
    }
    assert secret_error not in response.text
    assert secret_error not in caplog.text
    assert "Telemetry incident persistence failed for order-service (ValueError)" in (
        caplog.text
    )
    assert list_incidents(status="OPEN") == []
    assert handoffs.list_handoffs() == []


def test_advisory_get_rejects_unauthorized_before_workflow(
    api_client, monkeypatch
):
    import app.main as main

    workflow_calls = []

    async def unexpected_workflow(service):
        workflow_calls.append(service)
        return {}

    monkeypatch.setattr(main, "run_incident_workflow", unexpected_workflow)

    response = api_client.get("/workflow/order-service")

    assert response.status_code == 401
    assert workflow_calls == []


def test_repeated_advisory_get_refreshes_one_occurrence_and_handoff(
    api_client, monkeypatch
):
    _mock_advisory_evidence(monkeypatch, _actionable_telemetry())

    first = api_client.get(
        "/workflow/order-service",
        headers=auth(("read",)),
    )
    first_occurrence = list_incidents(status="OPEN")[0]
    first_handoff = handoffs.list_handoffs()[0]

    _mock_advisory_evidence(monkeypatch, _actionable_telemetry())
    repeated = api_client.get(
        "/workflow/order-service",
        headers=auth(("read",)),
    )

    assert first.status_code == repeated.status_code == 200
    assert repeated.json()["telemetry_incident_persistence"]["status"] == (
        "persisted"
    )
    occurrences = list_incidents(status="OPEN")
    repeated_handoffs = handoffs.list_handoffs()
    assert len(occurrences) == 1
    assert occurrences[0]["id"] == first_occurrence["id"]
    assert len(repeated_handoffs) == 1
    assert repeated_handoffs[0]["incident_row_id"] == (
        first_handoff["incident_row_id"]
    )
    assert repeated_handoffs[0]["idempotency_key"] == (
        first_handoff["idempotency_key"]
    )
    assert repeated_handoffs[0]["status"] == "PENDING"
    assert repeated_handoffs[0]["attempts"] == 0


@pytest.mark.parametrize(
    ("telemetry", "expected_persistence_status"),
    [
        (_incomplete_telemetry(), "no_supported_anomaly"),
        ({"status": "unavailable", "reason": "stale_features"}, "skipped"),
        ({"status": "unavailable", "reason": "source_unavailable"}, "skipped"),
    ],
    ids=["incomplete", "stale", "unavailable"],
)
def test_advisory_get_incomplete_or_unavailable_evidence_preserves_occurrence(
    api_client, monkeypatch, telemetry, expected_persistence_status
):
    _mock_advisory_evidence(monkeypatch, _actionable_telemetry())
    initial = api_client.get(
        "/workflow/order-service",
        headers=auth(("read",)),
    )
    assert initial.status_code == 200
    original_occurrence = list_incidents(status="OPEN")[0]
    original_handoff = handoffs.list_handoffs()[0]

    _mock_advisory_evidence(monkeypatch, telemetry)
    response = api_client.get(
        "/workflow/order-service",
        headers=auth(("read",)),
    )

    assert response.status_code == 200
    assert response.json()["telemetry_incident_persistence"]["status"] == (
        expected_persistence_status
    )
    occurrences = list_incidents(status="OPEN")
    current_handoffs = handoffs.list_handoffs()
    assert len(occurrences) == 1
    assert occurrences[0]["id"] == original_occurrence["id"]
    assert occurrences[0]["status"] == "OPEN"
    assert len(current_handoffs) == 1
    assert current_handoffs[0]["incident_row_id"] == (
        original_handoff["incident_row_id"]
    )
    assert current_handoffs[0]["status"] == "PENDING"
    assert current_handoffs[0]["attempts"] == 0


def test_advisory_get_contract_failure_retains_prior_persistence(
    api_client, monkeypatch
):
    _mock_advisory_evidence(monkeypatch, _actionable_telemetry())
    with pytest.raises(ValidationError) as validation_error:
        incident_workflow.IncidentContract.model_validate({})

    def fail_contract(_state):
        raise validation_error.value

    monkeypatch.setattr(
        incident_workflow, "build_incident_contract", fail_contract
    )
    response = api_client.get(
        "/workflow/order-service",
        headers=auth(("read",)),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["contract"] is None
    assert body["contract_status"] == "invalid"
    assert body["telemetry_incident_persistence"]["status"] == "persisted"
    occurrences = list_incidents(status="OPEN")
    current_handoffs = handoffs.list_handoffs()
    assert len(occurrences) == 1
    assert occurrences[0]["status"] == "OPEN"
    assert len(current_handoffs) == 1
    assert current_handoffs[0]["incident_row_id"] == occurrences[0]["id"]
    assert current_handoffs[0]["status"] == "PENDING"
    assert current_handoffs[0]["attempts"] == 0
