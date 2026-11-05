import pytest
from pydantic import ValidationError

from app.services.policy_engine import (
    PolicyConfig, ProposedAction, evaluate_policy,
)

CTX = {"deployment": {"desired_replicas": 2, "available_replicas": 2},
       "recent_action_count": 0}


def restart(**kw):
    base = dict(action="restart_pod", namespace="kybernaut-lab",
                resource="demo-web-abc-123", deployment="demo-web",
                confidence=0.95, severity="low", rationale="test")
    base.update(kw)
    return ProposedAction(**base)


def test_schema_rejects_extra_fields_and_bad_names():
    with pytest.raises(ValidationError):
        restart(command="rm -rf /")
    with pytest.raises(ValidationError):
        restart(resource="Bad Name; ls")


def test_scale_requires_target_and_restart_forbids_it():
    with pytest.raises(ValidationError):
        ProposedAction(action="scale_deployment", namespace="kybernaut-lab",
                       resource="demo-web", deployment="demo-web",
                       confidence=0.9, severity="low", rationale="x")
    with pytest.raises(ValidationError):
        restart(target_replicas=3)


def test_default_config_always_requires_approval():
    d = evaluate_policy(restart(), CTX, PolicyConfig())
    assert d.allowed and d.requires_approval and not d.auto_execute


def test_auto_only_when_enabled_low_risk_high_confidence():
    cfg = PolicyConfig(auto_remediation_enabled=True)
    assert evaluate_policy(restart(), CTX, cfg).auto_execute
    assert not evaluate_policy(restart(confidence=0.8), CTX, cfg).auto_execute


def test_high_severity_never_bypasses_approval():
    cfg = PolicyConfig(auto_remediation_enabled=True)
    for sev in ("high", "critical"):
        d = evaluate_policy(restart(severity=sev), CTX, cfg)
        assert d.requires_approval and not d.auto_execute


def test_rollback_never_auto():
    cfg = PolicyConfig(auto_remediation_enabled=True)
    p = ProposedAction(action="rollback_deployment", namespace="kybernaut-lab",
                       resource="demo-web", deployment="demo-web",
                       confidence=0.99, severity="low", rationale="x")
    d = evaluate_policy(p, CTX, cfg)
    assert d.allowed and d.requires_approval and not d.auto_execute


def test_namespace_and_deployment_allowlists():
    d = evaluate_policy(restart(namespace="production"), CTX, PolicyConfig())
    assert not d.allowed
    d = evaluate_policy(
        restart(resource="payments-x-1", deployment="payments"),
        CTX, PolicyConfig())
    assert not d.allowed


def test_pod_crash_simulator_is_allowlisted_but_still_requires_approval():
    decision = evaluate_policy(
        restart(
            resource="sim-pod-crash-abc-123",
            deployment="sim-pod-crash",
        ),
        CTX,
        PolicyConfig(),
    )

    assert decision.allowed
    assert decision.requires_approval
    assert not decision.auto_execute


def test_low_confidence_denied():
    assert not evaluate_policy(restart(confidence=0.3), CTX,
                               PolicyConfig()).allowed


def test_rate_limit_and_missing_count_fail_closed():
    assert not evaluate_policy(
        restart(), {**CTX, "recent_action_count": 5}, PolicyConfig()).allowed
    assert evaluate_policy(
        restart(), {**CTX, "recent_action_count": 4}, PolicyConfig()).allowed
    assert not evaluate_policy(
        restart(), {"deployment": CTX["deployment"]}, PolicyConfig()).allowed


def test_five_action_capacity_is_scoped_to_the_lab_namespace():
    config = PolicyConfig(max_actions_per_window=3, window_seconds=1800)

    assert config.action_capacity_for_namespace("kybernaut-lab") == (5, 3600)
    assert config.action_capacity_for_namespace("production") == (3, 1800)


def test_blast_radius_single_replica_and_missing_state_denied():
    single = {"deployment": {"desired_replicas": 1, "available_replicas": 1},
              "recent_action_count": 0}
    assert not evaluate_policy(restart(), single, PolicyConfig()).allowed
    assert not evaluate_policy(
        restart(), {"recent_action_count": 0}, PolicyConfig()).allowed


def test_scale_bounds_and_step():
    def scale(n):
        return ProposedAction(
            action="scale_deployment", namespace="kybernaut-lab",
            resource="demo-web", deployment="demo-web", target_replicas=n,
            confidence=0.9, severity="low", rationale="x")
    assert evaluate_policy(scale(3), CTX, PolicyConfig()).allowed
    assert not evaluate_policy(scale(9), CTX, PolicyConfig()).allowed
    assert not evaluate_policy(scale(0), CTX, PolicyConfig()).allowed
