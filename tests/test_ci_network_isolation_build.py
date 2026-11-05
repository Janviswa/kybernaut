from __future__ import annotations

import hashlib
import socket
import subprocess
import time
from pathlib import Path

import pytest

from scripts import ci_network_isolation as isolation

RUN_ID = "run-identifier"
REQUIREMENTS_BYTES = b"pytest==8.0.0\n"


def _prepare_build_context(tmp_path: Path, monkeypatch) -> Path:
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "requirements.txt").write_bytes(b"pytest==8.0.0\n")
    monkeypatch.setattr(isolation, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(
        isolation,
        "REQUIREMENTS_SHA256",
        hashlib.sha256(b"pytest==8.0.0\n").hexdigest(),
    )
    build_context = tmp_path / "build"
    build_context.mkdir()
    return build_context


def _completed(command, returncode=0, stdout=""):
    return subprocess.CompletedProcess(command, returncode, stdout, "")


def test_probe_timeout_recovers_only_its_labeled_container(monkeypatch):
    cleanup_records = []
    commands = []
    resource_id = "a" * 64

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if command[1:3] == ["ps", "--all"]:
            if len([item for item in commands if item[1:3] == ["ps", "--all"]]) == 1:
                return _completed(command, stdout=f"{resource_id}\n")
            return _completed(command)
        if command[:3] == ["docker", "rm", "--force"]:
            return _completed(command)
        raise AssertionError(f"Unexpected Docker command: {command}")

    monkeypatch.setattr(isolation, "_run", fake_run)

    with pytest.raises(subprocess.TimeoutExpired):
        isolation._run_probe(
            "test-image",
            network="bridge",
            program="pass",
            run_id=RUN_ID,
            resource_id="host-control",
            cleanup_records=cleanup_records,
        )

    assert cleanup_records[0]["status"] == "verified"
    assert cleanup_records[0]["run_id"] == RUN_ID
    assert cleanup_records[0]["resource_id"] == resource_id
    run_command = commands[0]
    assert "--name" in run_command
    assert "--rm" in run_command
    assert run_command.count("--label") == 2
    assert commands[1][0:2] == ["docker", "ps"]
    assert commands[1][-4:] == [
        "--filter",
        f"label=com.kybernaut.ci.run={RUN_ID}",
        "--filter",
        "label=com.kybernaut.ci.resource=host-control",
    ]
    assert commands[2] == ["docker", "rm", "--force", resource_id]
    assert commands[3][0:2] == ["docker", "ps"]


def test_probe_timeout_with_no_observed_resource_is_unverified(
    monkeypatch, capsys
):
    cleanup_records = []

    def fake_run(command, **kwargs):
        if command[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return _completed(command)

    monkeypatch.setattr(isolation, "_run", fake_run)

    with pytest.raises(subprocess.TimeoutExpired) as error:
        isolation._run_probe(
            "test-image",
            network="bridge",
            program="pass",
            run_id=RUN_ID,
            resource_id="host-control",
            cleanup_records=cleanup_records,
        )

    assert cleanup_records[0]["status"] == "unverified"
    assert cleanup_records[0]["verification_status"] == "unverified"
    assert "late creation cannot be excluded" in cleanup_records[0][
        "cleanup_diagnostic"
    ]
    assert any("Cleanup outcome: unverified" in note
               for note in error.value.__notes__)
    assert "Timed-out probe_container cleanup unverified" in capsys.readouterr().err


def test_missing_expected_resource_is_unverified_without_removal(monkeypatch):
    resource_id = "a" * 64
    commands = []
    labels = isolation._docker_resource_labels(RUN_ID, "pytest-container")
    listing, removal = isolation._container_label_commands(labels)

    def fake_run(command, **kwargs):
        commands.append(command)
        return _completed(command)

    monkeypatch.setattr(isolation, "_run", fake_run)
    cleanup = isolation._cleanup_labeled_docker_resource(
        resource="pytest_container",
        identity="pytest-name",
        run_id=RUN_ID,
        labels=labels,
        list_command=listing,
        remove_command=removal,
        expected_resource_id=resource_id,
    )

    assert cleanup["status"] == "unverified"
    assert cleanup["verification_status"] == "unverified"
    assert "absent before cleanup" in cleanup["cleanup_diagnostic"]
    assert commands == [listing]


@pytest.mark.parametrize(
    "post_cleanup_ids",
    [
        ["b" * 64],
        ["b" * 64, "c" * 64],
    ],
)
def test_new_matching_resources_during_verification_do_not_succeed(
    monkeypatch, post_cleanup_ids
):
    resource_id = "a" * 64
    commands = []
    labels = isolation._docker_resource_labels(RUN_ID, "pytest-container")
    listing, removal = isolation._container_label_commands(labels)
    discovery_results = iter([
        _completed(listing, stdout=f"{resource_id}\n"),
        _completed(listing, stdout="".join(f"{item}\n" for item in post_cleanup_ids)),
    ])

    def fake_run(command, **kwargs):
        commands.append(command)
        if command == listing:
            return next(discovery_results)
        if command == [*removal, resource_id]:
            return _completed(command)
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(isolation, "_run", fake_run)
    cleanup = isolation._cleanup_labeled_docker_resource(
        resource="pytest_container",
        identity="pytest-name",
        run_id=RUN_ID,
        labels=labels,
        list_command=listing,
        remove_command=removal,
        expected_resource_id=resource_id,
    )

    assert cleanup["status"] == "failed"
    assert cleanup["verification_status"] == "present"
    assert "run-associated resource remains" in cleanup["verification_diagnostic"]
    assert commands == [listing, [*removal, resource_id], listing]


def test_malformed_post_cleanup_identity_is_unverified_and_not_removed(monkeypatch):
    resource_id = "a" * 64
    malformed_id = "not-a-docker-resource-id"
    commands = []
    labels = isolation._docker_resource_labels(RUN_ID, "pytest-container")
    listing, removal = isolation._container_label_commands(labels)
    discovery_results = iter([
        _completed(listing, stdout=f"{resource_id}\n"),
        _completed(listing, stdout=f"{malformed_id}\n"),
    ])

    def fake_run(command, **kwargs):
        commands.append(command)
        if command == listing:
            return next(discovery_results)
        if command == [*removal, resource_id]:
            return _completed(command)
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(isolation, "_run", fake_run)
    cleanup = isolation._cleanup_labeled_docker_resource(
        resource="pytest_container",
        identity="pytest-name",
        run_id=RUN_ID,
        labels=labels,
        list_command=listing,
        remove_command=removal,
        expected_resource_id=resource_id,
    )

    assert cleanup["status"] == "unverified"
    assert cleanup["verification_status"] == "unverified"
    assert cleanup["verification_diagnostic"] == (
        "post-cleanup discovery returned invalid identities"
    )
    assert commands == [listing, [*removal, resource_id], listing]


def test_duplicate_post_cleanup_identity_is_unverified_and_not_removed(
    monkeypatch,
):
    resource_id = "a" * 64
    duplicate_id = "b" * 64
    commands = []
    labels = isolation._docker_resource_labels(RUN_ID, "pytest-container")
    listing, removal = isolation._container_label_commands(labels)
    discovery_results = iter([
        _completed(listing, stdout=f"{resource_id}\n"),
        _completed(listing, stdout=f"{duplicate_id}\n{duplicate_id}\n"),
    ])

    def fake_run(command, **kwargs):
        commands.append(command)
        if command == listing:
            return next(discovery_results)
        if command == [*removal, resource_id]:
            return _completed(command)
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(isolation, "_run", fake_run)
    cleanup = isolation._cleanup_labeled_docker_resource(
        resource="pytest_container",
        identity="pytest-name",
        run_id=RUN_ID,
        labels=labels,
        list_command=listing,
        remove_command=removal,
        expected_resource_id=resource_id,
    )

    assert cleanup["status"] == "unverified"
    assert cleanup["verification_status"] == "unverified"
    assert cleanup["verification_diagnostic"] == (
        "post-cleanup discovery returned duplicate identities"
    )
    assert commands == [listing, [*removal, resource_id], listing]


@pytest.mark.parametrize(
    "verification_failure",
    ["timeout", "error", "execution_error"],
)
def test_post_cleanup_query_failure_is_unverified(
    monkeypatch, verification_failure
):
    resource_id = "d" * 64
    labels = isolation._docker_resource_labels(RUN_ID, "pytest-container")
    listing, removal = isolation._container_label_commands(labels)
    discovery_count = 0

    def fake_run(command, **kwargs):
        nonlocal discovery_count
        if command == listing:
            discovery_count += 1
            if discovery_count == 1:
                return _completed(command, stdout=f"{resource_id}\n")
            if verification_failure == "timeout":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            if verification_failure == "execution_error":
                raise OSError("synthetic Docker query failure")
            return _completed(command, returncode=1)
        if command == [*removal, resource_id]:
            return _completed(command)
        raise AssertionError(f"Unexpected command: {command}")

    monkeypatch.setattr(isolation, "_run", fake_run)
    cleanup = isolation._cleanup_labeled_docker_resource(
        resource="pytest_container",
        identity="pytest-name",
        run_id=RUN_ID,
        labels=labels,
        list_command=listing,
        remove_command=removal,
        expected_resource_id=resource_id,
    )

    assert cleanup["status"] == "unverified"
    assert cleanup["verification_status"] == "unverified"
    assert "verification" in cleanup["verification_diagnostic"]


def test_unverified_resource_prevents_aggregate_cleanup_success():
    resources = [
        {
            "resource": name,
            "status": "unverified" if name == "peer_container" else "verified",
        }
        for name in ("peer_container", "pytest_container", "test_image")
    ]
    listeners = [
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
    ]

    assert isolation._cleanup_status(resources, listeners, run_id=RUN_ID) == (
        "unverified"
    )


def test_build_timeout_recovers_image_by_run_label(tmp_path, monkeypatch):
    build_context = _prepare_build_context(tmp_path, monkeypatch)
    cleanup_records = []
    commands = []
    image_id = "b" * 64

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[:2] == ["docker", "build"]:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if command[1:4] == ["image", "ls", "--quiet"]:
            if len([item for item in commands if item[1:4] == ["image", "ls", "--quiet"]]) == 1:
                return _completed(command, stdout=f"{image_id}\n")
            return _completed(command)
        if command[:3] == ["docker", "image", "rm"]:
            return _completed(command)
        raise AssertionError(f"Unexpected Docker command: {command}")

    monkeypatch.setattr(isolation, "_run", fake_run)

    with pytest.raises(subprocess.TimeoutExpired):
        isolation._build_image(
            build_context,
            base_image=isolation.SUPPORTED_BASE_IMAGES["3.11"],
            python_version="3.11",
            tag="test-image",
            run_id=RUN_ID,
            cleanup_records=cleanup_records,
            requirements_bytes=REQUIREMENTS_BYTES,
        )

    assert cleanup_records[0]["resource"] == "test_image"
    assert cleanup_records[0]["status"] == "verified"
    assert cleanup_records[0]["run_id"] == RUN_ID
    assert cleanup_records[0]["resource_id"] == image_id
    assert "--label" in commands[0]
    assert commands[1][-4:] == [
        "--filter",
        f"label=com.kybernaut.ci.run={RUN_ID}",
        "--filter",
        "label=com.kybernaut.ci.resource=test-image",
    ]
    assert commands[2] == ["docker", "image", "rm", image_id]


def test_multiple_labeled_resources_fail_without_removing_any(monkeypatch):
    first_id, second_id = "c" * 64, "d" * 64
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return _completed(command, stdout=f"{first_id}\n{second_id}\n")

    monkeypatch.setattr(isolation, "_run", fake_run)
    labels = isolation._docker_resource_labels(RUN_ID, "host-control")
    listing, removal = isolation._container_label_commands(labels)

    cleanup = isolation._cleanup_labeled_docker_resource(
        resource="probe_container",
        identity="probe-name",
        run_id=RUN_ID,
        labels=labels,
        list_command=listing,
        remove_command=removal,
        operation_timed_out=True,
    )

    assert cleanup["status"] == "failed"
    assert "multiple resources" in cleanup["cleanup_diagnostic"]
    assert len(commands) == 1
    assert all(command[:3] != ["docker", "rm", "--force"] for command in commands)


def test_cleanup_command_failure_with_resource_remaining_is_failed(monkeypatch):
    resource_id = "e" * 64
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[1:3] == ["ps", "--all"]:
            return _completed(command, stdout=f"{resource_id}\n")
        if command[:3] == ["docker", "rm", "--force"]:
            return _completed(command, returncode=1)
        raise AssertionError(f"Unexpected Docker command: {command}")

    monkeypatch.setattr(isolation, "_run", fake_run)
    labels = isolation._docker_resource_labels(RUN_ID, "host-control")
    listing, removal = isolation._container_label_commands(labels)

    cleanup = isolation._cleanup_labeled_docker_resource(
        resource="probe_container",
        identity="probe-name",
        run_id=RUN_ID,
        labels=labels,
        list_command=listing,
        remove_command=removal,
        operation_timed_out=True,
    )

    assert cleanup["status"] == "failed"
    assert cleanup["cleanup_exit_code"] == 1
    assert cleanup["verification_status"] == "present"
    assert commands[1] == ["docker", "rm", "--force", resource_id]


def test_cleanup_does_not_remove_id_mismatched_with_created_resource(
    monkeypatch
):
    discovered_id = "f" * 64
    expected_id = "e" * 64
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return _completed(command, stdout=f"{discovered_id}\n")

    monkeypatch.setattr(isolation, "_run", fake_run)
    labels = isolation._docker_resource_labels(RUN_ID, "pytest-container")
    listing, removal = isolation._container_label_commands(labels)

    cleanup = isolation._cleanup_labeled_docker_resource(
        resource="pytest_container",
        identity="pytest-name",
        run_id=RUN_ID,
        labels=labels,
        list_command=listing,
        remove_command=removal,
        expected_resource_id=expected_id,
    )

    assert cleanup["status"] == "failed"
    assert cleanup["resource_id"] == expected_id
    assert "does not match created resource" in cleanup["cleanup_diagnostic"]
    assert commands == [listing]


def test_build_timeout_preserves_timeout_and_cleanup_failure(
    tmp_path, monkeypatch
):
    build_context = _prepare_build_context(tmp_path, monkeypatch)
    cleanup_records = []

    def fake_run(command, **kwargs):
        if command[:2] == ["docker", "build"]:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if command[1:4] == ["image", "ls", "--quiet"]:
            return _completed(command)
        raise AssertionError(f"Unexpected Docker command: {command}")

    monkeypatch.setattr(isolation, "_run", fake_run)

    with pytest.raises(subprocess.TimeoutExpired) as error:
        isolation._build_image(
            build_context,
            base_image=isolation.SUPPORTED_BASE_IMAGES["3.11"],
            python_version="3.11",
            tag="test-image",
            run_id=RUN_ID,
            cleanup_records=cleanup_records,
            requirements_bytes=REQUIREMENTS_BYTES,
        )

    assert cleanup_records[0]["status"] == "unverified"
    assert any("Cleanup outcome: unverified" in note
               for note in error.value.__notes__)


def test_cleanup_recovery_exception_does_not_replace_probe_timeout(
    monkeypatch, capsys
):
    cleanup_records = []
    timeout = subprocess.TimeoutExpired(["docker", "run"], timeout=10)

    def fail_operation(command, **kwargs):
        raise timeout

    def fail_cleanup(**kwargs):
        raise RuntimeError("synthetic cleanup failure")

    monkeypatch.setattr(isolation, "_run", fail_operation)
    monkeypatch.setattr(
        isolation, "_cleanup_labeled_docker_resource", fail_cleanup
    )

    with pytest.raises(subprocess.TimeoutExpired) as error:
        isolation._run_probe(
            "test-image",
            network="bridge",
            program="pass",
            run_id=RUN_ID,
            resource_id="host-control",
            cleanup_records=cleanup_records,
        )

    assert error.value is timeout
    assert cleanup_records[0]["status"] == "unverified"
    assert "RuntimeError" in cleanup_records[0]["cleanup_diagnostic"]
    assert "Timed-out probe_container cleanup unverified" in capsys.readouterr().err


def test_incomplete_evidence_assembly_is_reported_explicitly():
    listener_records = [
        {
            "listener_id": "host-canary-listener",
            "status": "failed",
        }
    ]
    message = isolation._incomplete_evidence_summary(
        RuntimeError("primary operation failed"),
        [{
            "resource": "test_image",
            "resource_id": None,
            "status": "unverified",
        }],
        listener_records,
    )

    assert "Evidence bundle not emitted" in message
    assert "primary=RuntimeError" in message
    assert "test_image=unverified(id=unresolved)" in message
    assert "listener:host-canary-listener=failed" in message


def test_missing_listener_cleanup_records_cannot_be_overall_success():
    assert isolation._cleanup_status(
        [
            {"resource": name, "status": "verified"}
            for name in (
                "peer_container",
                "pytest_container",
                "test_image",
            )
        ],
        [],
        run_id=RUN_ID,
    ) == "unverified"


def test_missing_resource_cleanup_records_cannot_be_overall_success():
    listener_records = [
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
    ]

    assert isolation._cleanup_status([], listener_records, run_id=RUN_ID) == (
        "unverified"
    )


def test_evidence_serialization_failure_does_not_publish_partial_bundle(tmp_path):
    evidence_path = tmp_path / "network-isolation.json"
    cyclic = {}
    cyclic["self"] = cyclic

    with pytest.raises(ValueError, match="Circular reference"):
        isolation._write_evidence(evidence_path, cyclic)

    assert not evidence_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_evidence_publication_failure_does_not_leave_partial_bundle(
    tmp_path, monkeypatch
):
    evidence_path = tmp_path / "network-isolation.json"

    def fail_link(source, destination):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(isolation.os, "link", fail_link)
    with pytest.raises(OSError, match="synthetic publication failure"):
        isolation._write_evidence(evidence_path, {"complete": True})

    assert not evidence_path.exists()
    assert list(tmp_path.iterdir()) == []


def test_evidence_write_failure_message_reports_no_complete_bundle():
    message = isolation._evidence_write_failure_message(
        OSError("synthetic write failure"),
        None,
        [],
        [{
            "listener_id": "pytest-child-listener",
            "status": "failed",
        }],
    )

    assert "Evidence bundle not emitted" in message
    assert "serialization or write failed (OSError)" in message
    assert "listener:pytest-child-listener=failed" in message


def test_image_build_pins_git_to_the_verified_debian_snapshot(
    tmp_path, monkeypatch
):
    build_context = _prepare_build_context(tmp_path, monkeypatch)
    build_calls = []
    monkeypatch.setattr(
        isolation,
        "_run",
        lambda command, **kwargs: build_calls.append((command, kwargs)),
    )

    isolation._build_image(
        build_context,
        base_image=isolation.SUPPORTED_BASE_IMAGES["3.11"],
        python_version="3.11",
        tag="test-image",
        run_id=RUN_ID,
        cleanup_records=[],
        requirements_bytes=REQUIREMENTS_BYTES,
    )

    dockerfile = (build_context / "Dockerfile").read_text()
    assert (
        f"https://snapshot.debian.org/archive/debian/"
        f"{isolation.DEBIAN_SNAPSHOT}/ trixie main"
    ) in dockerfile
    assert f"git={isolation.GIT_PACKAGE_VERSION}" in dockerfile
    assert (
        f"dpkg-query -W -f='${{Version}}' git)\" = "
        f"'{isolation.GIT_PACKAGE_VERSION}'"
    ) in dockerfile
    assert "git --version | grep -Fx 'git version 2.47.3'" in dockerfile
    assert "apt-get update && apt-get install" not in dockerfile
    assert len(build_calls) == 1
    assert build_calls[0][0][:3] == ["docker", "build", "--pull"]


def test_image_build_stops_before_docker_on_lock_checksum_mismatch(
    tmp_path, monkeypatch
):
    build_context = _prepare_build_context(tmp_path, monkeypatch)
    monkeypatch.setattr(isolation, "REQUIREMENTS_SHA256", "0" * 64)
    build_calls = []
    monkeypatch.setattr(
        isolation,
        "_run",
        lambda command, **kwargs: build_calls.append(command),
    )

    with pytest.raises(RuntimeError, match="approved pinned lock checksum"):
        isolation._build_image(
            build_context,
            base_image=isolation.SUPPORTED_BASE_IMAGES["3.11"],
            python_version="3.11",
            tag="test-image",
            run_id=RUN_ID,
            cleanup_records=[],
            requirements_bytes=REQUIREMENTS_BYTES,
        )

    assert build_calls == []


def test_lock_preflight_verifies_the_pinned_requirements_bytes(
    tmp_path, monkeypatch
):
    _prepare_build_context(tmp_path, monkeypatch)

    assert isolation._verify_requirements_lock(
        project_root=isolation.PROJECT_ROOT
    ) == b"pytest==8.0.0\n"


def test_build_context_uses_exact_verified_requirements_bytes(
    tmp_path, monkeypatch
):
    build_context = _prepare_build_context(tmp_path, monkeypatch)
    verified = isolation._verify_requirements_lock(
        project_root=isolation.PROJECT_ROOT
    )

    def mutate_live_lock(command, **kwargs):
        (isolation.PROJECT_ROOT / "requirements.txt").write_bytes(
            b"pytest==0.0.0\n"
        )

    monkeypatch.setattr(isolation, "_run", mutate_live_lock)
    isolation._build_image(
        build_context,
        base_image=isolation.SUPPORTED_BASE_IMAGES["3.11"],
        python_version="3.11",
        tag="test-image",
        run_id=RUN_ID,
        cleanup_records=[],
        requirements_bytes=verified,
    )

    assert (build_context / "requirements.txt").read_bytes() == verified


def test_docker_build_failure_is_not_suppressed(tmp_path, monkeypatch):
    build_context = _prepare_build_context(tmp_path, monkeypatch)

    def fail_build(command, **kwargs):
        raise RuntimeError("docker build failed")

    monkeypatch.setattr(isolation, "_run", fail_build)

    with pytest.raises(RuntimeError, match="docker build failed"):
        isolation._build_image(
            build_context,
            base_image=isolation.SUPPORTED_BASE_IMAGES["3.12"],
            python_version="3.12",
            tag="test-image",
            run_id=RUN_ID,
            cleanup_records=[],
            requirements_bytes=REQUIREMENTS_BYTES,
        )


def test_pytest_container_entrypoint_runs_child_process_probe_before_tests():
    command = isolation._pytest_container_command(
        run_id="run-id",
        listener_host="172.17.0.1",
        listener_port=38241,
    )

    assert command[:2] == ["python", "-c"]
    entrypoint = command[2]
    assert "subprocess.run(" in entrypoint
    assert "PYTEST_CHILD_PROBE_DENIED" in entrypoint
    assert "pytest_container_child_process" in entrypoint
    assert entrypoint.index("probe = subprocess.run(") < entrypoint.index(
        'sys.executable, "-m", "pytest"'
    )
    compile(entrypoint, "<pytest-container-entrypoint>", "exec")
    assert command[3:] == ["run-id", "172.17.0.1", "38241"]


def test_ci_workflow_installs_yaml_validator_on_host():
    workflow_path = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "ci.yml"
    )
    workflow = workflow_path.read_text(encoding="utf-8")

    install = "python -m pip install PyYAML==6.0.3"
    validation = "import glob, yaml"
    assert install in workflow
    assert workflow.index(install) < workflow.index(validation)


@pytest.mark.parametrize("workflow_name", ["ci.yml", "tests.yml"])
def test_workflow_supplies_expected_revision(workflow_name):
    workflow_path = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / workflow_name
    )

    assert '--expected-revision "$GITHUB_SHA"' in workflow_path.read_text(
        encoding="utf-8"
    )


def test_listener_waits_for_expected_control_payload():
    listener = isolation._Listener("127.0.0.1")
    with listener:
        with socket.create_connection(("127.0.0.1", listener.port), timeout=1) as client:
            client.sendall(b"expected-control")

        record = listener.wait_for_payload("expected-control", timeout=1)

    assert record is not None
    assert record["payload"] == "expected-control"


def test_listener_wait_ignores_unexpected_payload_and_times_out():
    listener = isolation._Listener("127.0.0.1")
    with listener:
        with socket.create_connection(("127.0.0.1", listener.port), timeout=1) as client:
            client.sendall(b"unrelated-connection")
        deadline = time.monotonic() + 1
        while not listener.records and time.monotonic() < deadline:
            time.sleep(0.005)

        started = time.monotonic()
        record = listener.wait_for_payload("expected-control", timeout=0.05)
        elapsed = time.monotonic() - started

    assert record is None
    assert elapsed < 0.5


def test_listener_cleanup_confirms_thread_termination():
    listener = isolation._Listener("127.0.0.1")
    with listener:
        pass

    result = listener.close()

    assert result["attempted"] is True
    assert result["socket_closed"] is True
    assert result["thread_stopped"] is True
    assert result["status"] == "succeeded"


def _listener_with_close_failure(listener_id, cleanup_records):
    listener = isolation._Listener(
        "127.0.0.1",
        run_id=RUN_ID,
        listener_id=listener_id,
        cleanup_records=cleanup_records,
    )
    original_socket = listener._socket

    class BrokenSocket:
        def close(self):
            raise OSError("synthetic listener close failure")

    class StoppedThread:
        def join(self, *, timeout):
            assert timeout == 2

        def is_alive(self):
            return False

    listener._socket = BrokenSocket()
    listener._thread = StoppedThread()
    return listener, original_socket


def test_host_listener_cleanup_failure_after_successful_operation(capsys):
    cleanup_records = []
    listener, original_socket = _listener_with_close_failure(
        "host-canary-listener",
        cleanup_records,
    )
    try:
        result = listener.close()
    finally:
        original_socket.close()

    assert result["status"] == "failed"
    assert cleanup_records == [{
        "listener_id": "host-canary-listener",
        "run_id": RUN_ID,
        **result,
    }]
    assert "Listener cleanup host-canary-listener failed" in (
        capsys.readouterr().err
    )


def test_listener_cleanup_failures_remain_visible_with_primary_exception(capsys):
    cleanup_records = []
    host, host_socket = _listener_with_close_failure(
        "host-canary-listener",
        cleanup_records,
    )
    child, child_socket = _listener_with_close_failure(
        "pytest-child-listener",
        cleanup_records,
    )
    primary = ValueError("primary canary failure")
    try:
        with pytest.raises(ValueError, match="primary canary failure"):
            host.__exit__(ValueError, primary, None)
            child.__exit__(ValueError, primary, None)
            raise primary
    finally:
        host_socket.close()
        child_socket.close()

    assert primary.__notes__ == [
        "Listener cleanup failed: listener socket close failed",
        "Listener cleanup failed: listener socket close failed",
    ]
    assert [item["listener_id"] for item in cleanup_records] == [
        "host-canary-listener",
        "pytest-child-listener",
    ]
    output = capsys.readouterr().err
    assert "Listener cleanup host-canary-listener failed" in output
    assert "Listener cleanup pytest-child-listener failed" in output


def test_child_listener_cleanup_failure_is_recorded(capsys):
    cleanup_records = []
    listener, original_socket = _listener_with_close_failure(
        "pytest-child-listener",
        cleanup_records,
    )
    try:
        result = listener.close()
    finally:
        original_socket.close()

    assert result["status"] == "failed"
    assert cleanup_records[0]["listener_id"] == "pytest-child-listener"
    assert cleanup_records[0]["run_id"] == RUN_ID
    assert "Listener cleanup pytest-child-listener failed" in (
        capsys.readouterr().err
    )


def test_listener_cleanup_fails_when_bounded_join_leaves_thread_alive():
    listener = isolation._Listener("127.0.0.1")

    class StuckThread:
        def join(self, *, timeout):
            assert timeout == 0.01

        def is_alive(self):
            return True

    listener._thread = StuckThread()
    result = listener.close(timeout=0.01)

    assert result["thread_stopped"] is False
    assert result["status"] == "failed"
    assert result["diagnostic"] == "listener thread remained alive after bounded join"
    with pytest.raises(RuntimeError, match="Listener cleanup failed"):
        listener.__exit__(None, None, None)


def test_listener_cleanup_note_does_not_replace_primary_exception(monkeypatch):
    listener = isolation._Listener("127.0.0.1")
    primary = ValueError("primary harness failure")
    monkeypatch.setattr(
        listener,
        "close",
        lambda: {
            "status": "failed",
            "diagnostic": "synthetic listener cleanup failure",
        },
    )

    assert listener.__exit__(ValueError, primary, None) is False
    assert primary.__notes__ == [
        "Listener cleanup failed: synthetic listener cleanup failure"
    ]
    listener._socket.close()


def test_listener_socket_close_error_is_recorded():
    listener = isolation._Listener("127.0.0.1")
    real_socket = listener._socket

    class StoppedThread:
        def join(self, *, timeout):
            assert timeout == 2

        def is_alive(self):
            return False

    class BrokenSocket:
        def close(self):
            raise OSError("synthetic close error")

    listener._thread = StoppedThread()
    listener._socket = BrokenSocket()
    result = listener.close()

    assert result["socket_closed"] is False
    assert result["thread_stopped"] is True
    assert result["status"] == "failed"
    assert result["diagnostic"] == "listener socket close failed"
    real_socket.close()


def test_docker_cleanup_records_success_only_after_removal_is_verified(
    monkeypatch,
):
    results = iter([
        subprocess.CompletedProcess(["docker", "rm"], 0, "", ""),
        subprocess.CompletedProcess(["docker", "ps"], 0, "", ""),
    ])
    monkeypatch.setattr(isolation, "_run", lambda *args, **kwargs: next(results))

    result = isolation._cleanup_docker_resource(
        resource="pytest_container",
        identity="unique-container",
        cleanup_command=["docker", "rm", "--force", "unique-container"],
        verification_command=["docker", "ps"],
        is_present=lambda output: bool(output),
    )

    assert result["status"] == "succeeded"
    assert result["cleanup_exit_code"] == 0
    assert result["verification_status"] == "removed"


def test_docker_cleanup_command_failure_is_not_suppressed(monkeypatch):
    results = iter([
        subprocess.CompletedProcess(
            ["docker", "rm"], 1, "", "unbounded diagnostic is not retained"
        ),
        subprocess.CompletedProcess(["docker", "ps"], 0, "", ""),
    ])
    monkeypatch.setattr(isolation, "_run", lambda *args, **kwargs: next(results))

    result = isolation._cleanup_docker_resource(
        resource="pytest_container",
        identity="unique-container",
        cleanup_command=["docker", "rm", "--force", "unique-container"],
        verification_command=["docker", "ps"],
        is_present=lambda output: bool(output),
    )

    assert result["status"] == "failed"
    assert result["cleanup_exit_code"] == 1
    assert result["cleanup_diagnostic"] == "cleanup command returned nonzero"


def test_docker_cleanup_verification_failure_is_unverified(monkeypatch):
    results = iter([
        subprocess.CompletedProcess(["docker", "rm"], 0, "", ""),
        subprocess.CompletedProcess(["docker", "ps"], 1, "", ""),
    ])
    monkeypatch.setattr(isolation, "_run", lambda *args, **kwargs: next(results))

    result = isolation._cleanup_docker_resource(
        resource="pytest_container",
        identity="unique-container",
        cleanup_command=["docker", "rm", "--force", "unique-container"],
        verification_command=["docker", "ps"],
        is_present=lambda output: bool(output),
    )

    assert result["status"] == "unverified"
    assert result["verification_status"] == "unverified"


def test_docker_cleanup_timeout_is_unverified(monkeypatch):
    calls = iter([
        subprocess.TimeoutExpired(["docker", "rm"], timeout=15),
        subprocess.CompletedProcess(["docker", "ps"], 0, "", ""),
    ])

    def run(*args, **kwargs):
        result = next(calls)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(isolation, "_run", run)
    result = isolation._cleanup_docker_resource(
        resource="pytest_container",
        identity="unique-container",
        cleanup_command=["docker", "rm", "--force", "unique-container"],
        verification_command=["docker", "ps"],
        is_present=lambda output: bool(output),
    )

    assert result["status"] == "unverified"
    assert result["cleanup_exit_code"] is None
    assert result["cleanup_diagnostic"] == "cleanup command timed out"
