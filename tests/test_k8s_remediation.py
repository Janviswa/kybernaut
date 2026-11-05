import pytest

import app.services.k8s_remediation as remediation
from app.services.k8s_remediation import (
    CommandResult, build_command, execute_action,
)
from app.services.policy_engine import PolicyConfig, ProposedAction


def prop(action="restart_pod", **kw):
    base = dict(action=action, namespace="kybernaut-lab",
                resource="demo-web-abc-1", deployment="demo-web",
                confidence=0.9, severity="low", rationale="t")
    if action != "restart_pod":
        base["resource"] = "demo-web"
    if action == "scale_deployment":
        base["target_replicas"] = 3
    base.update(kw)
    return ProposedAction(**base)


class Recorder:
    def __init__(self, context="kind-kybernaut", rc=0):
        self.calls, self.context, self.rc = [], context, rc

    def __call__(self, args):
        self.calls.append(args)
        if args[:2] == ["config", "current-context"]:
            return CommandResult(0, self.context + "\n")
        return CommandResult(self.rc, "ok", "" if self.rc == 0 else "boom")


def test_commands_are_fixed_vectors():
    assert build_command(prop()) == [
        "delete", "pod", "demo-web-abc-1", "-n", "kybernaut-lab", "--wait=false"]
    assert build_command(prop("scale_deployment"))[:3] == [
        "scale", "deployment/demo-web", "--replicas=3"]
    assert build_command(prop("rollback_deployment"))[:3] == [
        "rollout", "undo", "deployment/demo-web"]


def test_default_mode_is_dry_run_and_executes_nothing(monkeypatch):
    monkeypatch.delenv("KYBERNAUT_REMEDIATION_MODE", raising=False)
    runner = Recorder()
    r = execute_action(prop(), runner=runner)
    assert r["mode"] == "dry_run" and not r["executed"] and r["ok"]
    assert runner.calls == []


def test_live_runs_only_on_kind_context(monkeypatch):
    runner = Recorder()
    r = execute_action(prop(), mode="live", runner=runner)
    assert r["executed"] and r["ok"]
    assert runner.calls[-1][0] == "delete"


def test_live_refuses_non_kind_context():
    runner = Recorder(context="prod-cluster")
    r = execute_action(prop(), mode="live", runner=runner)
    assert not r["executed"] and not r["ok"]
    assert all(c[0] == "config" for c in runner.calls)


def test_live_refuses_other_kind_context():
    runner = Recorder(context="kind-another-cluster")
    r = execute_action(prop(), mode="live", runner=runner)
    assert not r["executed"] and not r["ok"]
    assert all(c[0] == "config" for c in runner.calls)


def test_live_failure_reported():
    r = execute_action(prop(), mode="live", runner=Recorder(rc=1))
    assert r["executed"] and not r["ok"]


def test_command_runner_exception_is_an_ambiguous_attempt():
    calls = []

    def runner(args):
        calls.append(args)
        if args == ["config", "current-context"]:
            return CommandResult(0, "kind-kybernaut\n")
        raise TimeoutError("process outcome unavailable")

    result = execute_action(prop(), mode="live", runner=runner)

    assert result["executed"] is True
    assert result["ok"] is False
    assert result["outcome_unknown"] is True
    assert "outcome unknown" in result["error"].lower()
    assert len(calls) == 2


def test_context_lookup_exception_is_confirmed_pre_start_failure():
    calls = []

    def runner(args):
        calls.append(args)
        raise OSError("context lookup failed")

    result = execute_action(prop(), mode="live", runner=runner)

    assert result["executed"] is False
    assert result["ok"] is False
    assert result.get("outcome_unknown") is not True
    assert len(calls) == 1
    assert calls == [["config", "current-context"]]


def test_missing_kubectl_is_a_confirmed_command_start_failure(monkeypatch):
    calls = []

    def subprocess_run(args, **_kwargs):
        calls.append(args)
        if args == ["kubectl", "config", "current-context"]:
            return remediation.subprocess.CompletedProcess(
                args, 0, stdout="kind-kybernaut\n", stderr=""
            )
        raise FileNotFoundError("kubectl not installed")

    monkeypatch.setattr(remediation.subprocess, "run", subprocess_run)
    result = execute_action(prop(), mode="live")

    assert result["executed"] is False
    assert result.get("outcome_unknown") is not True
    assert "not started" in result["error"]
    assert len(calls) == 2


def test_out_of_scope_rejected_even_in_live():
    runner = Recorder()
    cfg = PolicyConfig(allowed_namespaces=frozenset({"other"}))
    r = execute_action(prop(), mode="live", runner=runner, config=cfg)
    assert not r["ok"] and runner.calls == []


def test_requires_validated_proposal():
    with pytest.raises(TypeError):
        execute_action({"action": "restart_pod"})
