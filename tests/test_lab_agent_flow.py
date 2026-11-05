"""Lab automation, agent remediation coverage, saved chats and cost PDF."""

import asyncio
import json

import pytest

import app.database as database
import app.api.lab as lab_api
import app.services.incident_response_graph as g
from app.services import lab_incident_scheduler as sched
from app.services.policy_engine import PolicyConfig, ProposedAction, evaluate_policy
from tests.conftest import create_operator_token


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "lab.db")
    database.initialize_database()


def auth(perms=("read",)):
    return {"Authorization": f"Bearer {create_operator_token(permissions=list(perms))}"}


# ---------------- agent: disk_full is now remediable (with approval) ------
def _disk_state(available=0):
    return {
        "incident_id": "inc-disk-1",
        "classification": {
            "incident_type": "disk_full", "confidence": 0.8, "severity": "high",
            "evidence": ["Pod sim-disk-fill-abc-123 Failed: Evicted ephemeral storage limit"],
            "target": {
                "namespace": "kybernaut-lab", "resource": "sim-disk-fill-abc-123",
                "resource_kind": "Pod", "deployment": "sim-disk-fill",
            },
        },
        "kubernetes": {"deployments": [{
            "name": "sim-disk-fill", "desired_replicas": 1,
            "available_replicas": available}]},
    }


def test_plan_proposes_pod_recreation_for_evicted_disk_full_pod():
    update = g.plan(_disk_state())

    assert update["proposal"] is not None
    assert update["proposal"]["action"] == "restart_pod"
    assert update["proposal"]["resource"] == "sim-disk-fill-abc-123"


def test_disk_full_on_a_deployment_gets_a_scale_option_but_no_pod_restart():
    state = _disk_state()
    state["classification"]["target"]["resource_kind"] = "Deployment"
    state["classification"]["target"]["resource"] = "sim-disk-fill"

    update = g.plan(state)

    actions = [o["action"] for o in update["remediation_options"] if o["kind"] == "executable"]
    assert "restart_pod" not in actions and "scale_deployment" in actions


def _restart(**kw):
    base = dict(action="restart_pod", namespace="kybernaut-lab",
                resource="sim-disk-fill-abc-123", deployment="sim-disk-fill",
                confidence=0.95, severity="high", rationale="t")
    base.update(kw)
    return ProposedAction(**base)


def test_policy_allows_restart_of_a_non_serving_pod_but_still_needs_approval():
    ctx = {"deployment": {"desired_replicas": 1, "available_replicas": 0},
           "recent_action_count": 0, "target_not_serving": True}

    decision = evaluate_policy(_restart(), ctx, PolicyConfig())

    assert decision.allowed and decision.requires_approval
    assert not decision.auto_execute


def test_policy_blast_radius_still_blocks_a_serving_single_replica():
    ctx = {"deployment": {"desired_replicas": 1, "available_replicas": 1},
           "recent_action_count": 0}

    assert not evaluate_policy(_restart(), ctx, PolicyConfig()).allowed


def test_policy_exemption_requires_known_deployment_state():
    ctx = {"recent_action_count": 0, "target_not_serving": True}

    assert not evaluate_policy(_restart(), ctx, PolicyConfig()).allowed


def test_remediation_options_report_action_capacity(monkeypatch):
    options = [
        {"kind": "executable", "executable": True, "action": "restore_command"},
        {"kind": "executable", "executable": False, "action": "restart_pod"},
        {"kind": "manual", "executable": False},
    ]
    monkeypatch.setattr(
        lab_api.claims, "count_recent_action_claims", lambda deployment, window: 5
    )

    lab_api._apply_action_capacity(options, "sim-memory-leak", "kybernaut-lab")

    assert options[0]["policy_available"] is False
    assert options[0]["policy_reason"] == "action_claim_limit_reached"
    assert "5/5" in options[0]["policy_detail"]
    assert options[1]["policy_available"] is False
    assert "policy_available" not in options[2]


# ---------------- lab incident generator ----------------
def test_scheduler_cleans_up_previous_scenario_then_injects_and_persists(monkeypatch):
    calls = []
    observations = iter([
        {"incidents": [{
            "type": "pod_container_failure",
            "resource": "sim-pod-crash-test-1",
        }]},
        {"incidents": [{
            "type": "pod_failed",
            "resource": "sim-disk-fill-test-1",
        }]},
    ])
    monkeypatch.setattr(sched, "run_scenario",
                        lambda name, phase, **kw: calls.append((name, phase, kw["dry_run"])))
    monkeypatch.setattr(sched, "observe_namespace", lambda: next(observations))
    monkeypatch.setattr(sched, "persist_observation",
                        lambda observation: {"persisted_count": 1})
    monkeypatch.setattr(sched.asyncio, "sleep", lambda _seconds: _no_sleep())
    monkeypatch.setattr(sched, "ROTATION", (("pod-crash", "high", 0), ("disk-full", "high", 0)))
    audit = []
    scheduler = sched.LabIncidentScheduler(
        runner=lambda *a, **k: None, audit=lambda event, *_: audit.append(event))

    first = asyncio.run(scheduler.run_cycle())
    second = asyncio.run(scheduler.run_cycle())

    assert (first["scenario"], second["scenario"]) == ("pod-crash", "disk-full")
    assert calls == [
        ("pod-crash", "cleanup", False),
        ("pod-crash", "inject", False),
        ("pod-crash", "cleanup", False),
        ("disk-full", "cleanup", False),
        ("disk-full", "inject", False),
    ]
    assert first["persistence"]["persisted_count"] == 1
    assert audit.count("lab_scenario_injected") == 2


async def _no_sleep():
    return None


def test_scheduler_is_off_unless_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("KYBERNAUT_LAB_AUTOGEN", raising=False)
    assert sched.autogen_enabled() is False
    monkeypatch.setenv("KYBERNAUT_LAB_AUTOGEN", "true")
    assert sched.autogen_enabled() is True


def test_scheduler_interval_has_a_safe_floor(monkeypatch):
    monkeypatch.delenv("KYBERNAUT_LAB_AUTOGEN_INTERVAL_SECONDS", raising=False)
    assert sched.interval_seconds() == 600
    monkeypatch.setenv("KYBERNAUT_LAB_AUTOGEN_INTERVAL_SECONDS", "60")
    assert sched.interval_seconds() == 600


def test_auto_incident_status_requires_auth_and_reports_rotation(api_client):
    assert api_client.get("/lab/auto-incidents").status_code == 401

    body = api_client.get("/lab/auto-incidents", headers=auth()).json()

    assert {item["scenario"] for item in body["rotation"]} >= {"pod-crash", "disk-full"}
    assert "classifier" in body["note"]


def test_run_now_requires_remediation_permission_without_shared_token(api_client):
    assert api_client.post(
        "/lab/auto-incidents/run-now", headers=auth()
    ).status_code == 403
    response = api_client.post(
        "/lab/auto-incidents/run-now", headers=auth(("remediation",))
    )
    assert response.status_code == 409
    assert "Lab incident generation is disabled" in response.json()["detail"]


def test_local_service_simulator_status_and_lab_generator_exclusion(
    api_client, monkeypatch,
):
    monkeypatch.setenv("KYBERNAUT_ENVIRONMENT", "local-demo")
    monkeypatch.setenv("KYBERNAUT_SERVICE_SIMULATION_ENABLED", "true")
    monkeypatch.setenv("KYBERNAUT_FAILURE_SIMULATION_TOKEN", "offline-test-token")
    api_client.app.state.local_service_incident_scheduler.state.update(
        enabled=True, running=True,
    )

    status = api_client.get(
        "/local-service-simulation", headers=auth(),
    )
    assert status.status_code == 200
    body = status.json()
    assert body["enabled"] is True
    assert body["missing_configuration"] == []
    assert body["incidents_per_cycle"] == 2
    assert body["severity_mix"] == ["critical", "high"]
    assert body["interval_anchor"] == "both_incidents_resolved"
    assert {item["service"] for item in body["scenarios"]} == {
        "order-service", "payment-service", "user-service", "inventory-service",
    }

    lab_status = api_client.get(
        "/lab/auto-incidents", headers=auth(),
    ).json()
    assert lab_status["enabled"] is False
    assert lab_status["disabled_reason"] == "local_service_simulator_active"


def test_local_service_simulation_run_now_requires_enabled_scheduler_and_permission(
    api_client, monkeypatch,
):
    scheduler = api_client.app.state.local_service_incident_scheduler
    response = api_client.post(
        "/local-service-simulation/run-now", headers=auth(("remediation",)),
    )
    assert response.status_code == 409

    monkeypatch.setenv("KYBERNAUT_ENVIRONMENT", "local-demo")
    monkeypatch.setenv("KYBERNAUT_SERVICE_SIMULATION_ENABLED", "true")
    monkeypatch.setenv("KYBERNAUT_FAILURE_SIMULATION_TOKEN", "offline-test-token")
    scheduler.state.update(enabled=True, running=True)

    async def run_cycle(*, manual=False):
        assert manual is True
        return {
            "services": ["order-service", "payment-service"],
            "incidents_created": 2,
            "duration_seconds": 600,
        }

    monkeypatch.setattr(scheduler, "run_cycle", run_cycle)
    response = api_client.post(
        "/local-service-simulation/run-now", headers=auth(("remediation",)),
    )

    assert response.status_code == 200
    assert response.json()["incidents_created"] == 2
    assert api_client.post("/local-service-simulation/run-now").status_code == 401
    assert api_client.post(
        "/local-service-simulation/run-now", headers=auth(),
    ).status_code == 403


# ---------------- agent / LLM status ----------------
def test_agent_status_never_exposes_the_api_key(api_client, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_super_secret_value")

    response = api_client.get("/agent/status", headers=auth())

    assert response.status_code == 200
    assert response.json()["llm"]["api_key_configured"] is True
    assert "gsk_super_secret_value" not in response.text


def test_agent_status_reports_missing_key_without_probing(api_client, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    body = api_client.get("/agent/status?probe=true", headers=auth()).json()

    assert body["llm"]["probe"]["status"] == "not_configured"


# ---------------- saved console investigations ----------------
def test_investigation_roundtrip_and_ownership(api_client):
    messages = [{"role": "user", "text": "hello"}, {"role": "agent", "text": "hi"}]
    put = api_client.put("/investigations/chat-0123456789ab", headers=auth(),
                         json={"title": "T", "incident_id": "inc-1", "messages": messages})
    assert put.status_code == 200 and put.json()["message_count"] == 2

    got = api_client.get("/investigations/chat-0123456789ab", headers=auth()).json()
    assert got["messages"] == messages
    listing = api_client.get("/investigations", headers=auth()).json()["investigations"]
    assert [item["investigation_id"] for item in listing] == ["chat-0123456789ab"]


def test_investigation_requires_auth_and_rejects_bad_ids(api_client):
    assert api_client.get("/investigations").status_code == 401
    bad = api_client.put("/investigations/x", headers=auth(), json={"messages": []})
    assert bad.status_code == 400
    assert api_client.get("/investigations/chat-doesnotexist1", headers=auth()).status_code == 404


# ---------------- cost report PDF ----------------
def test_cost_report_pdf_download(api_client, monkeypatch):
    import app.api.lab as lab
    import app.main as main

    async def collect(_client, *, required_features):
        assert required_features == ("cpu_cores_used", "memory_bytes")
        return {
            service: {
                "cpu_cores_used": 0.1,
                "memory_bytes": 1024 ** 3,
                "request_rate": 2.0,
                "p95_latency_seconds": 0.04,
            }
            for service in main.EXPECTED_SERVICES
        }

    monkeypatch.setenv("KYBERNAUT_ENVIRONMENT", "local-demo")
    monkeypatch.delenv("KYBERNAUT_COST_CPU_PER_CORE_HOUR_USD", raising=False)
    monkeypatch.delenv("KYBERNAUT_COST_MEMORY_PER_GIB_HOUR_USD", raising=False)
    monkeypatch.setattr(lab, "collect_service_features", collect)
    pdf_reports = []
    rendered_pdfs = []
    render_pdf = lab.build_live_cost_report_pdf

    def render_and_capture(report, **kwargs):
        pdf_reports.append(report)
        rendered = render_pdf(report, **kwargs)
        rendered_pdfs.append(rendered)
        return rendered

    monkeypatch.setattr(lab, "build_live_cost_report_pdf", render_and_capture)

    response = api_client.get("/cost/report.pdf", headers=auth())

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content.startswith(b"%PDF-")
    assert "kybernaut-live-cost-report.pdf" in response.headers["content-disposition"]
    assert b"USD estimates" in response.content
    assert b"$10.00" in response.content
    assert b"$5.25" in response.content
    assert b"not actual cloud-provider costs" in response.content
    assert b"$6.250000/hour" in response.content
    assert b"$4562.50/month" in response.content
    assert b"Illustrative 10% usage-reduction scenario" in response.content
    assert b"memory ~$383.25/month" in response.content
    assert api_client.get("/cost/report.pdf").status_code == 401

def test_saved_investigation_can_be_deleted_by_owner_only(api_client):
    body = {"title": "Delete me", "incident_id": None,
            "messages": [{"role": "user", "text": "hello"}]}
    assert api_client.put("/investigations/chat-aaaaaaaaaaaa", headers=auth(), json=body).status_code == 200
    assert api_client.delete("/investigations/chat-aaaaaaaaaaaa").status_code == 401
    assert api_client.delete("/investigations/chat-aaaaaaaaaaaa", headers=auth()).json()["deleted"] is True
    assert api_client.get("/investigations/chat-aaaaaaaaaaaa", headers=auth()).status_code == 404
    assert api_client.delete("/investigations/chat-aaaaaaaaaaaa", headers=auth()).status_code == 404