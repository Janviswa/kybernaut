#!/usr/bin/env python3
"""Create and inspect the controlled remediation lab scenarios (Kind only).

    python scripts/remediation_lab.py preload
    python scripts/remediation_lab.py apply
    python scripts/remediation_lab.py up bad-image|crash-loop|disk-evict
    python scripts/remediation_lab.py status
    python scripts/remediation_lab.py reset bad-image|crash-loop|disk-evict

Refuses to run unless the kubectl context is exactly kind-kybernaut. Touches
only the three dedicated sim-* deployments in kybernaut-lab. No shell is used
and nothing here accepts arbitrary kubectl arguments.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CONTEXT = "kind-kybernaut"
NAMESPACE = "kybernaut-lab"
KIND_CLUSTER = "kybernaut"
IMAGE = "busybox:1.36"
MANIFEST = Path(__file__).resolve().parents[1] / "k8s" / "lab" / "remediation-scenarios.yaml"
SCENARIOS = {
    "bad-image": "sim-bad-image",
    "crash-loop": "sim-crash-loop",
    "disk-evict": "sim-disk-evict",
}


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), text=True, capture_output=True,
                          check=check, timeout=120, shell=False)


def kubectl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run("kubectl", "--context", CONTEXT, *args, check=check)


def require_context() -> None:
    current = run("kubectl", "config", "current-context", check=False).stdout.strip()
    if current != CONTEXT:
        sys.exit(f"Refusing: current kubectl context is {current!r}, not {CONTEXT!r}.")


def deployment(name_key: str) -> str:
    if name_key not in SCENARIOS:
        sys.exit(f"Unknown scenario {name_key!r}; choose one of {', '.join(SCENARIOS)}.")
    return SCENARIOS[name_key]


def pods(name: str) -> str:
    return kubectl("-n", NAMESPACE, "get", "pods", "-l", f"app={name}", "-o", "wide",
                   check=False).stdout


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    command, rest = argv[0], argv[1:]
    require_context()
    if command == "preload":
        print(run("docker", "pull", IMAGE).stdout)
        print(run("kind", "load", "docker-image", IMAGE, "--name", KIND_CLUSTER).stdout)
    elif command == "apply":
        print(kubectl("apply", "-f", str(MANIFEST)).stdout)
    elif command == "up" and rest:
        name = deployment(rest[0])
        kubectl("apply", "-f", str(MANIFEST))
        kubectl("-n", NAMESPACE, "scale", f"deployment/{name}", "--replicas=1")
        if rest[0] == "disk-evict":
            kubectl("-n", NAMESPACE, "rollout", "status", f"deployment/{name}", "--timeout=90s")
            pod = kubectl("-n", NAMESPACE, "get", "pods", "-l", f"app={name}", "-o",
                          "jsonpath={.items[0].metadata.name}").stdout.strip()
            # ONE bounded write: 60Mi into a 40Mi emptyDir -> kubelet eviction.
            kubectl("-n", NAMESPACE, "exec", pod, "--", "sh", "-c",
                    "dd if=/dev/zero of=/scratch/fill bs=1M count=60 2>/dev/null; sleep 1",
                    check=False)
        print(f"{name} started. Failure appears within ~1 minute.\n{pods(name)}")
    elif command == "status":
        for key, name in SCENARIOS.items():
            print(f"== {key} ({name})\n{pods(name)}")
    elif command == "reset" and rest:
        name = deployment(rest[0])
        kubectl("-n", NAMESPACE, "scale", f"deployment/{name}", "--replicas=0")
        # Re-apply the broken spec so the scenario is repeatable.
        kubectl("apply", "-f", str(MANIFEST))
        print(f"{name} scaled to 0 and its broken spec restored.")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))