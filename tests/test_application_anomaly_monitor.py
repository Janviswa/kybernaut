import asyncio
import copy
from datetime import datetime, timedelta, timezone

import pytest

import app.database as database
import app.services.application_anomaly_monitor as monitor_module
from app.services.application_anomaly_monitor import (
    ApplicationAnomalyMonitor,
    HandoffDispatchResults,
    monitoring_enabled_from_environment,
    poll_interval_from_environment,
    start_application_anomaly_monitor,
    stop_application_anomaly_monitor,
)
from app.services.incident_repository import list_incidents
from app.services.prometheus_features import FEATURE_NAMES

REAL_DISPATCH_PENDING_HANDOFFS = monitor_module.dispatch_pending_handoffs


def _seed_component_results(
    monitor, service, outcomes, completed_at
):
    service_status = monitor._service_status[service]
    service_status["outcome"] = "evaluated"
    service_status["last_completed_at"] = completed_at.isoformat()
    for name, outcome in outcomes.items():
        component = service_status["components"][name]
        component["status"] = outcome
        component["reason"] = None
        component["last_completed_at"] = (
            completed_at.isoformat() if completed_at is not None else None
        )
        component["last_successful_at"] = (
            completed_at.isoformat()
            if outcome == "success" and completed_at is not None
            else None
        )


@pytest.fixture(autouse=True)
def skip_background_diagnosis_by_default(monkeypatch):
    async def no_diagnoses(_service):
        return []

    monkeypatch.setattr(
        monitor_module, "process_active_incidents", no_diagnoses
    )

    async def no_handoffs(_service):
        return []

    monkeypatch.setattr(
        monitor_module, "dispatch_pending_handoffs", no_handoffs
    )


def test_workflow_health_is_fresh_running_and_status_reads_are_pure(
    tmp_path,
):
    now = datetime(2026, 10, 26, tzinfo=timezone.utc)
    monitor = ApplicationAnomalyMonitor(
        60,
        services=("order-service",),
        lock_path=tmp_path / "monitor.lock",
        clock=lambda: now,
    )
    monitor._lifecycle_state = "running"
    _seed_component_results(
        monitor,
        "order-service",
        dict.fromkeys(monitor_module.MONITOR_COMPONENTS, "success"),
        now,
    )
    before = copy.deepcopy(monitor._service_status)
    expected_legacy_keys = {
        "health",
        "last_outcome",
        "last_poll_attempt_at",
        "last_completed_poll_at",
        "last_successful_poll_at",
    }

    first = monitor.status()
    second = monitor.status()

    assert first["workflow_health"] == "healthy"
    assert first["services"]["order-service"]["workflow_health"] == "healthy"
    assert all(
        item["freshness"] == "fresh"
        for item in first["services"]["order-service"]["components"].values()
    )
    assert expected_legacy_keys.issubset(first)
    assert first == second
    assert monitor._service_status == before


@pytest.mark.parametrize("lifecycle_state", ["not_started", "stopped"])
def test_workflow_health_is_not_healthy_when_monitor_is_not_running(
    tmp_path, lifecycle_state
):
    now = datetime(2026, 10, 26, tzinfo=timezone.utc)
    monitor = ApplicationAnomalyMonitor(
        60,
        services=("order-service",),
        lock_path=tmp_path / "monitor.lock",
        clock=lambda: now,
    )
    monitor._lifecycle_state = lifecycle_state
    _seed_component_results(
        monitor,
        "order-service",
        dict.fromkeys(monitor_module.MONITOR_COMPONENTS, "success"),
        now,
    )

    assert monitor.status()["workflow_health"] == "unknown"
    assert (
        monitor.status()["services"]["order-service"]["workflow_health"]
        == "unknown"
    )


def test_workflow_health_uses_two_poll_intervals_as_freshness_limit(tmp_path):
    now = datetime(2026, 10, 26, tzinfo=timezone.utc)
    completed_at = now - timedelta(seconds=120)
    monitor = ApplicationAnomalyMonitor(
        60,
        services=("order-service",),
        lock_path=tmp_path / "monitor.lock",
        clock=lambda: now,
    )
    monitor._lifecycle_state = "running"
    _seed_component_results(
        monitor,
        "order-service",
        dict.fromkeys(monitor_module.MONITOR_COMPONENTS, "success"),
        completed_at,
    )

    boundary = monitor.status()
    assert boundary["workflow_health"] == "healthy"
    assert (
        boundary["components"]["telemetry_collection"]["status_stale"]
        is False
    )

    now += timedelta(microseconds=1)
    stale = monitor.status()
    assert stale["workflow_health"] == "degraded"
    assert (
        stale["components"]["telemetry_collection"]["status_stale"] is True
    )
    assert (
        stale["services"]["order-service"]["components"]
        ["telemetry_collection"]["freshness"] == "stale"
    )


def test_missing_component_completion_timestamp_is_unknown(tmp_path):
    now = datetime(2026, 10, 26, tzinfo=timezone.utc)
    monitor = ApplicationAnomalyMonitor(
        60,
        services=("order-service",),
        lock_path=tmp_path / "monitor.lock",
        clock=lambda: now,
    )
    monitor._lifecycle_state = "running"
    _seed_component_results(
        monitor,
        "order-service",
        dict.fromkeys(monitor_module.MONITOR_COMPONENTS, "success"),
        now,
    )
    component = monitor._service_status["order-service"]["components"][
        "diagnosis"
    ]
    component["last_completed_at"] = None

    status = monitor.status()
    diagnosis = status["services"]["order-service"]["components"]["diagnosis"]

    assert status["workflow_health"] == "unknown"
    assert diagnosis["status"] == "success"
    assert diagnosis["status_stale"] is None
    assert diagnosis["freshness"] == "unknown"


def test_mixed_service_failure_has_aggregate_precedence_and_not_applicable_is_safe(
    tmp_path,
):
    now = datetime(2026, 10, 26, tzinfo=timezone.utc)
    monitor = ApplicationAnomalyMonitor(
        60,
        services=("order-service", "payment-service"),
        lock_path=tmp_path / "monitor.lock",
        clock=lambda: now,
    )
    monitor._lifecycle_state = "running"
    success = dict.fromkeys(monitor_module.MONITOR_COMPONENTS, "success")
    _seed_component_results(monitor, "order-service", success, now)
    other_service = {
        **success,
        "incident_persistence": "failed",
    }
    _seed_component_results(
        monitor, "payment-service", other_service, now
    )

    status = monitor.status()
    assert status["workflow_health"] == "degraded"
    assert status["components"]["incident_persistence"]["status"] == "failed"

    _seed_component_results(
        monitor,
        "order-service",
        {
            **dict.fromkeys(
                monitor_module.MONITOR_COMPONENTS, "not_applicable"
            ),
            "telemetry_collection": "success",
        },
        now,
    )
    _seed_component_results(
        monitor,
        "payment-service",
        {
            **dict.fromkeys(
                monitor_module.MONITOR_COMPONENTS, "not_applicable"
            ),
            "telemetry_collection": "success",
        },
        now,
    )
    assert monitor.status()["workflow_health"] == "healthy"

    monitor._service_status["payment-service"]["components"][
        "diagnosis"
    ]["status"] = "unknown"
    assert monitor.status()["workflow_health"] == "unknown"


def _evaluation(*, anomaly=False):
    observed_at = datetime.now(timezone.utc).isoformat()
    features = {
        name: {
            "value": 0.5 if name == "cpu_cores_used" and anomaly else 0.02,
            "baseline_median": 0.02,
            "deviation_score": 8.0 if anomaly and name == "cpu_cores_used" else 0.0,
            "direction": "HIGH" if anomaly and name == "cpu_cores_used" else "NORMAL",
            "classification": (
                "actionable_candidate"
                if anomaly and name == "cpu_cores_used"
                else "normal"
            ),
        }
        for name in FEATURE_NAMES
    }
    return {
        "status": "evaluated",
        "collection_status": "success",
        "evaluation_status": "success",
        "threshold": 5.0,
        "observed_at": observed_at,
        "sample_timestamps": {name: observed_at for name in FEATURE_NAMES},
        "features": features,
        "actionable_candidates": ["cpu_cores_used"] if anomaly else [],
        "informational_deviations": [],
        "operational_findings": [],
    }


def _dispatch_results(reconciliation_count=0, *items):
    results = HandoffDispatchResults(reconciliation_count)
    results.extend(items)
    return results


def _dispatch_stub(reconciliation_count=0, *items):
    async def dispatch(_service):
        return _dispatch_results(reconciliation_count, *items)

    return dispatch


@pytest.fixture
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "incidents.db")
    database.initialize_database()


def test_monitor_configuration_has_safe_defaults_and_validates(monkeypatch):
    monkeypatch.delenv("KYBERNAUT_ANOMALY_MONITOR_ENABLED", raising=False)
    monkeypatch.delenv(
        "KYBERNAUT_ANOMALY_POLL_INTERVAL_SECONDS", raising=False
    )
    assert monitoring_enabled_from_environment() is True
    assert poll_interval_from_environment() == 60.0

    monkeypatch.setenv("KYBERNAUT_ANOMALY_POLL_INTERVAL_SECONDS", "9")
    with pytest.raises(ValueError, match="at least 10"):
        poll_interval_from_environment()
    monkeypatch.setenv("KYBERNAUT_ANOMALY_MONITOR_ENABLED", "sometimes")
    with pytest.raises(ValueError, match="true or false"):
        monitoring_enabled_from_environment()


def test_diagnosis_outcomes_preserve_audit_success_and_in_progress():
    assert monitor_module._diagnosis_outcomes([{
        "status": "completed",
        "audit_status": "success",
    }]) == ("success", "success", None, None)
    assert monitor_module._diagnosis_outcomes([{
        "status": "completed",
    }]) == (
        "success",
        "unknown",
        None,
        "diagnosis_audit_outcome_not_reported",
    )
    assert monitor_module._diagnosis_outcomes([{
        "status": "skipped",
        "audit_status": "pending",
    }]) == (
        "pending",
        "pending",
        None,
        "diagnosis_in_progress",
    )


@pytest.mark.asyncio
async def test_monitor_explicit_disable_is_retained_in_lifespan(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("KYBERNAUT_ANOMALY_MONITOR_ENABLED", "false")
    app = SimpleNamespace(state=SimpleNamespace())

    await start_application_anomaly_monitor(app)

    assert app.state.anomaly_monitor is None
    assert app.state.anomaly_monitor_enabled is False
    assert app.state.anomaly_monitor_status["lifecycle_state"] == "disabled"
    assert app.state.anomaly_monitor_status["poll_activity"] == "idle"
    assert app.state.anomaly_monitor_status["freshness"] == "unknown"


@pytest.mark.asyncio
async def test_monitor_status_tracks_success_failure_recovery_and_staleness(
    monkeypatch, tmp_path
):
    now = [datetime(2026, 10, 24, tzinfo=timezone.utc)]
    results = iter([
        {"status": "evaluated"},
        {"status": "evaluated"},
        {"status": "unavailable", "reason": "telemetry_unavailable"},
        {"status": "evaluated"},
    ])

    async def evaluate(_service):
        return next(results)

    monkeypatch.setattr(monitor_module, "evaluate_service_anomaly", evaluate)
    monkeypatch.setattr(
        monitor_module.incident_workflow,
        "_persist_telemetry_incidents",
        lambda *_args: {"status": "skipped", "incident_ids": []},
    )
    monitor = ApplicationAnomalyMonitor(
        60,
        services=("order-service",),
        lock_path=tmp_path / "monitor.lock",
        clock=lambda: now[0],
    )
    monitor._lifecycle_state = "running"

    assert monitor.status()["health"] == "unknown"
    assert monitor.status()["status_stale"] is None
    assert monitor.status()["freshness"] == "unknown"
    assert (
        monitor.status()["services"]["order-service"]["status_stale"] is None
    )

    assert (await monitor.poll_once())[0]["status"] == "evaluated"
    successful_at = monitor.status()["last_successful_poll_at"]
    assert monitor.status()["health"] == "healthy"
    assert monitor.status()["services"]["order-service"]["outcome"] == "evaluated"
    assert monitor.status()["services"]["order-service"]["status_stale"] is False

    now[0] += timedelta(seconds=120)
    assert monitor.status()["status_stale"] is False
    assert monitor.status()["health"] == "healthy"
    now[0] += timedelta(seconds=1)
    assert monitor.status()["status_stale"] is True
    assert monitor.status()["health"] == "degraded"

    assert (await monitor.poll_once())[0]["status"] == "evaluated"
    successful_at = monitor.status()["last_successful_poll_at"]
    now[0] += timedelta(seconds=60)
    assert (await monitor.poll_once())[0]["status"] == "unavailable"
    assert monitor.status()["last_outcome"] == "unavailable"
    assert monitor.status()["last_successful_poll_at"] == successful_at
    assert monitor.status()["health"] == "degraded"

    now[0] += timedelta(seconds=60)
    assert (await monitor.poll_once())[0]["status"] == "evaluated"
    assert monitor.status()["last_outcome"] == "success"
    assert monitor.status()["health"] == "healthy"

    now[0] += timedelta(seconds=121)
    stale = monitor.status()
    assert stale["status_stale"] is True
    assert stale["health"] == "degraded"


@pytest.mark.asyncio
async def test_monitor_status_does_not_reuse_success_during_stalled_poll(
    monkeypatch, tmp_path
):
    now = [datetime(2026, 10, 24, tzinfo=timezone.utc)]
    started = asyncio.Event()
    release = asyncio.Event()
    evaluations = 0

    async def evaluate(_service):
        nonlocal evaluations
        evaluations += 1
        if evaluations == 3:
            started.set()
            await release.wait()
        return {"status": "evaluated"}

    monkeypatch.setattr(monitor_module, "evaluate_service_anomaly", evaluate)
    monkeypatch.setattr(
        monitor_module.incident_workflow,
        "_persist_telemetry_incidents",
        lambda *_args: {"status": "skipped", "incident_ids": []},
    )
    monitor = ApplicationAnomalyMonitor(
        60,
        services=("order-service",),
        lock_path=tmp_path / "monitor.lock",
        clock=lambda: now[0],
    )
    monitor._lifecycle_state = "running"
    await monitor.poll_once()

    now[0] += timedelta(seconds=120)
    assert monitor.status()["status_stale"] is False
    now[0] += timedelta(seconds=1)
    assert monitor.status()["status_stale"] is True
    await monitor.poll_once()

    now[0] += timedelta(seconds=60)
    polling = asyncio.create_task(monitor.poll_once())
    await started.wait()
    while_status = monitor.status()
    assert while_status["poll_activity"] == "in_progress"
    assert while_status["health"] == "unknown"
    assert while_status["last_outcome"] == "success"
    assert while_status["last_completed_poll_at"] == (
        while_status["last_successful_poll_at"]
    )

    now[0] += timedelta(seconds=121)
    stalled_status = monitor.status()
    assert stalled_status["poll_activity"] == "in_progress"
    assert stalled_status["status_stale"] is True
    assert stalled_status["health"] == "degraded"
    assert stalled_status["last_poll_attempt_at"] != (
        stalled_status["last_completed_poll_at"]
    )

    release.set()
    await polling
    assert monitor.status()["last_outcome"] == "success"
    assert monitor.status()["poll_activity"] == "idle"


@pytest.mark.asyncio
async def test_monitor_status_tracks_per_service_freshness_during_sequential_poll(
    monkeypatch, tmp_path
):
    now = [datetime(2026, 10, 24, tzinfo=timezone.utc)]
    second_service_started = asyncio.Event()
    release_second_service = asyncio.Event()
    should_block = False

    async def evaluate(service):
        if should_block and service == "payment-service":
            second_service_started.set()
            await release_second_service.wait()
        return {"status": "evaluated"}

    monkeypatch.setattr(monitor_module, "evaluate_service_anomaly", evaluate)
    monkeypatch.setattr(
        monitor_module.incident_workflow,
        "_persist_telemetry_incidents",
        lambda *_args: {"status": "skipped", "incident_ids": []},
    )
    monitor = ApplicationAnomalyMonitor(
        60,
        services=("order-service", "payment-service"),
        lock_path=tmp_path / "monitor.lock",
        clock=lambda: now[0],
    )
    monitor._lifecycle_state = "running"
    await monitor.poll_once()

    now[0] += timedelta(seconds=60)
    should_block = True
    polling = asyncio.create_task(monitor.poll_once())
    await second_service_started.wait()

    now[0] += timedelta(seconds=61)
    status = monitor.status()
    assert status["health"] == "degraded"
    assert status["services"]["order-service"]["poll_activity"] == "idle"
    assert status["services"]["order-service"]["status_stale"] is False
    assert status["services"]["payment-service"]["poll_activity"] == "in_progress"
    assert status["services"]["payment-service"]["status_stale"] is True
    assert status["services"]["payment-service"]["outcome"] == "evaluated"

    release_second_service.set()
    await polling


@pytest.mark.asyncio
async def test_mixed_service_results_are_partial_and_visible(
    monkeypatch, tmp_path
):
    now = datetime(2026, 10, 24, tzinfo=timezone.utc)
    second_service_started = asyncio.Event()
    release_second_service = asyncio.Event()

    async def evaluate(service):
        if service == "order-service":
            return {"status": "unavailable"}
        second_service_started.set()
        await release_second_service.wait()
        return {"status": "evaluated"}

    monkeypatch.setattr(monitor_module, "evaluate_service_anomaly", evaluate)
    monkeypatch.setattr(
        monitor_module.incident_workflow,
        "_persist_telemetry_incidents",
        lambda *_args: {"status": "skipped", "incident_ids": []},
    )
    monitor = ApplicationAnomalyMonitor(
        60,
        services=("order-service", "payment-service"),
        lock_path=tmp_path / "monitor.lock",
        clock=lambda: now,
    )
    monitor._lifecycle_state = "running"

    polling = asyncio.create_task(monitor.poll_once())
    await second_service_started.wait()
    in_progress = monitor.status()
    assert in_progress["poll_activity"] == "in_progress"
    assert in_progress["health"] == "degraded"
    assert in_progress["last_outcome"] == "unknown"
    assert (
        in_progress["services"]["order-service"]["outcome"] == "unavailable"
    )
    assert (
        in_progress["services"]["payment-service"]["poll_activity"]
        == "in_progress"
    )

    release_second_service.set()
    results = await polling

    status = monitor.status()
    assert [result["status"] for result in results] == [
        "unavailable",
        "evaluated",
    ]
    assert status["last_outcome"] == "partial"
    assert status["health"] == "degraded"
    assert status["services"]["order-service"]["outcome"] == "unavailable"
    assert status["services"]["payment-service"]["outcome"] == "evaluated"
    assert status["services"]["payment-service"]["status_stale"] is False


@pytest.mark.asyncio
async def test_poll_exception_is_sanitized_and_recorded_as_error(
    monkeypatch, tmp_path
):
    async def evaluate(_service):
        raise RuntimeError("/private/credentials/should-not-leak")

    monkeypatch.setattr(monitor_module, "evaluate_service_anomaly", evaluate)
    monitor = ApplicationAnomalyMonitor(
        60,
        services=("order-service",),
        lock_path=tmp_path / "monitor.lock",
        clock=lambda: datetime(2026, 10, 24, tzinfo=timezone.utc),
    )
    monitor._lifecycle_state = "running"

    result = await monitor.poll_once()
    status = monitor.status()

    assert result[0]["status"] == "error"
    assert result[0]["reason"] == "RuntimeError"
    assert status["last_outcome"] == "error"
    assert status["services"]["order-service"]["outcome"] == "error"
    assert "/private/credentials" not in str(status)


def test_monitor_status_distinguishes_follower_and_lock_unavailable(
    monkeypatch, tmp_path
):
    follower = ApplicationAnomalyMonitor(
        60, services=("order-service",), lock_path=tmp_path / "monitor.lock"
    )
    leader = ApplicationAnomalyMonitor(
        60, services=("order-service",), lock_path=tmp_path / "monitor.lock"
    )
    assert leader._acquire_lock() is True
    assert follower._acquire_lock() is False
    assert follower.status()["leadership_state"] == "follower"
    leader._release_lock()

    unavailable = ApplicationAnomalyMonitor(
        60, services=("order-service",), lock_path=tmp_path / "lock-error" / "monitor.lock"
    )
    monkeypatch.setattr(
        monitor_module.fcntl,
        "flock",
        lambda *_args: (_ for _ in ()).throw(OSError("private lock path")),
    )
    assert unavailable._acquire_lock() is False
    assert unavailable.status()["leadership_state"] == "lock_unavailable"


@pytest.mark.asyncio
async def test_poll_persists_deduplicates_and_resolves_healthy_observations(
    isolated_database, monkeypatch, tmp_path
):
    evaluations = iter([_evaluation(anomaly=True), _evaluation(anomaly=True),
                        _evaluation()])

    async def evaluate(_service):
        return next(evaluations)

    monkeypatch.setattr(monitor_module, "evaluate_service_anomaly", evaluate)
    monitor = ApplicationAnomalyMonitor(60, services=("order-service",),
                                        lock_path=tmp_path / "monitor.lock")

    first = await monitor.poll_once()
    second = await monitor.poll_once()

    assert first[0]["persistence"]["status"] == "persisted"
    assert second[0]["persistence"]["status"] == "persisted"
    assert len(list_incidents()) == 1
    incident = list_incidents()[0]
    assert incident["service"] == "order-service"
    assert incident["incident_type"] == "cpu_spike"
    assert incident["status"] == "OPEN"

    healthy = await monitor.poll_once()
    assert healthy[0]["persistence"]["status"] == "healthy"
    assert list_incidents()[0]["status"] == "RESOLVED"


@pytest.mark.asyncio
async def test_monitor_invokes_read_only_diagnosis_for_active_incidents(
    isolated_database, monkeypatch, tmp_path
):
    services = []

    async def evaluate(_service):
        return _evaluation(anomaly=True)

    async def diagnose(service):
        services.append(service)
        return [{"status": "limited", "limitations": ["mapping_missing"]}]

    monkeypatch.setattr(monitor_module, "evaluate_service_anomaly", evaluate)
    monkeypatch.setattr(
        monitor_module, "process_active_incidents", diagnose
    )
    monitor = ApplicationAnomalyMonitor(
        60,
        services=("order-service",),
        lock_path=tmp_path / "monitor.lock",
    )

    result = await monitor.poll_once()

    assert services == ["order-service"]
    assert result[0]["diagnoses"][0]["status"] == "limited"
    assert len(list_incidents()) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "evaluation",
    [
        {"status": "unavailable", "reason": "incomplete_telemetry"},
        {"status": "unavailable", "reason": "telemetry_collection_failed"},
    ],
)
async def test_failed_telemetry_does_not_create_or_resolve_incidents(
    isolated_database, monkeypatch, tmp_path, evaluation
):
    calls = iter([_evaluation(anomaly=True), evaluation])

    async def evaluate(_service):
        return next(calls)

    monkeypatch.setattr(monitor_module, "evaluate_service_anomaly", evaluate)
    monitor = ApplicationAnomalyMonitor(60, services=("order-service",),
                                        lock_path=tmp_path / "monitor.lock")
    await monitor.poll_once()
    result = await monitor.poll_once()

    assert result[0]["status"] == "unavailable"
    assert result[0]["persistence"]["status"] == "skipped"
    assert len(list_incidents()) == 1
    assert list_incidents()[0]["status"] == "OPEN"


@pytest.mark.asyncio
async def test_status_exposes_successful_downstream_outcomes_additively(
    isolated_database, monkeypatch, tmp_path
):
    async def evaluate(_service):
        return _evaluation(anomaly=True)

    monkeypatch.setattr(monitor_module, "evaluate_service_anomaly", evaluate)
    monkeypatch.setattr(
        monitor_module.incident_workflow,
        "_persist_telemetry_incidents",
        lambda *_args: {
            "status": "persisted",
            "incident_ids": ["inc-1"],
            "occurrences": [{"incident_row_id": 1}],
        },
    )
    monkeypatch.setattr(
        monitor_module,
        "dispatch_pending_handoffs",
        _dispatch_stub(
            0,
            {"kind": "dispatched", "status": "no_action"},
        ),
    )

    async def diagnoses(_service):
        return [{
            "incident_id": "inc-1",
            "status": "completed",
            "audit_status": "success",
        }]

    monkeypatch.setattr(monitor_module, "process_active_incidents", diagnoses)
    monitor = ApplicationAnomalyMonitor(
        60,
        services=("order-service",),
        lock_path=tmp_path / "monitor.lock",
    )
    monitor._lifecycle_state = "running"

    result = (await monitor.poll_once())[0]
    status = monitor.status()
    components = status["services"]["order-service"]["components"]

    assert result["status"] == "evaluated"
    assert status["health"] == "healthy"
    assert status["workflow_health"] == "healthy"
    assert status["last_successful_poll_at"] is not None
    assert status["last_successful_workflow_at"] is not None
    assert components["telemetry_collection"]["status"] == "success"
    assert components["anomaly_evaluation"]["status"] == "success"
    assert components["incident_persistence"]["status"] == "success"
    assert components["handoff_enqueue"]["status"] == "success"
    assert components["handoff_dispatch"]["status"] == "success"
    assert components["reconciliation"]["status"] == "success"
    assert components["diagnosis"]["status"] == "success"
    assert components["diagnosis_audit"]["status"] == "success"
    assert components["diagnosis_audit"]["reason"] is None
    assert status["components"]["incident_persistence"]["status"] == "success"


@pytest.mark.asyncio
async def test_successful_evaluation_with_persistence_failure_is_visible(
    monkeypatch, tmp_path
):
    async def evaluate(_service):
        return _evaluation(anomaly=True)

    def fail_persistence(*_args):
        raise RuntimeError("private /secret/path must not be exposed")

    monkeypatch.setattr(monitor_module, "evaluate_service_anomaly", evaluate)
    monkeypatch.setattr(
        monitor_module.incident_workflow,
        "_persist_telemetry_incidents",
        fail_persistence,
    )
    monitor = ApplicationAnomalyMonitor(
        60,
        services=("order-service",),
        lock_path=tmp_path / "monitor.lock",
    )
    monitor._lifecycle_state = "running"

    result = (await monitor.poll_once())[0]
    status = monitor.status()
    components = status["services"]["order-service"]["components"]

    assert result["status"] == "error"
    assert status["health"] == "degraded"
    assert status["workflow_health"] == "degraded"
    assert components["anomaly_evaluation"]["status"] == "success"
    assert components["incident_persistence"]["status"] == "failed"
    assert components["incident_persistence"]["reason"] == "RuntimeError"
    assert components["handoff_enqueue"]["status"] == "unknown"
    assert components["handoff_dispatch"]["status"] == "not_applicable"
    assert "/secret/path" not in str(status)
    assert "private" not in str(status)


@pytest.mark.asyncio
async def test_pending_and_retrying_handoffs_are_not_reported_as_success(
    monkeypatch, tmp_path
):
    async def evaluate(_service):
        return _evaluation(anomaly=True)

    monkeypatch.setattr(monitor_module, "evaluate_service_anomaly", evaluate)
    monkeypatch.setattr(
        monitor_module.incident_workflow,
        "_persist_telemetry_incidents",
        lambda *_args: {"status": "healthy", "incident_ids": []},
    )
    monkeypatch.setattr(
        monitor_module,
        "dispatch_pending_handoffs",
        _dispatch_stub(
            0,
            {"kind": "retry", "reason": "workflow_not_started"},
        ),
    )
    monitor = ApplicationAnomalyMonitor(
        60,
        services=("order-service",),
        lock_path=tmp_path / "monitor.lock",
    )
    monitor._lifecycle_state = "running"

    await monitor.poll_once()
    status = monitor.status()
    components = status["services"]["order-service"]["components"]

    assert status["health"] == "healthy"
    assert status["workflow_health"] == "degraded"
    assert components["handoff_dispatch"]["status"] == "retrying"
    assert components["handoff_dispatch"]["reason"] == "workflow_not_started"


@pytest.mark.asyncio
async def test_reconciliation_and_dispatch_failure_are_separately_visible(
    monkeypatch, tmp_path
):
    async def evaluate(_service):
        return _evaluation(anomaly=True)

    monkeypatch.setattr(monitor_module, "evaluate_service_anomaly", evaluate)
    monkeypatch.setattr(
        monitor_module.incident_workflow,
        "_persist_telemetry_incidents",
        lambda *_args: {"status": "healthy", "incident_ids": []},
    )
    monkeypatch.setattr(
        monitor_module,
        "dispatch_pending_handoffs",
        _dispatch_stub(
            1, {"kind": "reconciliation", "reason": "workflow_execution_ambiguous"}
        ),
    )
    monitor = ApplicationAnomalyMonitor(
        60,
        services=("order-service",),
        lock_path=tmp_path / "monitor.lock",
    )
    monitor._lifecycle_state = "running"

    await monitor.poll_once()
    components = monitor.status()["services"]["order-service"]["components"]
    assert components["handoff_dispatch"]["status"] == "pending"
    assert components["reconciliation"]["status"] == "pending"

    async def fail_dispatch(_service):
        raise RuntimeError("private /secret/path")

    monkeypatch.setattr(
        monitor_module, "dispatch_pending_handoffs", fail_dispatch
    )
    await monitor.poll_once()
    status = monitor.status()
    components = status["services"]["order-service"]["components"]
    assert components["handoff_dispatch"]["status"] == "failed"
    assert components["handoff_dispatch"]["reason"] == "RuntimeError"
    assert components["reconciliation"]["status"] == "unknown"
    assert "/secret/path" not in str(status)

    def fail_reconciliation(_service):
        raise RuntimeError("private /reconciliation/path")

    monkeypatch.setattr(
        monitor_module.handoffs,
        "reconcile_expired_leases",
        fail_reconciliation,
    )
    monkeypatch.setattr(
        monitor_module,
        "dispatch_pending_handoffs",
        REAL_DISPATCH_PENDING_HANDOFFS,
    )
    await monitor.poll_once()
    status = monitor.status()
    components = status["services"]["order-service"]["components"]
    assert components["handoff_dispatch"]["status"] == "failed"
    assert components["reconciliation"]["status"] == "failed"
    assert components["reconciliation"]["reason"] == "RuntimeError"
    assert "/reconciliation/path" not in str(status)


@pytest.mark.asyncio
async def test_diagnosis_and_audit_failures_are_exposed(
    monkeypatch, tmp_path
):
    async def evaluate(_service):
        return _evaluation(anomaly=True)

    monkeypatch.setattr(monitor_module, "evaluate_service_anomaly", evaluate)
    monkeypatch.setattr(
        monitor_module.incident_workflow,
        "_persist_telemetry_incidents",
        lambda *_args: {"status": "healthy", "incident_ids": []},
    )
    monkeypatch.setattr(
        monitor_module,
        "dispatch_pending_handoffs",
        _dispatch_stub(0),
    )

    async def diagnoses(_service):
        return [{
            "incident_id": "inc-1",
            "status": "failed",
            "limitations": ["audit_persistence_failed"],
            "error_code": "AuditPersistenceError",
            "audit_status": "failed",
        }]

    monkeypatch.setattr(monitor_module, "process_active_incidents", diagnoses)
    monitor = ApplicationAnomalyMonitor(
        60,
        services=("order-service",),
        lock_path=tmp_path / "monitor.lock",
    )
    monitor._lifecycle_state = "running"

    await monitor.poll_once()
    status = monitor.status()
    components = status["services"]["order-service"]["components"]
    assert components["diagnosis"]["status"] == "failed"
    assert components["diagnosis_audit"]["status"] == "failed"
    assert components["diagnosis_audit"]["reason"] == (
        "audit_persistence_failed"
    )
    assert status["workflow_health"] == "degraded"


@pytest.mark.asyncio
async def test_unknown_outcomes_and_mixed_service_health_are_not_promoted(
    monkeypatch, tmp_path
):
    async def evaluate(service):
        if service == "order-service":
            return _evaluation()
        return {"status": "evaluated"}

    monkeypatch.setattr(monitor_module, "evaluate_service_anomaly", evaluate)
    monkeypatch.setattr(
        monitor_module.incident_workflow,
        "_persist_telemetry_incidents",
        lambda _service, evaluation: (
            {"status": "healthy", "incident_ids": []}
            if evaluation.get("status") == "evaluated"
            else {"status": "skipped", "reason": "unknown", "incident_ids": []}
        ),
    )
    monkeypatch.setattr(
        monitor_module,
        "dispatch_pending_handoffs",
        _dispatch_stub(0),
    )
    monitor = ApplicationAnomalyMonitor(
        60,
        services=("order-service", "payment-service"),
        lock_path=tmp_path / "monitor.lock",
    )
    monitor._lifecycle_state = "running"

    results = await monitor.poll_once()
    status = monitor.status()

    assert [result["status"] for result in results] == [
        "evaluated", "evaluated"
    ]
    assert status["last_outcome"] == "success"
    assert status["health"] == "healthy"
    assert status["workflow_health"] == "unknown"
    assert (
        status["services"]["payment-service"]["components"]
        ["telemetry_collection"]["status"] == "unknown"
    )
    assert status["components"]["telemetry_collection"]["status"] == (
        "unknown"
    )
    assert status["services"]["order-service"]["last_successful_poll_at"]
    assert "components" in status
    assert "last_outcome" in status
    assert "health" in status


@pytest.mark.asyncio
async def test_per_service_poll_guard_prevents_overlap(monkeypatch, tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()
    evaluations = 0

    async def evaluate(_service):
        nonlocal evaluations
        evaluations += 1
        entered.set()
        await release.wait()
        return {"status": "unavailable", "reason": "test"}

    monkeypatch.setattr(monitor_module, "evaluate_service_anomaly", evaluate)
    monitor = ApplicationAnomalyMonitor(60, services=("order-service",),
                                        lock_path=tmp_path / "monitor.lock")
    active = asyncio.create_task(monitor.poll_service("order-service"))
    await entered.wait()
    overlap = await monitor.poll_service("order-service")
    release.set()
    await active

    assert overlap == {
        "service": "order-service",
        "status": "skipped",
        "reason": "poll_in_progress",
    }
    assert evaluations == 1


@pytest.mark.asyncio
async def test_monitor_lock_selects_one_scheduler_and_shutdown_releases_it(
    monkeypatch, tmp_path
):
    first_poll = asyncio.Event()
    second_poll = asyncio.Event()
    leader_calls = 0
    follower_calls = 0

    async def fake_poll_once(monitor):
        nonlocal leader_calls, follower_calls
        if monitor is leader:
            leader_calls += 1
            first_poll.set()
        else:
            follower_calls += 1
            second_poll.set()

    monkeypatch.setattr(ApplicationAnomalyMonitor, "poll_once", fake_poll_once)
    lock_path = tmp_path / "monitor.lock"
    leader = ApplicationAnomalyMonitor(
        0.01, services=("order-service",), lock_path=lock_path
    )
    follower = ApplicationAnomalyMonitor(
        0.01, services=("order-service",), lock_path=lock_path
    )
    await leader.start()
    await asyncio.wait_for(first_poll.wait(), timeout=1)
    assert leader.status()["leadership_state"] == "leader"
    await follower.start()
    await asyncio.sleep(0.04)
    assert leader_calls >= 1
    assert follower_calls == 0
    assert follower.status()["leadership_state"] == "follower"

    await leader.stop()
    await asyncio.wait_for(second_poll.wait(), timeout=1)
    await follower.stop()
    assert follower_calls >= 1
    assert leader._lock_file is None
    assert follower._lock_file is None


@pytest.mark.asyncio
async def test_monitor_stop_cancels_in_flight_poll_and_releases_lock(
    monkeypatch, tmp_path
):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocking_poll(_self):
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(ApplicationAnomalyMonitor, "poll_once", blocking_poll)
    monitor = ApplicationAnomalyMonitor(
        60, services=("order-service",), lock_path=tmp_path / "monitor.lock"
    )
    await monitor.start()
    await asyncio.wait_for(started.wait(), timeout=1)
    await monitor.stop()

    assert cancelled.is_set()
    assert monitor._task is None
    assert monitor._lock_file is None
    assert monitor.status()["lifecycle_state"] == "stopped"
    assert monitor.status()["leadership_state"] == "not_leader"
    assert monitor.status()["health"] == "unknown"
    assert monitor.status()["poll_activity"] == "idle"


@pytest.mark.asyncio
async def test_lifespan_monitor_hooks_are_idempotent_and_shutdown_cleanly(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    observed = asyncio.Event()

    async def fake_poll_once(_self):
        observed.set()

    monkeypatch.setattr(ApplicationAnomalyMonitor, "poll_once", fake_poll_once)
    monkeypatch.delenv("KYBERNAUT_ANOMALY_MONITOR_ENABLED", raising=False)
    monkeypatch.setenv("KYBERNAUT_ANOMALY_POLL_INTERVAL_SECONDS", "10")
    app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(
        monitor_module, "_lock_path", lambda: tmp_path / "monitor.lock"
    )

    await start_application_anomaly_monitor(app)
    monitor = app.state.anomaly_monitor
    assert app.state.anomaly_monitor_enabled is True
    await start_application_anomaly_monitor(app)
    assert app.state.anomaly_monitor is monitor
    await asyncio.wait_for(observed.wait(), timeout=1)
    await stop_application_anomaly_monitor(app)

    assert app.state.anomaly_monitor is None
    assert monitor._task is None
    assert monitor._lock_file is None

    await start_application_anomaly_monitor(app)
    restarted_monitor = app.state.anomaly_monitor
    assert restarted_monitor is not monitor
    await stop_application_anomaly_monitor(app)
    assert app.state.anomaly_monitor is None
