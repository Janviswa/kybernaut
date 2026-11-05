import subprocess
from pathlib import Path

import pytest
import yaml

from app.services import failure_scenarios as fs


def cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class FakeRunner:
    def __init__(self, *, context="kind-kybernaut", command_result=None):
        self.context = context
        self.command_result = command_result or cp()
        self.calls = []

    def __call__(self, args, *, timeout=120):
        self.calls.append(args)
        if args[:2] == ["--context", fs.CONTEXT_NAME]:
            args = args[2:]
        if args[:2] == ["config", "current-context"]:
            return cp(stdout=self.context)
        return self.command_result


def test_all_five_benchmark_types_have_scenarios():
    assert {scenario.incident_type for scenario in fs.SCENARIOS.values()} == {
        "cpu_spike",
        "memory_leak",
        "pod_crash",
        "disk_full",
        "deployment_failure",
    }


def test_every_scenario_command_is_fixed_to_the_lab_namespace():
    for scenario in fs.SCENARIOS.values():
        for command in (scenario.prepare or []) + scenario.inject + scenario.cleanup:
            assert "-n" in command
            assert command[command.index("-n") + 1] == fs.NAMESPACE


def test_dry_run_plans_pin_kind_context_and_lab_namespace():
    for name in fs.SCENARIOS:
        for phase in ("inject", "cleanup"):
            result = fs.run_scenario(name, phase)
            assert result["dry_run"] is True
            for command in result["commands"]:
                assert command.count("--context") == 1
                assert command[command.index("--context") + 1] == (
                    fs.CONTEXT_NAME
                )
                assert command[command.index("-n") + 1] == fs.NAMESPACE


def test_pod_crash_uses_dedicated_zero_replica_simulator():
    scenario = fs.SCENARIOS["pod-crash"]
    assert len(scenario.prepare) == 1
    patch = scenario.prepare[0]
    patch_payload = patch[patch.index("-p") + 1]
    assert patch[:2] == ["patch", "deployment/sim-pod-crash"]
    assert '"command":["sh","-c","sleep 2; exit 1"]' in patch_payload
    assert scenario.inject == [
        ["scale", "deployment/sim-pod-crash", "--replicas=1",
         "-n", fs.NAMESPACE]
    ]
    assert scenario.cleanup == [
        ["scale", "deployment/sim-pod-crash", "--replicas=0",
         "-n", fs.NAMESPACE]
    ]
    assert "demo-web" not in scenario.description


def test_live_scenario_requires_the_configured_kind_context():
    runner = FakeRunner(context="other-context")
    with pytest.raises(fs.SimulatorError, match="kind-kybernaut"):
        fs.run_scenario(
            "pod-crash", "inject", dry_run=False, runner=runner
        )
    assert len(runner.calls) == 1


def test_live_scenario_commands_stay_in_the_configured_context_and_namespace():
    runner = FakeRunner()
    fs.run_scenario("pod-crash", "inject", dry_run=False, runner=runner)

    assert all(
        call[:2] == ["--context", fs.CONTEXT_NAME]
        for call in runner.calls
    )
    injection = runner.calls[-1]
    assert injection[injection.index("-n") + 1] == fs.NAMESPACE
    assert "deployment/sim-pod-crash" in injection
    assert any(
        call[2:4] == ["patch", "deployment/sim-pod-crash"]
        for call in runner.calls
    )


def test_kubectl_failure_is_reported():
    runner = FakeRunner(command_result=cp(1, stderr="deployment missing"))
    with pytest.raises(fs.SimulatorError, match="deployment missing"):
        fs.run_scenario(
            "pod-crash", "inject", dry_run=False, runner=runner
        )


def test_other_scenarios_do_not_count_as_pod_crash_detection():
    assert not fs.scenario_detected("pod-crash", [{
        "type": "pod_container_failure",
        "resource": "demo-web-abc",
    }])


@pytest.mark.parametrize(
    ("name", "incident"),
    [
        ("pod-crash", {
            "type": "pod_container_failure",
            "resource": "sim-pod-crash-abc",
        }),
        ("memory-leak", {
            "type": "pod_container_failure",
            "resource": "sim-memory-leak-abc",
        }),
        ("disk-full", {
            "type": "pod_failed",
            "resource": "sim-disk-fill-abc",
        }),
        ("deployment-failure", {
            "type": "deployment_unavailable_replicas",
            "resource": "demo-web",
        }),
    ],
)
def test_scenario_detection_requires_expected_type_and_resource(name, incident):
    assert fs.scenario_detected(name, [incident])
    assert not fs.scenario_detected(name, [{
        **incident,
        "type": "unrelated",
    }])


def test_simulator_manifests_start_stopped_and_have_resource_limits():
    root = Path(__file__).resolve().parents[1] / "k8s" / "lab"
    docs = list(yaml.safe_load_all((root / "sim-workloads.yaml").read_text()))

    assert len(docs) == 4
    for deployment in docs:
        assert deployment["metadata"]["namespace"] == fs.NAMESPACE
        assert deployment["spec"]["replicas"] == 0
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        assert {"cpu", "memory"} <= set(container["resources"]["limits"])

    crash = next(
        deployment for deployment in docs
        if deployment["metadata"]["name"] == "sim-pod-crash"
    )
    assert crash["spec"]["template"]["spec"]["containers"][0]["command"] == [
        "sh", "-c", "sleep 2; exit 1",
    ]
