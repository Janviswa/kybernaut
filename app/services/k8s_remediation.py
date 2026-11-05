"""Whitelisted Kubernetes remediation tools and exact-UID pod cleanup.

There is no generic "run kubectl" entry point. Each operation builds a
fixed argument vector from a validated ``ProposedAction``; no shell is used.
Evicted-pod cleanup uses a loopback Kubernetes API proxy and a UID precondition.

Execution mode (``KYBERNAUT_REMEDIATION_MODE``):
  * ``dry_run`` (default): nothing is executed; the planned command is
    returned for review and audit.
  * ``live``: executes against the cluster only when the current kubectl
    context is exactly ``kind-kybernaut``. Every live kubectl call is also
    pinned with ``--context kind-kybernaut``.
  * unset -> ``dry_run`` (global safe default). Any *other* value is an
    invalid configuration and execution is BLOCKED with a clear error; an
    invalid value never silently degrades to dry_run.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from app.services import lab_known_good
from app.services.policy_engine import (
    ALLOWED_ACTIONS,
    PolicyConfig,
    ProposedAction,
)

COMMAND_TIMEOUT_SECONDS = 60
LIVE_CONTEXT = "kind-kybernaut"


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[list[str]], CommandResult]


class CommandNotStartedError(RuntimeError):
    """The OS confirmed that the kubectl process was not started."""


def default_runner(args: list[str]) -> CommandResult:
    try:
        completed = subprocess.run(
            ["kubectl", *args],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise CommandNotStartedError(type(exc).__name__) from exc
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def get_mode() -> str:
    """Configured mode: ``dry_run``, ``live`` or ``invalid`` (never guessed)."""
    raw = os.getenv("KYBERNAUT_REMEDIATION_MODE")
    if raw is None or not raw.strip():
        return "dry_run"
    mode = raw.strip().lower()
    return mode if mode in {"dry_run", "live"} else "invalid"


def mode_status() -> dict:
    """Describe the effective execution configuration for the API/UI."""
    raw = os.getenv("KYBERNAUT_REMEDIATION_MODE")
    mode = get_mode()
    return {
        "configured_value": raw,
        "mode": mode,
        "valid": mode != "invalid",
        "allowed_context": LIVE_CONTEXT,
        "detail": (
            "Live execution enabled for this API process."
            if mode == "live"
            else "Dry run: commands are recorded but never executed. Restart the "
                 "API with KYBERNAUT_REMEDIATION_MODE=live to execute."
            if mode == "dry_run"
            else f"Invalid KYBERNAUT_REMEDIATION_MODE={raw!r}; execution is blocked."
        ),
    }


def build_command(proposal: ProposedAction) -> list[str]:
    """Return a fixed command or API-operation description for a proposal."""
    if proposal.action == "cleanup_evicted_pod":
        path = (
            f"/api/v1/namespaces/{urllib.parse.quote(proposal.namespace, safe='')}"
            f"/pods/{urllib.parse.quote(proposal.target_pod or '', safe='')}"
        )
        return ["DELETE", path, f"uid={proposal.target_pod_uid}"]
    if proposal.action == "restart_pod":
        return [
            "delete", "pod", proposal.resource,
            "-n", proposal.namespace, "--wait=false",
        ]
    if proposal.action == "scale_deployment":
        return [
            "scale", f"deployment/{proposal.deployment}",
            f"--replicas={proposal.target_replicas}",
            "-n", proposal.namespace,
        ]
    if proposal.action == "rollback_deployment":
        return [
            "rollout", "undo", f"deployment/{proposal.deployment}",
            "-n", proposal.namespace,
        ]
    if proposal.action in lab_known_good.SPEC_ACTIONS:
        spec = lab_known_good.get(proposal.deployment)
        if spec is None or proposal.action not in spec.fixes:
            raise ValueError("No registered known-good spec for this deployment")
        if proposal.action == "set_image":
            return [
                "set", "image", f"deployment/{proposal.deployment}",
                f"{spec.container}={spec.image}", "-n", proposal.namespace,
            ]
        spec_patch = {"spec": {"template": {"spec": {"containers": [
            {"name": spec.container, "command": list(spec.command)},
        ]}}}}
        if proposal.action == "restore_command" and proposal.target_replicas is not None:
            spec_patch["spec"]["replicas"] = proposal.target_replicas
        return [
            "patch", "deployment", proposal.deployment,
            "-n", proposal.namespace, "--type=strategic",
            "-p", json.dumps(spec_patch, separators=(",", ":")),
        ]
    raise ValueError("Unsupported action")


def _blocked(proposal, mode, command, code: str, error: str) -> dict:
    result = _result(proposal, mode, False, command, ok=False, error=error)
    result["blocked"] = True
    result["blocked_reason"] = code
    return result


def _read_deployment(proposal, runner) -> dict | None:
    done = runner([
        "get", "deployment", proposal.deployment, "-n", proposal.namespace,
        "-o", "json", "--context", LIVE_CONTEXT,
    ])
    if done.returncode != 0:
        return None
    try:
        data = json.loads(done.stdout or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _read_target_pod(proposal, runner) -> dict | None:
    if not proposal.target_pod:
        return None
    done = runner([
        "get", "pod", proposal.target_pod, "-n", proposal.namespace,
        "-o", "json", "--context", LIVE_CONTEXT,
    ])
    if done.returncode != 0:
        return None
    try:
        data = json.loads(done.stdout or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _delete_target_pod_with_uid_precondition(proposal) -> CommandResult:
    if not proposal.target_pod or not proposal.target_pod_uid:
        raise ValueError("Exact evicted pod identity is required")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    try:
        proxy = subprocess.Popen(
            [
                "kubectl", "proxy", "--context", LIVE_CONTEXT,
                "--address=127.0.0.1", f"--port={port}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
    except (FileNotFoundError, PermissionError) as exc:
        raise CommandNotStartedError(type(exc).__name__) from exc

    try:
        base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if proxy.poll() is not None:
                stderr = proxy.communicate()[1]
                return CommandResult(
                    proxy.returncode or 1, "", (stderr or "").strip()
                )
            try:
                with urllib.request.urlopen(
                    f"{base_url}/api/v1", timeout=0.25
                ):
                    break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.05)
        else:
            return CommandResult(
                1, "", "The guarded Kubernetes API proxy did not become ready."
            )

        resource = urllib.parse.quote(proposal.target_pod, safe="")
        path = (
            f"/api/v1/namespaces/{urllib.parse.quote(proposal.namespace, safe='')}"
            f"/pods/{resource}"
        )
        body = json.dumps({
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "preconditions": {"uid": proposal.target_pod_uid},
        }).encode("utf-8")
        request = urllib.request.Request(
            f"{base_url}{path}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return CommandResult(
                    0,
                    json.dumps({
                        "http_status": response.status,
                        "body": response.read().decode(),
                    }),
                )
        except urllib.error.HTTPError as exc:
            return CommandResult(exc.code, "", exc.read().decode())
        except urllib.error.URLError as exc:
            return CommandResult(1, "", f"Kubernetes API delete failed: {exc.reason}")
    finally:
        if proxy.poll() is None:
            proxy.terminate()
            try:
                proxy.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proxy.kill()
                proxy.wait()
        if proxy.stderr is not None:
            proxy.stderr.close()


def _container(data: dict, name: str) -> dict | None:
    containers = (
        ((data.get("spec") or {}).get("template") or {}).get("spec") or {}
    ).get("containers")
    for item in containers if isinstance(containers, list) else []:
        if isinstance(item, dict) and item.get("name") == name:
            return item
    return None


def _spec_matches(proposal, data: dict) -> bool:
    spec = lab_known_good.get(proposal.deployment)
    container = _container(data, spec.container) if spec else None
    if spec is None or container is None:
        return False
    if proposal.action == "set_image":
        matches = container.get("image") == spec.image
    else:
        matches = list(container.get("command") or []) == list(spec.command)
    if proposal.target_replicas is not None:
        matches = (
            matches
            and (data.get("spec") or {}).get("replicas")
            == proposal.target_replicas
        )
    return matches


def _disk_full_restore_preflight_matches(proposal, data: dict) -> bool:
    spec = lab_known_good.get(proposal.deployment)
    if (
        proposal.action != "restore_command"
        or proposal.deployment != "sim-disk-fill"
        or proposal.target_replicas != 1
        or spec is None
        or spec.expected_command is None
        or spec.expected_image is None
    ):
        return False
    deployment_spec = data.get("spec") or {}
    desired = deployment_spec.get("replicas")
    if isinstance(desired, bool) or desired not in {0, 1}:
        return False
    container = _container(data, spec.container)
    containers = (
        ((deployment_spec.get("template") or {}).get("spec") or {})
        .get("containers")
    )
    if (
        container is None
        or container.get("image") != spec.expected_image
        or not isinstance(containers, list)
        or len(containers) != 1
    ):
        return False
    current_command = tuple(container.get("command") or [])
    if current_command not in {
        tuple(spec.expected_command),
        tuple(spec.command),
    }:
        return False
    volumes = (
        ((deployment_spec.get("template") or {}).get("spec") or {})
        .get("volumes")
    )
    return (
        isinstance(volumes, list)
        and len(volumes) == 1
        and isinstance(volumes[0], dict)
        and volumes[0].get("name") == "scratch"
        and (volumes[0].get("emptyDir") or {}).get("sizeLimit") == "40Mi"
    )


def _memory_leak_restore_preflight_matches(proposal, data: dict) -> bool:
    spec = lab_known_good.get(proposal.deployment)
    if (
        proposal.action != "restore_command"
        or proposal.deployment != "sim-memory-leak"
        or spec is None
        or spec.expected_command is None
        or spec.expected_image is None
    ):
        return False
    deployment_spec = data.get("spec") or {}
    desired = deployment_spec.get("replicas")
    containers = (
        ((deployment_spec.get("template") or {}).get("spec") or {})
        .get("containers")
    )
    container = _container(data, spec.container)
    return (
        isinstance(desired, int)
        and not isinstance(desired, bool)
        and 1 <= desired <= PolicyConfig.from_env().max_replicas
        and isinstance(containers, list)
        and len(containers) == 1
        and container is not None
        and container.get("image") == spec.expected_image
        and tuple(container.get("command") or []) in {
            tuple(spec.expected_command),
            tuple(spec.command),
        }
    )


def _disk_full_cleanup_preflight_matches(proposal, data: dict) -> bool:
    spec = lab_known_good.get(proposal.deployment)
    if (
        proposal.action != "cleanup_evicted_pod"
        or proposal.deployment != "sim-disk-fill"
        or spec is None
        or spec.expected_image is None
    ):
        return False
    deployment_spec = data.get("spec") or {}
    status = data.get("status") or {}
    desired = deployment_spec.get("replicas")
    available = status.get("availableReplicas")
    ready = status.get("readyReplicas")
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value == 1
        for value in (desired, available, ready)
    ):
        return False
    container = _container(data, spec.container)
    template = (deployment_spec.get("template") or {}).get("spec") or {}
    containers = template.get("containers")
    volumes = template.get("volumes")
    return (
        container is not None
        and container.get("image") == spec.expected_image
        and list(container.get("command") or []) == list(spec.command)
        and isinstance(containers, list)
        and len(containers) == 1
        and isinstance(volumes, list)
        and len(volumes) == 1
        and isinstance(volumes[0], dict)
        and volumes[0].get("name") == "scratch"
        and (volumes[0].get("emptyDir") or {}).get("sizeLimit") == "40Mi"
    )


def _disk_full_target_pod_matches(proposal, runner) -> bool:
    data = _read_target_pod(proposal, runner)
    if data is None:
        return False
    metadata = data.get("metadata") or {}
    status = data.get("status") or {}
    owners = metadata.get("ownerReferences")
    return bool(
        metadata.get("name") == proposal.target_pod
        and metadata.get("namespace") == proposal.namespace
        and metadata.get("uid") == proposal.target_pod_uid
        and not metadata.get("deletionTimestamp")
        and status.get("phase") == "Failed"
        and status.get("reason") == "Evicted"
        and isinstance(owners, list)
        and any(
            isinstance(owner, dict)
            and owner.get("controller") is True
            and owner.get("kind") == "ReplicaSet"
            and isinstance(owner.get("name"), str)
            and owner["name"].startswith(proposal.deployment + "-")
            for owner in owners
        )
    )


def _preflight_spec_action(proposal, runner, command) -> dict | None:
    """Read-only checks before a spec mutation. Returns a blocked result or None."""
    data = _read_deployment(proposal, runner)
    if data is None:
        return _blocked(proposal, "live", command, "deployment_unreadable",
                        "Blocked: the target deployment could not be read.")
    meta = data.get("metadata") or {}
    labels = meta.get("labels") or {}
    if (
        meta.get("name") != proposal.deployment
        or meta.get("namespace") != proposal.namespace
    ):
        return _blocked(proposal, "live", command, "resource_identity_mismatch",
                        "Blocked: deployment identity does not match the approved action.")
    if not any(labels.get(label) == "true" for label in lab_known_good.OWNERSHIP_LABELS):
        return _blocked(
            proposal, "live", command, "not_a_lab_scenario_resource",
            "Blocked: deployment carries none of the lab ownership labels "
            f"({', '.join(lab_known_good.OWNERSHIP_LABELS)}).")
    spec = lab_known_good.get(proposal.deployment)
    if spec is None or _container(data, spec.container) is None:
        return _blocked(proposal, "live", command, "container_not_found",
                        "Blocked: the registered container does not exist on the deployment.")
    if (
        proposal.action == "restore_command"
        and proposal.deployment == "sim-memory-leak"
        and not _memory_leak_restore_preflight_matches(proposal, data)
    ):
        return _blocked(
            proposal, "live", command, "memory_leak_restore_preflight_failed",
            "Blocked: the target no longer matches the registered memory-leak "
            "simulator state.",
        )
    if proposal.target_replicas is not None and not (
        _disk_full_restore_preflight_matches(proposal, data)
    ):
        return _blocked(
            proposal, "live", command, "disk_full_restore_preflight_failed",
            "Blocked: the disk-full recovery target no longer matches the "
            "registered simulator state.",
        )
    if proposal.target_pod and not _disk_full_target_pod_matches(
        proposal, runner
    ):
        return _blocked(
            proposal, "live", command, "evicted_pod_identity_mismatch",
            "Blocked: the exact approved pod is no longer the registered "
            "Evicted pod owned by the disk-fill ReplicaSet.",
        )
    if _spec_matches(proposal, data) and not proposal.target_pod:
        return _blocked(proposal, "live", command, "already_in_desired_state",
                        "Blocked: the deployment already has the known-good spec.")
    return None


def _preflight_cleanup_action(proposal, runner, command) -> dict | None:
    data = _read_deployment(proposal, runner)
    if data is None:
        return _blocked(
            proposal, "live", command, "deployment_unreadable",
            "Blocked: the target deployment could not be read.",
        )
    meta = data.get("metadata") or {}
    labels = meta.get("labels") or {}
    if (
        meta.get("name") != proposal.deployment
        or meta.get("namespace") != proposal.namespace
    ):
        return _blocked(
            proposal, "live", command, "resource_identity_mismatch",
            "Blocked: deployment identity does not match the approved action.",
        )
    if not any(
        labels.get(label) == "true"
        for label in lab_known_good.OWNERSHIP_LABELS
    ):
        return _blocked(
            proposal, "live", command, "not_a_lab_scenario_resource",
            "Blocked: deployment carries none of the lab ownership labels "
            f"({', '.join(lab_known_good.OWNERSHIP_LABELS)}).",
        )
    if not _disk_full_cleanup_preflight_matches(proposal, data):
        return _blocked(
            proposal, "live", command, "disk_full_cleanup_preflight_failed",
            "Blocked: cleanup requires the registered disk-fill spec and "
            "exactly one ready, available replica.",
        )
    if not _disk_full_target_pod_matches(proposal, runner):
        return _blocked(
            proposal, "live", command, "evicted_pod_identity_mismatch",
            "Blocked: the exact approved pod is no longer the registered "
            "Evicted pod owned by the disk-fill ReplicaSet.",
        )
    return None


def execute_action(
    proposal: ProposedAction,
    *,
    mode: str | None = None,
    runner: Runner | None = None,
    config: PolicyConfig | None = None,
) -> dict:
    """Execute (or dry-run) one validated action. Never raises for tool errors."""
    if not isinstance(proposal, ProposedAction):
        raise TypeError("execute_action requires a validated ProposedAction")

    config = config or PolicyConfig.from_env()
    mode = (mode or get_mode()).strip().lower()
    runner = runner or default_runner

    # Defence in depth: re-check scope even though policy already ran.
    if (
        proposal.action not in ALLOWED_ACTIONS
        or proposal.namespace not in config.allowed_namespaces
        or proposal.deployment not in config.allowed_deployments
    ):
        return _result(proposal, mode, False, None, ok=False,
                       error="Action is outside the allowed scope.")

    try:
        argv = build_command(proposal)
    except ValueError as exc:
        return _blocked(proposal, mode, None, "no_registered_fix", str(exc))
    command = [
        "kubernetes_api" if proposal.action == "cleanup_evicted_pod"
        else "kubectl",
        *argv,
    ]

    if mode == "dry_run":
        return _result(proposal, "dry_run", False, command, ok=True,
                       stdout="DRY RUN: command not executed.")
    if mode != "live":
        return _blocked(
            proposal, mode, command, "invalid_mode",
            f"Execution blocked: KYBERNAUT_REMEDIATION_MODE={mode!r} is invalid "
            "(expected 'dry_run' or 'live'). Nothing was executed.")

    allowed_context = "kind-kybernaut"
    try:
        ctx = runner(["config", "current-context"])
    except Exception as exc:  # noqa: BLE001 - surfaced in the result
        return _result(proposal, mode, False, command, ok=False,
                       error=f"Could not determine kubectl context: {type(exc).__name__}")
    context_name = (ctx.stdout or "").strip()
    if ctx.returncode != 0 or context_name != allowed_context:
        return _blocked(
            proposal, mode, command, "invalid_context",
            f"Refusing live execution: kubectl context "
            f"{context_name!r} is not {allowed_context!r}.")

    if proposal.action in lab_known_good.SPEC_ACTIONS:
        try:
            refusal = _preflight_spec_action(proposal, runner, command)
        except Exception as exc:  # noqa: BLE001 - read-only; nothing mutated
            return _blocked(proposal, mode, command, "preflight_failed",
                            f"Blocked: preflight read failed: {type(exc).__name__}")
        if refusal is not None:
            return refusal
    elif proposal.action == "cleanup_evicted_pod":
        try:
            refusal = _preflight_cleanup_action(proposal, runner, command)
        except Exception as exc:  # noqa: BLE001 - read-only; nothing mutated
            return _blocked(
                proposal, "live", command, "preflight_failed",
                f"Blocked: cleanup preflight read failed: {type(exc).__name__}",
            )
        if refusal is not None:
            return refusal

    if proposal.action == "cleanup_evicted_pod":
        try:
            cleanup = _delete_target_pod_with_uid_precondition(proposal)
        except CommandNotStartedError as exc:
            return _result(
                proposal, mode, False, command, ok=False,
                error=f"Kubernetes API proxy was not started: {exc}",
            )
        except Exception as exc:  # noqa: BLE001
            return {
                **_result(
                    proposal, mode, True, command, ok=False,
                    error=(
                        "Exact evicted-pod cleanup outcome unknown: "
                        f"{type(exc).__name__}"
                    ),
                ),
                "outcome_unknown": True,
            }
        if cleanup.returncode != 0:
            return {
                **_result(
                    proposal, mode, True, command, ok=False,
                    returncode=cleanup.returncode,
                    stderr=(cleanup.stderr or "").strip(),
                    error="Exact evicted-pod cleanup failed.",
                ),
                "cleanup_executed": True,
                "cleanup_returncode": cleanup.returncode,
                "cleanup_stderr": (cleanup.stderr or "").strip(),
            }
        return {
            **_result(
                proposal, mode, True, command, ok=True,
                returncode=cleanup.returncode,
                stdout=(cleanup.stdout or "").strip(),
            ),
            "cleanup_executed": True,
            "target_pod_delete_accepted": proposal.target_pod,
            "target_pod_uid": proposal.target_pod_uid,
        }

    # Pin every live kubectl mutation to the allowed context.
    argv = [*argv, "--context", LIVE_CONTEXT]
    command = ["kubectl", *argv]
    try:
        completed = runner(argv)
    except CommandNotStartedError as exc:
        return _result(
            proposal,
            mode,
            False,
            command,
            ok=False,
            error=f"kubectl process was not started: {exc}",
        )
    except Exception as exc:  # noqa: BLE001
        return {
            **_result(
                proposal,
                mode,
                True,
                command,
                ok=False,
                error=(
                    "Execution outcome unknown after kubectl invocation "
                    f"began: {type(exc).__name__}"
                ),
            ),
            "outcome_unknown": True,
        }

    ok = completed.returncode == 0
    if ok and proposal.action in lab_known_good.SPEC_ACTIONS:
        # The patch call succeeding is not proof the spec changed: read it back.
        try:
            applied = _spec_matches(proposal, _read_deployment(proposal, runner) or {})
        except Exception:  # noqa: BLE001
            applied = False
        if not applied:
            return _result(
                proposal, mode, True, command, ok=False,
                returncode=completed.returncode,
                stdout=(completed.stdout or "").strip(),
                stderr=(completed.stderr or "").strip(),
                error="kubectl succeeded but the known-good spec was not "
                      "confirmed on the deployment.",
            )
        if proposal.target_pod:
            try:
                cleanup = _delete_target_pod_with_uid_precondition(proposal)
            except CommandNotStartedError as exc:
                return {
                    **_result(
                        proposal, mode, True, command, ok=False,
                        returncode=completed.returncode,
                        stdout=(completed.stdout or "").strip(),
                        stderr=(completed.stderr or "").strip(),
                        error=f"Could not start exact evicted-pod cleanup: {exc}",
                    ),
                    "spec_confirmed": True,
                    "cleanup_request": {
                        "method": "DELETE",
                        "resource": proposal.target_pod,
                        "uid_precondition": proposal.target_pod_uid,
                    },
                    "cleanup_executed": False,
                }
            except Exception as exc:  # noqa: BLE001
                return {
                    **_result(
                        proposal, mode, True, command, ok=False,
                        returncode=completed.returncode,
                        stdout=(completed.stdout or "").strip(),
                        stderr=(completed.stderr or "").strip(),
                        error=(
                            "Exact evicted-pod cleanup outcome unknown: "
                            f"{type(exc).__name__}"
                        ),
                    ),
                    "spec_confirmed": True,
                    "cleanup_request": {
                        "method": "DELETE",
                        "resource": proposal.target_pod,
                        "uid_precondition": proposal.target_pod_uid,
                    },
                    "cleanup_executed": True,
                    "outcome_unknown": True,
                }
            if cleanup.returncode != 0:
                return {
                    **_result(
                        proposal, mode, True, command, ok=False,
                        returncode=completed.returncode,
                        stdout=(completed.stdout or "").strip(),
                        stderr=(completed.stderr or "").strip(),
                        error="Exact evicted-pod cleanup failed.",
                    ),
                    "spec_confirmed": True,
                    "cleanup_request": {
                        "method": "DELETE",
                        "resource": proposal.target_pod,
                        "uid_precondition": proposal.target_pod_uid,
                    },
                    "cleanup_executed": True,
                    "cleanup_returncode": cleanup.returncode,
                    "cleanup_stderr": (cleanup.stderr or "").strip(),
                }
            return {
                **_result(
                    proposal, mode, True, command, ok=True,
                    returncode=completed.returncode,
                    stdout=(completed.stdout or "").strip(),
                    stderr=(completed.stderr or "").strip(),
                ),
                "spec_confirmed": True,
                "cleanup_request": {
                    "method": "DELETE",
                    "resource": proposal.target_pod,
                    "uid_precondition": proposal.target_pod_uid,
                },
                "cleanup_executed": True,
                "cleanup_returncode": cleanup.returncode,
                "target_pod_removed": proposal.target_pod,
                "target_pod_uid": proposal.target_pod_uid,
            }
        return {**_result(
            proposal, mode, True, command, ok=True,
            returncode=completed.returncode,
            stdout=(completed.stdout or "").strip(),
            stderr=(completed.stderr or "").strip(),
        ), "spec_confirmed": True}
    return _result(
        proposal, mode, True, command, ok=ok,
        returncode=completed.returncode,
        stdout=(completed.stdout or "").strip(),
        stderr=(completed.stderr or "").strip(),
        error=None if ok else "kubectl returned a non-zero exit code.",
    )


def _result(proposal, mode, executed, command, *, ok, returncode=None,
            stdout="", stderr="", error=None) -> dict:
    return {
        "action": proposal.action,
        "namespace": proposal.namespace,
        "resource": proposal.resource,
        "deployment": proposal.deployment,
        "mode": mode,
        "executed": executed,
        "command": command,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "ok": ok,
        "error": error,
    }