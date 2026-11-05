"""Real-remediation scenarios with a mocked kubectl runner (never a live cluster).

Covers: strict dry_run/live mode, context pinning, ownership/identity
preflight, spec-correcting actions with read-back confirmation, patch
success/failure, runbook + policy prerequisites, rejection details, and the
three synthetic scenarios (bad image, crash loop, disk eviction).
"""

import json
from pathlib import Path

import pytest
import yaml

import app.services.k8s_remediation as rem
from app.services import lab_known_good as good
from app.services import remediation_catalog as catalog
from app.services.k8s_remediation import CommandResult, build_command, execute_action
from app.services.policy_engine import PolicyConfig, ProposedAction, evaluate_policy
from app.services.runbook_retriever import resolve_runbook

NS = "kybernaut-lab"
CTX_OK = {"config current-context": "kind-kybernaut\n"}


def deployment_json(name, image, command, labelled=True, ns=NS, container="app"):
    labels = {"app": name}
    if labelled:
        labels[good.OWNERSHIP_LABEL] = "true"
    return {"metadata": {"name": name, "namespace": ns, "labels": labels},
            "spec": {"template": {"spec": {"containers": [
                {"name": container, "image": image, "command": command}]}}}}


class FakeCluster:
    """Tiny kubectl double. Records every call; applies set image / patch."""

    def __init__(self, dep, context="kind-kybernaut", apply_changes=True,
                 patch_rc=0, raise_on_mutation=False):
        self.dep, self.context = dep, context
        self.apply_changes, self.patch_rc = apply_changes, patch_rc
        self.raise_on_mutation = raise_on_mutation
        self.calls = []

    @property
    def mutations(self):
        return [c for c in self.calls if c[0] in {"set", "patch", "delete", "scale", "rollout"}]

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["config", "current-context"]:
            return CommandResult(0, self.context + "\n")
        if args[0] == "get":
            return CommandResult(0, json.dumps(self.dep))
        if self.raise_on_mutation:
            raise TimeoutError("kubectl outcome unavailable")
        if self.patch_rc != 0:
            return CommandResult(self.patch_rc, "", "forbidden")
        container = self.dep["spec"]["template"]["spec"]["containers"][0]
        if self.apply_changes and args[0] == "set":
            container["image"] = args[3].split("=", 1)[1]
        if self.apply_changes and args[0] == "patch":
            patch = json.loads(args[args.index("-p") + 1])
            container["command"] = patch["spec"]["template"]["spec"]["containers"][0]["command"]
            if "replicas" in patch["spec"]:
                self.dep["spec"]["replicas"] = patch["spec"]["replicas"]
        return CommandResult(0, "deployment.apps patched")


class DiskFillFakeCluster(FakeCluster):
    def __init__(self, dep, pod=None, **kw):
        super().__init__(dep, **kw)
        self.pod = pod or {
            "metadata": {
                "name": "sim-disk-fill-5dc94cc998-abcde",
                "namespace": NS,
                "uid": "evicted-pod-uid",
                "ownerReferences": [{
                    "controller": True,
                    "kind": "ReplicaSet",
                    "name": "sim-disk-fill-5dc94cc998",
                }],
            },
            "status": {"phase": "Failed", "reason": "Evicted"},
        }

    def __call__(self, args):
        if args[:2] == ["get", "pod"]:
            self.calls.append(list(args))
            return CommandResult(0, json.dumps(self.pod))
        if args[:2] == ["delete", "pod"]:
            self.calls.append(list(args))
            expected_uid = next(
                (arg.removeprefix("--preconditions=uid=")
                 for arg in args if arg.startswith("--preconditions=uid=")),
                None,
            )
            if self.pod["metadata"]["uid"] != expected_uid:
                return CommandResult(1, "", "UID precondition failed")
            self.pod = None
            return CommandResult(0, "pod deleted")
        return super().__call__(args)


def bad_image_dep(**kw):
    return deployment_json("sim-bad-image", "busybox:kybernaut-missing-tag",
                           ["sh", "-c", "while :; do sleep 30; done"], **kw)


def crash_dep(**kw):
    return deployment_json("sim-crash-loop", "busybox:1.36",
                           ["sh", "-c", "echo x >&2; exit 1"], **kw)


def disk_fill_dep():
    spec = good.get("sim-disk-fill")
    dep = deployment_json(
        "sim-disk-fill", spec.expected_image, list(spec.expected_command),
        container=spec.container,
    )
    dep["spec"]["replicas"] = 0
    dep["spec"]["template"]["spec"]["volumes"] = [
        {"name": "scratch", "emptyDir": {"sizeLimit": "40Mi"}}
    ]
    return dep


def memory_leak_dep(command=None, image="busybox:1.36"):
    spec = good.get("sim-memory-leak")
    dep = deployment_json(
        "sim-memory-leak", image,
        list(command if command is not None else spec.expected_command),
        container=spec.container,
    )
    dep["spec"]["replicas"] = 4
    return dep


def healthy_disk_fill_dep():
    dep = disk_fill_dep()
    spec = good.get("sim-disk-fill")
    dep["spec"]["replicas"] = 1
    dep["spec"]["template"]["spec"]["containers"][0]["command"] = list(
        spec.command
    )
    dep["status"] = {"readyReplicas": 1, "availableReplicas": 1}
    return dep


def proposal(action, deployment, resource=None, **kw):
    base = dict(action=action, namespace=NS, resource=resource or deployment,
                deployment=deployment, confidence=0.9, severity="high", rationale="t")
    base.update(kw)
    if action in {"restore_command", "cleanup_evicted_pod"} and deployment == "sim-disk-fill":
        base.setdefault("target_pod", "sim-disk-fill-5dc94cc998-abcde")
        base.setdefault("target_pod_uid", "evicted-pod-uid")
    if action == "cleanup_evicted_pod":
        base["resource"] = base["target_pod"]
    return ProposedAction(**base)


# ---------------- mode selection / environment ----------------
def test_unset_mode_is_dry_run(monkeypatch):
    monkeypatch.delenv("KYBERNAUT_REMEDIATION_MODE", raising=False)
    assert rem.get_mode() == "dry_run" and rem.mode_status()["valid"] is True


def test_live_mode_is_read_from_the_environment_each_call(monkeypatch):
    monkeypatch.setenv("KYBERNAUT_REMEDIATION_MODE", " LIVE ")
    assert rem.get_mode() == "live"
    monkeypatch.setenv("KYBERNAUT_REMEDIATION_MODE", "dry_run")
    assert rem.get_mode() == "dry_run"  # no caching: a changed value is honoured


@pytest.mark.parametrize("raw", ["true", "1", "livee", "execute"])
def test_invalid_mode_blocks_instead_of_silently_dry_running(monkeypatch, raw):
    monkeypatch.setenv("KYBERNAUT_REMEDIATION_MODE", raw)
    cluster = FakeCluster(bad_image_dep())
    result = execute_action(proposal("set_image", "sim-bad-image"), runner=cluster)
    assert result["blocked"] is True and result["blocked_reason"] == "invalid_mode"
    assert result["executed"] is False and result["ok"] is False
    assert cluster.calls == []
    assert rem.mode_status()["valid"] is False


def test_dry_run_records_the_real_command_and_changes_nothing():
    cluster = FakeCluster(bad_image_dep())
    result = execute_action(proposal("set_image", "sim-bad-image"), mode="dry_run", runner=cluster)
    assert result["mode"] == "dry_run" and result["executed"] is False and result["ok"] is True
    assert result["command"][:3] == ["kubectl", "set", "image"]
    assert cluster.calls == []


# ---------------- context, ownership, identity ----------------
def test_invalid_context_is_blocked_before_any_mutation():
    cluster = FakeCluster(bad_image_dep(), context="prod-cluster")
    result = execute_action(proposal("set_image", "sim-bad-image"), mode="live", runner=cluster)
    assert result["blocked_reason"] == "invalid_context" and not result["executed"]
    assert cluster.mutations == []


def test_every_live_mutation_is_pinned_to_the_allowed_context():
    cluster = FakeCluster(bad_image_dep())
    execute_action(proposal("set_image", "sim-bad-image"), mode="live", runner=cluster)
    assert cluster.mutations and all(c[-2:] == ["--context", "kind-kybernaut"] for c in cluster.mutations)


def test_unlabelled_deployment_is_not_mutated():
    cluster = FakeCluster(bad_image_dep(labelled=False))
    result = execute_action(proposal("set_image", "sim-bad-image"), mode="live", runner=cluster)
    assert result["blocked_reason"] == "not_a_lab_scenario_resource"
    assert cluster.mutations == []


def test_identity_mismatch_is_not_mutated():
    dep = bad_image_dep()
    dep["metadata"]["namespace"] = "other"
    cluster = FakeCluster(dep)
    result = execute_action(proposal("set_image", "sim-bad-image"), mode="live", runner=cluster)
    assert result["blocked_reason"] == "resource_identity_mismatch" and cluster.mutations == []


def test_deployment_without_registered_fix_is_refused_by_scope_and_policy():
    with pytest.raises(ValueError):
        build_command(proposal("set_image", "demo-web"))
    decision = evaluate_policy(
        proposal("set_image", "demo-web"),
        {"recent_action_count": 0, "deployment": {"desired_replicas": 1, "available_replicas": 0}})
    assert not decision.allowed
    assert any("known_good_spec_registered" in r for r in decision.reasons)


def test_proposals_cannot_carry_an_image_or_command():
    with pytest.raises(ValueError):
        ProposedAction(action="set_image", namespace=NS, resource="sim-bad-image",
                       deployment="sim-bad-image", confidence=0.9, severity="high",
                       rationale="t", image="evil:latest")


# ---------------- Scenario A: bad image ----------------
def test_scenario_a_set_image_succeeds_and_is_confirmed_by_readback():
    cluster = FakeCluster(bad_image_dep())
    result = execute_action(proposal("set_image", "sim-bad-image"), mode="live", runner=cluster)
    assert result["ok"] and result["executed"] and result["spec_confirmed"] is True
    assert result["command"][:5] == ["kubectl", "set", "image", "deployment/sim-bad-image",
                                     f"app={good.get('sim-bad-image').image}"]
    assert cluster.dep["spec"]["template"]["spec"]["containers"][0]["image"] == good.get("sim-bad-image").image


def test_patch_that_returns_success_but_changes_nothing_is_not_ok():
    cluster = FakeCluster(bad_image_dep(), apply_changes=False)
    result = execute_action(proposal("set_image", "sim-bad-image"), mode="live", runner=cluster)
    assert result["executed"] is True and result["ok"] is False
    assert "not confirmed" in result["error"]


def test_failed_kubernetes_patch_is_reported_with_the_real_error():
    cluster = FakeCluster(bad_image_dep(), patch_rc=1)
    result = execute_action(proposal("set_image", "sim-bad-image"), mode="live", runner=cluster)
    assert result["executed"] is True and result["ok"] is False and "forbidden" in result["stderr"]


def test_already_good_spec_is_not_re_applied():
    dep = bad_image_dep()
    dep["spec"]["template"]["spec"]["containers"][0]["image"] = good.get("sim-bad-image").image
    cluster = FakeCluster(dep)
    result = execute_action(proposal("set_image", "sim-bad-image"), mode="live", runner=cluster)
    assert result["blocked_reason"] == "already_in_desired_state" and cluster.mutations == []


def test_uncertain_outcome_is_preserved_for_reconciliation():
    cluster = FakeCluster(bad_image_dep(), raise_on_mutation=True)
    result = execute_action(proposal("set_image", "sim-bad-image"), mode="live", runner=cluster)
    assert result["outcome_unknown"] is True and result["executed"] is True and not result["ok"]


# ---------------- Scenario B: crash loop ----------------
def test_scenario_b_restore_command_patches_only_the_registered_command():
    cluster = FakeCluster(crash_dep())
    result = execute_action(proposal("restore_command", "sim-crash-loop"), mode="live", runner=cluster)
    assert result["ok"] and result["spec_confirmed"] is True
    patch = json.loads(result["command"][result["command"].index("-p") + 1])
    assert patch == {"spec": {"template": {"spec": {"containers": [
        {"name": "app", "command": list(good.get("sim-crash-loop").command)}]}}}}
    assert all(c[0] != "delete" for c in cluster.calls)  # a crash fix is never a blind restart


def test_crash_candidates_prefer_restore_command_over_restart():
    pod = {"resource": "sim-crash-loop-abc", "resource_kind": "Pod", "deployment": "sim-crash-loop"}
    actions = [c["action"] for c in catalog.candidate_actions("pod_crash", pod, {"desired_replicas": 1})]
    assert actions[0] == "restore_command" and "restart_pod" in actions


def test_memory_leak_candidate_prefers_registered_command_restore():
    target = {
        "resource": "sim-memory-leak",
        "resource_kind": "Deployment",
        "deployment": "sim-memory-leak",
    }
    state = {"desired_replicas": 4, "available_replicas": 1}
    actions = catalog.candidate_actions("memory_leak", target, state)
    assert actions[0] == {
        "action": "restore_command",
        "resource": "sim-memory-leak",
    }
    result = _resolve(
        "memory_leak", OOM_EVIDENCE, "restore_command", state
    )
    assert result["status"] == "applicable", result
    options = catalog.build_options(
        "memory_leak", target, state,
        runbook_check=lambda raw: _resolve(
            "memory_leak", OOM_EVIDENCE, raw["action"], state
        ),
    )
    restore = next(item for item in options if item.get("action") == "restore_command")
    assert restore["executable"] is True and restore["selected"] is True


def test_memory_leak_restore_policy_requires_approval_and_keeps_rate_limit():
    action = proposal("restore_command", "sim-memory-leak")
    context = {
        "recent_action_count": 0,
        "deployment": {
            "desired_replicas": 4,
            "available_replicas": 1,
            "ready_replicas": 1,
        },
    }
    decision = evaluate_policy(action, context)
    assert decision.allowed and decision.requires_approval

    assert evaluate_policy(
        action, {**context, "recent_action_count": 4}
    ).allowed
    limited = evaluate_policy(action, {**context, "recent_action_count": 5})
    assert not limited.allowed
    assert any(reason.startswith("rate_limit:") for reason in limited.reasons)


def test_memory_leak_known_good_command_matches_registered_manifest():
    documents = list(yaml.safe_load_all(Path("k8s/lab/sim-workloads.yaml").read_text()))
    deployment = next(item for item in documents if item["metadata"]["name"] == "sim-memory-leak")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    spec = good.get("sim-memory-leak")
    assert container["name"] == spec.container
    assert container["image"] == spec.expected_image
    assert tuple(container["command"]) == spec.expected_command


def test_memory_leak_restore_patches_only_the_registered_idle_command():
    cluster = FakeCluster(memory_leak_dep())
    result = execute_action(
        proposal("restore_command", "sim-memory-leak"),
        mode="live",
        runner=cluster,
    )
    assert result["ok"] and result["spec_confirmed"] is True
    patch = json.loads(result["command"][result["command"].index("-p") + 1])
    assert patch == {"spec": {"template": {"spec": {"containers": [
        {"name": "leak", "command": list(good.get("sim-memory-leak").command)}
    ]}}}}


@pytest.mark.parametrize(
    "deployment",
    [
        memory_leak_dep(command=["sh", "-c", "sleep 30"]),
        memory_leak_dep(image="busybox:latest"),
    ],
)
def test_memory_leak_restore_refuses_unregistered_current_spec(deployment):
    cluster = FakeCluster(deployment)
    result = execute_action(
        proposal("restore_command", "sim-memory-leak"),
        mode="live",
        runner=cluster,
    )
    assert result["blocked_reason"] == "memory_leak_restore_preflight_failed"
    assert cluster.mutations == []


def test_unregistered_workloads_keep_their_original_candidates():
    pod = {"resource": "demo-web-abc", "resource_kind": "Pod", "deployment": "demo-web"}
    actions = [c["action"] for c in catalog.candidate_actions("pod_crash", pod, {"desired_replicas": 2})]
    assert actions == ["restart_pod", "rollback_deployment", "scale_deployment"]


# ---------------- runbook prerequisites + rejection details ----------------
def _resolve(itype, evidence, action, state):
    return resolve_runbook(" ".join([itype, *evidence]), incident_type=itype, evidence=evidence,
                           planned_action=action, deployment_state=state)


CRASH_EVIDENCE = [
    "Container app is waiting with reason CrashLoopBackOff: back-off 40s restarting failed container",
    "Container app restarted after termination reason Error (exit code 1).",
]
IMAGE_EVIDENCE = [
    'Container app is waiting with reason ImagePullBackOff: Back-off pulling image "busybox:kybernaut-missing-tag"',
]


def test_crash_restore_command_applicable_with_zero_available_replicas():
    result = _resolve("pod_crash", CRASH_EVIDENCE, "restore_command",
                      {"desired_replicas": 1, "available_replicas": 0})
    assert result["status"] == "applicable", result


def test_simulator_pod_crash_offers_approval_gated_command_restore():
    target = {
        "resource": "sim-pod-crash-abc-123",
        "resource_kind": "Pod",
        "deployment": "sim-pod-crash",
    }
    state = {"desired_replicas": 1, "available_replicas": 0}
    options = catalog.build_options(
        "pod_crash", target, state,
        runbook_check=lambda raw: _resolve(
            "pod_crash", CRASH_EVIDENCE, raw["action"], state
        ),
    )
    restore = next(
        option for option in options
        if option.get("action") == "restore_command"
    )
    decision = evaluate_policy(
        proposal("restore_command", "sim-pod-crash"),
        {"recent_action_count": 0, "deployment": state},
    )

    assert restore["executable"] is True
    assert restore["selected"] is True
    assert restore["requires_approval"] is True
    assert decision.allowed and decision.requires_approval
    assert not decision.auto_execute


def test_crash_restart_with_nothing_serving_is_runbook_applicable():
    state = {"desired_replicas": 1, "available_replicas": 0}
    assert _resolve("pod_crash", CRASH_EVIDENCE, "restart_pod", state)["status"] == "applicable"


def test_crash_restart_stays_blocked_when_one_replica_is_serving_and_says_why():
    state = {"desired_replicas": 1, "available_replicas": 1}
    result = _resolve("pod_crash", CRASH_EVIDENCE, "restart_pod", state)
    assert result["status"] == "missing_prerequisites"
    pod = {"resource": "sim-crash-loop-abc", "resource_kind": "Pod", "deployment": "sim-crash-loop"}
    options = catalog.build_options("pod_crash", pod, state,
                                    runbook_check=lambda raw: _resolve("pod_crash", CRASH_EVIDENCE, raw["action"], state))
    restart = next(o for o in options if o.get("action") == "restart_pod")
    rejection = restart["rejection"]
    assert rejection["action_id"] == "action-restart_pod"
    assert rejection["selected_runbook"] == "crashloop-pod-restart"
    assert rejection["failed_prerequisite"] == "min_ready_replicas"
    assert rejection["observed"] == {"available_replicas": 1}
    assert rejection["expected"] == {"min_ready_replicas": 2}
    assert rejection["safe_for_affected_resource"] is False
    assert rejection["other_remediation_available"] is True
    fix = next(o for o in options if o.get("action") == "restore_command")
    assert fix["executable"] is True and fix["rejection"] is None and fix["selected"] is True


def test_restart_policy_allows_a_pod_when_nothing_is_serving():
    pod = proposal("restart_pod", "sim-pod-crash", resource="sim-pod-crash-7bdd57f-abcde")
    state = {"desired_replicas": 1, "available_replicas": 0}
    assert graph._nothing_serving(state) is True
    decision = evaluate_policy(pod, {"recent_action_count": 0, "deployment": state,
                                     "target_not_serving": True})
    assert decision.allowed and decision.requires_approval
    assert graph._nothing_serving({"desired_replicas": 1, "available_replicas": 1}) is False
    assert graph._nothing_serving({"desired_replicas": 0, "available_replicas": 0}) is False


def test_scaled_down_deployment_is_never_a_spec_fix_target():
    result = _resolve("pod_crash", CRASH_EVIDENCE, "restore_command",
                      {"desired_replicas": 0, "available_replicas": 0})
    assert result["status"] == "missing_prerequisites"
    assert result["reason"] == "deployment_scaled_down_or_unknown"


def test_bad_image_set_image_is_runbook_backed():
    result = _resolve("deployment_failure", IMAGE_EVIDENCE, "set_image",
                      {"desired_replicas": 1, "available_replicas": 0})
    assert result["status"] == "applicable" and result["runbook_id"] == "deployment-failure-rollback"


# ---------------- Scenario C: disk eviction ----------------
def test_disk_evict_restart_is_runbook_and_policy_eligible_for_a_single_replica():
    evidence = ["Pod phase is Failed; reason: Evicted"]
    state = {"desired_replicas": 1, "available_replicas": 1}
    runbook = _resolve("disk_full", evidence, "restart_pod", state)
    assert runbook["status"] == "applicable", runbook
    decision = evaluate_policy(
        proposal("restart_pod", "sim-disk-evict", resource="sim-disk-evict-5d9-abcde"),
        {"recent_action_count": 0, "deployment": state, "target_not_serving": True})
    assert decision.allowed and decision.requires_approval and not decision.auto_execute


def test_disk_full_restore_is_registered_and_scoped_to_one_replica():
    spec = good.get("sim-disk-fill")
    assert spec is not None
    assert good.has_fix("sim-disk-fill", "restore_command")
    assert spec.expected_command is not None
    assert spec.expected_image == "busybox:1.36"

    actions = catalog.candidate_actions(
        "disk_full",
        {"resource": "sim-disk-fill-pod", "resource_kind": "Pod",
         "resource_uid": "disk-pod-uid",
         "deployment": "sim-disk-fill"},
        {"desired_replicas": 0},
    )
    assert actions[0] == {
        "action": "restore_command",
        "resource": "sim-disk-fill",
        "target_replicas": 1,
        "target_pod": "sim-disk-fill-pod",
        "target_pod_uid": "disk-pod-uid",
    }


def test_scaled_down_disk_restore_requires_explicit_runbook_gate():
    evidence = [
        "Pod phase is Failed; reason: Evicted",
        'Evicted: Usage of EmptyDir volume "scratch" exceeds the limit "40Mi".',
    ]
    without_gate = _resolve(
        "disk_full", evidence, "restore_command", {"desired_replicas": 0}
    )
    with_gate = resolve_runbook(
        "disk_full " + " ".join(evidence),
        incident_type="disk_full",
        evidence=evidence,
        planned_action="restore_command",
        deployment_state={"desired_replicas": 0},
        allow_scaled_down_restore=True,
    )
    assert without_gate["reason"] == "deployment_scaled_down_or_unknown"
    assert with_gate["status"] == "applicable"
    assert with_gate["runbook_id"] == "disk-full-evicted-pod-recreate"


def test_disk_full_cleanup_candidate_is_only_primary_when_replica_is_healthy():
    target = {
        "resource": "sim-disk-fill-5dc94cc998-abcde",
        "resource_kind": "Pod",
        "resource_uid": "evicted-pod-uid",
        "deployment": "sim-disk-fill",
    }
    state = {
        "desired_replicas": 1,
        "available_replicas": 1,
        "ready_replicas": 1,
    }

    actions = catalog.candidate_actions("disk_full", target, state)
    assert actions[0] == {
        "action": "cleanup_evicted_pod",
        "resource": target["resource"],
        "target_pod": target["resource"],
        "target_pod_uid": "evicted-pod-uid",
    }

    evidence = ["Pod phase is Failed; reason: Evicted"]
    result = resolve_runbook(
        "disk_full " + " ".join(evidence),
        incident_type="disk_full",
        evidence=evidence,
        planned_action="cleanup_evicted_pod",
        deployment_state=state,
    )
    assert result["status"] == "applicable", result
    assert result["approval_required"] is True


def test_disk_full_cleanup_requires_healthy_replica_and_human_approval():
    action = proposal("cleanup_evicted_pod", "sim-disk-fill")
    state = {
        "desired_replicas": 1,
        "available_replicas": 1,
        "ready_replicas": 1,
    }
    decision = evaluate_policy(
        action,
        {
            "recent_action_count": 0,
            "deployment": state,
            "target_not_serving": True,
        },
    )
    assert decision.allowed and decision.requires_approval
    assert not decision.auto_execute

    blocked = evaluate_policy(
        action,
        {
            "recent_action_count": 0,
            "deployment": {**state, "ready_replicas": 0},
            "target_not_serving": True,
        },
    )
    assert not blocked.allowed
    assert any("disk_full_cleanup_scope" in reason for reason in blocked.reasons)

    serving_target = evaluate_policy(
        action,
        {
            "recent_action_count": 0,
            "deployment": state,
            "target_not_serving": False,
        },
    )
    assert not serving_target.allowed


def test_disk_full_cleanup_deletes_only_after_fresh_safe_preflight(monkeypatch):
    cluster = DiskFillFakeCluster(healthy_disk_fill_dep())
    delete_requests = []

    def delete_target(action):
        delete_requests.append((action.target_pod, action.target_pod_uid))
        cluster.pod = None
        return CommandResult(0, "accepted")

    monkeypatch.setattr(rem, "_delete_target_pod_with_uid_precondition", delete_target)
    result = execute_action(
        proposal("cleanup_evicted_pod", "sim-disk-fill"),
        mode="live",
        runner=cluster,
    )

    assert result["ok"] and result["executed"]
    assert result["target_pod_delete_accepted"] == (
        "sim-disk-fill-5dc94cc998-abcde"
    )
    assert result["target_pod_uid"] == "evicted-pod-uid"
    assert "uid=evicted-pod-uid" in result["command"]
    assert delete_requests == [
        ("sim-disk-fill-5dc94cc998-abcde", "evicted-pod-uid")
    ]
    assert cluster.mutations == []


@pytest.mark.parametrize(
    "state_change",
    [
        lambda dep: dep["spec"].update(replicas=0),
        lambda dep: dep["status"].update(availableReplicas=0),
        lambda dep: dep["status"].update(readyReplicas=0),
        lambda dep: dep["spec"]["template"]["spec"]["containers"][0].update(
            command=["sh", "-c", "unexpected"]
        ),
    ],
)
def test_disk_full_cleanup_refuses_unhealthy_or_changed_deployment(state_change):
    dep = healthy_disk_fill_dep()
    state_change(dep)
    cluster = DiskFillFakeCluster(dep)

    result = execute_action(
        proposal("cleanup_evicted_pod", "sim-disk-fill"),
        mode="live",
        runner=cluster,
    )

    assert result["blocked_reason"] == "disk_full_cleanup_preflight_failed"
    assert cluster.mutations == []
    assert not any(call[:2] == ["get", "pod"] for call in cluster.calls)


def test_disk_full_cleanup_refuses_replaced_pod_uid():
    pod = DiskFillFakeCluster(healthy_disk_fill_dep()).pod
    pod["metadata"]["uid"] = "replacement-pod-uid"
    cluster = DiskFillFakeCluster(healthy_disk_fill_dep(), pod=pod)

    result = execute_action(
        proposal("cleanup_evicted_pod", "sim-disk-fill"),
        mode="live",
        runner=cluster,
    )

    assert result["blocked_reason"] == "evicted_pod_identity_mismatch"
    assert cluster.mutations == []


def test_disk_full_cleanup_refuses_unowned_deployment():
    dep = healthy_disk_fill_dep()
    dep["metadata"]["labels"][good.OWNERSHIP_LABEL] = "false"
    cluster = DiskFillFakeCluster(dep)

    result = execute_action(
        proposal("cleanup_evicted_pod", "sim-disk-fill"),
        mode="live",
        runner=cluster,
    )

    assert result["blocked_reason"] == "not_a_lab_scenario_resource"
    assert cluster.mutations == []
    assert not any(call[:2] == ["get", "pod"] for call in cluster.calls)


def test_disk_full_restore_patches_and_uid_deletes_only_registered_target(
    monkeypatch,
):
    cluster = DiskFillFakeCluster(disk_fill_dep())
    delete_requests = []

    def delete_target(action):
        delete_requests.append((action.target_pod, action.target_pod_uid))
        cluster.pod = None
        return CommandResult(0, "accepted")

    monkeypatch.setattr(rem, "_delete_target_pod_with_uid_precondition", delete_target)
    action = proposal(
        "restore_command", "sim-disk-fill", target_replicas=1
    )

    result = execute_action(action, mode="live", runner=cluster)

    assert result["ok"] and result["executed"]
    assert result["spec_confirmed"] is True
    assert cluster.dep["spec"]["replicas"] == 1
    assert cluster.dep["spec"]["template"]["spec"]["containers"][0][
        "command"
    ] == list(good.get("sim-disk-fill").command)
    patch_call = next(call for call in cluster.mutations if call[0] == "patch")
    patch = json.loads(patch_call[patch_call.index("-p") + 1])
    assert patch["spec"]["replicas"] == 1
    assert patch["spec"]["template"]["spec"]["containers"] == [{
        "name": "filler",
        "command": list(good.get("sim-disk-fill").command),
    }]
    assert patch_call[-2:] == ["--context", "kind-kybernaut"]
    assert delete_requests == [
        ("sim-disk-fill-5dc94cc998-abcde", "evicted-pod-uid")
    ]
    assert cluster.pod is None


@pytest.mark.parametrize(
    "tamper",
    ["image", "command", "replicas", "volume"],
)
def test_disk_full_restore_refuses_unrecognized_deployment_state(tamper):
    dep = disk_fill_dep()
    template = dep["spec"]["template"]["spec"]
    if tamper == "image":
        template["containers"][0]["image"] = "unexpected/image:latest"
    elif tamper == "command":
        template["containers"][0]["command"] = ["sh", "-c", "unexpected"]
    elif tamper == "replicas":
        dep["spec"]["replicas"] = 2
    else:
        template["volumes"][0]["emptyDir"]["sizeLimit"] = "100Mi"
    cluster = DiskFillFakeCluster(dep)

    result = execute_action(
        proposal("restore_command", "sim-disk-fill", target_replicas=1),
        mode="live",
        runner=cluster,
    )

    assert result["blocked_reason"] == "disk_full_restore_preflight_failed"
    assert cluster.mutations == []


def test_disk_full_restore_policy_is_approval_gated_and_one_replica_only():
    decision = evaluate_policy(
        proposal("restore_command", "sim-disk-fill", target_replicas=1),
        {"recent_action_count": 0,
         "deployment": {"desired_replicas": 0, "available_replicas": 0}},
    )
    assert decision.allowed and decision.requires_approval
    assert not decision.auto_execute

    with pytest.raises(ValueError, match="restore_command may restore"):
        proposal("restore_command", "sim-disk-fill", target_replicas=2)


def test_disk_full_restore_refuses_replaced_pod_uid():
    pod = DiskFillFakeCluster(disk_fill_dep()).pod
    pod["metadata"]["uid"] = "replacement-pod-uid"
    cluster = DiskFillFakeCluster(disk_fill_dep(), pod=pod)

    result = execute_action(
        proposal("restore_command", "sim-disk-fill", target_replicas=1),
        mode="live",
        runner=cluster,
    )

    assert result["blocked_reason"] == "evicted_pod_identity_mismatch"
    assert cluster.mutations == []


def test_uid_precondition_delete_uses_the_loopback_kubernetes_proxy(
    monkeypatch,
):
    class FakeProxy:
        stderr = None

        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return 0

    proxy = FakeProxy()
    popen_calls = []
    requests = []

    class Response:
        status = 202

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b"accepted"

    def fake_urlopen(request, timeout=None):
        requests.append(request)
        return Response()

    monkeypatch.setattr(
        rem.subprocess,
        "Popen",
        lambda argv, **kwargs: popen_calls.append((argv, kwargs)) or proxy,
    )
    monkeypatch.setattr(rem.urllib.request, "urlopen", fake_urlopen)
    action = proposal(
        "restore_command", "sim-disk-fill", target_replicas=1
    )

    result = rem._delete_target_pod_with_uid_precondition(action)

    assert result.returncode == 0
    assert proxy.terminated
    assert popen_calls[0][0][:4] == [
        "kubectl", "proxy", "--context", "kind-kybernaut"
    ]
    assert popen_calls[0][0][4] == "--address=127.0.0.1"
    request = requests[-1]
    assert request.method == "DELETE"
    assert request.full_url.endswith(
        "/api/v1/namespaces/kybernaut-lab/pods/"
        "sim-disk-fill-5dc94cc998-abcde"
    )
    assert json.loads(request.data) == {
        "apiVersion": "v1",
        "kind": "DeleteOptions",
        "preconditions": {"uid": "evicted-pod-uid"},
    }


def test_disk_full_restore_stays_failed_if_uid_bound_cleanup_fails(monkeypatch):
    cluster = DiskFillFakeCluster(disk_fill_dep())
    monkeypatch.setattr(
        rem,
        "_delete_target_pod_with_uid_precondition",
        lambda _proposal: CommandResult(409, "", "UID precondition failed"),
    )

    result = execute_action(
        proposal("restore_command", "sim-disk-fill", target_replicas=1),
        mode="live",
        runner=cluster,
    )

    assert result["spec_confirmed"] is True
    assert result["ok"] is False
    assert result["cleanup_returncode"] == 409
    assert result["cleanup_stderr"] == "UID precondition failed"


def test_restart_of_a_possibly_serving_single_replica_is_still_blocked_by_policy():
    decision = evaluate_policy(
        proposal("restart_pod", "sim-disk-evict", resource="sim-disk-evict-5d9-abcde"),
        {"recent_action_count": 0, "deployment": {"desired_replicas": 1, "available_replicas": 1},
         "target_not_serving": False})
    assert not decision.allowed
    assert any(r.startswith("blast_radius_restart") for r in decision.reasons)


# ---------------- manifests ----------------
def test_lab_manifests_are_isolated_bounded_and_registered():
    docs = list(yaml.safe_load_all(Path("k8s/lab/remediation-scenarios.yaml").read_text()))
    names = {d["metadata"]["name"] for d in docs}
    assert names == {"sim-bad-image", "sim-crash-loop", "sim-disk-evict"}
    config = PolicyConfig()
    for d in docs:
        assert d["metadata"]["namespace"] == NS and d["spec"]["replicas"] == 0
        assert d["metadata"]["labels"][good.OWNERSHIP_LABEL] == "true"
        assert d["metadata"]["name"] in config.allowed_deployments
        limits = d["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]
        assert "memory" in limits and "cpu" in limits
    disk = next(d for d in docs if d["metadata"]["name"] == "sim-disk-evict")
    assert disk["spec"]["template"]["spec"]["volumes"][0]["emptyDir"]["sizeLimit"] == "40Mi"
    for name in ("sim-bad-image", "sim-crash-loop"):
        assert good.get(name) is not None


# ---------------- scale-out is not blocked by the "last serving pod" rule ----------------
OOM_EVIDENCE = ["Container leak previously terminated with reason OOMKilled, exit code 137."]


def test_scale_out_is_runbook_applicable_with_a_single_replica():
    state = {"desired_replicas": 1, "available_replicas": 1}
    assert _resolve("memory_leak", OOM_EVIDENCE, "scale_deployment", state)["status"] == "applicable"


def test_restart_of_the_only_replica_stays_blocked_with_exact_reason():
    state = {"desired_replicas": 1, "available_replicas": 1}
    result = _resolve("memory_leak", OOM_EVIDENCE, "restart_pod", state)
    assert result["status"] == "missing_prerequisites"
    assert result["reason"] == "minimum_ready_replicas_not_met"
    assert result["observed_available_replicas"] == 1


def test_scale_out_is_still_blocked_for_a_scaled_down_deployment():
    state = {"desired_replicas": 0, "available_replicas": 0}
    assert _resolve("memory_leak", OOM_EVIDENCE, "scale_deployment", state)["status"] == "missing_prerequisites"


# ---------------- ongoing failures keep a stable identity ----------------
from datetime import datetime, timedelta, timezone  # noqa: E402

import app.services.k8s_observer as observer  # noqa: E402


def _pod(created, waiting_reason=None, finished=None, uid="uid-1"):
    status = {"name": "app", "restartCount": 3, "state": {}, "lastState": {}}
    if waiting_reason:
        status["state"] = {"waiting": {"reason": waiting_reason, "message": "back-off"}}
    if finished:
        status["lastState"] = {"terminated": {"reason": "Error", "exitCode": 1, "finishedAt": finished}}
    return {"metadata": {"name": "sim-crash-loop-abc", "uid": uid, "creationTimestamp": created},
            "status": {"phase": "Running", "containerStatuses": [status]}}


def _observe(monkeypatch, pod):
    data = {"pods": {"items": [pod]}, "events": {"items": []}, "deployments": {"items": []}}
    monkeypatch.setattr(observer, "_kubectl_json", lambda resource, *a, **k: data[resource])
    return observer.observe_namespace()["incidents"]


def test_crash_loop_timestamp_is_stable_across_observations(monkeypatch):
    created = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    first = _observe(monkeypatch, _pod(created, "CrashLoopBackOff"))
    finished = datetime.now(timezone.utc).isoformat()
    second = _observe(monkeypatch, _pod(created, "CrashLoopBackOff", finished))
    assert first[0]["source_timestamp"] == second[0]["source_timestamp"]
    assert first[0]["ongoing"] is True and second[0]["ongoing"] is True


def test_image_pull_failure_older_than_the_lookback_is_still_an_ongoing_incident(monkeypatch):
    created = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    incidents = _observe(monkeypatch, _pod(created, "ImagePullBackOff"))
    assert incidents and incidents[0]["ongoing"] is True
    assert datetime.fromisoformat(incidents[0]["source_timestamp"]) == datetime.fromisoformat(created)


def test_existing_simulator_has_a_registered_crash_fix():
    assert good.has_fix("sim-pod-crash", "restore_command")
    assert good.get("sim-pod-crash").container == "crash"


def test_simulator_label_is_accepted_as_ownership():
    dep = deployment_json("sim-pod-crash", "busybox:1.36", ["sh", "-c", "exit 1"], labelled=False, container="crash")
    dep["metadata"]["labels"]["kybernaut.io/simulator"] = "true"
    cluster = FakeCluster(dep)
    result = execute_action(proposal("restore_command", "sim-pod-crash"), mode="live", runner=cluster)
    assert result["ok"] and result["spec_confirmed"] is True


# ------- the approval-time "same incident?" check across re-observations -------
import app.services.incident_response_graph as graph  # noqa: E402
from app.services import incident_repository  # noqa: E402


def test_crash_loop_occurrence_still_matches_after_later_observations(monkeypatch):
    created = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    first = _observe(monkeypatch, _pod(created, "CrashLoopBackOff"))[0]
    persisted_ts = first["source_timestamp"]  # what the sync stores
    # Later: the container restarted again (finishedAt moved, message changed).
    later = _observe(monkeypatch, _pod(created, "CrashLoopBackOff",
                                       datetime.now(timezone.utc).isoformat()))
    kubernetes = {"namespace": "kybernaut-lab",
                  "observed_at": datetime.now(timezone.utc).isoformat(),
                  "incidents": later}
    fingerprint = f"k8s:kybernaut-lab:Pod:{first['resource']}:{first['type']}"
    state = {"occurrence_id": 7, "expected_fingerprint": fingerprint,
             "expected_occurrence_timestamp": persisted_ts}
    monkeypatch.setattr(graph, "_handoff_occurrence_is_active", lambda s: True)
    monkeypatch.setattr(incident_repository, "get_incident_occurrence",
                        lambda _id: {"service": graph.KUBERNETES_SERVICE,
                                     "source_timestamp": persisted_ts,
                                     "source_evidence_json": json.dumps({"resource_uid": "uid-1"})})
    classification = {"incident_type": "pod_crash",
                      "target": {"namespace": "kybernaut-lab", "resource": first["resource"],
                                 "resource_kind": "Pod", "resource_uid": "uid-1",
                                 "deployment": "sim-crash-loop"}}
    assert graph._bound_occurrence_matches(state, kubernetes, classification)


def test_a_different_pod_with_the_same_name_pattern_does_not_match(monkeypatch):
    created = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    first = _observe(monkeypatch, _pod(created, "CrashLoopBackOff"))[0]
    replacement_created = datetime.now(timezone.utc).isoformat()  # a NEW pod incarnation
    later = _observe(monkeypatch, _pod(replacement_created, "CrashLoopBackOff", uid="uid-2"))
    kubernetes = {"namespace": "kybernaut-lab",
                  "observed_at": datetime.now(timezone.utc).isoformat(), "incidents": later}
    fingerprint = f"k8s:kybernaut-lab:Pod:{first['resource']}:{first['type']}"
    state = {"occurrence_id": 7, "expected_fingerprint": fingerprint,
             "expected_occurrence_timestamp": first["source_timestamp"]}
    monkeypatch.setattr(graph, "_handoff_occurrence_is_active", lambda s: True)
    monkeypatch.setattr(incident_repository, "get_incident_occurrence",
                        lambda _id: {"service": graph.KUBERNETES_SERVICE,
                                     "source_timestamp": first["source_timestamp"],
                                     "source_evidence_json": json.dumps({"resource_uid": "uid-1"})})
    classification = {"incident_type": "pod_crash",
                      "target": {"namespace": "kybernaut-lab", "resource": first["resource"],
                                 "resource_kind": "Pod", "resource_uid": "uid-1",
                                 "deployment": "sim-crash-loop"}}
    assert not graph._bound_occurrence_matches(state, kubernetes, classification)