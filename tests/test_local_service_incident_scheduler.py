import asyncio

import pytest

import app.database as database
import app.services.local_service_incident_scheduler as scheduler_module
from app.services.incident_repository import list_incidents


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "simulation.db")
    database.initialize_database()


def install_fake_controls(monkeypatch, *, fail_start_for=None):
    calls = []

    async def control(self, scenario, *, action):
        calls.append((scenario["service"], action))
        if action == "start" and scenario["service"] == fail_start_for:
            raise RuntimeError("offline test control failure")
        return {
            "service": scenario["service"],
            "active": action == "start",
            "scenario": scenario["scenario"] if action == "start" else None,
        }

    monkeypatch.setattr(
        scheduler_module.LocalServiceIncidentScheduler, "_control", control,
    )
    return calls


def test_simulator_defaults_enabled_only_for_local_demo_with_shared_token(
    monkeypatch,
):
    monkeypatch.setenv("KYBERNAUT_ENVIRONMENT", "local-demo")
    monkeypatch.setenv("KYBERNAUT_FAILURE_SIMULATION_TOKEN", "offline-test-token")
    monkeypatch.delenv("KYBERNAUT_SERVICE_SIMULATION_ENABLED", raising=False)

    assert scheduler_module.enabled() is True


def test_simulator_can_be_explicitly_disabled_and_requires_token(monkeypatch):
    monkeypatch.setenv("KYBERNAUT_ENVIRONMENT", "local-demo")
    monkeypatch.setenv("KYBERNAUT_FAILURE_SIMULATION_TOKEN", "offline-test-token")
    monkeypatch.setenv("KYBERNAUT_SERVICE_SIMULATION_ENABLED", "false")
    assert scheduler_module.enabled() is False

    monkeypatch.delenv("KYBERNAUT_SERVICE_SIMULATION_ENABLED")
    monkeypatch.delenv("KYBERNAUT_FAILURE_SIMULATION_TOKEN")
    assert scheduler_module.enabled() is False


@pytest.mark.asyncio
async def test_cycle_creates_one_critical_and_one_high_incident(monkeypatch):
    install_fake_controls(monkeypatch)
    monkeypatch.setattr(
        scheduler_module.audit_repository, "record_event", lambda **_: {},
    )
    scheduler = scheduler_module.LocalServiceIncidentScheduler()

    result = await scheduler.run_cycle()
    incidents = list_incidents(status="OPEN")

    assert result["incidents_created"] == 2
    assert result["services"] == ["order-service", "payment-service"]
    assert {
        (item["service"], item["incident_type"], item["severity"])
        for item in incidents
    } == {
        ("order-service", "high_latency", "critical"),
        ("payment-service", "http_5xx", "high"),
    }
    assert all(
        item["source_evidence"]["simulation"] == "local_service_failure"
        for item in incidents
    )
    assert {item["source_evidence"]["simulation_cycle"] for item in incidents} == {1}


@pytest.mark.asyncio
async def test_each_cycle_rotates_to_a_different_pair_and_keeps_two_severities(
    monkeypatch,
):
    install_fake_controls(monkeypatch)
    monkeypatch.setattr(
        scheduler_module.audit_repository, "record_event", lambda **_: {},
    )
    first_scheduler = scheduler_module.LocalServiceIncidentScheduler()
    first = await first_scheduler.run_cycle()
    second_scheduler = scheduler_module.LocalServiceIncidentScheduler()
    second = await second_scheduler.run_cycle(manual=True)
    incidents = list_incidents(status="OPEN")

    assert first["services"] == ["order-service", "payment-service"]
    assert second["services"] == ["user-service", "inventory-service"]
    assert set(first["services"]).isdisjoint(second["services"])
    assert len(incidents) == 2
    assert {item["severity"] for item in incidents} == {"critical", "high"}
    assert {item["source_evidence"]["simulation_cycle"] for item in incidents} == {2}
    assert second_scheduler._schedule_reset.is_set()


@pytest.mark.asyncio
async def test_repeated_services_get_a_different_incident_scenario(monkeypatch):
    install_fake_controls(monkeypatch)
    monkeypatch.setattr(
        scheduler_module.audit_repository, "record_event", lambda **_: {},
    )
    scheduler = scheduler_module.LocalServiceIncidentScheduler()

    cycle_one = await scheduler.run_cycle()
    first_incidents = list_incidents(status="OPEN")
    cycle_two = await scheduler.run_cycle(manual=True)
    second_incidents = list_incidents(status="OPEN")
    restarted_scheduler = scheduler_module.LocalServiceIncidentScheduler()
    cycle_three = await restarted_scheduler.run_cycle(manual=True)
    third_incidents = list_incidents(status="OPEN")

    assert cycle_one["services"] == ["order-service", "payment-service"]
    assert cycle_two["services"] == ["user-service", "inventory-service"]
    assert cycle_three["services"] == ["payment-service", "user-service"]
    previous_types = {
        item["service"]: item["incident_type"] for item in second_incidents
    }
    assert all(
        item["incident_type"] != previous_types[item["service"]]
        for item in third_incidents
        if item["service"] in previous_types
    )
    assert {item["severity"] for item in third_incidents} == {"critical", "high"}
    assert all(item["severity"] in {"critical", "high"} for item in first_incidents)


@pytest.mark.asyncio
async def test_automatic_cycle_fires_every_interval_and_keeps_only_two_open(
    monkeypatch,
):
    install_fake_controls(monkeypatch)
    monkeypatch.setattr(scheduler_module, "BATCH_INTERVAL_SECONDS", 0.1)
    monkeypatch.setattr(
        scheduler_module.audit_repository, "record_event", lambda **_: {},
    )
    scheduler = scheduler_module.LocalServiceIncidentScheduler()
    loop_task = asyncio.create_task(scheduler._loop())

    async def wait_for_cycles(expected):
        async with asyncio.timeout(2):
            while scheduler.state["cycles"] < expected:
                await asyncio.sleep(0.005)

    try:
        await wait_for_cycles(1)
        assert len(list_incidents(status="OPEN")) == 2
        # Nothing is resolved by hand, yet the next pair still arrives on schedule.
        await wait_for_cycles(2)
        open_now = list_incidents(status="OPEN")
        assert len(open_now) == 2
        assert {item["source_evidence"]["simulation_cycle"] for item in open_now} == {2}
    finally:
        loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop_task


@pytest.mark.asyncio
async def test_partial_start_failure_clears_selected_controls_and_persists_nothing(
    monkeypatch,
):
    calls = install_fake_controls(monkeypatch, fail_start_for="payment-service")
    scheduler = scheduler_module.LocalServiceIncidentScheduler()

    with pytest.raises(RuntimeError, match="Could not start payment-service"):
        await scheduler.run_cycle()

    assert list_incidents(status="OPEN") == []
    assert {
        service for service, action in calls if action == "clear"
    } == {"order-service", "payment-service"}


@pytest.mark.asyncio
async def test_partial_persistence_is_compensated_after_database_failure(
    monkeypatch,
):
    install_fake_controls(monkeypatch)
    original_record = scheduler_module.record_incident
    count = 0

    def fail_on_second(incident):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("offline database failure")
        return original_record(incident)

    monkeypatch.setattr(scheduler_module, "record_incident", fail_on_second)
    scheduler = scheduler_module.LocalServiceIncidentScheduler()

    with pytest.raises(RuntimeError, match="offline database failure"):
        await scheduler.run_cycle()

    assert list_incidents(status="OPEN") == []
    assert scheduler._active_fingerprints == []
    assert scheduler._active_scenarios == []