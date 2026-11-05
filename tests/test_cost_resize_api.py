from datetime import datetime, timedelta, timezone

import pytest

import app.api.operations as operations
from app.database import get_connection
from app.services import audit_repository, cost_resize_repository
from tests.conftest import create_operator_token


def headers(*, operator_id="requester", permissions=("read", "remediation")):
    return {
        "Authorization": "Bearer " + create_operator_token(
            operator_id=operator_id,
            permissions=list(permissions),
        )
    }


def resize_report():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    period_start = now - timedelta(days=7)
    latest_observation = now - timedelta(hours=1)
    return {
        "run_id": "test-run",
        "generated_at": now.isoformat(),
        "reporting_period": {
            "start": period_start.isoformat(),
            "end_exclusive": now.isoformat(),
            "sample_interval_minutes": 60,
            "minimum_coverage": 0.8,
        },
        "pricing": {
            "model": "kubernetes_requests",
            "cost_model_status": "configured",
            "example": False,
            "source": "approved test rate card",
            "currency": "USD",
            "effective_date": "2026-01-01",
        },
        "thresholds": {"minimum_peak_headroom": 1.25},
        "recommendations": [{
            "resource": "kybernaut-lab/demo-web",
            "status": "request_downsize_candidate",
            "coverage": 1.0,
            "sample_count": 168,
            "first_observed_at": period_start.isoformat(),
            "last_observed_at": latest_observation.isoformat(),
            "replicas": 1,
            "container_identity": [{
                "pod": "demo-web-abc",
                "container": "web",
                "cpu_request": "1",
                "memory_request": "1Gi",
                "cpu_limit": "2",
                "memory_limit": "2Gi",
                "cpu_request_cores": 1.0,
                "memory_request_bytes": 1024 ** 3,
                "cpu_limit_cores": 2.0,
                "memory_limit_bytes": 2 * 1024 ** 3,
            }],
            "cpu_request_recommended_cores": 0.2,
            "memory_request_recommended_bytes": 512 * 1024 ** 2,
            "cpu_avg": 0.05,
            "cpu_peak": 0.1,
            "memory_avg": 0.1,
            "memory_peak": 0.2,
            "deployment_uid": "uid-demo",
            "resource_version": "11",
        }],
    }


def test_resize_approval_requires_different_owner_and_authorized_execution(
    api_client, monkeypatch
):
    monkeypatch.setattr(operations, "cost_report", lambda _authorization: resize_report())
    report_auth = headers()
    request_auth = headers(operator_id="operator-a")
    owner_auth = headers(
        operator_id="owner-b", permissions=("read", "owner", "remediation")
    )

    preview = api_client.post(
        "/cost/resizes/preview",
        headers=report_auth,
        json={"workload": "demo-web"},
    )
    assert preview.status_code == 200
    assert preview.json()["executed"] is False

    created = api_client.post(
        "/cost/resizes/requests",
        headers=request_auth,
        json={"workload": "demo-web"},
    )
    assert created.status_code == 200
    request_id = created.json()["request"]["request_id"]
    assert created.json()["request"]["status"] == "PENDING"

    self_approval = api_client.post(
        f"/cost/resizes/{request_id}/approve",
        headers=headers(
            operator_id="operator-a",
            permissions=("read", "owner", "remediation"),
        ),
        json={"reason": "reviewed"},
    )
    assert self_approval.status_code == 403

    approved = api_client.post(
        f"/cost/resizes/{request_id}/approve",
        headers=owner_auth,
        json={"reason": "reviewed request proposal"},
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"
    assert approved.json()["requested_by"] != approved.json()["approved_by"]

    calls = []

    def fake_execute(plan, *, operator_id, stage_callback):
        calls.append((plan, operator_id))
        stage_callback("PREFLIGHT_VALIDATION")
        stage_callback("EXECUTION_VERIFIED")
        return {
            "status": "VERIFIED",
            "execution_stage": "EXECUTION_VERIFIED",
            "executed": True,
            "realized_savings": 0.0,
        }

    monkeypatch.setattr(operations, "execute_approved_resize", fake_execute)
    executed = api_client.post(
        f"/cost/resizes/{request_id}/execute",
        headers=request_auth,
    )
    assert executed.status_code == 200
    assert executed.json()["status"] == "VERIFIED"
    assert calls[0][1] == "operator-a"

    status = api_client.get(
        f"/cost/resizes/{request_id}",
        headers=headers(permissions=("read",)),
    )
    assert status.status_code == 200
    assert status.json()["status"] == "VERIFIED"
    assert status.json()["execution"]["realized_savings"] == 0.0
    events = audit_repository.list_events(
        f"cost-resize:{request_id}", newest_first=True
    )
    assert [event["event_type"] for event in events] == [
        "cost_resize_execution_finished",
        "cost_resize_stage_changed",
        "cost_resize_stage_changed",
        "cost_resize_execution_started",
        "cost_resize_approved",
        "cost_resize_requested",
    ]


def test_resize_preview_and_requests_reject_missing_auth(api_client):
    response = api_client.post(
        "/cost/resizes/preview",
        json={"workload": "demo-web"},
    )
    assert response.status_code == 401


def test_resize_request_requires_remediation_permission(api_client):
    response = api_client.post(
        "/cost/resizes/requests",
        headers=headers(permissions=("read",)),
        json={"workload": "demo-web"},
    )
    assert response.status_code == 403


def test_expired_resize_execution_is_audited(api_client, monkeypatch):
    monkeypatch.setattr(operations, "cost_report", lambda _authorization: resize_report())
    created = api_client.post(
        "/cost/resizes/requests",
        headers=headers(operator_id="operator-a"),
        json={"workload": "demo-web"},
    )
    request_id = created.json()["request"]["request_id"]
    approved = api_client.post(
        f"/cost/resizes/{request_id}/approve",
        headers=headers(
            operator_id="owner-b",
            permissions=("read", "owner"),
        ),
        json={"reason": "reviewed"},
    )
    assert approved.status_code == 200

    connection = get_connection()
    try:
        connection.execute(
            "UPDATE cost_resize_requests SET expires_at=? WHERE request_id=?",
            ("2000-01-01T00:00:00+00:00", request_id),
        )
        connection.commit()
    finally:
        connection.close()

    response = api_client.post(
        f"/cost/resizes/{request_id}/execute",
        headers=headers(operator_id="operator-a"),
    )
    assert response.status_code == 410
    status = api_client.get(
        f"/cost/resizes/{request_id}",
        headers=headers(permissions=("read",)),
    )
    assert status.json()["status"] == "EXPIRED"
    events = audit_repository.list_events(
        f"cost-resize:{request_id}", newest_first=True
    )
    assert events[0]["event_type"] == "cost_resize_expired"
    assert events[0]["outcome"] == "expired"


def test_unexpected_resize_execution_failure_is_persisted(api_client, monkeypatch):
    monkeypatch.setattr(operations, "cost_report", lambda _authorization: resize_report())
    request_auth = headers(operator_id="operator-a")
    owner_auth = headers(
        operator_id="owner-b",
        permissions=("read", "owner"),
    )
    created = api_client.post(
        "/cost/resizes/requests",
        headers=request_auth,
        json={"workload": "demo-web"},
    )
    request_id = created.json()["request"]["request_id"]
    approved = api_client.post(
        f"/cost/resizes/{request_id}/approve",
        headers=owner_auth,
        json={"reason": "reviewed"},
    )
    assert approved.status_code == 200

    def fail_unexpectedly(*_args, **_kwargs):
        raise RuntimeError("internal detail must not be returned")

    monkeypatch.setattr(operations, "execute_approved_resize", fail_unexpectedly)
    response = api_client.post(
        f"/cost/resizes/{request_id}/execute",
        headers=request_auth,
    )
    assert response.status_code == 503
    assert response.json()["detail"]["status"] == "EXECUTION_UNKNOWN"
    assert "internal detail" not in response.text

    status = api_client.get(
        f"/cost/resizes/{request_id}",
        headers=headers(permissions=("read",)),
    )
    assert status.json()["status"] == "EXECUTION_UNKNOWN"
    assert status.json()["execution"]["execution_outcome"] == "unknown"


def _create_executing_request(api_client, monkeypatch):
    monkeypatch.setattr(operations, "cost_report", lambda _authorization: resize_report())
    created = api_client.post(
        "/cost/resizes/requests",
        headers=headers(operator_id="operator-a"),
        json={"workload": "demo-web"},
    )
    assert created.status_code == 200
    request_id = created.json()["request"]["request_id"]
    approved = api_client.post(
        f"/cost/resizes/{request_id}/approve",
        headers=headers(operator_id="owner-b", permissions=("read", "owner")),
        json={"reason": "reviewed"},
    )
    assert approved.status_code == 200
    cost_resize_repository.start_execution(request_id, operator_id="operator-a")
    return request_id


def test_reconciliation_requires_explicit_stopped_process_confirmation(
    api_client, monkeypatch
):
    request_id = _create_executing_request(api_client, monkeypatch)
    inspected = []
    monkeypatch.setattr(
        operations,
        "inspect_resize_plan",
        lambda plan: inspected.append(plan),
    )

    response = api_client.post(
        f"/cost/resizes/{request_id}/reconcile",
        headers=headers(operator_id="owner-b", permissions=("read", "owner")),
        json={"confirm_execution_process_stopped": False},
    )

    assert response.status_code == 409
    assert inspected == []
    assert cost_resize_repository.get_request(request_id)["status"] == "EXECUTING"


def test_reconciliation_is_owner_only_read_only_and_persisted(api_client, monkeypatch):
    request_id = _create_executing_request(api_client, monkeypatch)
    inspected = []
    reconciliation = {
        "status": "RECONCILED_PROPOSED_STATE",
        "execution_stage": "RECONCILED_PROPOSED_STATE",
        "execution_outcome": (
            "current_deployment_matches_approved_proposal; "
            "execution_attribution_not_proven"
        ),
        "executed": None,
        "mutation_attempted": True,
        "manual_review_required": False,
        "realized_savings": 0.0,
    }

    def inspect(plan):
        inspected.append(plan)
        return reconciliation

    monkeypatch.setattr(operations, "inspect_resize_plan", inspect)
    denied = api_client.post(
        f"/cost/resizes/{request_id}/reconcile",
        headers=headers(operator_id="reader", permissions=("read",)),
        json={"confirm_execution_process_stopped": True},
    )
    assert denied.status_code == 403
    assert inspected == []

    response = api_client.post(
        f"/cost/resizes/{request_id}/reconcile",
        headers=headers(operator_id="owner-b", permissions=("read", "owner")),
        json={"confirm_execution_process_stopped": True},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "RECONCILED_PROPOSED_STATE"
    assert response.json()["execution"]["executed"] is None
    assert "attribution_not_proven" in response.json()["execution"]["execution_outcome"]
    assert len(inspected) == 1
    events = audit_repository.list_events(
        f"cost-resize:{request_id}", newest_first=True
    )
    assert [event["event_type"] for event in events[:2]] == [
        "cost_resize_reconciliation_finished",
        "cost_resize_reconciliation_started",
    ]


def test_unexpected_reconciliation_failure_is_explicit_and_retryable(
    api_client, monkeypatch
):
    request_id = _create_executing_request(api_client, monkeypatch)

    def fail_inspection(_plan):
        raise RuntimeError("internal detail must not be returned")

    monkeypatch.setattr(operations, "inspect_resize_plan", fail_inspection)
    response = api_client.post(
        f"/cost/resizes/{request_id}/reconcile",
        headers=headers(operator_id="owner-b", permissions=("read", "owner")),
        json={"confirm_execution_process_stopped": True},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "RECONCILIATION_UNRESOLVED"
    assert response.json()["execution"]["executed"] is None
    assert "internal detail" not in response.text

    request, _attempt_id = cost_resize_repository.begin_reconciliation(
        request_id, operator_id="owner-c"
    )
    assert request["status"] == "RECONCILING"


def test_stale_reconciliation_attempt_cannot_overwrite_newer_result(
    api_client, monkeypatch
):
    request_id = _create_executing_request(api_client, monkeypatch)
    _request, first_attempt = cost_resize_repository.begin_reconciliation(
        request_id, operator_id="owner-a"
    )
    _request, second_attempt = cost_resize_repository.begin_reconciliation(
        request_id, operator_id="owner-b"
    )
    first_result = {
        "status": "RECONCILIATION_UNRESOLVED",
        "execution_stage": "RECONCILIATION_UNRESOLVED",
        "execution_outcome": "first read",
    }

    with pytest.raises(cost_resize_repository.CostResizeStateError) as failure:
        cost_resize_repository.finish_reconciliation(
            request_id,
            first_attempt,
            first_result,
            operator_id="owner-a",
        )
    assert failure.value.code == "stale_reconciliation"
    assert cost_resize_repository.get_request(request_id)["status"] == "RECONCILING"

    second_result = {
        "status": "RECONCILED_ORIGINAL_STATE",
        "execution_stage": "RECONCILED_ORIGINAL_STATE",
        "execution_outcome": "current state matches original",
    }
    finished = cost_resize_repository.finish_reconciliation(
        request_id,
        second_attempt,
        second_result,
        operator_id="owner-b",
    )
    assert finished["status"] == "RECONCILED_ORIGINAL_STATE"
    assert finished["execution"] == second_result
