"""Known-good specs for the designated synthetic lab workloads.

The two spec-correcting remediation actions (``set_image`` and
``restore_command``) never accept an image or command from a request, an
LLM or the frontend. They can only apply the values registered here, and only
to the dedicated lab deployments listed here. This keeps "fix the broken
workload" a deterministic, reviewable operation instead of arbitrary patching.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Every registered deployment must carry this label=true, otherwise a live
# mutation is refused (resource ownership check).
OWNERSHIP_LABEL = "kybernaut.io/lab-scenario"
# The pre-existing failure simulators (k8s/lab/sim-workloads.yaml) are also
# lab-created and carry this label.
OWNERSHIP_LABELS = (OWNERSHIP_LABEL, "kybernaut.io/simulator")

_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,120}(:[A-Za-z0-9._-]{1,64})?$")
DEFAULT_GOOD_IMAGE = "busybox:1.36"


def _good_image() -> str:
    """Lab image; override only with ``KYBERNAUT_LAB_GOOD_IMAGE`` (validated).

    Pre-load it into Kind (``kind load docker-image``) so no external
    registry access is needed.
    """
    value = os.getenv("KYBERNAUT_LAB_GOOD_IMAGE", DEFAULT_GOOD_IMAGE).strip()
    return value if _IMAGE.match(value) else DEFAULT_GOOD_IMAGE


@dataclass(frozen=True)
class KnownGood:
    namespace: str
    container: str
    command: tuple[str, ...]
    fixes: tuple[str, ...]            # actions allowed for this deployment
    scenario: str
    expected_command: tuple[str, ...] | None = None
    expected_image: str | None = None

    @property
    def image(self) -> str:
        return _good_image()


_HEALTHY_COMMAND = ("sh", "-c", "while :; do sleep 30; done")
_DISK_FILL_COMMAND = (
    "sh", "-c",
    "i=0; while :; do dd if=/dev/zero of=/scratch/f$i bs=1M "
    "count=5 2>/dev/null; i=$((i+1)); sleep 2; done",
)
_MEMORY_LEAK_COMMAND = (
    "sh", "-c",
    "a=''; while :; do a=\"$a$(head -c 1048576 /dev/zero | "
    "tr '\\0' x)\"; sleep 1; done",
)

KNOWN_GOOD: dict[str, KnownGood] = {
    # Scenario A: deliberately invalid image reference.
    "sim-bad-image": KnownGood(
        namespace="kybernaut-lab", container="app", command=_HEALTHY_COMMAND,
        fixes=("set_image",), scenario="bad_image",
    ),
    # Existing simulator: `sleep 2; exit 1` crash loop.
    "sim-pod-crash": KnownGood(
        namespace="kybernaut-lab", container="crash", command=_HEALTHY_COMMAND,
        fixes=("restore_command",), scenario="pod_crash",
    ),
    # Scenario B: command that exits non-zero immediately (CrashLoopBackOff).
    "sim-crash-loop": KnownGood(
        namespace="kybernaut-lab", container="app", command=_HEALTHY_COMMAND,
        fixes=("restore_command",), scenario="crash_loop",
    ),
    # Disk-full simulator: only replace its registered writer with an idle
    # command, and restore one replica through the same approved action.
    "sim-disk-fill": KnownGood(
        namespace="kybernaut-lab", container="filler",
        command=_HEALTHY_COMMAND, fixes=("restore_command",),
        scenario="disk_full", expected_command=_DISK_FILL_COMMAND,
        expected_image="busybox:1.36",
    ),
    "sim-memory-leak": KnownGood(
        namespace="kybernaut-lab", container="leak",
        command=_HEALTHY_COMMAND, fixes=("restore_command",),
        scenario="memory_leak", expected_command=_MEMORY_LEAK_COMMAND,
        expected_image="busybox:1.36",
    ),
}

SPEC_ACTIONS = ("set_image", "restore_command")


def get(deployment: str | None) -> KnownGood | None:
    return KNOWN_GOOD.get(deployment) if isinstance(deployment, str) else None


def has_fix(deployment: str | None, action: str) -> bool:
    entry = get(deployment)
    return bool(entry and action in entry.fixes)