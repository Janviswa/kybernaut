"""Deterministic policy engine for proposed remediation actions.

Safe action flow (guide section 07):

    LLM / rule proposal -> schema validation (ProposedAction)
        -> policy check (evaluate_policy) -> approval if required
        -> whitelisted tool (k8s_remediation) -> verification -> audit

This module is pure: it performs no I/O. Callers supply the context
(current deployment state, recent action count). Missing evidence fails
closed -- a missing deployment record is never treated as "healthy".
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services import lab_known_good

ALLOWED_ACTIONS = (
    "restart_pod", "scale_deployment", "rollback_deployment",
    # Spec-correcting actions: apply only the registered known-good value
    # (see lab_known_good); no free-form image or command is ever accepted.
    "set_image", "restore_command", "cleanup_evicted_pod",
)

# RFC 1123 style Kubernetes resource names.
K8S_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}


class ProposedAction(BaseModel):
    """Strictly validated remediation proposal.

    ``extra="forbid"`` means an LLM cannot smuggle extra fields such as a
    shell command or kubectl flag through this schema.
    """

    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "restart_pod", "scale_deployment", "rollback_deployment",
        "set_image", "restore_command", "cleanup_evicted_pod",
    ]
    namespace: str
    resource: str = Field(description="Pod name or deployment name.")
    deployment: str = Field(description="Owning deployment.")
    target_replicas: int | None = Field(default=None, ge=0, le=100)
    target_pod: str | None = None
    target_pod_uid: str | None = Field(default=None, min_length=1, max_length=128)
    confidence: float = Field(ge=0.0, le=1.0)
    severity: Literal["low", "medium", "high", "critical"]
    rationale: str = Field(min_length=1, max_length=1000)
    source: Literal["rules", "llm"] = "rules"

    @model_validator(mode="after")
    def _validate_shape(self) -> "ProposedAction":
        for field_name in ("namespace", "resource", "deployment"):
            value = getattr(self, field_name)
            if not K8S_NAME.match(value):
                raise ValueError(f"{field_name} is not a valid Kubernetes name")

        if self.action == "scale_deployment":
            if self.target_replicas is None:
                raise ValueError("scale_deployment requires target_replicas")
            if self.resource != self.deployment:
                raise ValueError("scale_deployment resource must be the deployment")
        elif self.action == "restore_command" and self.target_replicas is not None:
            if self.deployment != "sim-disk-fill" or self.target_replicas != 1:
                raise ValueError(
                    "restore_command may restore one replica only for sim-disk-fill"
                )
        elif self.action == "cleanup_evicted_pod":
            if self.target_replicas is not None:
                raise ValueError(
                    "cleanup_evicted_pod does not accept a replica target"
                )
            if (
                self.deployment != "sim-disk-fill"
                or not self.target_pod
                or not self.target_pod_uid
                or self.resource != self.target_pod
            ):
                raise ValueError(
                    "cleanup_evicted_pod requires the exact sim-disk-fill pod identity"
                )
        else:
            if self.target_replicas is not None:
                raise ValueError("target_replicas is only valid for scaling")

        if (
            self.action in {"rollback_deployment", "set_image", "restore_command"}
            and self.resource != self.deployment
        ):
            raise ValueError(f"{self.action} resource must be the deployment")

        has_target_pod = self.target_pod is not None
        has_target_uid = self.target_pod_uid is not None
        if has_target_pod != has_target_uid:
            raise ValueError("target_pod and target_pod_uid must be provided together")
        if has_target_pod:
            if (
                self.action not in {"restore_command", "cleanup_evicted_pod"}
                or self.deployment != "sim-disk-fill"
                or not K8S_NAME.match(self.target_pod or "")
                or not self.target_pod.startswith(self.deployment + "-")
                or (
                    self.action == "restore_command"
                    and self.target_replicas != 1
                )
            ):
                raise ValueError(
                    "target pod cleanup is only valid for the registered disk-fill recovery"
                )
        elif (
            (
                self.action == "restore_command"
                and self.deployment == "sim-disk-fill"
                and self.target_replicas == 1
            )
            or self.action == "cleanup_evicted_pod"
        ):
            raise ValueError(
                "disk-fill recovery requires the exact evicted pod identity"
            )

        return self


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class PolicyConfig:
    allowed_namespaces: frozenset[str] = frozenset({"kybernaut-lab"})
    allowed_deployments: frozenset[str] = frozenset(
        {
            "demo-web",
            "sim-cpu-stress",
            "sim-memory-leak",
            "sim-disk-fill",
            "sim-pod-crash",
            # Dedicated remediation-lab scenarios (k8s/lab/remediation-scenarios.yaml)
            "sim-bad-image",
            "sim-crash-loop",
            "sim-disk-evict",
        }
    )
    min_confidence: float = 0.6
    auto_confidence: float = 0.9
    # Automatic execution is OFF unless explicitly enabled.
    auto_remediation_enabled: bool = False
    auto_allowed_actions: frozenset[str] = frozenset({"restart_pod"})
    auto_max_severity: str = "medium"
    min_replicas: int = 1
    max_replicas: int = 5
    max_scale_step: int = 2
    # Blast radius: keep at least one replica serving during a restart.
    min_ready_replicas_for_restart: int = 2
    max_actions_per_window: int = 3
    window_seconds: int = 3600
    lab_max_actions_per_window: int = 5
    lab_window_seconds: int = 3600

    @classmethod
    def from_env(cls) -> "PolicyConfig":
        return cls(
            min_confidence=_env_float("KYBERNAUT_POLICY_MIN_CONFIDENCE", 0.6),
            auto_confidence=_env_float("KYBERNAUT_POLICY_AUTO_CONFIDENCE", 0.9),
            auto_remediation_enabled=_env_bool(
                "KYBERNAUT_POLICY_AUTO_REMEDIATION", False
            ),
            max_replicas=_env_int("KYBERNAUT_POLICY_MAX_REPLICAS", 5),
            max_actions_per_window=_env_int("KYBERNAUT_POLICY_MAX_ACTIONS", 3),
            window_seconds=_env_int("KYBERNAUT_POLICY_WINDOW_SECONDS", 3600),
            lab_max_actions_per_window=_env_int(
                "KYBERNAUT_LAB_POLICY_MAX_ACTIONS", 5
            ),
            lab_window_seconds=_env_int(
                "KYBERNAUT_LAB_POLICY_WINDOW_SECONDS", 3600
            ),
        )

    def action_capacity_for_namespace(self, namespace: str) -> tuple[int, int]:
        if namespace == "kybernaut-lab":
            return self.lab_max_actions_per_window, self.lab_window_seconds
        return self.max_actions_per_window, self.window_seconds


@dataclass
class PolicyDecision:
    allowed: bool
    requires_approval: bool
    auto_execute: bool
    reasons: list[str] = field(default_factory=list)
    rules: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "auto_execute": self.auto_execute,
            "reasons": list(self.reasons),
            "rules": list(self.rules),
        }


def evaluate_policy(
    proposal: ProposedAction,
    context: dict[str, Any] | None = None,
    config: PolicyConfig | None = None,
) -> PolicyDecision:
    """Evaluate a validated proposal. Never raises for bad context."""
    config = config or PolicyConfig.from_env()
    context = context if isinstance(context, dict) else {}

    rules: list[dict[str, Any]] = []
    reasons: list[str] = []

    def rule(rule_id: str, passed: bool, detail: str, hard: bool = True) -> None:
        rules.append({"id": rule_id, "passed": passed, "detail": detail})
        if hard and not passed:
            reasons.append(f"{rule_id}: {detail}")

    rule(
        "action_allowlisted",
        proposal.action in ALLOWED_ACTIONS,
        f"action={proposal.action}",
    )
    rule(
        "namespace_allowlisted",
        proposal.namespace in config.allowed_namespaces,
        f"namespace={proposal.namespace}",
    )
    rule(
        "deployment_allowlisted",
        proposal.deployment in config.allowed_deployments,
        f"deployment={proposal.deployment}",
    )

    deployment = context.get("deployment")
    if proposal.action == "restart_pod":
        rule(
            "pod_belongs_to_deployment",
            proposal.resource.startswith(proposal.deployment + "-"),
            f"pod={proposal.resource} deployment={proposal.deployment}",
        )
    elif proposal.action == "cleanup_evicted_pod":
        deployment_data = deployment if isinstance(deployment, dict) else {}
        available = deployment_data.get("available_replicas")
        ready = deployment_data.get("ready_replicas")
        desired = deployment_data.get("desired_replicas")
        healthy_counts = all(
            isinstance(value, int) and not isinstance(value, bool) and value == 1
            for value in (desired, available, ready)
        )
        rule(
            "disk_full_cleanup_scope",
            proposal.namespace == "kybernaut-lab"
            and proposal.deployment == "sim-disk-fill"
            and proposal.resource == proposal.target_pod
            and isinstance(proposal.target_pod_uid, str)
            and lab_known_good.has_fix(
                proposal.deployment, "restore_command"
            )
            and context.get("target_not_serving") is True
            and healthy_counts,
            (
                f"desired={desired} available={available} ready={ready} "
                f"pod={proposal.target_pod}"
            ),
        )

    rule(
        "confidence_threshold",
        proposal.confidence >= config.min_confidence,
        f"confidence={proposal.confidence:.2f} minimum={config.min_confidence:.2f}",
    )

    max_actions, window_seconds = config.action_capacity_for_namespace(
        proposal.namespace
    )
    recent = context.get("recent_action_count")
    if isinstance(recent, int) and not isinstance(recent, bool) and recent >= 0:
        rule(
            "rate_limit",
            recent < max_actions,
            f"recent_actions={recent} limit={max_actions}"
            f" per {window_seconds}s",
        )
    else:
        rule("rate_limit", False, "recent action count unavailable (fail closed)")

    desired = available = None
    if isinstance(deployment, dict):
        desired = deployment.get("desired_replicas")
        available = deployment.get("available_replicas")
    have_state = (
        isinstance(desired, int)
        and isinstance(available, int)
        and not isinstance(desired, bool)
        and not isinstance(available, bool)
    )

    if proposal.action in lab_known_good.SPEC_ACTIONS:
        rule(
            "known_good_spec_registered",
            lab_known_good.has_fix(proposal.deployment, proposal.action),
            f"deployment={proposal.deployment} action={proposal.action}",
        )
    if proposal.action == "restore_command" and proposal.target_replicas is not None:
        rule(
            "restore_command_replica_scope",
            proposal.deployment == "sim-disk-fill"
            and proposal.target_replicas == 1
            and have_state
            and desired in {0, 1},
            (
                f"deployment={proposal.deployment} target={proposal.target_replicas} "
                f"desired={desired}"
            ),
        )
    if (
        proposal.action == "restore_command"
        and proposal.deployment == "sim-memory-leak"
    ):
        rule(
            "memory_leak_restore_scope",
            proposal.namespace == "kybernaut-lab"
            and proposal.target_replicas is None
            and proposal.target_pod is None
            and proposal.target_pod_uid is None
            and lab_known_good.has_fix(
                proposal.deployment, "restore_command"
            ),
            f"deployment={proposal.deployment}",
        )

    if proposal.action == "restart_pod":
        not_serving = context.get("target_not_serving") is True
        rule(
            "blast_radius_restart",
            have_state
            and (
                not_serving
                or (
                    desired >= config.min_ready_replicas_for_restart
                    and available >= config.min_ready_replicas_for_restart
                )
            ),
            (
                f"available={available} desired={desired} "
                f"required>={config.min_ready_replicas_for_restart}"
                if have_state
                else "deployment state unavailable (fail closed)"
            ),
        )
    elif proposal.action == "scale_deployment":
        target = proposal.target_replicas
        in_bounds = (
            target is not None
            and config.min_replicas <= target <= config.max_replicas
        )
        rule(
            "scale_bounds",
            in_bounds,
            f"target={target} allowed={config.min_replicas}..{config.max_replicas}",
        )
        rule(
            "scale_step",
            have_state
            and target is not None
            and abs(target - desired) <= config.max_scale_step,
            (
                f"desired={desired} target={target} max_step={config.max_scale_step}"
                if have_state
                else "deployment state unavailable (fail closed)"
            ),
        )

    allowed = not reasons

    # Approval: required for everything except narrowly defined auto cases.
    auto_ok = (
        allowed
        and config.auto_remediation_enabled
        and proposal.action in config.auto_allowed_actions
        and SEVERITY_RANK[proposal.severity]
        <= SEVERITY_RANK[config.auto_max_severity]
        and proposal.confidence >= config.auto_confidence
    )
    rule(
        "approval_required",
        not auto_ok,
        "human approval required"
        if not auto_ok
        else "eligible for automatic execution",
        hard=False,
    )

    return PolicyDecision(
        allowed=allowed,
        requires_approval=allowed and not auto_ok,
        auto_execute=bool(auto_ok),
        reasons=reasons,
        rules=rules,
    )