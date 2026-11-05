import pytest

from app.services.verification import verify_recovery

HEALTHY = {"incidents": [], "deployments": [
    {"name": "demo-web", "desired_replicas": 2, "available_replicas": 2}]}
BROKEN = {"incidents": [{"resource": "demo-web-x-1", "type": "pod_failed"}],
          "deployments": [
              {"name": "demo-web", "desired_replicas": 2,
               "available_replicas": 1}]}


async def nosleep(_):
    return None


@pytest.mark.asyncio
async def test_recovers_after_consecutive_healthy():
    r = await verify_recovery(deployment="demo-web", observe=lambda: HEALTHY,
                              sleep=nosleep)
    assert r["recovered"] and r["attempts_used"] == 2


@pytest.mark.asyncio
async def test_targeted_recovery_only_ignores_unrelated_pod_incidents():
    observation = {
        "incidents": [
            {"resource": "sim-disk-fill-old", "type": "pod_failed"},
            {"resource": "sim-disk-fill-target", "type": "kubernetes_warning_event"},
        ],
        "deployments": [{
            "name": "sim-disk-fill",
            "desired_replicas": 1,
            "available_replicas": 1,
        }],
    }

    result = await verify_recovery(
        deployment="sim-disk-fill",
        target_resource="sim-disk-fill-target",
        target_incident_type="pod_failed",
        observe=lambda: observation,
        attempts=2,
        sleep=nosleep,
    )

    assert result["recovered"]


@pytest.mark.asyncio
async def test_targeted_recovery_fails_while_exact_incident_remains_active():
    observation = {
        "incidents": [{
            "resource": "sim-disk-fill-target",
            "type": "pod_failed",
        }],
        "deployments": [{
            "name": "sim-disk-fill",
            "desired_replicas": 1,
            "available_replicas": 1,
        }],
    }

    result = await verify_recovery(
        deployment="sim-disk-fill",
        target_resource="sim-disk-fill-target",
        target_incident_type="pod_failed",
        observe=lambda: observation,
        attempts=2,
        sleep=nosleep,
    )

    assert not result["recovered"]
    assert not result["checks"][1]["passed"]


@pytest.mark.parametrize(
    "summaries",
    [
        [
            {"name": "demo-web", "desired_replicas": 2, "available_replicas": 2},
            {"name": "demo-web", "desired_replicas": 2, "available_replicas": 2},
        ],
        [
            {"name": "demo-web", "desired_replicas": 2, "available_replicas": 2},
            {"name": "demo-web", "desired_replicas": 2, "available_replicas": 1},
        ],
        [
            {"name": "demo-web", "desired_replicas": 2, "available_replicas": 1},
            {"name": "demo-web", "desired_replicas": 2, "available_replicas": 2},
        ],
    ],
    ids=["both-healthy", "healthy-then-unhealthy", "unhealthy-then-healthy"],
)
@pytest.mark.asyncio
async def test_duplicate_deployment_summaries_cannot_verify_recovery(summaries):
    observation = {"incidents": [], "deployments": summaries}

    result = await verify_recovery(
        deployment="demo-web",
        observe=lambda: observation,
        attempts=2,
        sleep=nosleep,
    )

    assert not result["recovered"]
    assert result["checks"][-1] == {
        "name": "deployment_available",
        "passed": False,
        "detail": "multiple deployment summaries match the workload",
    }


@pytest.mark.parametrize("replica_field", [
    "desired_replicas",
    "available_replicas",
])
@pytest.mark.asyncio
async def test_boolean_replica_counts_cannot_verify_recovery(replica_field):
    observation = {
        "incidents": [],
        "deployments": [{
            "name": "demo-web",
            "desired_replicas": 2,
            "available_replicas": 2,
        }],
    }
    observation["deployments"][0][replica_field] = True

    result = await verify_recovery(
        deployment="demo-web",
        observe=lambda: observation,
        attempts=2,
        sleep=nosleep,
    )

    assert not result["recovered"]
    assert result["checks"][-1]["name"] == "deployment_available"
    assert not result["checks"][-1]["passed"]


@pytest.mark.asyncio
async def test_not_recovered_when_still_broken():
    r = await verify_recovery(deployment="demo-web", observe=lambda: BROKEN,
                              attempts=3, sleep=nosleep)
    assert not r["recovered"] and r["attempts_used"] == 3


@pytest.mark.asyncio
async def test_observation_failure_is_not_health():
    def boom():
        raise RuntimeError("kubectl down")
    r = await verify_recovery(deployment="demo-web", observe=boom,
                              attempts=2, sleep=nosleep)
    assert not r["recovered"]
    assert r["checks"][0]["name"] == "observation_available"


@pytest.mark.asyncio
async def test_transient_health_resets_streak():
    seq = iter([HEALTHY, BROKEN, HEALTHY, HEALTHY])
    r = await verify_recovery(deployment="demo-web",
                              observe=lambda: next(seq), attempts=4,
                              sleep=nosleep)
    assert r["recovered"] and r["attempts_used"] == 4


@pytest.mark.asyncio
async def test_missing_deployment_fails():
    r = await verify_recovery(deployment="demo-web",
                              observe=lambda: {"incidents": [],
                                               "deployments": []},
                              attempts=2, sleep=nosleep)
    assert not r["recovered"]


@pytest.mark.asyncio
async def test_missing_incident_evidence_cannot_confirm_recovery():
    incomplete = {
        "deployments": [
            {"name": "demo-web", "desired_replicas": 2, "available_replicas": 2}
        ],
    }

    result = await verify_recovery(
        deployment="demo-web",
        observe=lambda: incomplete,
        attempts=2,
        sleep=nosleep,
    )

    assert not result["recovered"]
    assert result["checks"][0] == {
        "name": "incident_evidence_available",
        "passed": False,
        "detail": "incident evidence is unavailable or malformed",
    }


@pytest.mark.parametrize(
    "incidents",
    [
        [None],
        [{}],
        [{"resource": "", "type": "pod_failed"}],
        [{"resource": "demo-web-pod-1"}],
    ],
    ids=["non-object", "empty-object", "empty-resource", "missing-type"],
)
@pytest.mark.asyncio
async def test_malformed_incident_evidence_cannot_confirm_recovery(incidents):
    observation = {
        "incidents": incidents,
        "deployments": [{
            "name": "demo-web",
            "desired_replicas": 2,
            "available_replicas": 2,
        }],
    }

    result = await verify_recovery(
        deployment="demo-web",
        observe=lambda: observation,
        attempts=2,
        sleep=nosleep,
    )

    assert not result["recovered"]
    assert result["checks"][0] == {
        "name": "incident_evidence_available",
        "passed": False,
        "detail": "incident evidence is unavailable or malformed",
    }


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"attempts": 0}, "attempts must be a positive integer"),
        ({"attempts": True}, "attempts must be a positive integer"),
        (
            {"consecutive_required": 0},
            "consecutive_required must be between 1 and attempts",
        ),
        (
            {"attempts": 1, "consecutive_required": 2},
            "consecutive_required must be between 1 and attempts",
        ),
        (
            {"interval_seconds": -1},
            "interval_seconds must be finite and non-negative",
        ),
        (
            {"interval_seconds": float("nan")},
            "interval_seconds must be finite and non-negative",
        ),
    ],
)
@pytest.mark.asyncio
async def test_invalid_verification_parameters_fail_closed(kwargs, message):
    with pytest.raises(ValueError, match=message):
        await verify_recovery(
            deployment="demo-web",
            observe=lambda: HEALTHY,
            sleep=nosleep,
            **kwargs,
        )
