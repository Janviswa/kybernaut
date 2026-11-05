#!/usr/bin/env python3
"""Validate a versioned, run-bound Docker network-isolation evidence bundle."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PYTHON_BASE_IMAGES = {
    "3.11.17": "python@sha256:b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377",
    "3.12.15": "python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1",
}
REQUIREMENTS_SHA256 = (
    "461823143e2e58fb9f487347e59178205de9bba3513ce6fc65a52971fe04bff9"
)
REVISION_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
IMAGE_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


class EvidenceError(ValueError):
    """The evidence is missing, malformed, contradictory, or mismatched."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EvidenceError(message)


def _object(value: Any, keys: set[str], label: str) -> dict[str, Any]:
    _require(isinstance(value, dict) and set(value) == keys,
             f"{label} is missing required fields or contains unexpected fields")
    return value


def _timestamp(value: Any, label: str) -> datetime:
    _require(isinstance(value, str), f"{label} timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise EvidenceError(f"{label} timestamp is invalid") from exc
    _require(parsed.tzinfo is not None, f"{label} timestamp lacks a timezone")
    return parsed.astimezone(timezone.utc)


def _validate_listener_records(
    listener: Any,
    *,
    expected_scope: str,
    expected_payload: str,
    started_at: datetime,
    completed_at: datetime,
) -> None:
    listener = _object(listener, {"scope", "records"}, "listener evidence")
    _require(listener["scope"] == expected_scope, "listener scope is unexpected")
    records = listener["records"]
    _require(isinstance(records, list) and len(records) == 1,
             "listener records must contain exactly one control connection")
    record = _object(
        records[0],
        {"payload", "source_ip", "received_at"},
        "listener record",
    )
    _require(record["payload"] == expected_payload,
             "listener record does not match this test run")
    try:
        _require(
            isinstance(record["source_ip"], str),
            "listener source address is invalid",
        )
        ipaddress.ip_address(record["source_ip"])
    except (TypeError, ValueError) as exc:
        raise EvidenceError("listener source address is invalid") from exc
    received_at = _timestamp(record["received_at"], "listener record")
    _require(started_at <= received_at <= completed_at,
             "listener record timestamp is outside the test run")


def _validate_probe(
    probe: Any,
    *,
    network_mode: str,
    outcome: str,
    marker: str,
    label: str,
) -> None:
    probe = _object(
        probe,
        {"network_mode", "exit_code", "marker", "outcome"},
        f"{label} probe",
    )
    _require(probe["network_mode"] == network_mode,
             f"{label} probe network mode is unexpected")
    _require(type(probe["exit_code"]) is int and probe["exit_code"] == 0,
             f"{label} probe did not exit successfully")
    _require(probe["marker"] == marker and probe["outcome"] == outcome,
             f"{label} probe result is invalid")


def _validate_canary(
    canary: Any,
    *,
    run_id: str,
    started_at: datetime,
    completed_at: datetime,
    pytest_container_id: str,
) -> None:
    canary = _object(canary, {"provenance", "host", "peer"}, "canary evidence")
    _require(canary["provenance"] == "ci_network_isolation.py",
             "canary provenance is invalid")

    host = _object(
        canary["host"],
        {"listener", "control", "isolated_probe"},
        "host canary evidence",
    )
    _validate_probe(
        host["control"],
        network_mode="bridge",
        outcome="connected",
        marker="HOST_CONTROL_CONNECTED",
        label="host control",
    )
    _validate_probe(
        host["isolated_probe"],
        network_mode="none",
        outcome="denied",
        marker="HOST_PROBE_DENIED",
        label="host isolated",
    )
    _validate_listener_records(
        host["listener"],
        expected_scope="docker_bridge_gateway",
        expected_payload=f"control-host:{run_id}",
        started_at=started_at,
        completed_at=completed_at,
    )

    peer = _object(
        canary["peer"],
        {"container_id", "listener", "control", "isolated_probe"},
        "peer canary evidence",
    )
    peer_id = peer["container_id"]
    _require(
        isinstance(peer_id, str)
        and re.fullmatch(r"[0-9a-f]{64}", peer_id) is not None
        and peer_id != pytest_container_id,
        "peer listener container identity is invalid",
    )
    _validate_probe(
        peer["control"],
        network_mode="bridge",
        outcome="connected",
        marker="PEER_CONTROL_CONNECTED",
        label="peer control",
    )
    _validate_probe(
        peer["isolated_probe"],
        network_mode="none",
        outcome="denied",
        marker="PEER_PROBE_DENIED",
        label="peer isolated",
    )
    _validate_listener_records(
        peer["listener"],
        expected_scope="peer_container_bridge_address",
        expected_payload=f"control-peer:{run_id}",
        started_at=started_at,
        completed_at=completed_at,
    )


def _validate_pytest_child_process_probe(
    probe: Any,
    *,
    run_id: str,
    container_id: str,
    started_at: datetime,
    completed_at: datetime,
) -> None:
    probe = _object(
        probe,
        {
            "schema_version", "run_id", "scope", "target", "network_mode",
            "child_process", "exit_code", "marker", "outcome", "container_id",
            "listener",
        },
        "pytest child-process probe",
    )
    _require(
        type(probe["schema_version"]) is int and probe["schema_version"] == 1,
        "child-process probe schema is invalid",
    )
    _require(probe["run_id"] == run_id, "child-process probe run is mismatched")
    _require(
        probe["scope"] == "pytest_container_child_process",
        "child-process probe scope is invalid",
    )
    _require(
        probe["target"] == "synthetic_host_listener",
        "child-process probe target is invalid",
    )
    _require(
        probe["network_mode"] == "none" and probe["child_process"] is True,
        "child-process probe did not run in the isolated pytest container",
    )
    _require(
        type(probe["exit_code"]) is int
        and probe["exit_code"] == 0
        and probe["marker"] == "PYTEST_CHILD_PROBE_DENIED"
        and probe["outcome"] == "denied",
        "pytest child-process network probe was not denied",
    )
    _require(
        probe["container_id"] == container_id,
        "child-process probe belongs to another container",
    )
    listener = _object(
        probe["listener"],
        {"scope", "records_before_probe", "records_after_probe"},
        "pytest child-process listener",
    )
    _require(
        listener["scope"] == "docker_bridge_gateway",
        "child-process listener scope is invalid",
    )
    expected_payload = f"control-pytest-child:{run_id}"
    _validate_listener_records(
        {
            "scope": listener["scope"],
            "records": listener["records_before_probe"],
        },
        expected_scope="docker_bridge_gateway",
        expected_payload=expected_payload,
        started_at=started_at,
        completed_at=completed_at,
    )
    _validate_listener_records(
        {
            "scope": listener["scope"],
            "records": listener["records_after_probe"],
        },
        expected_scope="docker_bridge_gateway",
        expected_payload=expected_payload,
        started_at=started_at,
        completed_at=completed_at,
    )
    _require(
        listener["records_before_probe"] == listener["records_after_probe"],
        "isolated child process reached the synthetic listener",
    )


def _validate_container(container: Any, run: dict[str, Any]) -> None:
    container = _object(
        container,
        {"source", "inspected_at", "container_id", "image_id", "config"},
        "Docker inspection evidence",
    )
    _require(container["source"] == "docker inspect",
             "Docker inspection provenance is invalid")
    _require(container["container_id"] == run["container_id"],
             "Docker inspection belongs to another test container")
    _require(
        isinstance(container["image_id"], str)
        and IMAGE_ID_PATTERN.fullmatch(container["image_id"]) is not None,
        "Docker inspection image identity is invalid",
    )
    inspection_time = _timestamp(container["inspected_at"], "Docker inspection")
    _require(
        _timestamp(run["started_at"], "run start")
        <= inspection_time
        <= _timestamp(run["completed_at"], "run completion"),
        "Docker inspection timestamp is outside the test run",
    )
    config = _object(
        container["config"],
        {
            "network_mode", "network_names", "rootfs_read_only", "mounts",
            "tmpfs_destinations", "user", "privileged", "cap_drop", "cap_add",
            "security_options", "pid_mode", "ipc_mode",
        },
        "Docker container configuration",
    )
    _require(config["network_mode"] == "none",
             "inspected pytest container network mode is not none")
    _require(config["network_names"] == ["none"],
             "inspected pytest container has unexpected networks")
    _require(config["rootfs_read_only"] is True,
             "inspected pytest root filesystem is writable")
    _require(
        config["mounts"] == [
            {"destination": "/reports/junit", "read_only": False, "type": "bind"},
            {"destination": "/src", "read_only": True, "type": "bind"},
        ],
        "inspected pytest mounts are unexpected",
    )
    _require(config["tmpfs_destinations"] == ["/tmp"],
             "inspected pytest temporary storage is unexpected")
    _require(
        isinstance(config["user"], str)
        and re.fullmatch(r"[1-9][0-9]*:[0-9]+", config["user"]) is not None,
        "inspected pytest user is not non-root",
    )
    _require(config["privileged"] is False,
             "inspected pytest container is privileged")
    _require(config["cap_drop"] == ["ALL"] and config["cap_add"] == [],
             "inspected pytest capabilities are not restricted")
    _require(
        config["security_options"] == ["no-new-privileges"],
        "inspected pytest container lacks no-new-privileges",
    )
    _require(
        config["pid_mode"] == ""
        and isinstance(config["ipc_mode"], str)
        and config["ipc_mode"] in {"", "private"},
        "inspected pytest container shares a host namespace",
    )


def _validate_preflight(preflight: Any, *, revision: str) -> None:
    preflight = _object(
        preflight,
        {
            "schema_version", "status", "expected_revision", "actual_revision",
            "worktree", "snapshot_worktree", "lock_status",
            "requirements_sha256",
        },
        "source preflight",
    )
    _require(
        type(preflight["schema_version"]) is int
        and preflight["schema_version"] == 1,
        "source preflight schema is invalid",
    )
    _require(
        preflight["status"] == "passed"
        and preflight["worktree"] == "clean"
        and preflight["snapshot_worktree"] == "clean",
        "source preflight is not clean",
    )
    _require(
        preflight["expected_revision"] == revision
        and preflight["actual_revision"] == revision,
        "source preflight revision is mismatched",
    )
    _require(
        preflight["lock_status"] == "verified"
        and preflight["requirements_sha256"] == REQUIREMENTS_SHA256,
        "source preflight lock is not verified",
    )


def _validate_source_snapshot(source_snapshot: Any, *, revision: str) -> None:
    source_snapshot = _object(
        source_snapshot,
        {"schema_version", "revision", "files", "manifest_sha256"},
        "source snapshot",
    )
    _require(
        type(source_snapshot["schema_version"]) is int
        and source_snapshot["schema_version"] == 1,
        "source snapshot schema is invalid",
    )
    _require(
        source_snapshot["revision"] == revision,
        "source snapshot revision is mismatched",
    )
    files = source_snapshot["files"]
    _require(isinstance(files, list) and bool(files),
             "source snapshot manifest is missing")
    paths: list[str] = []
    requirements_entry: dict[str, Any] | None = None
    for item in files:
        item = _object(
            item,
            {"path", "git_blob_oid", "source_sha256", "snapshot_sha256"},
            "source snapshot manifest entry",
        )
        path = item["path"]
        _require(
            isinstance(path, str)
            and bool(path)
            and not path.startswith("/")
            and "\\" not in path
            and all(part not in {"", ".", ".."} for part in path.split("/")),
            "source snapshot manifest path is invalid",
        )
        paths.append(path)
        _require(
            isinstance(item["git_blob_oid"], str)
            and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})",
                             item["git_blob_oid"]) is not None,
            "source snapshot Git blob identity is invalid",
        )
        for digest_field in ("source_sha256", "snapshot_sha256"):
            _require(
                isinstance(item[digest_field], str)
                and re.fullmatch(r"[0-9a-f]{64}", item[digest_field])
                is not None,
                "source snapshot content digest is invalid",
            )
        if path == "requirements.txt":
            requirements_entry = item
    _require(
        paths == sorted(set(paths)),
        "source snapshot manifest paths are duplicated or unordered",
    )
    _require(
        isinstance(source_snapshot["manifest_sha256"], str)
        and re.fullmatch(
            r"[0-9a-f]{64}", source_snapshot["manifest_sha256"]
        ) is not None,
        "source snapshot manifest digest is invalid",
    )
    canonical = json.dumps(
        files,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    _require(
        hashlib.sha256(canonical).hexdigest()
        == source_snapshot["manifest_sha256"],
        "source snapshot manifest digest does not match its entries",
    )
    _require(
        requirements_entry is not None
        and requirements_entry["source_sha256"] == REQUIREMENTS_SHA256
        and requirements_entry["snapshot_sha256"] == REQUIREMENTS_SHA256,
        "source snapshot requirements bytes are not approved",
    )


def _validate_cleanup(
    cleanup: Any,
    *,
    run_id: str,
    expected_resource_ids: dict[str, str],
) -> None:
    cleanup = _object(
        cleanup,
        {"schema_version", "status", "listeners", "resources"},
        "cleanup evidence",
    )
    _require(
        type(cleanup["schema_version"]) is int
        and cleanup["schema_version"] == 3,
        "cleanup evidence schema is invalid",
    )
    listeners = cleanup["listeners"]
    _require(isinstance(listeners, list), "listener cleanup records are malformed")
    required_listeners = {"host-canary-listener", "pytest-child-listener"}
    actual_listeners: set[str] = set()
    for listener in listeners:
        listener = _object(
            listener,
            {
                "listener_id", "run_id", "attempted", "socket_closed",
                "thread_stopped", "status", "diagnostic",
            },
            "listener cleanup",
        )
        listener_id = listener["listener_id"]
        _require(
            isinstance(listener_id, str)
            and listener_id in required_listeners
            and listener_id not in actual_listeners,
            "listener cleanup identity is missing or duplicated",
        )
        actual_listeners.add(listener_id)
        _require(
            listener["run_id"] == run_id,
            "listener cleanup run identifier is mismatched",
        )
        _require(
            listener["attempted"] is True
            and listener["socket_closed"] is True
            and listener["thread_stopped"] is True
            and listener["status"] == "succeeded"
            and listener["diagnostic"] == "",
            "listener cleanup is incomplete or contradictory",
        )
    _require(
        actual_listeners == required_listeners,
        "listener cleanup is missing required listeners",
    )
    resources = cleanup["resources"]
    _require(isinstance(resources, list), "cleanup resources are malformed")
    required_resources = {"peer_container", "pytest_container", "test_image"}
    actual_resources: set[str] = set()
    actual_resource_ids: set[str] = set()
    for item in resources:
        item = _object(
            item,
            {
                "resource", "identity", "run_id", "resource_id",
                "cleanup_attempted",
                "cleanup_exit_code", "cleanup_diagnostic",
                "verification_exit_code", "verification_status",
                "verification_diagnostic", "status",
            },
            "resource cleanup",
        )
        _require(
            item["resource"] in required_resources
            and item["resource"] not in actual_resources,
            "cleanup resource set is contradictory",
        )
        actual_resources.add(item["resource"])
        _require(
            isinstance(item["identity"], str) and bool(item["identity"]),
            "cleanup resource identity is invalid",
        )
        _require(
            item["run_id"] == run_id,
            "cleanup resource run identifier is mismatched",
        )
        resource_id = item["resource_id"]
        pattern = (
            r"(?:sha256:)?[0-9a-f]{64}"
            if item["resource"] == "test_image"
            else r"[0-9a-f]{64}"
        )
        _require(
            isinstance(resource_id, str)
            and re.fullmatch(pattern, resource_id) is not None,
            "cleanup authoritative resource identity is malformed",
        )
        _require(
            resource_id not in actual_resource_ids,
            "cleanup resource identities are duplicated",
        )
        actual_resource_ids.add(resource_id)
        _require(
            resource_id == expected_resource_ids[item["resource"]],
            "cleanup resource identity does not match the resource created by this run",
        )
        _require(
            item["cleanup_attempted"] is True
            and type(item["cleanup_exit_code"]) is int
            and item["cleanup_exit_code"] == 0
            and item["cleanup_diagnostic"] == ""
            and type(item["verification_exit_code"]) is int
            and item["verification_exit_code"] == 0
            and item["verification_status"] == "removed"
            and item["verification_diagnostic"] == ""
            and item["status"] == "verified",
            "resource cleanup is incomplete or contradictory",
        )
    _require(
        actual_resources == required_resources,
        "cleanup evidence is missing required resources",
    )
    _require(
        cleanup["status"] == "verified",
        "overall cleanup status contradicts resource evidence",
    )


def _junit_counts(path: Path, run_id: str) -> dict[str, int]:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError, ValueError) as exc:
        raise EvidenceError("JUnit report is missing or invalid XML") from exc
    _require(root.tag in {"testsuite", "testsuites"},
             "JUnit report has an unexpected root element")
    cases = root.findall(".//testcase")
    _require(bool(cases), "JUnit report contains no test cases")
    summary_fields = ("tests", "failures", "errors", "skipped")

    def summary_counts(suite: ET.Element) -> dict[str, int]:
        counts: dict[str, int] = {}
        for field in summary_fields:
            raw_value = suite.get(field)
            _require(
                isinstance(raw_value, str)
                and re.fullmatch(r"[0-9]+", raw_value) is not None,
                f"JUnit summary field {field} is missing or malformed",
            )
            counts[field] = int(raw_value)
        return counts

    suites = [root] if root.tag == "testsuite" else root.findall(".//testsuite")
    _require(bool(suites), "JUnit report contains no testsuite summary")
    for case in cases:
        outcome_count = sum(
            len(case.findall(result_tag))
            for result_tag in ("failure", "error", "skipped")
        )
        _require(
            outcome_count <= 1,
            "JUnit testcase has contradictory result annotations",
        )
    reported = {field: 0 for field in summary_fields}
    for suite in suites:
        suite_counts = summary_counts(suite)
        suite_cases = suite.findall(".//testcase")
        annotated = {
            "tests": len(suite_cases),
            "failures": sum(len(case.findall("failure")) for case in suite_cases),
            "errors": sum(len(case.findall("error")) for case in suite_cases),
            "skipped": sum(len(case.findall("skipped")) for case in suite_cases),
        }
        _require(
            all(suite_counts[field] == annotated[field] for field in summary_fields),
            "JUnit suite summary contradicts its test cases",
        )
        reported = {
            field: reported[field] + suite_counts[field]
            for field in summary_fields
        }
    _require(
        reported["tests"] == len(cases)
        and reported["failures"]
        == sum(len(case.findall("failure")) for case in cases)
        and reported["errors"]
        == sum(len(case.findall("error")) for case in cases)
        and reported["skipped"]
        == sum(len(case.findall("skipped")) for case in cases),
        "JUnit summary totals contradict the report",
    )
    if root.tag == "testsuites" and any(
        root.get(field) is not None for field in summary_fields
    ):
        root_counts = summary_counts(root)
        _require(
            all(root_counts[field] == reported[field] for field in summary_fields),
            "JUnit root summary contradicts its test suites",
        )
    expected_prefix = f"kybernaut-run-{run_id}."
    _require(
        all(
            isinstance(case.get("classname"), str)
            and case.get("classname", "").startswith(expected_prefix)
            for case in cases
        ),
        "JUnit report is associated with another test run",
    )
    return reported


def validate(evidence_path: str | Path, junit_path: str | Path) -> None:
    evidence_file = Path(evidence_path)
    report_file = Path(junit_path)
    try:
        evidence_bytes = evidence_file.read_bytes()
        evidence = json.loads(evidence_bytes)
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise EvidenceError("isolation evidence is missing or invalid JSON") from exc
    _require(isinstance(evidence, dict), "isolation evidence has an invalid structure")
    _require(type(evidence.get("schema_version")) is int
             and evidence.get("schema_version") == 6,
             "isolation evidence has an unsupported schema version")
    _object(
        evidence,
        {"schema_version", "provenance", "preflight", "source_snapshot",
         "run", "runtime",
         "container_inspection", "canary", "pytest_child_process_probe",
         "cleanup"},
        "isolation evidence",
    )
    provenance = _object(
        evidence["provenance"], {"generator", "revision"}, "provenance"
    )
    _require(provenance["generator"] == "scripts/ci_network_isolation.py",
             "evidence generator is unsupported")
    _require(
        isinstance(provenance["revision"], str)
        and REVISION_PATTERN.fullmatch(provenance["revision"]) is not None,
        "evidence revision is invalid",
    )

    run = _object(
        evidence["run"],
        {"id", "started_at", "completed_at", "revision", "command",
         "pytest_exit_code", "container_id", "junit"},
        "test run",
    )
    try:
        _require(str(uuid.UUID(run["id"])) == run["id"],
                 "test-run identifier is invalid")
    except (ValueError, TypeError, AttributeError) as exc:
        raise EvidenceError("test-run identifier is invalid") from exc
    started_at = _timestamp(run["started_at"], "run start")
    completed_at = _timestamp(run["completed_at"], "run completion")
    _require(started_at <= completed_at, "test-run timestamps are inconsistent")
    _require(
        run["revision"] == provenance["revision"],
        "test-run revision does not match evidence provenance",
    )
    _validate_preflight(evidence["preflight"], revision=run["revision"])
    _validate_source_snapshot(evidence["source_snapshot"], revision=run["revision"])
    _require(
        run["command"] == "python -m pytest -q -p no:cacheprovider "
        f"--basetemp=/tmp/pytest --junit-prefix=kybernaut-run-{run['id']} "
        "--junitxml=/reports/junit/results.xml",
        "test command is unexpected",
    )
    _require(type(run["pytest_exit_code"]) is int
             and 0 <= run["pytest_exit_code"] <= 255,
             "pytest exit code is invalid")
    _require(
        isinstance(run["container_id"], str)
        and re.fullmatch(r"[0-9a-f]{64}", run["container_id"]) is not None,
        "pytest container identity is invalid",
    )
    junit = _object(
        run["junit"], {"path", "sha256", "size_bytes", "mtime_ns"}, "JUnit identity"
    )
    _require(junit["path"] == "junit/results.xml",
             "JUnit report path is unexpected")
    _require(report_file.is_file(), "JUnit report is missing")
    try:
        report_bytes = report_file.read_bytes()
        report_stat = report_file.stat()
    except OSError as exc:
        raise EvidenceError("JUnit report is missing or unreadable") from exc
    _require(
        isinstance(junit["sha256"], str)
        and hashlib.sha256(report_bytes).hexdigest() == junit["sha256"],
        "JUnit report checksum does not match this test run",
    )
    _require(type(junit["size_bytes"]) is int
             and len(report_bytes) == junit["size_bytes"],
             "JUnit report size does not match this test run")
    _require(type(junit["mtime_ns"]) is int
             and report_stat.st_mtime_ns == junit["mtime_ns"],
             "JUnit report timestamp does not match this test run")
    report_time = datetime.fromtimestamp(
        report_stat.st_mtime_ns / 1_000_000_000,
        timezone.utc,
    )
    _require(started_at <= report_time <= completed_at,
             "JUnit report timestamp is outside the test run")
    counts = _junit_counts(report_file, run["id"])
    junit_has_failures = counts["failures"] > 0 or counts["errors"] > 0
    _require(
        (run["pytest_exit_code"] == 0 and not junit_has_failures)
        or (run["pytest_exit_code"] != 0 and junit_has_failures),
        "JUnit report contradicts the pytest exit code",
    )

    runtime = _object(
        evidence["runtime"],
        {
            "python_version", "base_image", "platform", "requirements_sha256",
            "test_image_id",
        },
        "runtime",
    )
    _require(
        isinstance(runtime["python_version"], str)
        and runtime["python_version"] in PYTHON_BASE_IMAGES,
             "runtime reports an unsupported Python version")
    _require(runtime["base_image"] == PYTHON_BASE_IMAGES[runtime["python_version"]],
             "runtime reports an unapproved base image digest")
    _require(runtime["platform"] == "linux/amd64",
             "runtime reports an unexpected image platform")
    _require(runtime["requirements_sha256"] == REQUIREMENTS_SHA256,
             "runtime reports an unapproved requirements checksum")
    _require(
        isinstance(runtime["test_image_id"], str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", runtime["test_image_id"])
        is not None,
        "runtime test image identity is invalid",
    )

    _validate_container(evidence["container_inspection"], run)
    _require(
        evidence["container_inspection"]["image_id"] == runtime["test_image_id"],
        "runtime test image identity does not match inspected container",
    )
    _validate_canary(
        evidence["canary"],
        run_id=run["id"],
        started_at=started_at,
        completed_at=completed_at,
        pytest_container_id=run["container_id"],
    )
    _validate_pytest_child_process_probe(
        evidence["pytest_child_process_probe"],
        run_id=run["id"],
        container_id=run["container_id"],
        started_at=started_at,
        completed_at=completed_at,
    )
    _validate_cleanup(
        evidence["cleanup"],
        run_id=run["id"],
        expected_resource_ids={
            "peer_container": evidence["canary"]["peer"]["container_id"],
            "pytest_container": run["container_id"],
            "test_image": runtime["test_image_id"],
        },
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print(
            "usage: validate_network_isolation_evidence.py EVIDENCE.json JUNIT.xml",
            file=sys.stderr,
        )
        return 2
    try:
        validate(args[0], args[1])
    except EvidenceError as exc:
        print(f"Network isolation evidence validation failed: {exc}", file=sys.stderr)
        return 1
    print("Network isolation evidence passed validation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
