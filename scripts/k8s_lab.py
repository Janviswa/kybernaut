#!/usr/bin/env python3
"""Safe, namespace-scoped Kubernetes failure simulator for Kybernaut."""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

NAMESPACE = "kybernaut-lab"
DEPLOYMENT = "demo-web"
HEALTHY_IMAGE = "nginx:1.28-alpine"
BROKEN_IMAGE = "nginx:kybernaut-intentionally-missing"
ROOT = Path(__file__).resolve().parents[1]
LOG_FILE = ROOT / "logs" / "k8s-lab-events.jsonl"


def kubectl(*args, check=True):
    return subprocess.run(
        ["kubectl", *args],
        check=check,
        text=True,
        capture_output=True,
        timeout=90,
    )


def record(event, details):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "namespace": NAMESPACE,
        "deployment": DEPLOYMENT,
        "details": details,
    }
    with LOG_FILE.open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry) + "\n")
    print(json.dumps(entry, indent=2))


def verify_lab():
    result = kubectl(
        "get", "deployment", DEPLOYMENT, "-n", NAMESPACE,
        "-o", "json",
    )
    deployment = json.loads(result.stdout)
    if deployment.get("metadata", {}).get("namespace") != NAMESPACE:
        raise RuntimeError("Deployment namespace verification failed.")
    return deployment


def status():
    verify_lab()
    for resource in ("deployments", "pods", "events"):
        result = kubectl(
            "get", resource, "-n", NAMESPACE, "-o", "wide",
            check=False,
        )
        print(f"\n===== {resource.upper()} =====")
        print(result.stdout or result.stderr)


def crash_pod():
    verify_lab()
    result = kubectl(
        "get", "pods", "-n", NAMESPACE,
        "-l", "app=demo-web", "-o", "json",
    )
    pods = json.loads(result.stdout).get("items", [])
    ready_pods = [
        pod for pod in pods
        if pod.get("status", {}).get("phase") == "Running"
        and all(
            c.get("ready", False)
            for c in pod.get("status", {}).get("containerStatuses", [])
        )
    ]
    if len(ready_pods) < 2:
        raise RuntimeError("Need two healthy replicas before this test.")

    name = ready_pods[0]["metadata"]["name"]
    kubectl("delete", "pod", name, "-n", NAMESPACE)
    kubectl(
        "rollout", "status", f"deployment/{DEPLOYMENT}",
        "-n", NAMESPACE, "--timeout=120s",
    )
    record("pod_deleted_and_recovered", {"deleted_pod": name})


def bad_image():
    deployment = verify_lab()
    current_image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
    if current_image != HEALTHY_IMAGE:
        raise RuntimeError(
            f"Unexpected current image {current_image!r}; "
            "restore the known healthy image before proceeding."
        )

    kubectl(
        "set", "image", f"deployment/{DEPLOYMENT}",
        f"nginx={BROKEN_IMAGE}", "-n", NAMESPACE,
    )
    record(
        "bad_image_injected",
        {
            "previous_image": HEALTHY_IMAGE,
            "injected_image": BROKEN_IMAGE,
            "expected_signal": "ImagePullBackOff or ErrImagePull",
        },
    )
    print(
        "\nFailure injected. Observe with 'status'. "
        "Run 'restore' immediately after collecting evidence."
    )


def restore():
    verify_lab()
    kubectl(
        "set", "image", f"deployment/{DEPLOYMENT}",
        f"nginx={HEALTHY_IMAGE}", "-n", NAMESPACE,
    )
    kubectl(
        "rollout", "status", f"deployment/{DEPLOYMENT}",
        "-n", NAMESPACE, "--timeout=120s",
    )
    record("healthy_image_restored", {"image": HEALTHY_IMAGE})


def main():
    if len(sys.argv) != 2:
        print("Usage: python scripts/k8s_lab.py status|crash-pod|bad-image|restore")
        return 2

    actions = {
        "status": status,
        "crash-pod": crash_pod,
        "bad-image": bad_image,
        "restore": restore,
    }
    action = actions.get(sys.argv[1])
    if action is None:
        print("Unknown action. Choose status, crash-pod, bad-image, or restore.")
        return 2

    try:
        action()
    except (subprocess.SubprocessError, RuntimeError, KeyError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
