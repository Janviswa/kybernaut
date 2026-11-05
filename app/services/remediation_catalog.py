"""Remediation options for every incident type.

Every alert gets a *set* of options. Options marked ``executable`` map to
registered actions and still go through runbook resolution, policy and human
approval. ``manual`` options are guidance for a human operator; Kybernaut never
runs them.
"""

from __future__ import annotations

from typing import Callable

from app.services import lab_known_good

ACTION_TITLES = {
    "restart_pod": "Recreate the affected pod",
    "scale_deployment": "Scale the deployment out by one replica",
    "rollback_deployment": "Roll the deployment back to the previous revision",
    "set_image": "Correct the container image to the registered known-good image",
    "restore_command": "Restore the registered known-good container command",
    "cleanup_evicted_pod": "Remove the exact evicted simulator pod",
}

ACTION_RISK = {
    "restart_pod": "Briefly removes one pod; in-memory state is lost.",
    "scale_deployment": "Uses extra cluster capacity; does not fix the root cause.",
    "rollback_deployment": "Reverts the latest release, including any intended changes in it.",
    "set_image": "Triggers a rolling update of one designated lab deployment to the registered image.",
    "restore_command": "Triggers a rolling update of one designated lab deployment with the registered command.",
    "cleanup_evicted_pod": "Removes only the approved evicted pod UID after confirming the simulator has one healthy replica.",
}

# Executable candidates in priority order (first runnable one is the primary).
PRIORITY: dict[str, tuple[str, ...]] = {
    "pod_crash": ("restart_pod", "rollback_deployment", "scale_deployment"),
    "memory_leak": ("restart_pod", "scale_deployment", "rollback_deployment"),
    "disk_full": ("restart_pod", "scale_deployment"),
    "deployment_failure": ("rollback_deployment", "restart_pod"),
    "cpu_spike": ("scale_deployment", "restart_pod", "rollback_deployment"),
}

_WHY = {
    ("pod_crash", "restart_pod"): "A restart clears a transient crash loop and is the lowest-impact first step.",
    ("pod_crash", "rollback_deployment"): "If crashes began with the last release, reverting it removes the likely cause.",
    ("pod_crash", "scale_deployment"): "Adds healthy capacity while the crashing pod is investigated.",
    ("memory_leak", "restart_pod"): "Recreating the pod releases leaked memory and stops OOM kills for now.",
    ("memory_leak", "scale_deployment"): "Spreads load across more replicas so each one leaks more slowly.",
    ("memory_leak", "rollback_deployment"): "If the leak started with the last release, reverting it removes the cause.",
    ("memory_leak", "restore_command"): "Stops the registered simulator's memory writer with its registered idle command.",
    ("disk_full", "restart_pod"): "Recreating an evicted pod releases its exhausted ephemeral storage.",
    ("disk_full", "scale_deployment"): "Spreads writes across more replicas while retention is corrected.",
    ("deployment_failure", "rollback_deployment"): "A failed rollout (for example a bad image) is reverted to the last working revision.",
    ("deployment_failure", "restart_pod"): "Recreates a failing pod once the underlying image or config problem is fixed.",
    ("deployment_failure", "set_image"): "The pod cannot pull its image; setting the registered known-good image fixes the actual cause, which a rollback cannot when no earlier good revision exists.",
    ("pod_crash", "restore_command"): "The container exits immediately; a restart would crash again, so the registered known-good command is applied instead.",
    ("disk_full", "restore_command"): "The registered idle command stops the simulator's disk writer before one replica is restored.",
    ("disk_full", "cleanup_evicted_pod"): "The simulator is already healthy; remove only the exact approved evicted pod UID.",
    ("cpu_spike", "scale_deployment"): "Adds replicas to absorb sustained CPU load.",
    ("cpu_spike", "restart_pod"): "Restarts a runaway process that is saturating its CPU.",
    ("cpu_spike", "rollback_deployment"): "If CPU rose after a release, revert it.",
}

# Human-run guidance, always available even when nothing can be automated.
MANUAL: dict[str, list[dict]] = {
    "pod_crash": [
        {"title": "Read the previous container's logs", "steps": [
            "kubectl -n <namespace> logs <pod> --previous",
            "kubectl -n <namespace> describe pod <pod>  (check last state, exit code, events)"],
         "risk": "Read-only."},
        {"title": "Check probes, config and secrets", "steps": [
            "Confirm liveness/readiness probes match what the app really serves.",
            "Confirm required ConfigMaps/Secrets exist and are mounted."],
         "risk": "Read-only until you change a manifest."},
    ],
    "memory_leak": [
        {"title": "Confirm sustained memory growth in <service>", "steps": [
            "Compare fresh working-set memory samples for <service> with its baseline, request volume, and configured memory limit; check whether the increase persists and whether restarts or OOM events occurred.",
            "Correlate when the growth began with recent releases or traffic changes, then capture a memory profile for the owning team."],
         "risk": "Read-only."},
        {"title": "Fix the memory leak in <service>", "steps": [
            "Use the profile to identify retained objects, an unbounded cache or buffer, or resources that are not released; correct the confirmed cause in the owning code.",
            "Deploy the reviewed fix. If immediate capacity relief is needed, consider a temporary memory-limit increase only after checking available capacity and getting change approval."],
         "risk": "A limit increase can mask the leak and increase node memory pressure; code and configuration changes require review."},
        {"title": "Verify memory recovery in <service>", "steps": [
            "Observe fresh memory samples under representative traffic and confirm usage stabilizes below the configured limit, with no continuing restart or OOM events.",
            "Run a representative <service> business request successfully before resolving the incident."],
         "risk": "Read-only verification."},
    ],
    "disk_full": [
        {"title": "Find what is writing to the volume", "steps": [
            "kubectl -n <namespace> exec <pod> -- du -sh /<mount>/*",
            "Identify unrotated logs, caches or temp files."],
         "risk": "Read-only."},
        {"title": "Set an emptyDir sizeLimit and log rotation", "steps": [
            "Add sizeLimit to the emptyDir volume and rotate or cap logs.",
            "Never delete persistent volumes or data without explicit approval."],
         "risk": "Requires a reviewed manifest change."},
    ],
    "deployment_failure": [
        {"title": "Inspect the rollout and the pod events", "steps": [
            "kubectl -n <namespace> rollout status deployment/<name>",
            "kubectl -n <namespace> describe pod <pod>  (look for ImagePullBackOff or probe failures)"],
         "risk": "Read-only."},
        {"title": "Fix the image reference or registry credentials", "steps": [
            "Verify the image tag exists and the imagePullSecret is valid.",
            "Redeploy with the corrected manifest."],
         "risk": "Requires a reviewed manifest change."},
    ],
    "cpu_spike": [
        {"title": "Correlate CPU with request rate and recent releases", "steps": [
            "Compare CPU with request rate and p95 latency in Grafana.",
            "Check whether CPU rose right after a deployment."],
         "risk": "Read-only."},
        {"title": "Review CPU limits and throttling", "steps": [
            "Check container_cpu_cfs_throttled_periods_total.",
            "Adjust requests/limits in a reviewed change if throttled."],
         "risk": "Requires a reviewed manifest change."},
    ],
    "service_unavailable": [
        {"title": "Check the service's pods and endpoints", "steps": [
            "kubectl -n <namespace> get pods,endpoints -l app=<service>",
            "Confirm at least one ready endpoint exists."],
         "risk": "Read-only."},
        {"title": "Check recent deployments and dependencies", "steps": [
            "Look for a rollout in the last hour and for failing downstream services."],
         "risk": "Read-only."},
        {"title": "Escalate to the service owner", "steps": [
            "Page the owning team with the incident link and the evidence collected."],
         "risk": "None."},
    ],
    "high_latency": [
        {"title": "Check <service> latency and request volume", "steps": [
            "Compare <service> p95 latency and request rate with the other services.",
            "Inspect slow endpoints and recent dependency or release changes."],
         "risk": "Read-only."},
        {"title": "Review <service> request handling", "steps": [
            "Profile the slow handler and check for blocking I/O or downstream waits.",
            "After review, restart only the affected service if the delay persists."],
         "risk": "Read-only until a reviewed restart."},
        {"title": "Verify <service> recovery", "steps": [
            "Compare fresh p95 latency against its baseline after the cause is corrected.",
            "Confirm normal request success before resolving the incident."],
         "risk": "Read-only."},
    ],
    "http_5xx": [
        {"title": "Trace <service> 5xx responses", "steps": [
            "Correlate the 5xx rate with <service> logs and dependency health.",
            "Confirm whether the errors began with a deployment or configuration change."],
         "risk": "Read-only."},
        {"title": "Recover <service> request handling", "steps": [
            "Restore the affected dependency or roll back the implicated release.",
            "Verify successful <service> requests and a healthy 5xx rate before closing."],
         "risk": "Requires an approved recovery change."},
        {"title": "Verify <service> error-rate recovery", "steps": [
            "Check fresh request samples and confirm the 5xx rate returned to baseline."],
         "risk": "Read-only."},
    ],
    "request_failure": [
        {"title": "Inspect <service> request failures", "steps": [
            "Review <service> logs and the failing request/response contract.",
            "Check stock state and downstream dependencies without modifying inventory."],
         "risk": "Read-only."},
        {"title": "Restore <service> request handling", "steps": [
            "Correct the identified handler, data, or dependency issue.",
            "Verify inventory reads and reservations before resolving the incident."],
         "risk": "Requires an approved recovery change."},
        {"title": "Verify <service> request recovery", "steps": [
            "Repeat a read-only request and confirm the expected response is returned."],
         "risk": "Read-only."},
    ],
    "unknown": [
        {"title": "Collect more evidence", "steps": [
            "Review the audit timeline and the Prometheus metrics for the service.",
            "Re-run the investigation after fresh evidence is available."],
         "risk": "Read-only."},
        {"title": "Escalate to a human reviewer", "steps": [
            "The evidence does not support an automated action; assign an owner."],
         "risk": "None."},
    ],
}
GENERIC_EXTRA = {"title": "Escalate to the owning team", "steps": [
    "Attach the incident, the evidence and this diagnosis, and hand over to the owner."],
    "risk": "None."}


def candidate_actions(
    incident_type: str, target: dict, deployment_state: dict | None,
) -> list[dict]:
    """Raw proposal dicts that can be built from the evidence, in priority order."""
    deployment = target.get("deployment")
    resource = target.get("resource")
    out: list[dict] = []
    if (
        incident_type == "disk_full"
        and deployment == "sim-disk-fill"
        and target.get("resource_kind") == "Pod"
        and isinstance(resource, str)
        and resource.startswith(deployment + "-")
        and isinstance(target.get("resource_uid"), str)
        and target["resource_uid"]
        and isinstance(deployment_state, dict)
        and all(
            isinstance(deployment_state.get(key), int)
            and not isinstance(deployment_state.get(key), bool)
            and deployment_state[key] == 1
            for key in (
                "desired_replicas", "available_replicas", "ready_replicas"
            )
        )
    ):
        out.append({
            "action": "cleanup_evicted_pod",
            "resource": resource,
            "target_pod": resource,
            "target_pod_uid": target["resource_uid"],
        })
    # Spec-correcting fixes go first, but only for registered lab deployments:
    # blindly restarting or rolling back cannot fix a broken image/command.
    for fix, applies_to in (
        ("set_image", "deployment_failure"),
        ("restore_command", "pod_crash"),
        ("restore_command", "disk_full"),
        ("restore_command", "memory_leak"),
    ):
        if (
            incident_type == applies_to and deployment
            and lab_known_good.has_fix(deployment, fix)
        ):
            candidate = {"action": fix, "resource": deployment}
            if incident_type == "disk_full" and fix == "restore_command":
                desired = (deployment_state or {}).get("desired_replicas")
                if (
                    isinstance(desired, bool)
                    or not isinstance(desired, int)
                    or desired not in {0, 1}
                    or target.get("resource_kind") != "Pod"
                    or not isinstance(resource, str)
                    or not resource.startswith(deployment + "-")
                    or not isinstance(target.get("resource_uid"), str)
                    or not target["resource_uid"]
                ):
                    continue
                candidate["target_replicas"] = 1
                candidate["target_pod"] = resource
                candidate["target_pod_uid"] = target["resource_uid"]
            out.append(candidate)
    for action in PRIORITY.get(incident_type, ()):
        if action == "restart_pod":
            if target.get("resource_kind") == "Pod" and resource:
                out.append({"action": action, "resource": resource})
        elif action == "rollback_deployment":
            if deployment:
                out.append({"action": action, "resource": deployment})
        elif action == "scale_deployment":
            desired = (deployment_state or {}).get("desired_replicas")
            if deployment and isinstance(desired, int) and not isinstance(desired, bool):
                out.append({"action": action, "resource": deployment,
                            "target_replicas": desired + 1})
    return out


def manual_options(
    incident_type: str, service: str | None = None,
) -> list[dict]:
    service_specific_unavailable = [
        {"title": "Check <service> health and listening port", "steps": [
            "Request the service's /health endpoint and inspect its recent logs.",
            "Confirm whether the process is stopped or returning errors."],
         "risk": "Read-only."},
        {"title": "Restart only <service> after checking the cause", "steps": [
            "Use the reviewed local recovery procedure for this service.",
            "Do not restart other services as part of this incident."],
         "risk": "Requires an approved recovery action."},
        {"title": "Verify <service> availability", "steps": [
            "Confirm /health and a representative business request both succeed."],
         "risk": "Read-only."},
    ]
    payment_memory_recovery = [
        {"title": "Migrate payment history to PostgreSQL", "steps": [
            "Pause new payment writes while keeping the current payment-service process running.",
            "Start payment-postgres and run scripts/migrate_payment_history.py to copy the current in-memory records; confirm the import succeeds before replacing the old process."],
         "risk": "The migration reads payment records and writes them to PostgreSQL. Replacing the old process before migration completes loses its in-memory-only records."},
        {"title": "Deploy the PostgreSQL-backed payment service", "steps": [
            "Rebuild and replace only payment-service after the history import is complete.",
            "Confirm /health succeeds and /payments returns the migrated records before allowing new writes."],
         "risk": "Requires a service rollout; keep payment writes paused until the replacement is healthy."},
        {"title": "Verify payment-service memory recovery", "steps": [
            "Resume representative payment traffic and review fresh working-set memory samples over time.",
            "Confirm memory stabilizes and payment history remains available before resolving the incident."],
         "risk": "Read-only verification."},
    ]
    if service == "payment-service" and incident_type == "memory_leak":
        items = payment_memory_recovery
    elif service and incident_type == "service_unavailable":
        items = service_specific_unavailable
    else:
        items = MANUAL.get(incident_type) or MANUAL["unknown"]
    items = [*items, GENERIC_EXTRA] if len(items) < 3 else items
    return [
        {"option_id": f"manual-{index + 1}", "kind": "manual", "executable": False,
         "title": item["title"].replace("<service>", service or "the service"),
         "steps": [
             step.replace("<service>", service or "the service")
             for step in item["steps"]
         ],
         "risk": item["risk"], "rationale": "Guidance for a human operator; Kybernaut does not run it."}
        for index, item in enumerate(items)
    ]


def build_options(
    incident_type: str, target: dict, deployment_state: dict | None, *,
    runbook_check: Callable[[dict], dict] | None = None,
    selected_action: str | None = None,
) -> list[dict]:
    """Executable candidates (checked against runbooks) followed by manual guidance."""
    options: list[dict] = []
    for raw in candidate_actions(incident_type, target, deployment_state):
        check = runbook_check(raw) if runbook_check else {"status": "not_checked"}
        applicable = check.get("status") == "applicable"
        action = raw["action"]
        detail = ACTION_TITLES[action]
        if action == "restart_pod":
            detail += f" ({raw['resource']})"
        elif action == "scale_deployment":
            detail += f" ({raw['resource']} → {raw['target_replicas']} replicas)"
        elif action == "restore_command" and raw.get("target_replicas") == 1:
            detail = (
                "Stop the disk writer, start one replica, and remove the "
                f"exact evicted pod ({raw['resource']})"
            )
        elif action == "cleanup_evicted_pod":
            detail = f"Remove the exact evicted pod ({raw['resource']})"
        else:
            detail += f" ({raw['resource']})"
        risk = ACTION_RISK[action]
        if action == "restore_command" and raw.get("target_replicas") == 1:
            risk = (
                "Applies the registered idle command, restores exactly one "
                "replica, and removes only the approved evicted pod UID; "
                "recovery must be verified."
            )
        elif action == "cleanup_evicted_pod":
            risk = (
                "Deletes only the approved evicted pod UID after confirming "
                "the registered simulator has one healthy replica."
            )
        prerequisites = check.get("prerequisites")
        options.append({
            "option_id": f"action-{action}", "kind": "executable",
            "executable": applicable, "action": action,
            "resource": raw["resource"], "target_replicas": raw.get("target_replicas"),
            "target_pod": raw.get("target_pod"),
            "target_pod_uid": raw.get("target_pod_uid"),
            "title": detail, "rationale": _WHY.get((incident_type, action), ""),
            "risk": risk,
            "runbook_status": check.get("status"),
            "runbook_reason": check.get("reason"),
            "runbook_id": check.get("runbook_id"),
            "runbook_prerequisites": prerequisites,
            "observed_available_replicas": check.get(
                "observed_available_replicas"
            ),
            "requires_approval": True,
            "selected": selected_action == action,
        })
    _annotate_rejections(options, incident_type, target)
    if options and not any(item["selected"] for item in options):
        first = next((item for item in options if item["executable"]), None)
        if first is not None:
            first["selected"] = True
    return [
        *options,
        *manual_options(incident_type, target.get("service")),
    ]


def _annotate_rejections(options: list[dict], incident_type: str, target: dict) -> None:
    """Explain exactly why each non-runnable executable option was rejected."""
    any_runnable = any(item["executable"] for item in options)
    pod_target = target.get("resource_kind") == "Pod"
    for item in options:
        if item["executable"]:
            item["rejection"] = None
            continue
        reason = item.get("runbook_reason") or item.get("runbook_status")
        prerequisites = item.get("runbook_prerequisites") or {}
        if reason == "minimum_ready_replicas_not_met":
            failed = "min_ready_replicas"
            observed = {"available_replicas": item.get("observed_available_replicas")}
            expected = {"min_ready_replicas": prerequisites.get("min_ready_replicas")}
        else:
            failed = reason or "runbook_not_applicable"
            observed = {"runbook_status": item.get("runbook_status")}
            expected = {"runbook_status": "applicable"}
        action = item["action"]
        spec_fix = action in lab_known_good.SPEC_ACTIONS
        removes_serving_capacity = action == "restart_pod" and not (
            pod_target and incident_type == "disk_full"
        )
        safe = spec_fix or not removes_serving_capacity
        item["rejection"] = {
            "action_id": item["option_id"],
            "action": action,
            "selected_runbook": item.get("runbook_id"),
            "failed_prerequisite": failed,
            "observed": observed,
            "expected": expected,
            "safe_for_affected_resource": safe,
            "safety_basis": (
                "Applies a registered known-good value to one designated lab deployment."
                if spec_fix else
                "The evicted target pod is not serving, so removing it cannot reduce capacity."
                if safe and action == "restart_pod" else
                "Removing a possibly serving pod could reduce capacity; "
                "a spec-correcting action is the safe alternative."
                if not safe else
                "Does not remove serving capacity."
            ),
            "other_remediation_available": any_runnable,
        }