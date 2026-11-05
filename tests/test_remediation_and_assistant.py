"""Remediation option sets for every alert, and the grounded Console assistant."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import app.database as database
import app.api.lab as lab_api
import app.services.incident_response_graph as g
from app.services import audit_repository as audit
from app.services import console_assistant as assistant
from app.services import remediation_catalog as catalog
from app.services.incident_repository import (
    claim_incident_diagnosis,
    finish_incident_diagnosis,
    list_incidents,
    record_incident,
    resolve_incident_fingerprint,
)
from app.services.runbook_retriever import resolve_runbook
from tests.conftest import create_operator_token


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "assist.db")
    database.initialize_database()


def auth(perms=("read",)):
    return {"Authorization": f"Bearer {create_operator_token(permissions=list(perms))}"}


POD = {"resource": "demo-web-abc-1", "resource_kind": "Pod", "deployment": "demo-web"}
DEP = {"resource": "demo-web", "resource_kind": "Deployment", "deployment": "demo-web"}


# ---------------- catalog: every alert has a set of options ----------------
@pytest.mark.parametrize("incident_type", [
    "pod_crash", "memory_leak", "disk_full", "deployment_failure", "cpu_spike",
    "service_unavailable", "high_latency", "http_5xx", "request_failure",
    "unknown", "something_new"])
def test_every_incident_type_gets_at_least_three_options(incident_type):
    options = catalog.build_options(incident_type, POD, {"desired_replicas": 2})

    assert len(options) >= 3
    assert any(item["kind"] == "manual" for item in options)


@pytest.mark.parametrize(
    ("incident_type", "service"),
    [
        ("high_latency", "order-service"),
        ("http_5xx", "payment-service"),
        ("service_unavailable", "user-service"),
        ("request_failure", "inventory-service"),
    ],
)
def test_local_service_remedies_are_specific_to_the_fault_and_service(
    incident_type, service,
):
    options = catalog.build_options(
        incident_type, {"service": service}, None,
    )
    manual = [item for item in options if item["kind"] == "manual"]

    assert len(manual) == 3
    assert all(service in item["title"] for item in manual)
    assert all(not item["executable"] for item in manual)


def test_executable_options_are_limited_to_supported_actions():
    options = catalog.build_options(
        "pod_crash", POD, {"desired_replicas": 2},
        runbook_check=lambda raw: {"status": "applicable"})

    actions = [item["action"] for item in options if item["kind"] == "executable"]
    assert actions == ["restart_pod", "rollback_deployment", "scale_deployment"]
    assert [item["action"] for item in options if item.get("selected")] == ["restart_pod"]
    assert all(not item["executable"] for item in options if item["kind"] == "manual")


def test_option_not_backed_by_a_runbook_is_not_executable():
    options = catalog.build_options(
        "pod_crash", POD, {"desired_replicas": 2},
        runbook_check=lambda raw: {"status": "applicable"} if raw["action"] == "restart_pod"
        else {"status": "human_review_required"})

    by_action = {item["action"]: item["executable"] for item in options if item["kind"] == "executable"}
    assert by_action == {"restart_pod": True, "rollback_deployment": False, "scale_deployment": False}


def test_blocked_option_preserves_observed_and_required_replica_counts():
    options = catalog.build_options(
        "pod_crash",
        POD,
        {"desired_replicas": 2},
        runbook_check=lambda _: {
            "status": "missing_prerequisites",
            "reason": "minimum_ready_replicas_not_met",
            "prerequisites": {"min_ready_replicas": 2},
            "observed_available_replicas": 1,
        },
    )

    restart = next(item for item in options if item.get("action") == "restart_pod")
    assert restart["executable"] is False
    assert restart["runbook_prerequisites"] == {"min_ready_replicas": 2}
    assert restart["observed_available_replicas"] == 1


def test_restart_needs_a_pod_and_scale_needs_known_replicas():
    assert [c["action"] for c in catalog.candidate_actions("pod_crash", DEP, None)] == ["rollback_deployment"]
    scaled = catalog.candidate_actions("cpu_spike", DEP, {"desired_replicas": 2})
    assert scaled[0] == {"action": "scale_deployment", "resource": "demo-web", "target_replicas": 3}


# ---------------- runbooks: alternatives and the deployment-failure contract ----------------
def test_deployment_failure_now_has_an_applicable_rollback_runbook():
    result = resolve_runbook(
        "deployment_failure Pod demo-web-x is in ImagePullBackOff",
        incident_type="deployment_failure",
        evidence=["Pod demo-web-x is in ImagePullBackOff"],
        planned_action="rollback_deployment",
        deployment_state={"desired_replicas": 2, "available_replicas": 0})

    assert result["status"] == "applicable"
    assert result["proposed_action"] == "rollback_deployment"


def test_runbook_alternative_action_is_accepted_for_the_same_evidence():
    evidence = ["Pod web-1 is in CrashLoopBackOff"]
    result = resolve_runbook(
        "pod_crash " + evidence[0], incident_type="pod_crash", evidence=evidence,
        planned_action="rollback_deployment",
        deployment_state={"desired_replicas": 2, "available_replicas": 2})

    assert result["status"] == "applicable"


# ---------------- plan(): preferred action ----------------
def _state(preferred=None):
    return {
        "incident_id": "inc-x",
        "preferred_action": preferred,
        "classification": {
            "incident_type": "pod_crash", "confidence": 0.85, "severity": "high",
            "evidence": ["Pod web-1 is in CrashLoopBackOff"],
            "target": {"namespace": "kybernaut-lab", "resource": "web-1",
                       "resource_kind": "Pod", "deployment": "demo-web"}},
        "kubernetes": {"deployments": [{"name": "demo-web", "desired_replicas": 2,
                                        "available_replicas": 2}]},
    }


def test_plan_defaults_to_the_primary_action_and_lists_all_options():
    update = g.plan(_state())

    assert update["proposal"]["action"] == "restart_pod"
    assert len(update["remediation_options"]) >= 3


def test_plan_uses_the_operators_preferred_alternative():
    update = g.plan(_state("rollback_deployment"))

    assert update["proposal"]["action"] == "rollback_deployment"
    assert update["proposal"]["resource"] == "demo-web"


def test_plan_never_silently_swaps_in_an_alternative():
    state = _state()
    state["kubernetes"]["deployments"][0]["available_replicas"] = 1  # primary prerequisite unmet

    update = g.plan(state)

    assert update["proposal"] is None
    assert update["remediation_options"]


# ---------------- endpoints ----------------
def _incident(**kw):
    base = {"incident_id": "inc-tele-1", "service": "user-service", "type": "memory_leak",
            "severity": "medium", "message": "Prometheus anomaly", "fingerprint": "telemetry:x"}
    base.update(kw)
    return record_incident(base)


def test_remediation_options_for_a_telemetry_alert_are_guidance(api_client):
    _incident()

    body = api_client.get("/incidents/inc-tele-1/remediation-options", headers=auth()).json()

    assert body["source"] == "telemetry"
    assert body["service"] == "user-service"
    assert body["incident_alert"] == "Prometheus anomaly"
    assert len(body["options"]) >= 3
    assert all(not item["executable"] for item in body["options"])
    assert body["options"][0]["title"] == "Confirm sustained memory growth in user-service"
    assert all("<service>" not in " ".join(
        [item["title"], *item["steps"]]
    ) for item in body["options"])
    assert "business request" in body["options"][2]["steps"][1]
    assert "verified execution target" in body["note"]


def test_cpu_spike_without_target_or_sustained_evidence_explains_remediation_gate(
    api_client,
):
    incident = _incident(
        incident_id="inc-cpu-no-target",
        service="payment-service",
        type="cpu_spike",
        source_evidence={
            "source": "prometheus",
            "metric": {"name": "cpu_cores_used", "value": 0.0047},
        },
    )
    _persist_diagnosis(
        incident,
        {
            "limitations": ["trusted_workload_mapping_missing"],
            "diagnosis": {"status": "unknown"},
            "trusted_target": None,
        },
        status="limited",
    )

    response = api_client.get(
        "/incidents/inc-cpu-no-target/remediation-options",
        headers=auth(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["live_evidence"] is False
    assert all(not item["executable"] for item in body["options"])
    assert "no trusted workload mapping is configured" in body["note"]
    assert "current service health is unknown" in body["note"]
    assert "one CPU sample" in body["note"]
    assert "request rate and p95 latency" in body["note"]
    assert "runbook and policy checks" in body["note"]


def test_memory_leak_guidance_names_the_affected_service_and_fix(
    api_client, monkeypatch,
):
    monkeypatch.setenv("KYBERNAUT_ENVIRONMENT", "local-demo")
    monkeypatch.setattr(
        lab_api.payment_compose_remediation.shutil, "which", lambda _: None
    )
    _incident(
        incident_id="inc-payment-memory-leak",
        service="payment-service",
        type="memory_leak",
        source_evidence={"source": "prometheus"},
    )

    response = api_client.get(
        "/incidents/inc-payment-memory-leak/remediation-options",
        headers=auth(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "payment-service"
    assert body["incident_alert"] == "Prometheus anomaly"
    assert all(not item["executable"] for item in body["options"])
    assert body["options"][0]["title"] == "Migrate payment history to PostgreSQL"
    assert "scripts/migrate_payment_history.py" in " ".join(
        body["options"][0]["steps"]
    )
    assert body["options"][1]["title"] == (
        "Deploy the PostgreSQL-backed payment service"
    )
    assert "returns the migrated records" in " ".join(
        body["options"][1]["steps"]
    )
    assert "stores payment history in PostgreSQL" in body["note"]
    assert "Docker CLI is not available" in body["note"]


@pytest.mark.parametrize(
    ("service", "incident_type", "expected_title"),
    [
        ("order-service", "high_latency", "Remove the added 2-second request delay"),
        ("payment-service", "http_5xx", "Stop the forced HTTP 503 responses"),
        ("user-service", "service_unavailable", "Stop rejecting user-service requests with HTTP 503"),
        ("inventory-service", "request_failure", "Stop the forced HTTP 500 responses"),
    ],
)
def test_service_incident_remedies_describe_recovery_and_verification(
    api_client, service, incident_type, expected_title,
):
    incident_id = f"inc-local-{service}"
    record_incident({
        "incident_id": incident_id,
        "service": service,
        "type": incident_type,
        "severity": "high",
        "message": f"{service} reported {incident_type}.",
        "fingerprint": f"local-service-simulation:test:{service}:{incident_type}",
        "source_evidence": {
            "simulation": "local_service_failure",
            "simulated": True,
            "service": service,
            "scenario": incident_type,
        },
    })

    response = api_client.get(
        f"/incidents/{incident_id}/remediation-options",
        headers=auth(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "local_service_simulator"
    execute = next(
        item for item in body["options"]
        if item.get("action") == "clear_local_simulation"
    )
    assert execute["executable"] is True
    assert execute["resource"] == service
    assert execute["title"] == f"{expected_title} on {service}"
    assert execute["rationale"]
    assert "normal request handl" in execute["rationale"]
    assert any("inactive" in step for step in execute["steps"])
    assert any("check" in step or "verify" in step
               for step in execute["steps"])
    displayed_copy = " ".join(
        str(execute.get(key, ""))
        for key in ("title", "rationale", "steps", "risk")
    ).lower()
    assert "simulat" not in displayed_copy
    assert "kubernetes" not in displayed_copy
    assert body["note"] == ""
    manual = [item for item in body["options"] if item["kind"] == "manual"]
    assert len(manual) == 3
    assert all(service in item["title"] for item in manual)


def test_execute_local_simulation_requires_remediation_permission_and_resolves(
    api_client, monkeypatch,
):
    incident_id = "inc-local-user"
    fingerprint = "local-service-simulation:test:user-service:service_unavailable"
    record_incident({
        "incident_id": incident_id,
        "service": "user-service",
        "type": "service_unavailable",
        "severity": "critical",
        "message": "User requests return HTTP 503.",
        "fingerprint": fingerprint,
        "source_evidence": {
            "simulation": "local_service_failure",
            "simulated": True,
            "service": "user-service",
            "scenario": "service_unavailable",
        },
    })
    scheduler = api_client.app.state.local_service_incident_scheduler
    scheduler.state["enabled"] = False
    scheduler._active_fingerprints = [fingerprint]
    cleared = []

    async def fake_control(scenario, *, action):
        cleared.append((scenario["service"], action))
        return {"service": scenario["service"], "active": False}

    monkeypatch.setattr(scheduler, "_control", fake_control)
    read_token = create_operator_token(permissions=["read"])
    denied = api_client.post(
        f"/incidents/{incident_id}/execute-local-simulation",
        headers={"Authorization": f"Bearer {read_token}"},
    )
    assert denied.status_code == 403
    assert cleared == []

    response = api_client.post(
        f"/incidents/{incident_id}/execute-local-simulation",
        headers={"Authorization": f"Bearer {create_operator_token()}"},
    )

    assert response.status_code == 200
    assert response.json()["resolved"] is True
    assert response.json()["audit_recorded"] is True
    assert cleared == [("user-service", "clear")]
    assert next(
        item for item in list_incidents()
        if item["incident_id"] == incident_id
    )["status"] == "RESOLVED"


def test_execute_local_simulation_reports_unreachable_service_and_keeps_incident_open(
    api_client, monkeypatch,
):
    incident_id = "inc-local-inventory-unreachable"
    fingerprint = (
        "local-service-simulation:test:inventory-service:request_failure"
    )
    record_incident({
        "incident_id": incident_id,
        "service": "inventory-service",
        "type": "request_failure",
        "severity": "high",
        "message": "Inventory requests return HTTP 500.",
        "fingerprint": fingerprint,
        "source_evidence": {
            "simulation": "local_service_failure",
            "simulated": True,
            "service": "inventory-service",
            "scenario": "request_failure",
        },
    })
    scheduler = api_client.app.state.local_service_incident_scheduler
    scheduler._active_fingerprints = [fingerprint]

    async def unreachable_control(_scenario, *, action):
        assert action == "clear"
        request = httpx.Request("POST", "http://127.0.0.1:8004/__kybernaut/simulation")
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(scheduler, "_control", unreachable_control)
    response = api_client.post(
        f"/incidents/{incident_id}/execute-local-simulation",
        headers=auth(("read", "remediation")),
    )

    assert response.status_code == 502
    assert "Could not reach inventory-service" in (
        response.json()["detail"]
    )
    assert "incident remains open" in response.json()["detail"]
    assert next(
        item for item in list_incidents()
        if item["incident_id"] == incident_id
    )["status"] == "OPEN"


def test_kubernetes_remediation_options_are_scoped_to_the_incident(api_client, monkeypatch):
    observed_at = datetime.now(timezone.utc)
    resource = "sim-disk-fill-pod-1"
    resource_uid = "disk-pod-uid"
    incident_id = "inc-k8s-disk-full"
    fingerprint = (
        f"k8s:kybernaut-lab:Pod:{resource}:pod_failed"
    )
    record_incident({
        "incident_id": incident_id,
        "service": "kubernetes-lab",
        "type": "pod_failed",
        "severity": "high",
        "message": 'Evicted: EmptyDir volume "scratch" exceeds limit.',
        "fingerprint": fingerprint,
        "source_timestamp": observed_at.isoformat(),
        "source_evidence": {
            "source": "kubernetes",
            "namespace": "kybernaut-lab",
            "type": "pod_failed",
            "resource": resource,
            "resource_kind": "Pod",
            "resource_uid": resource_uid,
            "source_timestamp": observed_at.isoformat(),
            "evidence": [
                'Evicted: EmptyDir volume "scratch" exceeds limit.'
            ],
        },
    })
    monkeypatch.setattr(
        lab_api,
        "observe_namespace",
        lambda: {
            "namespace": "kybernaut-lab",
            "observed_at": observed_at.isoformat(),
            "incidents": [
                {
                    "type": "pod_failed",
                    "resource": resource,
                    "resource_kind": "Pod",
                    "resource_uid": resource_uid,
                    "severity": "high",
                    "source_timestamp": observed_at.isoformat(),
                    "evidence": [
                        'Evicted: Usage of EmptyDir volume "scratch" '
                        'exceeds the limit "40Mi".'
                    ],
                },
                {
                    "type": "pod_container_failure",
                    "resource": "sim-web-bad-image-1",
                    "resource_kind": "Pod",
                    "resource_uid": "unrelated-pod-uid",
                    "severity": "high",
                    "source_timestamp": (
                        observed_at + timedelta(seconds=1)
                    ).isoformat(),
                    "evidence": ["Container waiting: ImagePullBackOff"],
                },
            ],
            "deployments": [
                {"name": "sim-disk-fill", "desired_replicas": 1},
                {"name": "sim-web-bad-image", "desired_replicas": 1},
            ],
        },
    )

    response = api_client.get(
        f"/incidents/{incident_id}/remediation-options",
        headers=auth(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["incident_type"] == "disk_full"
    assert body["live_evidence"] is True
    titles = [item["title"] for item in body["options"]]
    assert "Find what is writing to the volume" in titles
    assert "Set an emptyDir sizeLimit and log rotation" in titles
    assert "Fix the image reference or registry credentials" not in titles


def test_stale_kubernetes_incident_keeps_type_specific_guidance(api_client, monkeypatch):
    observed_at = datetime.now(timezone.utc)
    resource = "sim-pod-crash"
    incident_id = "inc-k8s-unavailable-replicas"
    record_incident({
        "incident_id": incident_id,
        "service": "kubernetes-lab",
        "type": "deployment_unavailable_replicas",
        "severity": "high",
        "message": "Deployment has 0/1 available replicas",
        "fingerprint": (
            f"k8s:kybernaut-lab:Deployment:{resource}:deployment_unavailable_replicas"
        ),
        "source_timestamp": observed_at.isoformat(),
        "source_evidence": {
            "source": "kubernetes",
            "namespace": "kybernaut-lab",
            "type": "deployment_unavailable_replicas",
            "resource": resource,
            "resource_kind": "Deployment",
            "source_timestamp": observed_at.isoformat(),
            "evidence": ["Deployment has 0/1 available replicas"],
        },
    })
    monkeypatch.setattr(
        lab_api,
        "observe_namespace",
        lambda: {
            "namespace": "kybernaut-lab",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "incidents": [],
            "deployments": [{"name": resource, "desired_replicas": 0}],
        },
    )

    response = api_client.get(
        f"/incidents/{incident_id}/remediation-options",
        headers=auth(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["incident_status"] == "OPEN"
    assert body.get("condition_cleared") is not True
    assert body["incident_type"] == "deployment_failure"
    assert body["live_evidence"] is False
    assert body["classification"]["evidence"] == [
        "Deployment has 0/1 available replicas"
    ]
    assert any(
        item["title"] == "Inspect the rollout and the pod events"
        for item in body["options"]
    )
    blocked_actions = [
        item for item in body["options"] if item["kind"] == "executable"
    ]
    assert len(blocked_actions) == 1
    assert blocked_actions[0]["action"] == "rollback_deployment"
    assert blocked_actions[0]["executable"] is False
    assert blocked_actions[0]["runbook_reason"] == "incident_not_current"
    assert all(
        item["executable"] is False for item in body["options"]
    )


@pytest.mark.parametrize(
    "permissions, expected_status",
    [
        (["read", "remediation"], "RESOLVED"),
        (["read"], "OPEN"),
    ],
)
def test_complete_observation_reconciles_cleared_incident_by_permission(
    api_client, monkeypatch, permissions, expected_status,
):
    observed_at = datetime.now(timezone.utc)
    resource = "sim-pod-crash"
    incident_id = "inc-k8s-cleared-condition"
    fingerprint = (
        f"k8s:kybernaut-lab:Deployment:{resource}:"
        "deployment_unavailable_replicas"
    )
    incident = record_incident({
        "incident_id": incident_id,
        "service": "kubernetes-lab",
        "type": "deployment_unavailable_replicas",
        "severity": "high",
        "message": "Deployment has 0/1 available replicas",
        "fingerprint": fingerprint,
        "source_timestamp": observed_at.isoformat(),
        "source_evidence": {
            "source": "kubernetes",
            "namespace": "kybernaut-lab",
            "type": "deployment_unavailable_replicas",
            "resource": resource,
            "resource_kind": "Deployment",
            "source_timestamp": observed_at.isoformat(),
            "evidence": ["Deployment has 0/1 available replicas"],
        },
    })
    approval = audit.create_approval(
        incident_ref=incident_id,
        proposed_action={"action": "rollback_deployment", "resource": resource},
        context={},
        requested_by="offline-test",
    )
    monkeypatch.setattr(
        lab_api,
        "observe_namespace",
        lambda: {
            "namespace": "kybernaut-lab",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "incident_count": 0,
            "incidents": [],
            "deployments": [
                {"name": "demo-web", "desired_replicas": 1},
                {"name": resource, "desired_replicas": 1},
            ],
        },
    )

    headers = {
        "Authorization": "Bearer "
        + create_operator_token(permissions=permissions),
    }
    response = api_client.get(
        f"/incidents/{incident_id}/remediation-options",
        headers=headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert all(not option["executable"] for option in body["options"])
    persisted_approval = audit.get_approval(approval["approval_id"])
    assert body["incident_status"] == expected_status
    if expected_status == "RESOLVED":
        assert body["condition_cleared"] is True
        assert body["resolution_reason"] == "condition_cleared"
        assert body["note"].endswith("No remediation was executed.")
        resolved = lab_api.get_incident_detail(incident_id)
        assert resolved["id"] == incident["id"]
        assert resolved["status"] == "RESOLVED"
        assert resolved["resolution_reason"] == "condition_cleared"
        assert persisted_approval["status"] == "CANCELLED"
        assert persisted_approval["decision_reason"] == "condition_cleared"
        assert [event["event_type"] for event in audit.list_events(incident_id)] == [
            "approval_requested",
            "approval_cancelled",
            "incident_resolved",
        ]
    else:
        assert body.get("condition_cleared") is not True
        assert "remediation permission" in body["note"]
        assert lab_api.get_incident_detail(incident_id)["status"] == "OPEN"
        assert persisted_approval["status"] == "PENDING"
        assert [event["event_type"] for event in audit.list_events(incident_id)] == [
            "approval_requested",
        ]
    lab_api.repo.save_investigation(
        "investigation-cleared-123",
        "offline-test",
        incident_id=incident_id,
        title="Cleared condition audit",
        messages=[],
    )
    history = api_client.get("/investigations", headers=headers)
    assert history.status_code == 200
    entry = next(
        item for item in history.json()["investigations"]
        if item["investigation_id"] == "investigation-cleared-123"
    )
    assert entry["incident_status"] == expected_status
    assert entry["resolution_reason"] == (
        "condition_cleared" if expected_status == "RESOLVED" else None
    )


def test_remediation_options_require_auth_and_known_incident(api_client):
    assert api_client.get("/incidents/inc-x/remediation-options").status_code == 401
    assert api_client.get("/incidents/inc-missing/remediation-options", headers=auth()).status_code == 404


def test_ask_answers_only_from_validated_model_output(api_client, monkeypatch):
    _incident()

    async def model(system, user):
        assert "ONLY the JSON context" in system
        assert "inc-tele-1" in user
        return json.dumps({"answer": "Incident inc-tele-1 is OPEN.", "citations": ["incident"]})
    monkeypatch.setattr(assistant, "_call_groq", model)

    body = api_client.post("/console/ask", headers=auth(),
                           json={"question": "status?", "incident_id": "inc-tele-1"}).json()

    assert body["llm_used"] is True
    assert body["answer"] == "Incident inc-tele-1 is OPEN."


def test_ask_rejects_an_answer_that_invents_an_identifier(api_client, monkeypatch):
    _incident()

    async def model(system, user):
        return json.dumps({"answer": "inc-made-up-99 was resolved.", "citations": ["incident"]})
    monkeypatch.setattr(assistant, "_call_groq", model)

    body = api_client.post("/console/ask", headers=auth(),
                           json={"question": "status?", "incident_id": "inc-tele-1"}).json()

    assert body["llm_used"] is False
    assert "inc-made-up-99" not in body["answer"]
    assert "inc-tele-1" in body["answer"]


def test_ask_falls_back_to_records_when_the_model_is_unavailable(api_client, monkeypatch):
    _incident()

    async def model(system, user):
        raise RuntimeError("no network")
    monkeypatch.setattr(assistant, "_call_groq", model)

    body = api_client.post("/console/ask", headers=auth(),
                           json={"question": "status?", "incident_id": "inc-tele-1"}).json()

    assert body["llm_used"] is False and "unavailable" in body["note"]


def test_ask_validates_input_and_auth(api_client):
    assert api_client.post("/console/ask", json={"question": "x"}).status_code == 401
    assert api_client.post("/console/ask", headers=auth(), json={"question": ""}).status_code == 422
    assert api_client.post("/console/ask", headers=auth(),
                           json={"question": "x", "incident_id": "inc-nope"}).status_code == 404


def _persist_diagnosis(incident, result, status="completed"):
    digest = f"test-diagnosis-{incident['incident_id']}"
    claim = claim_incident_diagnosis(incident["id"], digest)
    assert claim["claimed"] is True
    finish_incident_diagnosis(
        incident["id"], digest, status, result, attempts=1,
    )


def _ask_cause(api_client, incident_id, monkeypatch):
    async def no_model_call(*_):
        raise AssertionError("Cause answers must use persisted records only")

    def no_live_snapshot(_):
        raise AssertionError("Cause answers must not use current live telemetry")

    monkeypatch.setattr(assistant, "_call_groq", no_model_call)
    monkeypatch.setattr(lab_api, "_service_snapshot", no_live_snapshot)
    return api_client.post(
        "/console/ask",
        headers=auth(("read",)),
        json={"question": "What caused this incident?", "incident_id": incident_id},
    )


def test_cause_answer_explains_resolved_incident_and_observation_recovery(
    api_client, monkeypatch,
):
    incident = _incident(
        source_timestamp="2026-02-01T10:00:00+00:00",
        source_evidence={
            "source": "prometheus",
            "description": "Memory usage exceeded the recorded threshold.",
            "metric": {"name": "memory_usage", "value": 96, "baseline_median": 54},
        },
    )
    _persist_diagnosis(incident, {
        "root_cause_analysis_status": "completed",
        "root_cause_analysis": {
            "root_cause": "A retained object set caused memory use to grow.",
            "evidence": ["Memory usage rose above the recorded baseline."],
        },
        "diagnosis": {
            "observed_at": "2026-02-01T10:00:00+00:00",
            "findings": [{
                "type": "memory_pressure",
                "evidence": "Memory usage was 96 against a baseline of 54.",
            }],
        },
    })
    assert resolve_incident_fingerprint(
        incident["fingerprint"], resolution_reason="healthy_service_observation",
    ) == 1

    response = _ask_cause(api_client, incident["incident_id"], monkeypatch)

    assert response.status_code == 200
    body = response.json()
    assert body["llm_used"] is False
    assert "## Original failure" in body["answer"]
    assert "## Root-cause analysis" in body["answer"]
    assert "## Diagnostic evidence" in body["answer"]
    assert "A retained object set caused memory use to grow." in body["answer"]
    assert "Original evidence timestamp: 2026-02-01T10:00:00+00:00" in body["answer"]
    assert "## Recovery action" in body["answer"]
    assert "no remediation execution is recorded" in body["answer"]
    assert "healthy/cleared-condition observation" in body["answer"]
    assert "## Final status\n- **RESOLVED**." in body["answer"]


def test_cause_answer_reports_unresolved_incident_status(api_client, monkeypatch):
    incident = _incident()

    response = _ask_cause(api_client, incident["incident_id"], monkeypatch)

    answer = response.json()["answer"]
    assert response.status_code == 200
    assert "## Final status\n- **OPEN**." in answer
    assert "No successful remediation execution is recorded." in answer
    assert "No successful remediation verification is recorded." in answer


def test_cause_answer_does_not_promote_observed_symptom_to_root_cause(
    api_client, monkeypatch,
):
    incident = _incident(source_evidence={
        "source": "prometheus",
        "description": "CPU usage was unusually high.",
        "metric": {"name": "cpu_usage", "value": 99, "baseline_median": 42},
    })
    _persist_diagnosis(incident, {
        "root_cause_analysis_status": "unavailable",
        "root_cause_analysis": None,
        "diagnosis": {
            "findings": [{
                "type": "cpu_anomaly",
                "evidence": "CPU usage measured 99 against baseline 42.",
            }],
        },
    })

    response = _ask_cause(api_client, incident["incident_id"], monkeypatch)

    answer = response.json()["answer"]
    assert "The saved record identifies the trigger above" in answer
    assert "Diagnostic finding (cpu anomaly): CPU usage measured 99" in answer
    assert "Observed condition: CPU usage was unusually high." in answer


def test_cause_answer_explains_recorded_request_failure_without_test_mechanism(
    api_client, monkeypatch,
):
    incident = _incident(
        service="inventory-service",
        type="request_failure",
        severity="high",
        message="LOCAL SIMULATION: inventory-service requests return HTTP 500.",
        source_timestamp="2026-11-04T06:00:00+00:00",
        source_evidence={
            "source": "local_service_simulator",
            "simulation": "local_service_failure",
            "description": "Inventory requests return HTTP 500.",
            "evidence": [
                "The local inventory service returned HTTP 500 for business requests."
            ],
        },
    )
    with database.get_connection() as connection:
        connection.execute(
            """UPDATE incidents
               SET status = 'RESOLVED', resolved_at = ?,
                   resolution_reason = 'condition_cleared'
               WHERE id = ?""",
            ("2026-11-04T06:05:00+00:00", incident["id"]),
        )
    audit.record_event(
        incident_ref=incident["incident_id"],
        event_type="local_service_simulation_cleared",
        actor="test-operator",
        summary="Cleared simulated fault on inventory-service",
        resource="inventory-service",
        outcome="resolved",
        details={"resolution_reason": "condition_cleared"},
    )

    response = _ask_cause(api_client, incident["incident_id"], monkeypatch)

    answer = response.json()["answer"]
    assert response.status_code == 200
    assert "incident on **inventory-service** was triggered" in answer
    assert "Inventory requests return HTTP 500." in answer
    assert "inventory service returned HTTP 500" in answer
    assert "The saved record identifies the trigger above" in answer
    assert "service fault was cleared" in answer
    assert "Final status\n- **RESOLVED**" in answer
    assert "simulat" not in answer.lower()
    assert "local_service" not in answer.lower()


def test_cause_answer_separates_successful_remediation_from_verification(
    api_client, monkeypatch,
):
    incident = _incident()
    _persist_diagnosis(incident, {
        "root_cause_analysis_status": "completed",
        "root_cause_analysis": {
            "root_cause": "The recorded image reference did not exist.",
            "evidence": ["The pod reported ImagePullBackOff."],
        },
    })
    audit.record_event(
        incident_ref=incident["incident_id"],
        event_type="remediation_executed",
        actor="test-operator",
        summary="rollback_deployment: live, ok=True",
        resource="demo-web",
        outcome="ok",
        details={"mode": "live", "executed": True, "ok": True},
    )
    audit.record_event(
        incident_ref=incident["incident_id"],
        event_type="verification_completed",
        actor="kybernaut-workflow",
        summary="Verification: deployment returned to healthy state",
        resource="demo-web",
        outcome="recovered",
        details={"recovered": True, "reason": "deployment returned healthy"},
    )
    with database.get_connection() as connection:
        connection.execute(
            """UPDATE incidents
               SET status = 'RESOLVED', resolved_at = ?,
                   resolution_reason = 'verified_kubernetes_recovery'
               WHERE id = ?""",
            ("2026-02-01T10:30:00+00:00", incident["id"]),
        )
    audit.record_event(
        incident_ref=incident["incident_id"],
        event_type="incident_recovery_verified",
        actor="kybernaut-workflow",
        summary="Live remediation recovery verified and incident resolved",
        outcome="resolved",
        details={
            "execution_mode": "live",
            "verification_recovered": True,
            "resolution_reason": "verified_kubernetes_recovery",
        },
    )

    response = _ask_cause(api_client, incident["incident_id"], monkeypatch)

    answer = response.json()["answer"]
    assert "The recorded image reference did not exist." in answer
    assert "## Recovery action" in answer
    assert "Remediation was executed successfully" in answer
    assert "rollback_deployment: live, ok=True" in answer
    assert "## Verification evidence" in answer
    assert "deployment returned healthy" in answer
    assert "## Final status\n- **RESOLVED**." in answer
    assert "Resolution reason: verified kubernetes recovery." in answer