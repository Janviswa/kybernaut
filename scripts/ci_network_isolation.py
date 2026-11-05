#!/usr/bin/env python3
"""Build and run the full pytest suite inside an isolated Docker network."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_SHA256 = (
    "461823143e2e58fb9f487347e59178205de9bba3513ce6fc65a52971fe04bff9"
)
SUPPORTED_BASE_IMAGES = {
    "3.11": "python@sha256:b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377",
    "3.12": "python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1",
}
DEBIAN_SNAPSHOT = "20261026T000000Z"
GIT_PACKAGE_VERSION = "1:2.47.3-0+deb13u1"
PYTEST_TIMEOUT_SECONDS = 600
PEER_PORT = 38241

PYTHON_SOURCE_ROOTS = ("app", "tests")
APPROVED_SCRIPT_FILES = (
    "scripts/analyze_anomaly_false_negatives.py",
    "scripts/ci_network_isolation.py",
    "scripts/database_recovery.py",
    "scripts/demo_incident.py",
    "scripts/diagnose_isolation_forest.py",
    "scripts/evaluate_anomaly_benchmark.py",
    "scripts/issue_operator_token.py",
    "scripts/run_benchmark.py",
    "scripts/run_synthetic_benchmark.py",
    "scripts/run_synthetic_rca_evaluation.py",
    "scripts/validate_junit_report.py",
    "scripts/validate_llm_evaluation_report.py",
    "scripts/validate_network_isolation_evidence.py",
)
APPROVED_SOURCE_FILES = (
    ".env.example",
    "README.md",
    ".github/workflows/ci.yml",
    "docker-compose.yml",
    "docker-compose.observability.yml",
    "requirements.txt",
    "benchmark/scenarios.json",
    "benchmark/llm_evaluation/reference_notes_v1.json",
    "benchmark/llm_evaluation/report_schema_v2.json",
    "benchmark/results/direction-aware-anomaly-benchmark.json",
    "benchmark/results/f29-anomaly-false-negative-analysis.json",
    "benchmark/results/isolation-forest-diagnostic-v1.json",
    "benchmark/results/three-way-anomaly-benchmark-v4.json",
    "config/application_workload_map.json",
    "config/pricing.example.json",
    "data/telemetry_baseline_v6.csv",
    "deploy/cron/kybernaut-weekly-cost.cron",
    "k8s/lab/sim-workloads.yaml",
    "monitoring/alloy/config.alloy",
    "monitoring/grafana/dashboards/kybernaut-overview.json",
    "monitoring/grafana/provisioning/dashboards.yml",
    "monitoring/grafana/provisioning/datasources.yml",
    "monitoring/loki/loki-config.yml",
    "monitoring/prometheus/prometheus.yml",
    "app/static/console/console.css",
    "app/static/console/console.js",
    "app/static/console/index.html",
    "runbooks/crashloopbackoff.md",
    "runbooks/database-timeout.md",
    "runbooks/deployment-failure.md",
    "runbooks/disk-full.md",
    "runbooks/high-cpu.md",
    "runbooks/memory-pressure.md",
    "runbooks/network-failure.md",
    "runbooks/oom-killed.md",
    "runbooks/pod-crash.md",
    "runbooks/pod-not-ready.md",
    "target_apps/inventory_service/Dockerfile",
    "target_apps/inventory_service/main.py",
    "target_apps/inventory_service/requirements.txt",
    "target_apps/order_service/Dockerfile",
    "target_apps/order_service/main.py",
    "target_apps/order_service/requirements.txt",
    "target_apps/payment_service/Dockerfile",
    "target_apps/payment_service/main.py",
    "target_apps/payment_service/requirements.txt",
    "target_apps/user_service/Dockerfile",
    "target_apps/user_service/main.py",
    "target_apps/user_service/requirements.txt",
)
APPROVED_INPUT_SHA256 = {
    "benchmark/results/direction-aware-anomaly-benchmark.json": (
        "7694af9b92a42fca5e4d1214f3ab3f529f8f5915c7e0657729e15c484307571d"
    ),
    "benchmark/results/f29-anomaly-false-negative-analysis.json": (
        "f2f1ae1082b0b637c52eacdcfeaa49f73f6e481e283224e69e9de2dd9df79e30"
    ),
    "benchmark/results/isolation-forest-diagnostic-v1.json": (
        "69ad102b110525a64b7fe5744054dd8d2ab78a4f2ab7aad941c04f6a2052c516"
    ),
    "data/telemetry_baseline_v6.csv": (
        "777724eb6aae20da2104fef327dd691ab31a542be06f0c45fc5f66d8cfcac1a1"
    ),
    "benchmark/results/three-way-anomaly-benchmark-v4.json": (
        "ac1aa9b43c6f819fc22c6f109fc98d2c53ac073dc6d346f0f44d44de11bdd61e"
    ),
    "deploy/cron/kybernaut-weekly-cost.cron": (
        "c01d2f9e803f023a077caee14979267d45b9f10424802124887b9dec09e62aed"
    ),
    "requirements.txt": (
        "461823143e2e58fb9f487347e59178205de9bba3513ce6fc65a52971fe04bff9"
    ),
}
DOTENV_EXAMPLE_NAME = "GRAFANA_ADMIN_PASSWORD"


def _run(
    command: list[str],
    *,
    timeout: float,
    check: bool = True,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        check=False,
        capture_output=capture_output,
        text=True,
        timeout=timeout,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}: "
            f"{command[0]} ({detail[-2000:]})"
        )
    return result


def _git_preflight(expected_revision: str, *, project_root: Path) -> dict[str, str]:
    if not isinstance(expected_revision, str) or re.fullmatch(
        r"(?:[0-9a-f]{40}|[0-9a-f]{64})", expected_revision
    ) is None:
        raise RuntimeError("An explicit valid expected source revision is required")
    try:
        revision = _run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            timeout=5,
            check=False,
        )
        worktree = _run(
            [
                "git", "-C", str(project_root), "status", "--porcelain=v1",
                "--untracked-files=all",
            ],
            timeout=5,
            check=False,
        )
        ignored_sources = _run(
            [
                "git", "-C", str(project_root), "ls-files", "--others",
                "--ignored", "--exclude-standard", "--", "app", "tests",
                *APPROVED_SCRIPT_FILES, *APPROVED_SOURCE_FILES,
            ],
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Could not establish clean source preflight") from exc
    if revision.returncode != 0 or worktree.returncode != 0 or ignored_sources.returncode != 0:
        raise RuntimeError("Git could not establish clean source preflight")
    actual_revision = revision.stdout.strip()
    if actual_revision != expected_revision:
        raise RuntimeError("Checked-out revision does not match expected revision")
    if worktree.stdout.strip():
        raise RuntimeError("Qualification worktree is not clean")
    approved_paths = set(APPROVED_SCRIPT_FILES) | set(APPROVED_SOURCE_FILES)
    for path in ignored_sources.stdout.splitlines():
        if (
            path in approved_paths
            or (
                path.startswith(("app/", "tests/"))
                and path.endswith(".py")
            )
        ):
            raise RuntimeError(
                "Ignored source exists in the qualification snapshot"
            )
    return {
        "expected_revision": expected_revision,
        "actual_revision": actual_revision,
        "worktree": "clean",
        "status": "passed",
    }


def _verify_requirements_lock(*, project_root: Path) -> bytes:
    try:
        lock_bytes = (project_root / "requirements.txt").read_bytes()
    except OSError as exc:
        raise RuntimeError("Approved requirements lock is missing or unreadable") from exc
    if hashlib.sha256(lock_bytes).hexdigest() != REQUIREMENTS_SHA256:
        raise RuntimeError("requirements.txt does not match the approved pinned lock checksum")
    return lock_bytes


def _cleanup_docker_resource(
    *,
    resource: str,
    identity: str,
    cleanup_command: list[str],
    verification_command: list[str],
    is_present: Callable[[str], bool],
) -> dict[str, Any]:
    cleanup_exit_code: int | None = None
    cleanup_diagnostic = ""
    try:
        cleanup = _run(cleanup_command, timeout=15, check=False)
        cleanup_exit_code = cleanup.returncode
        if cleanup.returncode != 0:
            cleanup_diagnostic = "cleanup command returned nonzero"
    except subprocess.TimeoutExpired:
        cleanup_diagnostic = "cleanup command timed out"
    except OSError:
        cleanup_diagnostic = "cleanup command could not execute"

    verification_exit_code: int | None = None
    verification_status = "unverified"
    verification_diagnostic = "resource verification did not complete"
    try:
        verification = _run(verification_command, timeout=10, check=False)
        verification_exit_code = verification.returncode
        if verification.returncode == 0:
            if is_present(verification.stdout):
                verification_status = "present"
                verification_diagnostic = "resource remains after cleanup"
            else:
                verification_status = "removed"
                verification_diagnostic = ""
        else:
            verification_diagnostic = "resource verification returned nonzero"
    except subprocess.TimeoutExpired:
        verification_diagnostic = "resource verification timed out"
    except OSError:
        verification_diagnostic = "resource verification could not execute"

    if verification_status == "present":
        status = "failed"
    elif verification_status != "removed" or cleanup_exit_code is None:
        status = "unverified"
    elif cleanup_exit_code != 0:
        status = "failed"
    else:
        status = "succeeded"
    return {
        "resource": resource,
        "identity": identity,
        "cleanup_attempted": True,
        "cleanup_exit_code": cleanup_exit_code,
        "cleanup_diagnostic": cleanup_diagnostic,
        "verification_exit_code": verification_exit_code,
        "verification_status": verification_status,
        "verification_diagnostic": verification_diagnostic,
        "status": status,
    }


def _cleanup_status(
    resources: list[dict[str, Any]],
    listeners: list[dict[str, Any]],
    *,
    run_id: str,
) -> str:
    expected_resources = {"peer_container", "pytest_container", "test_image"}
    resource_ids = [item.get("resource") for item in resources]
    resources_complete = (
        len(resource_ids) == len(expected_resources)
        and set(resource_ids) == expected_resources
        and all(item.get("status") == "verified" for item in resources)
    )
    expected_listeners = {"host-canary-listener", "pytest-child-listener"}
    listener_ids = [item.get("listener_id") for item in listeners]
    listeners_complete = (
        len(listener_ids) == len(expected_listeners)
        and set(listener_ids) == expected_listeners
        and all(
            item.get("run_id") == run_id
            and item.get("attempted") is True
            and item.get("socket_closed") is True
            and item.get("thread_stopped") is True
            and item.get("status") == "succeeded"
            and item.get("diagnostic") == ""
            for item in listeners
        )
    )
    statuses = [
        *(item["status"] for item in resources),
        *(
            "verified" if item["status"] == "succeeded" else item["status"]
            for item in listeners
        ),
    ]
    if any(status == "failed" for status in statuses):
        return "failed"
    if not resources_complete or not listeners_complete:
        return "unverified"
    if statuses and all(status == "verified" for status in statuses):
        return "verified"
    return "unverified"


def _docker_resource_labels(run_id: str, resource_id: str) -> tuple[str, str]:
    return (
        f"com.kybernaut.ci.run={run_id}",
        f"com.kybernaut.ci.resource={resource_id}",
    )


def _cleanup_labeled_docker_resource(
    *,
    resource: str,
    identity: str,
    run_id: str,
    labels: tuple[str, str],
    list_command: list[str],
    remove_command: list[str],
    expected_resource_id: str | None = None,
    operation_timed_out: bool = False,
) -> dict[str, Any]:
    cleanup_exit_code: int | None = None
    verification_exit_code: int | None = None
    cleanup_diagnostic = ""
    verification_diagnostic = "resource verification did not complete"
    verification_status = "unverified"
    resource_id = expected_resource_id
    if labels[0] != f"com.kybernaut.ci.run={run_id}":
        return {
            "resource": resource,
            "identity": identity,
            "run_id": run_id,
            "resource_id": expected_resource_id,
            "cleanup_attempted": False,
            "cleanup_exit_code": None,
            "cleanup_diagnostic": "cleanup labels do not match run identifier",
            "verification_exit_code": None,
            "verification_status": "unverified",
            "verification_diagnostic": "resource identity was not queried",
            "status": "unverified",
        }

    try:
        discovery = _run(list_command, timeout=10, check=False)
    except subprocess.TimeoutExpired:
        cleanup_diagnostic = "resource discovery timed out"
        discovery = None
    except OSError:
        cleanup_diagnostic = "resource discovery could not execute"
        discovery = None

    resource_ids: list[str] = []
    if discovery is not None:
        if discovery.returncode != 0:
            cleanup_diagnostic = "resource discovery returned nonzero"
        else:
            output_ids = discovery.stdout.splitlines()
            if any(
                re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", item) is None
                for item in output_ids
            ):
                cleanup_diagnostic = "resource discovery returned invalid identities"
            elif len(set(output_ids)) != len(output_ids):
                cleanup_diagnostic = "resource discovery returned duplicate identities"
            elif len(output_ids) > 1:
                cleanup_diagnostic = (
                    "multiple resources matched unique run labels; none removed"
                )
                verification_exit_code = 0
                verification_status = "present"
                verification_diagnostic = cleanup_diagnostic
                cleanup_exit_code = 1
            elif output_ids and expected_resource_id and (
                output_ids[0] != expected_resource_id
            ):
                cleanup_diagnostic = (
                    "discovered resource identity does not match created resource"
                )
                cleanup_exit_code = 1
                verification_exit_code = 0
                verification_status = "present"
                verification_diagnostic = cleanup_diagnostic
            else:
                resource_ids = output_ids

    if discovery is None or cleanup_diagnostic:
        status = (
            "failed"
            if cleanup_exit_code == 1
            else "unverified"
        )
    elif not resource_ids:
        verification_exit_code = 0
        status = "unverified"
        verification_status = "unverified"
        if operation_timed_out:
            cleanup_diagnostic = (
                "resource absent at bounded check after timeout; late creation "
                "cannot be excluded"
            )
        elif expected_resource_id is not None:
            cleanup_diagnostic = (
                "expected resource absent before cleanup; removal by this "
                "invocation cannot be established"
            )
        else:
            cleanup_diagnostic = "resource identity/removal cannot be established"
        verification_diagnostic = cleanup_diagnostic
    else:
        resource_id = resource_ids[0]
        cleanup_command = [*remove_command, resource_id]
        try:
            cleanup = _run(cleanup_command, timeout=15, check=False)
            cleanup_exit_code = cleanup.returncode
            if cleanup.returncode != 0:
                cleanup_diagnostic = "cleanup command returned nonzero"
        except subprocess.TimeoutExpired:
            cleanup_diagnostic = "cleanup command timed out"
        except OSError:
            cleanup_diagnostic = "cleanup command could not execute"

        try:
            verification = _run(list_command, timeout=10, check=False)
            verification_exit_code = verification.returncode
            if verification.returncode == 0:
                remaining = verification.stdout.splitlines()
                if any(
                    re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", item) is None
                    for item in remaining
                ):
                    verification_diagnostic = (
                        "post-cleanup discovery returned invalid identities"
                    )
                elif len(set(remaining)) != len(remaining):
                    verification_diagnostic = (
                        "post-cleanup discovery returned duplicate identities"
                    )
                elif remaining:
                    verification_status = "present"
                    verification_diagnostic = (
                        "matching run-associated resource remains after cleanup"
                    )
                else:
                    verification_status = "removed"
                    verification_diagnostic = ""
            else:
                verification_diagnostic = (
                    "resource verification returned nonzero"
                )
        except subprocess.TimeoutExpired:
            verification_diagnostic = "resource verification timed out"
        except OSError:
            verification_diagnostic = "resource verification could not execute"

        if verification_status == "present":
            status = "failed"
        elif verification_status != "removed" or cleanup_exit_code is None:
            status = "unverified"
        elif cleanup_exit_code != 0:
            status = "failed"
        else:
            status = "verified"

    return {
        "resource": resource,
        "identity": identity,
        "run_id": run_id,
        "resource_id": resource_id,
        "cleanup_attempted": True,
        "cleanup_exit_code": cleanup_exit_code,
        "cleanup_diagnostic": cleanup_diagnostic,
        "verification_exit_code": verification_exit_code,
        "verification_status": verification_status,
        "verification_diagnostic": verification_diagnostic,
        "status": status,
    }


def _recover_timed_out_resource(
    *,
    resource: str,
    identity: str,
    run_id: str,
    labels: tuple[str, str],
    list_command: list[str],
    remove_command: list[str],
    cleanup_records: list[dict[str, Any]],
) -> dict[str, Any]:
    try:
        result = _cleanup_labeled_docker_resource(
            resource=resource,
            identity=identity,
            run_id=run_id,
            labels=labels,
            list_command=list_command,
            remove_command=remove_command,
            operation_timed_out=True,
        )
    except Exception as exc:
        result = {
            "resource": resource,
            "identity": identity,
            "run_id": run_id,
            "resource_id": None,
            "cleanup_attempted": True,
            "cleanup_exit_code": None,
            "cleanup_diagnostic": (
                f"cleanup recovery raised {type(exc).__name__}"
            ),
            "verification_exit_code": None,
            "verification_status": "unverified",
            "verification_diagnostic": (
                "resource removal could not be verified"
            ),
            "status": "unverified",
        }
    cleanup_records.append(result)
    message = (
        f"Timed-out {resource} cleanup {result['status']}: "
        f"{result['cleanup_diagnostic'] or result['verification_diagnostic']}"
    )
    print(message, file=sys.stderr)
    return result


def _safe_cleanup_labeled_resource(
    *,
    resource: str,
    identity: str,
    run_id: str,
    labels: tuple[str, str],
    list_command: list[str],
    remove_command: list[str],
    expected_resource_id: str | None = None,
    operation_timed_out: bool = False,
) -> dict[str, Any]:
    try:
        return _cleanup_labeled_docker_resource(
            resource=resource,
            identity=identity,
            run_id=run_id,
            labels=labels,
            list_command=list_command,
            remove_command=remove_command,
            expected_resource_id=expected_resource_id,
            operation_timed_out=operation_timed_out,
        )
    except Exception as exc:
        result = {
            "resource": resource,
            "identity": identity,
            "run_id": run_id,
            "resource_id": expected_resource_id,
            "cleanup_attempted": True,
            "cleanup_exit_code": None,
            "cleanup_diagnostic": f"cleanup raised {type(exc).__name__}",
            "verification_exit_code": None,
            "verification_status": "unverified",
            "verification_diagnostic": "resource removal could not be verified",
            "status": "unverified",
        }
        print(
            f"Cleanup {resource} unverified: {result['cleanup_diagnostic']}",
            file=sys.stderr,
        )
        return result


def _incomplete_evidence_summary(
    primary_error: BaseException | None,
    cleanup_records: list[dict[str, Any]],
    listener_cleanup_records: list[dict[str, Any]] | None = None,
) -> str:
    primary = (
        type(primary_error).__name__ if primary_error is not None else "unknown"
    )
    outcomes = [
        f"{item['resource']}={item['status']}"
        f"(id={item.get('resource_id') or 'unresolved'})"
        for item in cleanup_records
    ]
    outcomes.extend(
        f"listener:{item['listener_id']}={item['status']}"
        for item in listener_cleanup_records or []
    )
    return (
        f"Evidence bundle not emitted (primary={primary}); cleanup outcomes: "
        f"{', '.join(outcomes) or 'no cleanup records'}"
    )


def _evidence_write_failure_message(
    error: Exception,
    primary_error: BaseException | None,
    cleanup_records: list[dict[str, Any]],
    listener_cleanup_records: list[dict[str, Any]],
) -> str:
    return (
        "Evidence serialization or write failed "
        f"({type(error).__name__}); "
        + _incomplete_evidence_summary(
            primary_error,
            cleanup_records,
            listener_cleanup_records,
        )
    )


def _container_label_commands(labels: tuple[str, str]) -> tuple[list[str], list[str]]:
    filters = [part for label in labels for part in ("--filter", f"label={label}")]
    return (
        ["docker", "ps", "--all", "--quiet", "--no-trunc", *filters],
        ["docker", "rm", "--force"],
    )


def _image_label_commands(labels: tuple[str, str]) -> tuple[list[str], list[str]]:
    filters = [part for label in labels for part in ("--filter", f"label={label}")]
    return (
        ["docker", "image", "ls", "--quiet", "--no-trunc", *filters],
        ["docker", "image", "rm"],
    )


class _Listener:
    def __init__(
        self,
        bind_address: str,
        *,
        run_id: str | None = None,
        listener_id: str | None = None,
        cleanup_records: list[dict[str, Any]] | None = None,
    ) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((bind_address, 0))
        self._socket.listen(8)
        self._socket.settimeout(0.2)
        self.port = self._socket.getsockname()[1]
        self._stopping = threading.Event()
        self._records: list[dict[str, str]] = []
        self._records_condition = threading.Condition()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._close_result: dict[str, Any] | None = None
        self._run_id = run_id
        self._listener_id = listener_id
        self._cleanup_records = cleanup_records

    def __enter__(self) -> _Listener:
        self._thread.start()
        return self

    def close(self, *, timeout: float = 2) -> dict[str, Any]:
        if self._close_result is not None:
            return dict(self._close_result)
        self._stopping.set()
        socket_closed = True
        diagnostic = ""
        try:
            self._socket.close()
        except Exception:
            socket_closed = False
            diagnostic = "listener socket close failed"
        try:
            self._thread.join(timeout=timeout)
        except Exception:
            self._close_result = {
                "attempted": True,
                "socket_closed": socket_closed,
                "thread_stopped": False,
                "status": "failed",
                "diagnostic": "listener thread join failed",
            }
            self._record_cleanup()
            return dict(self._close_result)
        try:
            thread_stopped = not self._thread.is_alive()
        except Exception:
            thread_stopped = False
            diagnostic = "listener thread state could not be verified"
        status = "succeeded" if socket_closed and thread_stopped else "failed"
        if not thread_stopped:
            diagnostic = "listener thread remained alive after bounded join"
        self._close_result = {
            "attempted": True,
            "socket_closed": socket_closed,
            "thread_stopped": thread_stopped,
            "status": status,
            "diagnostic": diagnostic,
        }
        self._record_cleanup()
        return dict(self._close_result)

    def _record_cleanup(self) -> None:
        if (
            self._close_result is None
            or self._cleanup_records is None
            or self._run_id is None
            or self._listener_id is None
        ):
            return
        result = {
            "listener_id": self._listener_id,
            "run_id": self._run_id,
            **self._close_result,
        }
        self._cleanup_records.append(result)
        if result["status"] != "succeeded":
            print(
                f"Listener cleanup {self._listener_id} "
                f"{result['status']}: {result['diagnostic']}",
                file=sys.stderr,
            )

    def __exit__(self, exc_type: Any, exc: BaseException | None, *_: object) -> bool:
        result = self.close()
        if result["status"] != "succeeded":
            message = f"Listener cleanup {result['status']}: {result['diagnostic']}"
            if exc is not None:
                exc.add_note(message)
            else:
                raise RuntimeError(message)
        return False

    @property
    def records(self) -> list[dict[str, str]]:
        with self._records_condition:
            return [dict(record) for record in self._records]

    def wait_for_payload(
        self,
        expected_payload: str,
        *,
        timeout: float,
    ) -> dict[str, str] | None:
        deadline = time.monotonic() + timeout
        with self._records_condition:
            while True:
                matching_record = next(
                    (
                        record for record in self._records
                        if record["payload"] == expected_payload
                    ),
                    None,
                )
                if matching_record is not None:
                    return dict(matching_record)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._records_condition.wait(remaining)

    def _serve(self) -> None:
        while not self._stopping.is_set():
            try:
                connection, _address = self._socket.accept()
            except (OSError, TimeoutError):
                if self._stopping.is_set():
                    break
                continue
            with connection:
                source_ip = connection.getpeername()[0]
                connection.settimeout(1)
                try:
                    message = connection.recv(128).decode("ascii", "replace")
                except (OSError, TimeoutError):
                    message = ""
                with self._records_condition:
                    self._records.append({
                        "payload": message,
                        "source_ip": source_ip,
                        "received_at": datetime.now(timezone.utc).isoformat(),
                    })
                    self._records_condition.notify_all()
                try:
                    connection.sendall(b"received")
                except OSError:
                    pass

def _run_probe(
    image: str,
    *,
    network: str,
    program: str,
    run_id: str,
    resource_id: str,
    cleanup_records: list[dict[str, Any]],
    timeout: float = 15,
    add_host_gateway: bool = False,
) -> subprocess.CompletedProcess[str]:
    labels = _docker_resource_labels(run_id, resource_id)
    container_name = f"kybernaut-ci-probe-{run_id.replace('-', '')}-{resource_id}"
    list_command, remove_command = _container_label_commands(labels)
    command = [
        "docker", "run", "--rm", "--name", container_name,
        "--label", labels[0], "--label", labels[1],
        "--network", network,
        "--read-only", "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=32",
    ]
    if add_host_gateway:
        command.extend([
            "--add-host=host.docker.internal:host-gateway",
        ])
    command.extend([image, "python", "-c", program])
    try:
        return _run(command, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        cleanup = _recover_timed_out_resource(
            resource="probe_container",
            identity=container_name,
            run_id=run_id,
            labels=labels,
            list_command=list_command,
            remove_command=remove_command,
            cleanup_records=cleanup_records,
        )
        if cleanup["status"] != "verified":
            exc.add_note(
                f"Cleanup outcome: {cleanup['status']} "
                f"({cleanup['cleanup_diagnostic'] or cleanup['verification_diagnostic']})"
            )
        raise


def _network_canary(
    image: str,
    suffix: str,
    run_id: str,
    cleanup_records: list[dict[str, Any]],
    listener_cleanup_records: list[dict[str, Any]],
) -> dict[str, Any]:
    evidence: dict[str, Any] = {"provenance": "ci_network_isolation.py"}
    peer_name = f"kybernaut-ci-peer-{suffix}"
    peer_labels = _docker_resource_labels(run_id, "peer-listener")
    peer_create_attempted = False
    peer_create_timed_out = False
    peer_container_id: str | None = None
    try:
        bridge = json.loads(_run(
            ["docker", "network", "inspect", "bridge"],
            timeout=10,
        ).stdout)[0]
        bridge_addresses = bridge["IPAM"]["Config"]
        gateway = next(
            (item.get("Gateway") for item in bridge_addresses if item.get("Gateway")),
            None,
        )
        if not gateway:
            raise RuntimeError("Docker bridge has no local gateway address")
        host_listener = _Listener(
            gateway,
            run_id=run_id,
            listener_id="host-canary-listener",
            cleanup_records=listener_cleanup_records,
        )
        with host_listener:
            host_control = _run_probe(
                image,
                network="bridge",
                run_id=run_id,
                resource_id="host-control",
                cleanup_records=cleanup_records,
                add_host_gateway=True,
                program=(
                    "import socket; "
                    "s=socket.create_connection("
                    "('host.docker.internal', "
                    f"{host_listener.port}), timeout=3); "
                    f"s.sendall(b'control-host:{run_id}'); s.close(); "
                    "print('HOST_CONTROL_CONNECTED')"
                ),
            )
            if host_control.returncode != 0:
                raise RuntimeError(
                    "Networked control container could not reach the "
                    "controlled host listener"
                )
            if "HOST_CONTROL_CONNECTED" not in host_control.stdout:
                raise RuntimeError("Host canary control output was invalid")
            if [
                record["payload"] for record in host_listener.records
            ] != [f"control-host:{run_id}"]:
                raise RuntimeError("Host listener did not record the control")

            host_isolated = _run_probe(
                image,
                network="none",
                run_id=run_id,
                resource_id="host-isolated",
                cleanup_records=cleanup_records,
                add_host_gateway=True,
                program=(
                    "import socket, sys\n"
                    "try:\n"
                    " s=socket.create_connection("
                    "('host.docker.internal', "
                    f"{host_listener.port}), timeout=1.5)\n"
                    f" s.sendall(b'isolated-host:{run_id}'); s.close()\n"
                    " print('HOST_PROBE_CONNECTED'); sys.exit(42)\n"
                    "except OSError:\n"
                    " print('HOST_PROBE_DENIED')"
                ),
                timeout=10,
            )
            if host_isolated.returncode != 0 or (
                "HOST_PROBE_DENIED" not in host_isolated.stdout
            ):
                raise RuntimeError(
                    "Isolated container unexpectedly reached the host listener"
                )
            time.sleep(0.25)
            if [
                record["payload"] for record in host_listener.records
            ] != [f"control-host:{run_id}"]:
                raise RuntimeError(
                    "Host listener recorded a connection from the isolated probe"
                )
            evidence["host"] = {
                "listener": {
                    "scope": "docker_bridge_gateway",
                    "records": host_listener.records,
                },
                "control": {
                    "network_mode": "bridge",
                    "exit_code": host_control.returncode,
                    "marker": "HOST_CONTROL_CONNECTED",
                    "outcome": "connected",
                },
                "isolated_probe": {
                    "network_mode": "none",
                    "exit_code": host_isolated.returncode,
                    "marker": "HOST_PROBE_DENIED",
                    "outcome": "denied",
                },
            }

        peer_program = (
            "import datetime, json, socket, time\n"
            "s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, "
            "socket.SO_REUSEADDR, 1); s.bind(('0.0.0.0', "
            f"{PEER_PORT})); s.listen(4); s.settimeout(0.2)\n"
            "print('PEER_READY', flush=True)\n"
            "deadline=time.monotonic()+10; first=None\n"
            "while time.monotonic()<deadline:\n"
            " if first is not None and time.monotonic()-first>=2.5: break\n"
            " try: c,address=s.accept()\n"
            " except TimeoutError: continue\n"
            " with c:\n"
            "  c.settimeout(1)\n"
            "  try: payload=c.recv(128).decode('ascii','replace')\n"
            "  except OSError: payload=''\n"
            "  print(json.dumps({'payload':payload,'source_ip':address[0],"
            "'received_at':datetime.datetime.now(datetime.timezone.utc)"
            ".isoformat()}), "
            "flush=True)\n"
            "  if first is None: first=time.monotonic()\n"
            "s.close()\n"
        )
        peer_create_attempted = True
        try:
            _run(
                [
                    "docker", "run", "-d", "--network", "bridge",
                    "--name", peer_name,
                    "--label", peer_labels[0], "--label", peer_labels[1],
                    "--read-only", "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--pids-limit=32",
                    image, "python", "-u", "-c", peer_program,
                ],
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            peer_create_timed_out = True
            raise
        peer_info = json.loads(_run(
            ["docker", "inspect", peer_name],
            timeout=10,
        ).stdout)[0]
        peer_container_id = peer_info.get("Id")
        if not isinstance(peer_container_id, str) or re.fullmatch(
            r"[0-9a-f]{64}", peer_container_id
        ) is None:
            raise RuntimeError("Peer container identity is invalid")
        peer_ip = next(
            iter(peer_info["NetworkSettings"]["Networks"].values())
        )["IPAddress"]
        if not peer_ip:
            raise RuntimeError("Peer container has no bridge-network address")

        ready_deadline = time.monotonic() + 5
        while time.monotonic() < ready_deadline:
            logs = _run(
                ["docker", "logs", peer_name],
                timeout=3,
                check=False,
            )
            if "PEER_READY" in logs.stdout:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Peer listener did not become ready")

        peer_control = _run_probe(
            image,
            network="bridge",
            run_id=run_id,
            resource_id="peer-control",
            cleanup_records=cleanup_records,
            program=(
                "import socket, time\n"
                "for attempt in range(15):\n"
                " try:\n"
                f"  s=socket.create_connection(('{peer_ip}', "
                f"{PEER_PORT}), timeout=1); break\n"
                " except OSError:\n"
                "  if attempt == 14: raise\n"
                "  time.sleep(0.1)\n"
                f"s.sendall(b'control-peer:{run_id}'); s.close()\n"
                "print('PEER_CONTROL_CONNECTED')"
            ),
            timeout=15,
        )
        if peer_control.returncode != 0 or (
            "PEER_CONTROL_CONNECTED" not in peer_control.stdout
        ):
            raise RuntimeError(
                "Networked control container could not reach the peer listener"
            )

        peer_isolated = _run_probe(
            image,
            network="none",
            run_id=run_id,
            resource_id="peer-isolated",
            cleanup_records=cleanup_records,
            program=(
                "import socket, sys\n"
                "try:\n"
                f" s=socket.create_connection(('{peer_ip}', "
                f"{PEER_PORT}), timeout=1.5)\n"
                f" s.sendall(b'isolated-peer:{run_id}'); s.close()\n"
                " print('PEER_PROBE_CONNECTED'); sys.exit(42)\n"
                "except OSError:\n"
                " print('PEER_PROBE_DENIED')"
            ),
            timeout=10,
        )
        if peer_isolated.returncode != 0 or (
            "PEER_PROBE_DENIED" not in peer_isolated.stdout
        ):
            raise RuntimeError(
                "Isolated container unexpectedly reached the peer listener"
            )

        wait = _run(
            ["docker", "wait", peer_name],
            timeout=8,
        )
        if wait.stdout.strip() != "0":
            raise RuntimeError("Peer listener exited unexpectedly")
        peer_logs = _run(["docker", "logs", peer_name], timeout=5).stdout
        peer_events = [
            json.loads(line)
            for line in peer_logs.splitlines()
            if line.startswith("{")
        ]
        if [
            record.get("payload") for record in peer_events
        ] != [f"control-peer:{run_id}"]:
            raise RuntimeError(
                "Peer listener did not record exactly one control connection"
            )
        evidence["peer"] = {
            "container_id": peer_container_id,
            "listener": {
                "scope": "peer_container_bridge_address",
                "records": peer_events,
            },
            "control": {
                "network_mode": "bridge",
                "exit_code": peer_control.returncode,
                "marker": "PEER_CONTROL_CONNECTED",
                "outcome": "connected",
            },
            "isolated_probe": {
                "network_mode": "none",
                "exit_code": peer_isolated.returncode,
                "marker": "PEER_PROBE_DENIED",
                "outcome": "denied",
            },
        }
        return evidence
    finally:
        if peer_create_attempted:
            list_command, remove_command = _container_label_commands(peer_labels)
            cleanup_records.append(_safe_cleanup_labeled_resource(
                resource="peer_container",
                identity=peer_name,
                run_id=run_id,
                labels=peer_labels,
                list_command=list_command,
                remove_command=remove_command,
                expected_resource_id=peer_container_id,
                operation_timed_out=peer_create_timed_out,
            ))


def _source_path(source_root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
        or "\\" in relative_path
    ):
        raise RuntimeError("Snapshot manifest contains an unsafe path")
    candidate = source_root.joinpath(*relative.parts)
    current = source_root
    for part in relative.parts:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except OSError as exc:
            raise RuntimeError(
                f"Required snapshot input is missing: {relative_path}"
            ) from exc
        if stat.S_ISLNK(mode):
            raise RuntimeError("Snapshot inputs must not be symlinks")
        if current != candidate and not stat.S_ISDIR(mode):
            raise RuntimeError("Snapshot input has a non-directory parent")
        if current == candidate and not stat.S_ISREG(mode):
            raise RuntimeError("Snapshot input is not a regular file")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(source_root)
    except (OSError, ValueError) as exc:
        raise RuntimeError("Snapshot input resolves outside the repository") from exc
    return resolved


def _python_source_files(source_root: Path, relative_root: str) -> list[str]:
    source_dir = source_root / relative_root
    try:
        mode = source_dir.lstat().st_mode
    except OSError as exc:
        raise RuntimeError(
            f"Required snapshot source directory is missing: {relative_root}"
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise RuntimeError("Snapshot source root must be a real directory")

    selected: list[str] = []

    def visit(directory: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise RuntimeError("Could not inspect approved source directory") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                entry_mode = path.lstat().st_mode
            except OSError as exc:
                raise RuntimeError("Could not inspect approved source entry") from exc
            if stat.S_ISLNK(entry_mode):
                raise RuntimeError("Approved source trees must not contain symlinks")
            if stat.S_ISDIR(entry_mode):
                if entry.name in {"__pycache__", ".pytest_cache"} or entry.name.startswith("."):
                    continue
                visit(path)
            elif stat.S_ISREG(entry_mode):
                if entry.name.endswith(".py"):
                    try:
                        relative = path.relative_to(source_root).as_posix()
                        resolved = path.resolve(strict=True)
                        resolved.relative_to(source_root)
                    except (OSError, ValueError) as exc:
                        raise RuntimeError(
                            "Approved Python source resolves outside the repository"
                        ) from exc
                    selected.append(relative)
            else:
                raise RuntimeError("Approved source trees contain a special file")

    visit(source_dir)
    if not selected:
        raise RuntimeError("Approved source directory contains no Python files")
    return selected


def _dotenv_example_projection(contents: bytes) -> bytes:
    matches = []
    try:
        for line in contents.decode("utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == DOTENV_EXAMPLE_NAME:
                matches.append(value.strip())
    except UnicodeError as exc:
        raise RuntimeError("Required example configuration is unreadable") from exc
    if len(matches) != 1 or matches[0]:
        raise RuntimeError(
            "Example configuration must contain one blank Grafana password setting"
        )
    return f"{DOTENV_EXAMPLE_NAME}=\n".encode("ascii")


def _git_tree_entries(
    project_root: Path,
    revision: str,
) -> dict[str, tuple[str, str, str]]:
    try:
        result = subprocess.run(
            [
                "git", "-C", str(project_root), "ls-tree", "-r", "-z",
                revision,
            ],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Could not read expected source revision tree") from exc
    if result.returncode != 0:
        raise RuntimeError("Could not read expected source revision tree")
    entries: dict[str, tuple[str, str, str]] = {}
    for item in result.stdout.split(b"\0"):
        if not item:
            continue
        metadata, separator, raw_path = item.partition(b"\t")
        if not separator:
            raise RuntimeError("Expected source revision tree is malformed")
        try:
            mode, object_type, object_id = metadata.decode("ascii").split()
            relative_path = raw_path.decode("utf-8")
        except (UnicodeError, ValueError) as exc:
            raise RuntimeError("Expected source revision tree is malformed") from exc
        if relative_path in entries:
            raise RuntimeError("Expected source revision has duplicate paths")
        entries[relative_path] = (mode, object_type, object_id)
    return entries


def _git_blob_contents(
    project_root: Path,
    entries: Mapping[str, tuple[str, str, str]],
    selected_paths: Iterable[str],
) -> dict[str, bytes]:
    paths = sorted(selected_paths)
    for path in paths:
        mode, object_type, _object_id = entries[path]
        if mode not in {"100644", "100755"} or object_type != "blob":
            raise RuntimeError("Expected source file is not a regular Git blob")
    object_ids = [entries[path][2] for path in paths]
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "cat-file", "--batch"],
            input="".join(f"{object_id}\n" for object_id in object_ids).encode("ascii"),
            check=False,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Could not read expected source revision blobs") from exc
    if result.returncode != 0:
        raise RuntimeError("Could not read expected source revision blobs")
    contents: dict[str, bytes] = {}
    offset = 0
    for path, expected_id in zip(paths, object_ids):
        header_end = result.stdout.find(b"\n", offset)
        if header_end < 0:
            raise RuntimeError("Expected source revision blob output is incomplete")
        try:
            object_id, object_type, size_text = result.stdout[offset:header_end].decode(
                "ascii"
            ).split()
            size = int(size_text)
        except (UnicodeError, ValueError) as exc:
            raise RuntimeError("Expected source revision blob output is malformed") from exc
        if object_id != expected_id or object_type != "blob" or size < 0:
            raise RuntimeError("Expected source revision blob identity is invalid")
        content_start = header_end + 1
        content_end = content_start + size
        if (
            content_end >= len(result.stdout)
            or result.stdout[content_end:content_end + 1] != b"\n"
        ):
            raise RuntimeError("Expected source revision blob output is truncated")
        contents[path] = result.stdout[content_start:content_end]
        offset = content_end + 1
    if offset != len(result.stdout):
        raise RuntimeError("Expected source revision blob output has trailing data")
    return contents


def _source_manifest_digest(entries: list[dict[str, str]]) -> str:
    canonical = json.dumps(
        entries,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _copy_source_snapshot(
    destination: Path,
    revision: str,
    *,
    project_root: Path = PROJECT_ROOT,
    python_source_roots: Iterable[str] = PYTHON_SOURCE_ROOTS,
    approved_files: Iterable[str] | None = None,
    input_sha256: Mapping[str, str] | None = None,
    manifest_entries: list[dict[str, str]] | None = None,
) -> list[str]:
    try:
        source_root = project_root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("Repository root is unavailable") from exc
    if not source_root.is_dir() or project_root.is_symlink():
        raise RuntimeError("Repository root must be a real directory")

    destination = Path(destination)
    if destination.is_symlink():
        raise RuntimeError("Snapshot destination must not be a symlink")
    destination_resolved = destination.resolve()
    if destination_resolved == source_root or source_root in destination_resolved.parents:
        raise RuntimeError("Snapshot destination must be outside the repository")
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError("Snapshot destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)

    if approved_files is None:
        approved_files = (*APPROVED_SCRIPT_FILES, *APPROVED_SOURCE_FILES)
    if input_sha256 is None:
        input_sha256 = APPROVED_INPUT_SHA256
    tree_entries = _git_tree_entries(source_root, revision)
    selected = set(approved_files)
    for source_dir in python_source_roots:
        checkout_paths = set(_python_source_files(source_root, source_dir))
        revision_paths = {
            path
            for path, (mode, object_type, _object_id) in tree_entries.items()
            if path.startswith(f"{source_dir}/")
            and path.endswith(".py")
            and not any(
                part.startswith(".") or part in {"__pycache__", ".pytest_cache"}
                for part in Path(path).relative_to(source_dir).parts[:-1]
            )
        }
        if checkout_paths != revision_paths:
            raise RuntimeError(
                "Python source paths do not match the expected Git revision"
            )
        selected.update(revision_paths)

    for relative_path in selected:
        relative = Path(relative_path)
        if (
            relative.is_absolute()
            or not relative.parts
            or any(part in {"", ".", ".."} for part in relative.parts)
            or "\\" in relative_path
        ):
            raise RuntimeError("Snapshot manifest contains an unsafe path")
        if relative_path not in tree_entries:
            raise RuntimeError(
                f"Required snapshot input is missing from expected revision: {relative_path}"
            )
        _source_path(source_root, relative_path)

    blob_contents = _git_blob_contents(source_root, tree_entries, selected)
    for relative_path, expected_hash in input_sha256.items():
        contents = blob_contents.get(relative_path)
        if contents is None or hashlib.sha256(contents).hexdigest() != expected_hash:
            raise RuntimeError("Approved test-data input checksum mismatch")

    selected_manifest: list[dict[str, str]] = []
    for relative_path in sorted(selected):
        source_contents = blob_contents[relative_path]
        contents = (
            _dotenv_example_projection(source_contents)
            if relative_path == ".env.example"
            else source_contents
        )
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        target.write_bytes(contents)
        target.chmod(0o644)
        selected_manifest.append({
            "path": relative_path,
            "git_blob_oid": tree_entries[relative_path][2],
            "source_sha256": hashlib.sha256(source_contents).hexdigest(),
            "snapshot_sha256": hashlib.sha256(contents).hexdigest(),
        })

    git_directory = destination / ".git"
    (git_directory / "objects").mkdir(parents=True)
    (git_directory / "refs" / "heads").mkdir(parents=True)
    (git_directory / "HEAD").write_text(revision + "\n", encoding="ascii")
    (destination / "incidents.db").symlink_to(
        "/tmp/kybernaut-ci.sqlite3"
    )
    if manifest_entries is not None:
        manifest_entries.clear()
        manifest_entries.extend(selected_manifest)
    return sorted(selected)


def _build_image(
    build_context: Path,
    *,
    base_image: str,
    python_version: str,
    tag: str,
    run_id: str,
    cleanup_records: list[dict[str, Any]],
    requirements_bytes: bytes,
) -> None:
    if not isinstance(requirements_bytes, bytes):
        raise RuntimeError("Verified requirements must be immutable bytes")
    if hashlib.sha256(requirements_bytes).hexdigest() != REQUIREMENTS_SHA256:
        raise RuntimeError(
            "requirements bytes do not match the approved pinned lock checksum"
        )
    (build_context / "requirements.txt").write_bytes(requirements_bytes)
    labels = _docker_resource_labels(run_id, "test-image")
    list_command, remove_command = _image_label_commands(labels)
    major, minor = python_version.split(".")
    dockerfile = (
        f"FROM --platform=linux/amd64 {base_image}\n"
        f"RUN python -c \"import sys; assert sys.version_info[:2] == "
        f"({major}, {minor}), sys.version\"\n"
        "RUN printf 'deb [check-valid-until=no] "
        f"https://snapshot.debian.org/archive/debian/{DEBIAN_SNAPSHOT}/ "
        "trixie main\\n' > /tmp/debian-snapshot.list \\\n"
        " && apt-get -o Acquire::Check-Valid-Until=false "
        "-o Dir::Etc::sourcelist=/tmp/debian-snapshot.list "
        "-o Dir::Etc::sourceparts=- update \\\n"
        " && apt-get -o Acquire::Check-Valid-Until=false "
        "-o Dir::Etc::sourcelist=/tmp/debian-snapshot.list "
        "-o Dir::Etc::sourceparts=- install -y --no-install-recommends "
        f"git={GIT_PACKAGE_VERSION} \\\n"
        " && test \"$(dpkg-query -W -f='${Version}' git)\" = "
        f"'{GIT_PACKAGE_VERSION}' \\\n"
        " && git --version | grep -Fx 'git version 2.47.3' \\\n"
        " && rm -f /tmp/debian-snapshot.list \\\n"
        " && rm -rf /var/lib/apt/lists/*\n"
        "COPY requirements.txt /tmp/requirements.txt\n"
        "RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt\n"
        "WORKDIR /src\n"
    )
    (build_context / "Dockerfile").write_text(dockerfile, encoding="ascii")
    try:
        _run(
            [
                "docker", "build", "--pull", "--platform", "linux/amd64",
                "--label", labels[0], "--label", labels[1],
                "--tag", tag, str(build_context),
            ],
            timeout=900,
            capture_output=False,
        )
    except subprocess.TimeoutExpired as exc:
        cleanup = _recover_timed_out_resource(
            resource="test_image",
            identity=tag,
            run_id=run_id,
            labels=labels,
            list_command=list_command,
            remove_command=remove_command,
            cleanup_records=cleanup_records,
        )
        if cleanup["status"] != "verified":
            exc.add_note(
                f"Cleanup outcome: {cleanup['status']} "
                f"({cleanup['cleanup_diagnostic'] or cleanup['verification_diagnostic']})"
            )
        raise


def _inspect_test_container(
    container_id: str,
    expected_image_id: str,
) -> dict[str, Any]:
    inspection = json.loads(_run(
        ["docker", "inspect", container_id],
        timeout=10,
    ).stdout)[0]
    host_config = inspection["HostConfig"]
    mounts = inspection["Mounts"]
    sanitized_mounts = sorted(
        (
            {
                "destination": mount["Destination"],
                "read_only": not mount["RW"],
                "type": mount["Type"],
            }
            for mount in mounts
        ),
        key=lambda mount: mount["destination"],
    )
    expected_mounts = [
        {"destination": "/reports/junit", "read_only": False, "type": "bind"},
        {"destination": "/src", "read_only": True, "type": "bind"},
    ]
    if host_config["NetworkMode"] != "none":
        raise RuntimeError("Pytest container is not using Docker --network none")
    if not host_config["ReadonlyRootfs"]:
        raise RuntimeError("Pytest container root filesystem is writable")
    if sanitized_mounts != expected_mounts:
        raise RuntimeError("Pytest container has unexpected writable or extra mounts")
    if not host_config.get("Tmpfs", {}).get("/tmp"):
        raise RuntimeError("Pytest container lacks its designated tmpfs")
    if host_config.get("CapDrop") != ["ALL"] or host_config.get("CapAdd"):
        raise RuntimeError("Pytest container has unexpected Linux capabilities")
    if host_config.get("Privileged") is not False:
        raise RuntimeError("Pytest container is privileged")
    security_options = sorted(host_config.get("SecurityOpt") or [])
    if "no-new-privileges" not in security_options:
        raise RuntimeError("Pytest container lacks no-new-privileges")
    expected_user = f"{os.getuid()}:{os.getgid()}"
    if inspection["Config"].get("User") != expected_user or os.getuid() == 0:
        raise RuntimeError("Pytest container is not running as the non-root runner")
    network_names = sorted(
        inspection.get("NetworkSettings", {}).get("Networks", {})
    )
    if network_names != ["none"]:
        raise RuntimeError("Pytest container has unexpected Docker networks")
    if host_config.get("PidMode") or host_config.get("IpcMode") not in {"", "private"}:
        raise RuntimeError("Pytest container shares a host namespace")
    image_id = inspection.get("Image")
    if not isinstance(image_id, str) or not re.fullmatch(
        r"sha256:[0-9a-f]{64}", image_id
    ):
        raise RuntimeError("Docker inspection did not report a valid image ID")
    if image_id != expected_image_id:
        raise RuntimeError("Pytest container does not use the built test image")
    return {
        "source": "docker inspect",
        "inspected_at": datetime.now(timezone.utc).isoformat(),
        "container_id": inspection["Id"],
        "image_id": image_id,
        "config": {
            "network_mode": host_config["NetworkMode"],
            "network_names": network_names,
            "rootfs_read_only": host_config["ReadonlyRootfs"],
            "mounts": sanitized_mounts,
            "tmpfs_destinations": sorted(host_config.get("Tmpfs", {})),
            "user": inspection["Config"]["User"],
            "privileged": host_config["Privileged"],
            "cap_drop": sorted(host_config.get("CapDrop") or []),
            "cap_add": sorted(host_config.get("CapAdd") or []),
            "security_options": security_options,
            "pid_mode": host_config.get("PidMode") or "",
            "ipc_mode": host_config.get("IpcMode") or "",
        },
    }


def _pytest_container_command(
    *,
    run_id: str,
    listener_host: str,
    listener_port: int,
) -> list[str]:
    entrypoint = """import json
import subprocess
import sys
from pathlib import Path

run_id, listener_host, listener_port = sys.argv[1:4]
child_program = (
    "import socket,sys\\n"
    "try:\\n"
    " s=socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=1.5)\\n"
    " s.sendall(sys.argv[3].encode('ascii')); s.close()\\n"
    " print('PYTEST_CHILD_PROBE_CONNECTED'); raise SystemExit(42)\\n"
    "except OSError:\\n"
    " print('PYTEST_CHILD_PROBE_DENIED'); raise SystemExit(0)\\n"
)
try:
    probe = subprocess.run(
        [sys.executable, "-c", child_program, listener_host, listener_port,
         "pytest-child:" + run_id],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    denied = (
        probe.returncode == 0
        and probe.stdout.strip() == "PYTEST_CHILD_PROBE_DENIED"
    )
    connected = (
        probe.returncode == 42
        and probe.stdout.strip() == "PYTEST_CHILD_PROBE_CONNECTED"
    )
    result = {
        "exit_code": probe.returncode,
        "marker": (
            "PYTEST_CHILD_PROBE_DENIED" if denied
            else "PYTEST_CHILD_PROBE_CONNECTED" if connected
            else "PYTEST_CHILD_PROBE_FAILED"
        ),
        "outcome": "denied" if denied else "connected" if connected else "failed",
    }
except subprocess.TimeoutExpired:
    result = {
        "exit_code": 124,
        "marker": "PYTEST_CHILD_PROBE_TIMEOUT",
        "outcome": "failed",
    }

Path("/reports/junit/child-process-network-probe.json").write_text(
    json.dumps({
        "schema_version": 1,
        "run_id": run_id,
        "scope": "pytest_container_child_process",
        "target": "synthetic_host_listener",
        "network_mode": "none",
        "child_process": True,
        **result,
    }, sort_keys=True) + "\\n",
    encoding="utf-8",
)
print(
    "CHILD_PROCESS_NETWORK_PROBE "
    + json.dumps({"scope": "pytest_container_child_process", **result}),
    flush=True,
)
if result["outcome"] != "denied":
    raise SystemExit(1)

pytest_result = subprocess.run(
    [
        sys.executable, "-m", "pytest", "-q",
        "-p", "no:cacheprovider",
        "--basetemp=/tmp/pytest",
        f"--junit-prefix=kybernaut-run-{run_id}",
        "--junitxml=/reports/junit/results.xml",
    ],
    check=False,
)
raise SystemExit(pytest_result.returncode)
"""
    return [
        "python",
        "-c",
        entrypoint,
        run_id,
        listener_host,
        str(listener_port),
    ]


def _write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    contents = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    temporary_path: Path | None = None
    published = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(contents)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.link(temporary_path, path)
        published = True
    except Exception:
        if temporary_path is not None and not published:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as cleanup_error:
                print(
                    "Evidence temporary-file cleanup failed: "
                    f"{type(cleanup_error).__name__}",
                    file=sys.stderr,
                )
        raise
    if temporary_path is not None:
        try:
            temporary_path.unlink()
        except OSError as cleanup_error:
            print(
                "Evidence temporary-file cleanup failed: "
                f"{type(cleanup_error).__name__}",
                file=sys.stderr,
            )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python-version", required=True, choices=("3.11", "3.12"))
    parser.add_argument("--base-image", required=True)
    parser.add_argument("--report-dir", required=True, type=Path)
    parser.add_argument("--expected-revision", required=True)
    args = parser.parse_args()
    expected_image = SUPPORTED_BASE_IMAGES[args.python_version]
    if args.base_image != expected_image:
        parser.error("base image must match the reviewed official-image digest")

    initial_preflight = _git_preflight(
        args.expected_revision,
        project_root=PROJECT_ROOT,
    )
    requirements_bytes = _verify_requirements_lock(project_root=PROJECT_ROOT)
    lock_checksum = hashlib.sha256(requirements_bytes).hexdigest()
    report_dir = args.report_dir.resolve()
    report_dir.mkdir(parents=True, exist_ok=True)
    junit_path = report_dir / "junit" / "results.xml"
    evidence_path = report_dir / "network-isolation.json"
    if junit_path.exists() or evidence_path.exists():
        raise RuntimeError("Report output paths already exist; refusing overwrite")
    junit_path.parent.mkdir(parents=True, exist_ok=True)

    revision = initial_preflight["actual_revision"]

    suffix = uuid.uuid4().hex
    image_tag = f"kybernaut-ci-py{args.python_version.replace('.', '')}:{suffix}"
    container_name = f"kybernaut-ci-pytest-{suffix}"
    run_id = str(uuid.uuid4())
    image_labels = _docker_resource_labels(run_id, "test-image")
    container_labels = _docker_resource_labels(run_id, "pytest-container")
    run_started_at = datetime.now(timezone.utc).isoformat()
    image_id: str | None = None
    container_id: str | None = None
    image_created = False
    container_create_attempted = False
    container_create_timed_out = False
    primary_error: BaseException | None = None
    child_probe_listener: _Listener | None = None
    listener_cleanup_records: list[dict[str, Any]] = []
    cleanup_records: list[dict[str, Any]] = []
    pytest_exit_code = 125
    evidence: dict[str, Any] | None = None
    source_manifest: list[dict[str, str]] = []

    try:
        with tempfile.TemporaryDirectory(
            prefix="kybernaut-ci-image-"
        ) as image_context, tempfile.TemporaryDirectory(
            prefix="kybernaut-ci-source-"
        ) as source_root:
            source_snapshot = Path(source_root) / "src"
            _copy_source_snapshot(
                source_snapshot,
                revision,
                manifest_entries=source_manifest,
            )
            snapshot_preflight = _git_preflight(
                args.expected_revision,
                project_root=PROJECT_ROOT,
            )
            snapshot_requirements = (
                source_snapshot / "requirements.txt"
            ).read_bytes()
            if snapshot_requirements != requirements_bytes:
                raise RuntimeError(
                    "Snapshot requirements do not match the verified lock bytes"
                )
            image_created = True
            _build_image(
                Path(image_context),
                base_image=args.base_image,
                python_version=args.python_version,
                tag=image_tag,
                run_id=run_id,
                cleanup_records=cleanup_records,
                requirements_bytes=requirements_bytes,
            )
            image_id = _run(
                ["docker", "image", "inspect", image_tag, "--format", "{{.Id}}"],
                timeout=10,
            ).stdout.strip()
            platform = _run(
                [
                    "docker", "image", "inspect", image_tag,
                    "--format", "{{.Os}}/{{.Architecture}}",
                ],
                timeout=10,
            ).stdout.strip()
            if platform != "linux/amd64":
                raise RuntimeError(f"Unexpected test image platform: {platform}")
            version_result = _run_probe(
                image_tag,
                network="none",
                run_id=run_id,
                resource_id="python-version",
                cleanup_records=cleanup_records,
                program=(
                    "import sys; "
                    "print(f'Python {sys.version_info.major}."
                    "{sys.version_info.minor}.{sys.version_info.micro}')"
                ),
                timeout=15,
            )
            if version_result.returncode != 0:
                raise RuntimeError("Test image Python version probe failed")
            version_output = version_result.stdout.strip()
            if not version_output.startswith(f"Python {args.python_version}."):
                raise RuntimeError(
                    f"Test image reported unexpected interpreter: {version_output}"
                )
            python_runtime = version_output.removeprefix("Python ")

            print("Running controlled host and peer network canaries.")
            canary_evidence = _network_canary(
                image_tag,
                suffix,
                run_id,
                cleanup_records,
                listener_cleanup_records,
            )
            if any(
                item["status"] != "verified"
                for item in cleanup_records
            ):
                raise RuntimeError("Peer-canary cleanup was not verified")
            bridge = json.loads(_run(
                ["docker", "network", "inspect", "bridge"],
                timeout=10,
            ).stdout)[0]
            gateway = next(
                (
                    item.get("Gateway")
                    for item in bridge["IPAM"]["Config"]
                    if item.get("Gateway")
                ),
                None,
            )
            if not gateway:
                raise RuntimeError("Docker bridge has no local gateway address")
            listener = _Listener(
                gateway,
                run_id=run_id,
                listener_id="pytest-child-listener",
                cleanup_records=listener_cleanup_records,
            )
            listener.__enter__()
            child_probe_listener = listener
            control_payload = f"control-pytest-child:{run_id}"
            child_probe_control = _run_probe(
                image_tag,
                network="bridge",
                run_id=run_id,
                resource_id="pytest-child-control",
                cleanup_records=cleanup_records,
                program=(
                    "import socket; "
                    f"s=socket.create_connection(('{gateway}', "
                    f"{child_probe_listener.port}), timeout=3); "
                    f"s.sendall(b'{control_payload}'); s.close(); "
                    "print('PYTEST_CHILD_CONTROL_CONNECTED')"
                ),
            )
            if (
                child_probe_control.returncode != 0
                or "PYTEST_CHILD_CONTROL_CONNECTED" not in child_probe_control.stdout
            ):
                raise RuntimeError(
                    "Networked control container could not reach the "
                    "pytest child-process probe listener"
                )
            control_record = child_probe_listener.wait_for_payload(
                control_payload,
                timeout=2,
            )
            if control_record is None:
                raise RuntimeError(
                    "Networked control connection was not recorded by the "
                    "pytest child-process probe listener"
                )
            child_probe_control_records = child_probe_listener.records
            if [
                record["payload"] for record in child_probe_control_records
            ] != [control_payload]:
                raise RuntimeError(
                    "Unexpected connection reached the pytest child-process "
                    "probe listener before the isolated probe"
                )

            container_create_attempted = True
            try:
                create = _run(
                    [
                    "docker", "create",
                    "--name", container_name,
                    "--label", container_labels[0],
                    "--label", container_labels[1],
                    "--network", "none",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges",
                    "--pids-limit=256",
                    "--memory=5g",
                    "--user", f"{os.getuid()}:{os.getgid()}",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,size=2g",
                    "--mount",
                    f"type=bind,source={source_snapshot},target=/src,readonly",
                    "--mount",
                    f"type=bind,source={junit_path.parent},"
                    "target=/reports/junit",
                    "--workdir", "/src",
                    "--env", "HOME=/tmp",
                    "--env", "TMPDIR=/tmp",
                    "--env", "PYTHONDONTWRITEBYTECODE=1",
                    image_tag,
                    *_pytest_container_command(
                        run_id=run_id,
                        listener_host=gateway,
                        listener_port=child_probe_listener.port,
                    ),
                    ],
                    timeout=30,
                )
            except subprocess.TimeoutExpired:
                container_create_timed_out = True
                raise
            container_id = create.stdout.strip()
            if not container_id:
                raise RuntimeError("Docker did not create the pytest container")
            inspection = _inspect_test_container(container_id, image_id)
            if inspection["container_id"] != container_id:
                raise RuntimeError("Docker returned an unexpected pytest container")
            print(
                "Verified pytest container: network=none, source=read-only, "
                "rootfs=read-only, tmp=/tmp, report=/reports."
            )
            print(
                f"Running full pytest suite in Python {python_runtime} "
                "with Docker network mode none."
            )
            try:
                started = _run(
                    ["docker", "start", "--attach", container_id],
                    timeout=PYTEST_TIMEOUT_SECONDS,
                    check=False,
                )
                if started.stdout:
                    print(started.stdout, end="" if started.stdout.endswith("\n") else "\n")
                if started.stderr:
                    print(started.stderr, file=sys.stderr, end="")
            except subprocess.TimeoutExpired:
                pytest_exit_code = 124
                print(
                    f"Pytest exceeded {PYTEST_TIMEOUT_SECONDS} seconds.",
                    file=sys.stderr,
                )
            else:
                state = json.loads(_run(
                    ["docker", "inspect", container_id],
                    timeout=10,
                ).stdout)[0]["State"]
                pytest_exit_code = (
                    int(state["ExitCode"]) if state["Status"] == "exited" else 125
                )
            if not junit_path.is_file():
                raise RuntimeError("Pytest did not produce the required JUnit report")
            child_probe_path = (
                junit_path.parent / "child-process-network-probe.json"
            )
            if not child_probe_path.is_file():
                raise RuntimeError(
                    "Pytest container did not produce child-process probe evidence"
                )
            try:
                child_probe = json.loads(
                    child_probe_path.read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError, UnicodeError) as exc:
                raise RuntimeError(
                    "Pytest child-process probe evidence is invalid"
                ) from exc
            listener_records = child_probe_listener.records
            if listener_records != child_probe_control_records:
                raise RuntimeError("Child-process probe listener evidence changed")
            if [
                record["payload"] for record in listener_records
            ] != [f"control-pytest-child:{run_id}"]:
                raise RuntimeError(
                    "The isolated pytest child process reached the listener"
                )
            if (
                not isinstance(child_probe, dict)
                or set(child_probe)
                != {
                    "schema_version", "run_id", "scope", "target",
                    "network_mode", "child_process", "exit_code", "marker",
                    "outcome",
                }
                or type(child_probe["schema_version"]) is not int
                or child_probe["schema_version"] != 1
                or child_probe["run_id"] != run_id
                or child_probe["scope"] != "pytest_container_child_process"
                or child_probe["target"] != "synthetic_host_listener"
                or child_probe["network_mode"] != "none"
                or child_probe["child_process"] is not True
                or type(child_probe["exit_code"]) is not int
                or child_probe["exit_code"] != 0
                or child_probe["marker"] != "PYTEST_CHILD_PROBE_DENIED"
                or child_probe["outcome"] != "denied"
            ):
                raise RuntimeError(
                    "Pytest child-process probe did not provide passing evidence"
                )
            junit_bytes = junit_path.read_bytes()
            junit_stat = junit_path.stat()
            evidence = {
                "schema_version": 6,
                "provenance": {
                    "generator": "scripts/ci_network_isolation.py",
                    "revision": revision,
                },
                "preflight": {
                    "schema_version": 1,
                    "status": "passed",
                    "expected_revision": args.expected_revision,
                    "actual_revision": revision,
                    "worktree": initial_preflight["worktree"],
                    "snapshot_worktree": snapshot_preflight["worktree"],
                    "lock_status": "verified",
                    "requirements_sha256": lock_checksum,
                },
                "source_snapshot": {
                    "schema_version": 1,
                    "revision": revision,
                    "files": source_manifest,
                    "manifest_sha256": _source_manifest_digest(source_manifest),
                },
                "run": {
                    "id": run_id,
                    "started_at": run_started_at,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "revision": revision,
                    "command": "python -m pytest -q -p no:cacheprovider "
                    f"--basetemp=/tmp/pytest "
                    f"--junit-prefix=kybernaut-run-{run_id} "
                    "--junitxml=/reports/junit/results.xml",
                    "pytest_exit_code": pytest_exit_code,
                    "container_id": container_id,
                    "junit": {
                        "path": "junit/results.xml",
                        "sha256": hashlib.sha256(junit_bytes).hexdigest(),
                        "size_bytes": len(junit_bytes),
                        "mtime_ns": junit_stat.st_mtime_ns,
                    },
                },
                "runtime": {
                    "python_version": python_runtime,
                    "base_image": args.base_image,
                    "platform": platform,
                    "requirements_sha256": REQUIREMENTS_SHA256,
                    "test_image_id": image_id,
                },
                "container_inspection": inspection,
                "canary": canary_evidence,
                "pytest_child_process_probe": {
                    **child_probe,
                    "container_id": container_id,
                    "listener": {
                        "scope": "docker_bridge_gateway",
                        "records_before_probe": child_probe_control_records,
                        "records_after_probe": listener_records,
                    },
                },
            }
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if child_probe_listener:
            child_probe_listener.close()
        if container_create_attempted:
            list_command, remove_command = _container_label_commands(
                container_labels
            )
            cleanup_records.append(_safe_cleanup_labeled_resource(
                resource="pytest_container",
                identity=container_name,
                run_id=run_id,
                labels=container_labels,
                list_command=list_command,
                remove_command=remove_command,
                expected_resource_id=container_id,
                operation_timed_out=container_create_timed_out,
            ))
        if image_created and not any(
            item["resource"] == "test_image" for item in cleanup_records
        ):
            list_command, remove_command = _image_label_commands(image_labels)
            cleanup_records.append(_safe_cleanup_labeled_resource(
                resource="test_image",
                identity=image_tag,
                run_id=run_id,
                labels=image_labels,
                list_command=list_command,
                remove_command=remove_command,
                expected_resource_id=image_id,
            ))
        incomplete = [
            item for item in cleanup_records
            if item["status"] != "verified"
        ]
        expected_resource_names = {
            "peer_container",
            "pytest_container",
            "test_image",
        }
        recorded_resource_names = [
            item.get("resource") for item in cleanup_records
        ]
        if (
            len(recorded_resource_names) != len(expected_resource_names)
            or set(recorded_resource_names) != expected_resource_names
        ):
            incomplete.append({
                "resource": "docker_cleanup_evidence",
                "identity": "resource set",
                "status": "unverified",
                "diagnostic": "missing or duplicate resource cleanup record",
            })
        incomplete.extend(
            {
                "resource": "listener",
                "identity": item["listener_id"],
                "status": item["status"],
                "diagnostic": item["diagnostic"],
            }
            for item in listener_cleanup_records
            if item["status"] != "succeeded"
        )
        expected_listener_ids = {
            "host-canary-listener",
            "pytest-child-listener",
        }
        recorded_listener_ids = [
            item.get("listener_id") for item in listener_cleanup_records
        ]
        if (
            len(recorded_listener_ids) != len(expected_listener_ids)
            or set(recorded_listener_ids) != expected_listener_ids
            or any(item.get("run_id") != run_id for item in listener_cleanup_records)
        ):
            incomplete.append({
                "resource": "listener_cleanup_evidence",
                "identity": "listener set",
                "status": "unverified",
                "diagnostic": "missing, duplicate, or cross-run listener record",
            })
        if incomplete:
            summary = "; ".join(
                (
                    f"{item['resource']}={item['status']}"
                    f"(cleanup_exit={item.get('cleanup_exit_code', 'n/a')},"
                    f"verify={item.get('verification_status', 'n/a')},"
                    f"diagnostic={item.get('cleanup_diagnostic', item.get('diagnostic', ''))})"
                )
                for item in incomplete
            )
            print(f"Cleanup incomplete: {summary}", file=sys.stderr)
            if primary_error is not None:
                primary_error.add_note(f"Cleanup failures: {summary}")
        if evidence is None:
            summary = _incomplete_evidence_summary(
                primary_error,
                cleanup_records,
                listener_cleanup_records,
            )
            print(summary, file=sys.stderr)
            if primary_error is not None:
                primary_error.add_note(summary)

    cleanup_evidence = {
        "schema_version": 3,
        "status": _cleanup_status(
            cleanup_records,
            listener_cleanup_records,
            run_id=run_id,
        ),
        "listeners": listener_cleanup_records,
        "resources": cleanup_records,
    }
    if evidence is None:
        raise RuntimeError(
            "Evidence bundle not emitted: harness ended without complete "
            "isolation evidence"
        )
    evidence["cleanup"] = cleanup_evidence
    try:
        _write_evidence(evidence_path, evidence)
    except Exception as exc:
        message = _evidence_write_failure_message(
            exc,
            primary_error,
            cleanup_records,
            listener_cleanup_records,
        )
        print(message, file=sys.stderr)
        if primary_error is not None:
            primary_error.add_note(message)
        raise RuntimeError(message) from exc
    if cleanup_evidence["status"] != "verified":
        raise RuntimeError(
            "Harness cleanup was not fully successful; evidence is not qualified"
        )
    return pytest_exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"CI isolation failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
