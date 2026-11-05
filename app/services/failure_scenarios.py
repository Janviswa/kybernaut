"""Controlled, repeatable failure scenarios for the Kind lab (guide step 7).

Safety boundaries (enforced in code, not just documented):
  * namespace is fixed to ``kybernaut-lab``;
  * the kubectl context must be exactly ``kind-kybernaut``;
  * only fixed kubectl argument vectors are produced -- no shell;
  * ``dry_run=True`` returns the commands without running anything;
  * cleanup is namespace-scoped; self-healing scenarios may have no cleanup
    mutation to perform.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

NAMESPACE = "kybernaut-lab"
CONTEXT_NAME = "kind-kybernaut"
HEALTHY_IMAGE = "nginx:1.28-alpine"
BROKEN_IMAGE = "nginx:kybernaut-intentionally-missing"
POD_CRASH_VERIFY_TIMEOUT_SECONDS = 30
POD_CRASH_VERIFY_INTERVAL_SECONDS = 1
POD_CRASH_IDENTITY_TIMEOUT_SECONDS = 5
POD_CRASH_EVIDENCE_LOOKBACK = timedelta(minutes=5)
POD_CRASH_DIAGNOSTIC_LIMIT = 1000
POD_CRASH_PID1_IDENTITY_SCRIPT = (
    'IFS= read -r process < /proc/1/comm && printf "%s\\n" "$process"'
)


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    incident_type: str
    description: str
    prerequisites: list[str]
    inject: list[list[str]]
    cleanup: list[list[str]]
    expected_telemetry: str
    expected_detection: str
    observed_resource_prefix: str | None = None
    expected_incident_types: frozenset[str] = frozenset()
    prepare: list[list[str]] | None = None


def _scale(dep: str, n: int) -> list[str]:
    return ["scale", f"deployment/{dep}", f"--replicas={n}", "-n", NAMESPACE]


def _patch_simulator(
    deployment: str, container: str, command: list[str], image: str,
) -> list[str]:
    patch = {
        "spec": {
            "template": {
                "spec": {
                    "containers": [{
                        "name": container,
                        "image": image,
                        "command": command,
                    }],
                },
            },
        },
    }
    return [
        "patch", f"deployment/{deployment}", "-n", NAMESPACE,
        "--type=strategic", "-p", json.dumps(patch, separators=(",", ":")),
    ]


SCENARIOS: dict[str, Scenario] = {
    "cpu-spike": Scenario(
        "cpu-spike", "cpu_spike",
        "Run a CPU-burning pod capped at 300m CPU.",
        ["sim-workloads.yaml applied", "Kind cluster running"],
        [_scale("sim-cpu-stress", 1)], [_scale("sim-cpu-stress", 0)],
        "container CPU usage pinned at its 300m limit; throttling",
        "cpu_cores_used flagged HIGH by the anomaly policy",
    ),
    "memory-leak": Scenario(
        "memory-leak", "memory_leak",
        "Grow memory ~1MiB/s to the 96Mi limit, causing OOMKilled restarts.",
        ["sim-workloads.yaml applied"],
        [_scale("sim-memory-leak", 2)], [_scale("sim-memory-leak", 0)],
        "rising memory, OOMKilled termination, increasing restart count",
        "CrashLoopBackOff / OOMKilled surfaced by the Kubernetes observer",
        "sim-memory-leak",
        frozenset({"pod_container_failure"}),
        [[
            *_patch_simulator(
                "sim-memory-leak", "leak",
                [
                    "sh", "-c",
                    "a=''; while :; do a=\"$a$(head -c 1048576 /dev/zero | "
                    "tr '\\0' x)\"; sleep 1; done",
                ],
                "busybox:1.36",
            ),
        ]],
    ),
    "pod-crash": Scenario(
        "pod-crash", "pod_crash",
        "Run a bounded lab pod that exits non-zero and enters CrashLoopBackOff.",
        ["sim-workloads.yaml applied"],
        [_scale("sim-pod-crash", 1)], [_scale("sim-pod-crash", 0)],
        "sim-pod-crash container exits non-zero and Kubernetes restarts it",
        "CrashLoopBackOff on a sim-pod-crash pod",
        "sim-pod-crash",
        frozenset({"pod_container_failure"}),
        [[
            *_patch_simulator(
                "sim-pod-crash", "crash",
                ["sh", "-c", "sleep 2; exit 1"],
                "busybox:1.36",
            ),
        ]],
    ),
    "disk-full": Scenario(
        "disk-full", "disk_full",
        "Write into a 40Mi emptyDir until the pod is evicted.",
        ["sim-workloads.yaml applied"],
        [_scale("sim-disk-fill", 1)], [_scale("sim-disk-fill", 0)],
        "ephemeral-storage usage grows; pod evicted (Evicted / ephemeral storage)",
        "pod_failed with eviction evidence",
        "sim-disk-fill",
        frozenset({"pod_failed", "kubernetes_warning_event"}),
        [[
            *_patch_simulator(
                "sim-disk-fill", "filler",
                [
                    "sh", "-c",
                    "i=0; while :; do dd if=/dev/zero of=/scratch/f$i bs=1M "
                    "count=5 2>/dev/null; i=$((i+1)); sleep 2; done",
                ],
                "busybox:1.36",
            ),
        ]],
    ),
    "deployment-failure": Scenario(
        "deployment-failure", "deployment_failure",
        "Set demo-web to a non-existent image tag (ImagePullBackOff).",
        [f"demo-web currently uses {HEALTHY_IMAGE}"],
        [["set", "image", "deployment/demo-web",
          f"nginx={BROKEN_IMAGE}", "-n", NAMESPACE]],
        [["set", "image", "deployment/demo-web",
          f"nginx={HEALTHY_IMAGE}", "-n", NAMESPACE],
         ["rollout", "status", "deployment/demo-web", "-n", NAMESPACE,
          "--timeout=120s"]],
        "ImagePullBackOff / ErrImagePull; unavailable replicas",
        "pod_container_failure + deployment_unavailable_replicas",
        "demo-web",
        frozenset({
            "pod_container_failure",
            "deployment_unavailable_replicas",
        }),
    ),
}


def scenario_detected(name: str, incidents: list[dict]) -> bool:
    """Return whether observation contains evidence for the requested scenario."""
    scenario = SCENARIOS[name]
    prefix = scenario.observed_resource_prefix
    if prefix is None:
        return False
    return any(
        incident.get("type") in scenario.expected_incident_types
        and isinstance(incident.get("resource"), str)
        and (
            incident["resource"] == prefix
            or incident["resource"].startswith(f"{prefix}-")
        )
        for incident in incidents
    )


class SimulatorError(RuntimeError):
    pass


Runner = Callable[..., subprocess.CompletedProcess]


def _default_runner(
    args: list[str], *, timeout: float = 120,
) -> subprocess.CompletedProcess:
    return subprocess.run(["kubectl", *args], capture_output=True, text=True,
                          timeout=timeout, check=False, shell=False)


def _run_command(
    runner: Runner, args: list[str], *, timeout: float = 120,
) -> subprocess.CompletedProcess:
    try:
        return runner(args, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        command = "kubectl " + " ".join(args)
        raise SimulatorError(f"{command} timed out.") from exc


def _contextual_args(args: list[str]) -> list[str]:
    if any(arg == "--context" or arg.startswith("--context=") for arg in args):
        raise SimulatorError(
            "Scenario command must not provide or override kubectl context."
        )
    return ["--context", CONTEXT_NAME, *args]


def assert_lab_context(runner: Runner) -> str:
    result = _run_command(
        runner,
        _contextual_args(["config", "current-context"]),
    )
    ctx = (result.stdout or "").strip()
    if result.returncode != 0 or ctx != CONTEXT_NAME:
        raise SimulatorError(
            f"Refusing to run: kubectl context {ctx!r} is not "
            f"{CONTEXT_NAME!r}.")
    return ctx


def _first_ready_pod(runner: Runner) -> dict:
    r = _run_command(
        runner,
        _contextual_args([
            "get", "pods", "-n", NAMESPACE, "-l", "app=demo-web",
            "-o", "json",
        ]),
    )
    if r.returncode != 0:
        raise SimulatorError(
            f"Could not inspect demo-web pods: {(r.stderr or '').strip()}"
        )
    try:
        items = json.loads(r.stdout or "{}").get("items", [])
    except (json.JSONDecodeError, AttributeError) as exc:
        raise SimulatorError("Could not parse demo-web pod status.") from exc
    pods = []
    for pod in items:
        metadata = pod.get("metadata", {})
        status = pod.get("status", {})
        container_statuses = status.get("containerStatuses", [])
        if (
            isinstance(metadata.get("name"), str)
            and metadata["name"].startswith("demo-web-")
            and status.get("phase") == "Running"
            and container_statuses
            and all(item.get("ready") is True for item in container_statuses)
        ):
            pod_uid = metadata.get("uid")
            if not isinstance(pod_uid, str) or not pod_uid:
                continue
            restart_counts = {
                item.get("name"): item.get("restartCount", 0)
                for item in container_statuses
                if isinstance(item.get("name"), str)
                and isinstance(item.get("restartCount", 0), int)
            }
            last_terminations = {
                item["name"]: (
                    (item.get("lastState") or {}).get("terminated")
                )
                for item in container_statuses
                if isinstance(item.get("name"), str)
            }
            spec_containers = {
                item.get("name")
                for item in (pod.get("spec") or {}).get("containers", [])
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }
            ready_containers = {
                item.get("name")
                for item in container_statuses
                if isinstance(item, dict)
                and item.get("ready") is True
                and isinstance(item.get("name"), str)
                and item.get("name") in spec_containers
            }
            pods.append({
                "name": metadata["name"],
                "uid": pod_uid,
                "restart_counts": restart_counts,
                "last_terminations": last_terminations,
                "ready_containers": ready_containers,
            })
    if len(pods) < 2:
        raise SimulatorError(
            "Need >= 2 ready demo-web pods before crashing one."
        )
    selected = pods[0]
    if len(selected["ready_containers"]) != 1:
        raise SimulatorError(
            f"Expected exactly one declared Ready container in Pod "
            f"{selected['name']}; found "
            f"{len(selected['ready_containers'])}. Refusing pod-crash "
            "injection."
        )
    selected["container"] = next(iter(selected["ready_containers"]))
    return selected


def _expand(cmds: list[list[str]], runner: Runner | None) -> list[list[str]]:
    out = []
    for cmd in cmds:
        if "{POD}" in cmd:
            if runner is None:
                cmd = [
                    "<first demo-web pod>" if c == "{POD}" else
                    "<validated container>" if c == "{CONTAINER}" else c
                    for c in cmd
                ]
                out.append(cmd)
                continue
            pod = _first_ready_pod(runner)
            cmd = [
                pod["name"] if c == "{POD}" else
                pod["container"] if c == "{CONTAINER}" else c
                for c in cmd
            ]
        out.append(cmd)
    return out


def _sanitize_diagnostic(value: object) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value or "")
    text = "".join(
        char if char.isprintable() or char in "\n\t" else " "
        for char in text
    )
    text = re.sub(
        r"(?im)(\bauthorization\b\s*[:=]\s*)[^\r\n]*",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)\b(password|passwd|token|secret|api[_-]?key)"
        r"(\s*[:=]\s*)(\S+)",
        r"\1\2[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [REDACTED]", text)
    if len(text) > POD_CRASH_DIAGNOSTIC_LIMIT:
        text = text[:POD_CRASH_DIAGNOSTIC_LIMIT] + "...[truncated]"
    return text


def _exec_diagnostics(result: subprocess.CompletedProcess) -> str:
    return _format_command_diagnostics(
        result.returncode, result.stdout, result.stderr
    )


def _format_command_diagnostics(
    returncode: object, stdout: object, stderr: object,
    *, prefix: str = "exec",
) -> str:
    stdout = _sanitize_diagnostic(stdout)
    stderr = _sanitize_diagnostic(stderr)
    return (
        f"{prefix}_returncode={returncode}; "
        f"{prefix}_stdout={stdout!r}; {prefix}_stderr={stderr!r}"
    )


def _run_pod_crash_command(
    runner: Runner, command: list[str], *, timeout: float,
    stage: str,
) -> subprocess.CompletedProcess:
    try:
        return runner(command, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        diagnostics = _format_command_diagnostics(
            "timeout", exc.stdout, exc.stderr, prefix=stage.replace(" ", "_")
        )
        raise SimulatorError(
            f"Pod-crash {stage} command timed out. {diagnostics}"
        ) from exc


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _pod_crash_evidence(
    runner: Runner, pod: dict, injection_started_at: datetime,
    *, timeout: float,
) -> dict | None:
    result = _run_command(
        runner,
        _contextual_args([
            "get", "pod", pod["name"], "-n", NAMESPACE, "-o", "json",
        ]),
        timeout=timeout,
    )
    if result.returncode != 0:
        raise SimulatorError(
            f"Could not verify pod-crash evidence for {pod['name']}: "
            f"{(result.stderr or '').strip()}"
        )
    try:
        current_pod = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise SimulatorError(
            f"Could not parse status for pod {pod['name']}."
        ) from exc

    metadata = current_pod.get("metadata") or {}
    if metadata.get("uid") != pod["uid"]:
        raise SimulatorError(
            f"Pod {pod['name']} was replaced before crash evidence could be "
            "verified; refusing to attribute evidence to a different Pod UID."
        )

    now = datetime.now(timezone.utc)
    status = current_pod.get("status") or {}
    for container in status.get("containerStatuses", []):
        container_name = container.get("name")
        if container_name != pod["container"]:
            continue
        restart_count = container.get("restartCount")
        previous_count = pod["restart_counts"].get(container_name, 0)
        terminated = (
            (container.get("lastState") or {}).get("terminated") or {}
        )
        previous_termination = pod["last_terminations"].get(container_name)
        finished_at = _parse_timestamp(terminated.get("finishedAt"))
        exit_code = terminated.get("exitCode")
        fresh_termination = (
            terminated != previous_termination
            and finished_at is not None
            and injection_started_at <= finished_at <= now
            and now - finished_at <= POD_CRASH_EVIDENCE_LOOKBACK
            and isinstance(exit_code, int)
            and exit_code != 0
        )
        if (
            isinstance(restart_count, int)
            and restart_count > previous_count
            and fresh_termination
        ):
            return {
                "pod_uid": pod["uid"],
                "container": container_name,
                "restart_count": restart_count,
                "finished_at": finished_at.isoformat(),
                "evidence": "restart_count_increased",
            }
        if fresh_termination:
            return {
                "pod_uid": pod["uid"],
                "container": container_name,
                "exit_code": exit_code,
                "finished_at": finished_at.isoformat(),
                "evidence": "recent_nonzero_termination",
            }
    return None


def _verify_pod_crash(
    runner: Runner, pod: dict, injection_started_at: datetime,
) -> dict:
    deadline = time.monotonic() + POD_CRASH_VERIFY_TIMEOUT_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SimulatorError(
                f"Timed out waiting for pod-crash evidence for Pod "
                f"{pod['name']} (UID {pod['uid']})."
            )
        evidence = _pod_crash_evidence(
            runner, pod, injection_started_at, timeout=remaining
        )
        if time.monotonic() >= deadline:
            raise SimulatorError(
                f"Timed out waiting for pod-crash evidence for Pod "
                f"{pod['name']} (UID {pod['uid']})."
            )
        if evidence is not None:
            return evidence
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SimulatorError(
                f"pod-crash injection did not produce a restart or recent "
                f"nonzero termination for Pod {pod['name']} "
                f"(UID {pod['uid']}); no injection success is reported."
            )
        time.sleep(min(POD_CRASH_VERIFY_INTERVAL_SECONDS, remaining))


def run_scenario(name: str, phase: str, *, dry_run: bool = True,
                 runner: Runner | None = None) -> dict:
    if name not in SCENARIOS:
        raise SimulatorError(f"Unknown scenario {name!r}")
    if phase not in {"inject", "cleanup"}:
        raise SimulatorError("phase must be inject or cleanup")

    sc = SCENARIOS[name]
    cmds = (
        [*(sc.prepare or []), *sc.inject]
        if phase == "inject" else sc.cleanup
    )
    if dry_run:
        planned_commands = _expand(cmds, None)
        planned_commands = [
            _contextual_args(command) for command in planned_commands
        ]
        return {"scenario": name, "phase": phase, "dry_run": True,
                "commands": [["kubectl", *command]
                             for command in planned_commands]}

    runner = runner or _default_runner
    ctx = assert_lab_context(runner)
    executed = []
    expanded_cmds = _expand(cmds, runner)

    for argv in expanded_cmds:
        command = _contextual_args(argv)
        res = _run_command(runner, command)
        executed.append({"command": ["kubectl", *command],
                         "returncode": res.returncode,
                         "stderr": (res.stderr or "").strip()})
        if res.returncode != 0:
            raise SimulatorError(
                f"kubectl {' '.join(argv)} failed: "
                f"{(res.stderr or '').strip()}"
            )
    result = {"scenario": name, "phase": phase, "dry_run": False,
              "context": ctx, "executed": executed}
    return result