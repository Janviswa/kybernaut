from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from scripts.validate_network_isolation_evidence import (
    EvidenceError,
    main,
    validate,
)


REVISION = "a" * 40
RUN_ID = str(uuid.uuid4())
CONTAINER_ID = "b" * 64
PEER_ID = "c" * 64
IMAGE_ID = "sha256:" + "d" * 64


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _record(payload: str, received_at: datetime) -> dict[str, str]:
    return {
        "payload": payload,
        "source_ip": "172.17.0.2",
        "received_at": _timestamp(received_at),
    }


def _probe(mode: str, outcome: str, marker: str) -> dict[str, object]:
    return {
        "network_mode": mode,
        "exit_code": 0,
        "marker": marker,
        "outcome": outcome,
    }


def _cleanup_evidence() -> dict[str, object]:
    resources = []
    for name, identity, resource_id in (
        ("peer_container", "kybernaut-ci-peer-run", PEER_ID),
        ("pytest_container", "kybernaut-ci-pytest-run", CONTAINER_ID),
        ("test_image", "kybernaut-ci-py312:run", IMAGE_ID),
    ):
        resources.append({
            "resource": name,
            "identity": identity,
            "run_id": RUN_ID,
            "resource_id": resource_id,
            "cleanup_attempted": True,
            "cleanup_exit_code": 0,
            "cleanup_diagnostic": "",
            "verification_exit_code": 0,
            "verification_status": "removed",
            "verification_diagnostic": "",
            "status": "verified",
        })
    return {
        "schema_version": 3,
        "status": "verified",
        "listeners": [
            {
                "listener_id": listener_id,
                "run_id": RUN_ID,
                "attempted": True,
                "socket_closed": True,
                "thread_stopped": True,
                "status": "succeeded",
                "diagnostic": "",
            }
            for listener_id in (
                "host-canary-listener",
                "pytest-child-listener",
            )
        ],
        "resources": resources,
    }


def _evidence_fixture(tmp_path):
    report = tmp_path / "results.xml"
    report.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        f'<testcase classname="kybernaut-run-{RUN_ID}.example" '
        'name="passes"/></testsuite>',
        encoding="utf-8",
    )
    report_stat = report.stat()
    report_time = datetime.fromtimestamp(
        report_stat.st_mtime_ns / 1_000_000_000,
        timezone.utc,
    )
    started = report_time - timedelta(seconds=2)
    completed = report_time + timedelta(seconds=2)
    evidence = {
        "schema_version": 6,
        "provenance": {
            "generator": "scripts/ci_network_isolation.py",
            "revision": REVISION,
        },
        "preflight": {
            "schema_version": 1,
            "status": "passed",
            "expected_revision": REVISION,
            "actual_revision": REVISION,
            "worktree": "clean",
            "snapshot_worktree": "clean",
            "lock_status": "verified",
            "requirements_sha256": (
                "29fab34c58b0f3130bd216f26bcf1bc5d662eac91474bdf592f68509b2e403a3"
            ),
        },
        "source_snapshot": {
            "schema_version": 1,
            "revision": REVISION,
            "files": [{
                "path": "requirements.txt",
                "git_blob_oid": "f" * 40,
                "source_sha256": (
                    "29fab34c58b0f3130bd216f26bcf1bc5d662eac91474bdf592f68509b2e403a3"
                ),
                "snapshot_sha256": (
                    "29fab34c58b0f3130bd216f26bcf1bc5d662eac91474bdf592f68509b2e403a3"
                ),
            }],
            "manifest_sha256": "",
        },
        "run": {
            "id": RUN_ID,
            "started_at": _timestamp(started),
            "completed_at": _timestamp(completed),
            "revision": REVISION,
            "command": "python -m pytest -q -p no:cacheprovider "
            f"--basetemp=/tmp/pytest --junit-prefix=kybernaut-run-{RUN_ID} "
            "--junitxml=/reports/junit/results.xml",
            "pytest_exit_code": 0,
            "container_id": CONTAINER_ID,
            "junit": {
                "path": "junit/results.xml",
                "sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
                "size_bytes": report_stat.st_size,
                "mtime_ns": report_stat.st_mtime_ns,
            },
        },
        "runtime": {
            "python_version": "3.11.17",
            "base_image": (
                "python@sha256:"
                "b7a9e5f6a93d6d4a31f129bbd4bdee69efccb6ded0fb230d78aafaf37af98377"
            ),
            "platform": "linux/amd64",
            "requirements_sha256": (
                "29fab34c58b0f3130bd216f26bcf1bc5d662eac91474bdf592f68509b2e403a3"
            ),
            "test_image_id": IMAGE_ID,
        },
        "container_inspection": {
            "source": "docker inspect",
            "inspected_at": _timestamp(report_time),
            "container_id": CONTAINER_ID,
            "image_id": IMAGE_ID,
            "config": {
                "network_mode": "none",
                "network_names": ["none"],
                "rootfs_read_only": True,
                "mounts": [
                    {
                        "destination": "/reports/junit",
                        "read_only": False,
                        "type": "bind",
                    },
                    {"destination": "/src", "read_only": True, "type": "bind"},
                ],
                "tmpfs_destinations": ["/tmp"],
                "user": "1000:1000",
                "privileged": False,
                "cap_drop": ["ALL"],
                "cap_add": [],
                "security_options": ["no-new-privileges"],
                "pid_mode": "",
                "ipc_mode": "private",
            },
        },
        "canary": {
            "provenance": "ci_network_isolation.py",
            "host": {
                "listener": {
                    "scope": "docker_bridge_gateway",
                    "records": [
                        _record(f"control-host:{RUN_ID}", report_time),
                    ],
                },
                "control": _probe(
                    "bridge", "connected", "HOST_CONTROL_CONNECTED"
                ),
                "isolated_probe": _probe(
                    "none", "denied", "HOST_PROBE_DENIED"
                ),
            },
            "peer": {
                "container_id": PEER_ID,
                "listener": {
                    "scope": "peer_container_bridge_address",
                    "records": [
                        _record(f"control-peer:{RUN_ID}", report_time),
                    ],
                },
                "control": _probe(
                    "bridge", "connected", "PEER_CONTROL_CONNECTED"
                ),
                "isolated_probe": _probe(
                    "none", "denied", "PEER_PROBE_DENIED"
                ),
            },
        },
        "pytest_child_process_probe": {
            "schema_version": 1,
            "run_id": RUN_ID,
            "scope": "pytest_container_child_process",
            "target": "synthetic_host_listener",
            "network_mode": "none",
            "child_process": True,
            "exit_code": 0,
            "marker": "PYTEST_CHILD_PROBE_DENIED",
            "outcome": "denied",
            "container_id": CONTAINER_ID,
            "listener": {
                "scope": "docker_bridge_gateway",
                "records_before_probe": [
                    _record(f"control-pytest-child:{RUN_ID}", report_time),
                ],
                "records_after_probe": [
                    _record(f"control-pytest-child:{RUN_ID}", report_time),
                ],
            },
        },
        "cleanup": _cleanup_evidence(),
    }
    manifest_bytes = json.dumps(
        evidence["source_snapshot"]["files"],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    evidence["source_snapshot"]["manifest_sha256"] = hashlib.sha256(
        manifest_bytes
    ).hexdigest()
    evidence_path = tmp_path / "network-isolation.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    return evidence_path, report, evidence


def _write_evidence(path, evidence):
    path.write_text(json.dumps(evidence), encoding="utf-8")


def _replace_junit_report(evidence_path, report_path, evidence, contents):
    report_path.write_text(contents, encoding="utf-8")
    report_stat = report_path.stat()
    report_time = datetime.fromtimestamp(
        report_stat.st_mtime_ns / 1_000_000_000,
        timezone.utc,
    )
    evidence["run"]["started_at"] = _timestamp(report_time - timedelta(seconds=2))
    evidence["run"]["completed_at"] = _timestamp(report_time + timedelta(seconds=2))
    evidence["run"]["junit"].update({
        "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "size_bytes": report_stat.st_size,
        "mtime_ns": report_stat.st_mtime_ns,
    })
    _write_evidence(evidence_path, evidence)


def test_complete_evidence_bundle_is_valid(tmp_path):
    evidence_path, report_path, _ = _evidence_fixture(tmp_path)

    validate(evidence_path, report_path)


def test_missing_cleanup_evidence_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    del evidence["cleanup"]
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="missing required fields"):
        validate(evidence_path, report_path)


def test_malformed_cleanup_evidence_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["cleanup"] = {"schema_version": 1, "status": "succeeded"}
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="missing required fields"):
        validate(evidence_path, report_path)


def test_missing_source_snapshot_evidence_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    del evidence["source_snapshot"]
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="missing required fields"):
        validate(evidence_path, report_path)


def test_wrong_source_snapshot_revision_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["source_snapshot"]["revision"] = "e" * 40
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="source snapshot revision"):
        validate(evidence_path, report_path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("snapshot_sha256", "0" * 64, "manifest digest does not match"),
        ("git_blob_oid", "invalid", "Git blob identity is invalid"),
    ],
)
def test_malformed_source_snapshot_manifest_is_rejected(
    tmp_path, field, value, message
):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["source_snapshot"]["files"][0][field] = value
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match=message):
        validate(evidence_path, report_path)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda cleanup: cleanup.update(status="failed"), "overall cleanup status"),
        (
            lambda cleanup: cleanup["resources"][0].update(status="failed"),
            "resource cleanup is incomplete",
        ),
        (
            lambda cleanup: cleanup["resources"][0].update(
                verification_status="present"
            ),
            "resource cleanup is incomplete",
        ),
        (
            lambda cleanup: cleanup["listeners"][0].update(thread_stopped=False),
            "listener cleanup is incomplete or contradictory",
        ),
        (
            lambda cleanup: cleanup["resources"].pop(),
            "missing required resources",
        ),
    ],
)
def test_incomplete_or_contradictory_cleanup_is_rejected(
    tmp_path, change, message
):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    change(evidence["cleanup"])
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match=message):
        validate(evidence_path, report_path)


@pytest.mark.parametrize(
    ("resource", "resource_id"),
    [
        ("pytest_container", "e" * 64),
        ("peer_container", "e" * 64),
        ("test_image", "sha256:" + "e" * 64),
    ],
)
def test_cleanup_identity_must_match_created_run_resource(
    tmp_path, resource, resource_id
):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    cleanup_record = next(
        item for item in evidence["cleanup"]["resources"]
        if item["resource"] == resource
    )
    cleanup_record["resource_id"] = resource_id
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="does not match the resource created"):
        validate(evidence_path, report_path)


def test_cleanup_record_from_another_run_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["cleanup"]["resources"][0]["run_id"] = str(uuid.uuid4())
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="run identifier is mismatched"):
        validate(evidence_path, report_path)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            lambda cleanup: cleanup["listeners"].pop(),
            "missing required listeners",
        ),
        (
            lambda cleanup: cleanup["listeners"].append(
                dict(cleanup["listeners"][0])
            ),
            "missing or duplicated",
        ),
        (
            lambda cleanup: cleanup["listeners"][0].update(
                listener_id="pytest_container"
            ),
            "missing or duplicated",
        ),
        (
            lambda cleanup: cleanup["listeners"][0].update(run_id=str(uuid.uuid4())),
            "run identifier is mismatched",
        ),
        (
            lambda cleanup: cleanup["listeners"][0].update(status="failed"),
            "listener cleanup is incomplete or contradictory",
        ),
    ],
)
def test_listener_cleanup_must_be_complete_and_bound_to_run(
    tmp_path, change, message
):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    change(evidence["cleanup"])
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match=message):
        validate(evidence_path, report_path)


def test_old_cleanup_listener_schema_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["cleanup"]["schema_version"] = 2
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="cleanup evidence schema is invalid"):
        validate(evidence_path, report_path)


def test_duplicate_cleanup_resource_identity_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["cleanup"]["resources"][1]["resource_id"] = PEER_ID
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="identities are duplicated"):
        validate(evidence_path, report_path)


def test_malformed_cleanup_resource_identity_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["cleanup"]["resources"][0]["resource_id"] = "short"
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="identity is malformed"):
        validate(evidence_path, report_path)


def test_missing_cleanup_resource_identity_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    del evidence["cleanup"]["resources"][0]["resource_id"]
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="missing required fields"):
        validate(evidence_path, report_path)


def test_old_cleanup_identity_schema_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["cleanup"]["schema_version"] = 1
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="cleanup evidence schema is invalid"):
        validate(evidence_path, report_path)


def test_runtime_image_identity_must_match_inspected_container(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["runtime"]["test_image_id"] = "sha256:" + "e" * 64
    _write_evidence(evidence_path, evidence)

    with pytest.raises(
        EvidenceError,
        match="does not match inspected container",
    ):
        validate(evidence_path, report_path)


def test_schema_four_cleanup_bundle_is_intentionally_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["schema_version"] = 4
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="unsupported schema"):
        validate(evidence_path, report_path)


def test_schema_five_evidence_bundle_is_intentionally_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["schema_version"] = 5
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="unsupported schema version"):
        validate(evidence_path, report_path)


def test_unverified_resource_cleanup_is_rejected_as_complete_bundle(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["cleanup"]["status"] = "unverified"
    resource = evidence["cleanup"]["resources"][0]
    resource.update({
        "cleanup_exit_code": None,
        "cleanup_diagnostic": "resource cleanup could not be established",
        "verification_exit_code": None,
        "verification_status": "unverified",
        "verification_diagnostic": "resource verification timed out",
        "status": "unverified",
    })
    _write_evidence(evidence_path, evidence)

    with pytest.raises(
        EvidenceError,
        match="resource cleanup is incomplete or contradictory",
    ):
        validate(evidence_path, report_path)


def test_wrong_expected_revision_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["preflight"]["expected_revision"] = "e" * 40
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="preflight revision is mismatched"):
        validate(evidence_path, report_path)


def test_dirty_snapshot_preflight_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["preflight"]["snapshot_worktree"] = "dirty"
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="preflight is not clean"):
        validate(evidence_path, report_path)


def test_unverified_preflight_lock_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["preflight"]["lock_status"] = "unverified"
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="preflight lock is not verified"):
        validate(evidence_path, report_path)


def test_missing_preflight_lock_evidence_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    del evidence["preflight"]["requirements_sha256"]
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="missing required fields"):
        validate(evidence_path, report_path)


@pytest.mark.parametrize("expected_revision", [None, "not-a-revision"])
def test_missing_or_malformed_expected_revision_is_rejected(
    tmp_path, expected_revision
):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["preflight"]["expected_revision"] = expected_revision
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="preflight revision is mismatched"):
        validate(evidence_path, report_path)


def test_missing_child_process_probe_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    del evidence["pytest_child_process_probe"]
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="missing required fields"):
        validate(evidence_path, report_path)


def test_malformed_child_process_probe_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["pytest_child_process_probe"] = {
        "schema_version": 1,
        "outcome": "denied",
    }
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="missing required fields"):
        validate(evidence_path, report_path)


def test_child_process_probe_run_id_must_match_enclosing_run(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["pytest_child_process_probe"]["run_id"] = str(uuid.uuid4())
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="run is mismatched"):
        validate(evidence_path, report_path)


def test_child_process_probe_container_id_must_match_pytest_container(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["pytest_child_process_probe"]["container_id"] = "e" * 64
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="another container"):
        validate(evidence_path, report_path)


def test_child_process_connection_to_synthetic_listener_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["pytest_child_process_probe"]["outcome"] = "connected"
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="probe was not denied"):
        validate(evidence_path, report_path)


def test_contradictory_child_process_probe_marker_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["pytest_child_process_probe"]["marker"] = (
        "PYTEST_CHILD_PROBE_CONNECTED"
    )
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="probe was not denied"):
        validate(evidence_path, report_path)


def test_child_process_listener_records_must_show_no_probe_connection(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["pytest_child_process_probe"]["listener"][
        "records_after_probe"
    ].append(_record(f"pytest-child:{RUN_ID}", datetime.now(timezone.utc)))
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="listener records"):
        validate(evidence_path, report_path)


def test_missing_listener_records_are_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["canary"]["host"]["listener"]["records"] = []
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="listener records"):
        validate(evidence_path, report_path)


def test_summary_counter_cannot_override_listener_records(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["host_listener_control_connections"] = 0
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="unexpected fields"):
        validate(evidence_path, report_path)


def test_missing_docker_inspection_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    del evidence["container_inspection"]
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="missing required fields"):
        validate(evidence_path, report_path)


def test_incorrect_inspected_network_mode_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["container_inspection"]["config"]["network_mode"] = "bridge"
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="network mode"):
        validate(evidence_path, report_path)


@pytest.mark.parametrize("listener_name", ["host", "peer"])
def test_isolated_listener_connection_is_rejected(tmp_path, listener_name):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    listener = evidence["canary"][listener_name]["listener"]["records"]
    listener.append(_record(f"isolated:{RUN_ID}", datetime.now(timezone.utc)))
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="exactly one control"):
        validate(evidence_path, report_path)


def test_failed_networked_control_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["canary"]["host"]["control"]["exit_code"] = 1
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="did not exit successfully"):
        validate(evidence_path, report_path)


def test_listener_record_from_another_run_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["canary"]["peer"]["listener"]["records"][0]["payload"] = (
        f"control-peer:{uuid.uuid4()}"
    )
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="does not match this test run"):
        validate(evidence_path, report_path)


@pytest.mark.parametrize(
    ("version", "digest"),
    [
        ("3.14.0", None),
        ("3.11.17", "python@sha256:" + "e" * 64),
    ],
)
def test_wrong_runtime_or_image_digest_is_rejected(
    tmp_path, version, digest
):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["runtime"]["python_version"] = version
    if digest is not None:
        evidence["runtime"]["base_image"] = digest
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError):
        validate(evidence_path, report_path)


def test_wrong_lock_checksum_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["runtime"]["requirements_sha256"] = "0" * 64
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="requirements checksum"):
        validate(evidence_path, report_path)


def test_mismatched_run_identifier_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["run"]["id"] = str(uuid.uuid4())
    evidence["run"]["command"] = (
        "python -m pytest -q -p no:cacheprovider --basetemp=/tmp/pytest "
        f"--junit-prefix=kybernaut-run-{evidence['run']['id']} "
        "--junitxml=/reports/junit/results.xml"
    )
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="another test run"):
        validate(evidence_path, report_path)


def test_mismatched_junit_checksum_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["run"]["junit"]["sha256"] = "0" * 64
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="checksum"):
        validate(evidence_path, report_path)


def test_junit_report_from_another_run_is_rejected(tmp_path):
    evidence_path, report_path, _ = _evidence_fixture(tmp_path)
    report_path.write_text(
        '<testsuite tests="1"><testcase name="different-run"/></testsuite>',
        encoding="utf-8",
    )

    with pytest.raises(EvidenceError, match="checksum"):
        validate(evidence_path, report_path)


def test_junit_report_with_another_run_id_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    report_path.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        '<testcase classname="kybernaut-run-other.example" '
        'name="different-run"/></testsuite>',
        encoding="utf-8",
    )
    report_stat = report_path.stat()
    evidence["run"]["junit"]["sha256"] = hashlib.sha256(
        report_path.read_bytes()
    ).hexdigest()
    evidence["run"]["junit"]["size_bytes"] = report_stat.st_size
    evidence["run"]["junit"]["mtime_ns"] = report_stat.st_mtime_ns
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="another test run"):
        validate(evidence_path, report_path)


def test_nonzero_pytest_exit_with_passing_junit_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["run"]["pytest_exit_code"] = 1
    contents = (
        '<testsuite tests="1" failures="0" errors="0" skipped="0">'
        f'<testcase classname="kybernaut-run-{RUN_ID}.example" '
        'name="passes"/></testsuite>'
    )
    _replace_junit_report(evidence_path, report_path, evidence, contents)

    with pytest.raises(EvidenceError, match="contradicts the pytest exit code"):
        validate(evidence_path, report_path)


def test_zero_pytest_exit_with_junit_failure_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    contents = (
        '<testsuite tests="1" failures="1" errors="0" skipped="0">'
        f'<testcase classname="kybernaut-run-{RUN_ID}.example" '
        'name="fails"><failure message="failure"/></testcase></testsuite>'
    )
    _replace_junit_report(evidence_path, report_path, evidence, contents)

    with pytest.raises(EvidenceError, match="contradicts the pytest exit code"):
        validate(evidence_path, report_path)


@pytest.mark.parametrize(
    ("summary", "message"),
    [
        ('tests="1" errors="0" skipped="0"', "failures is missing or malformed"),
        (
            'tests="2" failures="0" errors="0" skipped="0"',
            "summary contradicts its test cases",
        ),
        (
            'tests="1" failures="0" errors="0" skipped="1"',
            "summary contradicts its test cases",
        ),
        (
            'tests="1" failures="0" errors="0" skipped="0"',
            "root summary contradicts its test suites",
        ),
        (
            'tests="1" failures="not-a-number" errors="0" skipped="0"',
            "failures is missing or malformed",
        ),
    ],
)
def test_missing_or_contradictory_junit_summary_is_rejected(
    tmp_path, summary, message
):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    if message == "root summary contradicts its test suites":
        contents = (
            '<testsuites tests="2" failures="0" errors="0" skipped="0">'
            '<testsuite tests="1" failures="0" errors="0" skipped="0">'
            f'<testcase classname="kybernaut-run-{RUN_ID}.example" '
            'name="passes"/></testsuite></testsuites>'
        )
    else:
        contents = (
            f"<testsuite {summary}>"
            f'<testcase classname="kybernaut-run-{RUN_ID}.example" '
            'name="passes"/></testsuite>'
        )
    _replace_junit_report(evidence_path, report_path, evidence, contents)

    with pytest.raises(EvidenceError, match=message):
        validate(evidence_path, report_path)


def test_unsupported_schema_version_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["schema_version"] = 99
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="unsupported schema"):
        validate(evidence_path, report_path)


def test_legacy_summary_bundle_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    evidence["schema_version"] = 1
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="unsupported schema"):
        validate(evidence_path, report_path)


@pytest.mark.parametrize("contents", ["{", '{"schema_version":'])
def test_malformed_json_is_rejected(tmp_path, contents):
    evidence_path, report_path, _ = _evidence_fixture(tmp_path)
    evidence_path.write_text(contents, encoding="utf-8")

    with pytest.raises(EvidenceError, match="invalid JSON"):
        validate(evidence_path, report_path)


def test_missing_required_field_is_rejected(tmp_path):
    evidence_path, report_path, evidence = _evidence_fixture(tmp_path)
    del evidence["runtime"]["platform"]
    _write_evidence(evidence_path, evidence)

    with pytest.raises(EvidenceError, match="missing required fields"):
        validate(evidence_path, report_path)


def test_cli_returns_nonzero_for_missing_report(tmp_path, capsys):
    evidence_path, _, _ = _evidence_fixture(tmp_path)

    assert main([str(evidence_path), str(tmp_path / "missing.xml")]) == 1
    assert "JUnit report is missing" in capsys.readouterr().err
