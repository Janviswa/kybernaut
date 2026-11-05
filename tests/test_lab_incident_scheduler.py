import asyncio

import pytest

import app.services.lab_incident_scheduler as scheduler_module


def _observation(incidents=None):
    incidents = incidents if incidents is not None else [{
        "type": "pod_container_failure",
        "severity": "high",
        "resource": "sim-pod-crash-test-1",
        "resource_kind": "Pod",
        "evidence": ["Container waiting: CrashLoopBackOff"],
    }, {
        "type": "pod_container_failure",
        "severity": "high",
        "resource": "sim-memory-leak-test-1",
        "resource_kind": "Pod",
        "evidence": ["Container terminated: OOMKilled"],
    }]
    return {
        "namespace": "kybernaut-lab",
        "observed_at": "2026-10-27T12:00:00+00:00",
        "incidents": incidents,
        "incident_count": len(incidents),
        "deployments": [{
            "name": "demo-web",
            "desired_replicas": 2,
            "available_replicas": 1 if incidents else 2,
            "ready_replicas": 1 if incidents else 2,
        }],
    }


@pytest.mark.asyncio
async def test_cycle_rotates_cleans_up_and_counts_only_persisted_incidents(
    monkeypatch,
):
    calls = []
    persisted = []

    def run_scenario(name, action, **_kwargs):
        calls.append((name, action))

    def persist(observation):
        persisted.append(observation)
        return {"persisted_count": len(observation["incidents"])}

    monkeypatch.setattr(scheduler_module, "run_scenario", run_scenario)
    monkeypatch.setattr(scheduler_module, "observe_namespace", _observation)
    monkeypatch.setattr(scheduler_module, "persist_observation", persist)
    monkeypatch.setattr(
        scheduler_module.asyncio, "sleep", lambda _seconds: _no_sleep()
    )
    monkeypatch.setattr(
        scheduler_module,
        "SCENARIO_VERIFICATION_TIMEOUT_SECONDS",
        scheduler_module.SCENARIO_VERIFICATION_INTERVAL_SECONDS,
    )
    scheduler = scheduler_module.LabIncidentScheduler()

    first = await scheduler.run_cycle()
    second = await scheduler.run_cycle()

    assert first["scenario"] == "pod-crash"
    assert second["scenario"] == "memory-leak"
    assert calls == [
        ("pod-crash", "cleanup"),
        ("pod-crash", "inject"),
        ("pod-crash", "cleanup"),
        ("memory-leak", "cleanup"),
        ("memory-leak", "inject"),
    ]
    assert len(persisted) == 2
    assert scheduler.state["cycles"] == 2
    assert scheduler.state["last_error"] is None
    assert scheduler.state["last_attempt_at"]
    assert scheduler.state["last_run_at"] == second["at"]


@pytest.mark.asyncio
async def test_undetected_failure_does_not_persist_or_count_as_success(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        scheduler_module,
        "run_scenario",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        scheduler_module, "observe_namespace", lambda: _observation([])
    )
    monkeypatch.setattr(
        scheduler_module,
        "persist_observation",
        lambda *_args: calls.append("persist"),
    )
    monkeypatch.setattr(
        scheduler_module,
        "SCENARIO_VERIFICATION_TIMEOUT_SECONDS",
        0,
    )
    scheduler = scheduler_module.LabIncidentScheduler()

    with pytest.raises(RuntimeError, match="was not detected"):
        await scheduler.run_cycle()

    assert calls == []
    assert scheduler.state["cycles"] == 0
    assert scheduler.state["last_run_at"] is None
    assert scheduler.state["last_error"] == (
        "scenario_verification: Lab scenario pod-crash was not detected; "
        "persistence was skipped"
    )


@pytest.mark.asyncio
async def test_unrelated_observation_does_not_count_as_scenario_success(
    monkeypatch,
):
    persisted = []
    monkeypatch.setattr(
        scheduler_module,
        "run_scenario",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        scheduler_module,
        "observe_namespace",
        lambda: _observation([{
            "type": "pod_container_failure",
            "severity": "high",
            "resource": "demo-web-unrelated",
            "resource_kind": "Pod",
            "evidence": ["unrelated failure"],
        }]),
    )
    monkeypatch.setattr(
        scheduler_module,
        "persist_observation",
        lambda observation: persisted.append(observation),
    )
    monkeypatch.setattr(
        scheduler_module,
        "SCENARIO_VERIFICATION_TIMEOUT_SECONDS",
        0,
    )
    scheduler = scheduler_module.LabIncidentScheduler()

    with pytest.raises(RuntimeError, match="pod-crash was not detected"):
        await scheduler.run_cycle()

    assert persisted == []


@pytest.mark.asyncio
async def test_cycle_retries_observation_until_scenario_evidence_appears(
    monkeypatch,
):
    observations = iter((_observation([]), _observation()))
    persisted = []
    calls = []
    observed_cycle_states = []
    scheduler = None

    def run_scenario(name, action, **_kwargs):
        calls.append((name, action))

    def observe():
        observed_cycle_states.append(scheduler.state["cycle_in_progress"])
        return next(observations)

    monkeypatch.setattr(scheduler_module, "run_scenario", run_scenario)
    monkeypatch.setattr(
        scheduler_module,
        "observe_namespace",
        observe,
    )
    monkeypatch.setattr(
        scheduler_module,
        "persist_observation",
        lambda observation: (
            persisted.append(observation),
            {"persisted_count": len(observation["incidents"])},
        )[1],
    )

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        scheduler_module, "SCENARIO_VERIFICATION_TIMEOUT_SECONDS", 5
    )
    scheduler = scheduler_module.LabIncidentScheduler()

    result = await scheduler.run_cycle()

    assert result["scenario"] == "pod-crash"
    assert len(persisted) == 1
    assert calls == [
        ("pod-crash", "cleanup"),
        ("pod-crash", "inject"),
    ]
    assert observed_cycle_states == [True, True]
    assert scheduler.state["cycle_in_progress"] is False


@pytest.mark.asyncio
async def test_persistence_failure_is_reported_without_incrementing_cycles(
    monkeypatch,
):
    monkeypatch.setattr(
        scheduler_module,
        "run_scenario",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        scheduler_module, "observe_namespace", _observation
    )
    monkeypatch.setattr(
        scheduler_module,
        "persist_observation",
        lambda *_args: (_ for _ in ()).throw(OSError("offline database")),
    )
    scheduler = scheduler_module.LabIncidentScheduler()

    with pytest.raises(OSError):
        await scheduler.run_cycle()

    assert scheduler.state["cycles"] == 0
    assert scheduler.state["last_run_at"] is None
    assert scheduler.state["last_error"] == "persistence: OSError"


@pytest.mark.asyncio
async def test_scheduler_uses_ten_minute_start_to_start_cadence(monkeypatch):
    monkeypatch.setattr(scheduler_module, "interval_seconds", lambda: 600)
    sleeps = []
    runs = 0

    async def fake_sleep(delay):
        sleeps.append(delay)

    async def fake_cycle():
        nonlocal runs
        runs += 1
        if runs == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(scheduler_module.asyncio, "sleep", fake_sleep)
    scheduler = scheduler_module.LabIncidentScheduler()
    monkeypatch.setattr(scheduler, "run_cycle", fake_cycle)

    with pytest.raises(asyncio.CancelledError):
        await scheduler._loop()

    assert runs == 2
    assert sleeps == pytest.approx([600], abs=0.1)


def test_memory_scenario_has_time_to_reach_its_96_mib_limit():
    memory_settle = next(
        settle for scenario, _, settle in scheduler_module.ROTATION
        if scenario == "memory-leak"
    )

    assert memory_settle > 96
    assert memory_settle < 180


async def _no_sleep():
    return None
