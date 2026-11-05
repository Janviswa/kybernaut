from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from scripts import ci_network_isolation as isolation


def _synthetic_repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    (root / "app").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "app" / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_core.py").write_text(
        "from app.core import VALUE\nassert VALUE == 1\n",
        encoding="utf-8",
    )
    (root / "fixtures").mkdir()
    (root / "fixtures" / "required.json").write_text(
        '{"fixture": true}\n',
        encoding="utf-8",
    )
    return root


def _copy_synthetic(root: Path, destination: Path) -> list[str]:
    if (root / ".git").exists():
        revision = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    else:
        revision = _initialize_git_repository(root)
    return isolation._copy_source_snapshot(
        destination,
        revision,
        project_root=root,
        python_source_roots=("app", "tests"),
        approved_files=("fixtures/required.json",),
        input_sha256={},
    )


def _snapshot_hashes(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            relative = path.relative_to(root).as_posix()
            result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _initialize_git_repository(root: Path) -> str:
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "CI isolation test",
        "GIT_AUTHOR_EMAIL": "ci-isolation-test@example.invalid",
        "GIT_COMMITTER_NAME": "CI isolation test",
        "GIT_COMMITTER_EMAIL": "ci-isolation-test@example.invalid",
    }
    for command in (
        ["git", "init", "-q", str(root)],
        ["git", "-C", str(root), "add", "."],
        ["git", "-C", str(root), "commit", "-q", "-m", "fixture"],
    ):
        subprocess.run(command, check=True, timeout=5, env=environment)
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
        env=environment,
    ).stdout.strip()


def test_required_source_and_real_suite_inputs_are_included(tmp_path):
    root = _synthetic_repository(tmp_path)
    approved_files = (
        ".github/workflows/ci.yml",
        "scripts/ci_network_isolation.py",
        "scripts/run_synthetic_rca_evaluation.py",
        "benchmark/scenarios.json",
        "data/telemetry_baseline_v6.csv",
        "benchmark/results/three-way-anomaly-benchmark-v4.json",
        "deploy/cron/kybernaut-weekly-cost.cron",
    )
    for relative_path in (
        "app/main.py",
        "tests/conftest.py",
        *approved_files,
        "config/operators.json",
    ):
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic fixture\n", encoding="utf-8")
    revision = _initialize_git_repository(root)
    destination = tmp_path / "full-snapshot"
    manifest: list[dict[str, str]] = []

    selected = isolation._copy_source_snapshot(
        destination,
        revision,
        project_root=root,
        approved_files=approved_files,
        input_sha256={},
        manifest_entries=manifest,
    )

    assert "app/main.py" in selected
    assert "tests/conftest.py" in selected
    assert ".github/workflows/ci.yml" in selected
    assert "scripts/ci_network_isolation.py" in selected
    assert "scripts/run_synthetic_rca_evaluation.py" in selected
    assert "benchmark/scenarios.json" in selected
    assert "data/telemetry_baseline_v6.csv" in selected
    assert "benchmark/results/three-way-anomaly-benchmark-v4.json" in selected
    assert "deploy/cron/kybernaut-weekly-cost.cron" in selected
    assert "config/operators.json" not in selected
    assert (destination / "app/main.py").is_file()
    assert (destination / "tests/conftest.py").is_file()
    assert (destination / "scripts/run_synthetic_rca_evaluation.py").is_file()
    assert (destination / ".git" / "HEAD").read_text() == f"{revision}\n"
    assert manifest


def test_unusual_dummy_secret_filename_is_not_selected(tmp_path):
    root = _synthetic_repository(tmp_path)
    (root / "unexpected-credential-payload.weird").write_text(
        "synthetic dummy secret content",
        encoding="utf-8",
    )
    destination = tmp_path / "snapshot"

    _copy_synthetic(root, destination)

    assert not (destination / "unexpected-credential-payload.weird").exists()
    assert "unexpected-credential-payload.weird" not in _snapshot_hashes(
        destination
    )


def test_dotenv_and_operator_files_are_not_copied(tmp_path):
    root = _synthetic_repository(tmp_path)
    (root / ".env").write_text("DUMMY_SECRET=not-for-copy\n", encoding="utf-8")
    (root / ".env.synthetic-token-file").write_text(
        "DUMMY_TOKEN=not-for-copy\n",
        encoding="utf-8",
    )
    config = root / "config"
    config.mkdir()
    (config / "operators.json").write_text(
        '{"synthetic":"credential fixture"}\n',
        encoding="utf-8",
    )
    destination = tmp_path / "snapshot"

    _copy_synthetic(root, destination)

    assert not (destination / ".env").exists()
    assert not (destination / ".env.synthetic-token-file").exists()
    assert not (destination / "config" / "operators.json").exists()


def test_example_dotenv_is_projected_to_only_the_validated_blank_setting(
    tmp_path,
):
    root = _synthetic_repository(tmp_path)
    (root / ".env.example").write_text(
        "GRAFANA_ADMIN_PASSWORD=\nOTHER_EXAMPLE=value\n",
        encoding="utf-8",
    )
    destination = tmp_path / "snapshot"

    isolation._copy_source_snapshot(
        destination,
        _initialize_git_repository(root),
        project_root=root,
        python_source_roots=("app", "tests"),
        approved_files=("fixtures/required.json", ".env.example"),
        input_sha256={},
    )

    assert (destination / ".env.example").read_text() == (
        "GRAFANA_ADMIN_PASSWORD=\n"
    )
    assert "OTHER_EXAMPLE" not in (destination / ".env.example").read_text()


def test_example_dotenv_projection_fails_if_password_is_not_blank(tmp_path):
    root = _synthetic_repository(tmp_path)
    (root / ".env.example").write_text(
        "GRAFANA_ADMIN_PASSWORD=non-empty\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="one blank Grafana password"):
        revision = _initialize_git_repository(root)
        isolation._copy_source_snapshot(
            tmp_path / "snapshot",
            revision,
            project_root=root,
            python_source_roots=("app", "tests"),
            approved_files=(".env.example",),
            input_sha256={},
        )


def test_symlink_in_approved_tree_is_rejected_and_cannot_escape(tmp_path):
    root = _synthetic_repository(tmp_path)
    _initialize_git_repository(root)
    outside = tmp_path / "outside.py"
    outside.write_text("OUTSIDE = True\n", encoding="utf-8")
    (root / "tests" / "escape.py").symlink_to(outside)

    with pytest.raises(RuntimeError, match="must not contain symlinks"):
        _copy_synthetic(root, tmp_path / "snapshot")


@pytest.mark.parametrize(
    "relative_path",
    ["../outside.py", "/tmp/outside.py", "app/../../outside.py"],
)
def test_unsafe_manifest_paths_are_rejected(tmp_path, relative_path):
    root = _synthetic_repository(tmp_path)

    with pytest.raises(RuntimeError, match="unsafe path"):
        isolation._source_path(root, relative_path)


def test_special_file_in_approved_tree_is_rejected(tmp_path):
    root = _synthetic_repository(tmp_path)
    _initialize_git_repository(root)
    os.mkfifo(root / "tests" / "unexpected-pipe")

    with pytest.raises(RuntimeError, match="special file"):
        _copy_synthetic(root, tmp_path / "snapshot")


def test_unexpected_regular_file_is_excluded(tmp_path):
    root = _synthetic_repository(tmp_path)
    (root / "tests" / "unknown-credential.data").write_text(
        "synthetic dummy content",
        encoding="utf-8",
    )
    destination = tmp_path / "snapshot"

    selected = _copy_synthetic(root, destination)

    assert "tests/unknown-credential.data" not in selected
    assert not (destination / "tests" / "unknown-credential.data").exists()


def test_missing_required_manifest_file_fails_closed(tmp_path):
    root = _synthetic_repository(tmp_path)
    (root / "fixtures" / "required.json").unlink()
    revision = _initialize_git_repository(root)

    with pytest.raises(RuntimeError, match="Required snapshot input is missing"):
        isolation._copy_source_snapshot(
            tmp_path / "snapshot",
            revision,
            project_root=root,
            python_source_roots=("app", "tests"),
            approved_files=("fixtures/required.json",),
            input_sha256={},
        )


def test_snapshot_is_deterministic_and_does_not_modify_source(tmp_path):
    root = _synthetic_repository(tmp_path)
    _initialize_git_repository(root)
    before = _snapshot_hashes(root)

    first_files = _copy_synthetic(root, tmp_path / "snapshot-one")
    second_files = _copy_synthetic(root, tmp_path / "snapshot-two")

    assert first_files == second_files
    assert _snapshot_hashes(tmp_path / "snapshot-one") == _snapshot_hashes(
        tmp_path / "snapshot-two"
    )
    assert _snapshot_hashes(root) == before


def test_destination_inside_repository_is_rejected(tmp_path):
    root = _synthetic_repository(tmp_path)

    with pytest.raises(RuntimeError, match="outside the repository"):
        _copy_synthetic(root, root / "snapshot")


def test_snapshot_manifest_matches_expected_git_revision(tmp_path):
    root = _synthetic_repository(tmp_path)
    revision = _initialize_git_repository(root)
    destination = tmp_path / "snapshot"
    manifest: list[dict[str, str]] = []

    isolation._copy_source_snapshot(
        destination,
        revision,
        project_root=root,
        python_source_roots=("app", "tests"),
        approved_files=("fixtures/required.json",),
        input_sha256={},
        manifest_entries=manifest,
    )

    for item in manifest:
        expected = subprocess.run(
            ["git", "-C", str(root), "cat-file", "blob", item["git_blob_oid"]],
            check=True,
            capture_output=True,
            timeout=5,
        ).stdout
        actual = (destination / item["path"]).read_bytes()
        assert actual == expected
        assert item["source_sha256"] == hashlib.sha256(expected).hexdigest()
        assert item["snapshot_sha256"] == hashlib.sha256(actual).hexdigest()
    assert manifest == sorted(manifest, key=lambda item: item["path"])
    assert isolation._source_manifest_digest(manifest)


def test_snapshot_rejects_wrong_expected_revision(tmp_path):
    root = _synthetic_repository(tmp_path)
    _initialize_git_repository(root)

    with pytest.raises(RuntimeError, match="Could not read expected source revision"):
        isolation._copy_source_snapshot(
            tmp_path / "snapshot",
            "a" * 40,
            project_root=root,
            python_source_roots=("app", "tests"),
            approved_files=("fixtures/required.json",),
            input_sha256={},
        )


def test_worktree_change_during_copy_cannot_enter_snapshot(tmp_path, monkeypatch):
    root = _synthetic_repository(tmp_path)
    revision = _initialize_git_repository(root)
    original_blob_reader = isolation._git_blob_contents

    def change_worktree_after_read(project_root, entries, selected_paths):
        blobs = original_blob_reader(project_root, entries, selected_paths)
        (root / "app" / "core.py").write_text("VALUE = 999\n", encoding="utf-8")
        return blobs

    monkeypatch.setattr(isolation, "_git_blob_contents", change_worktree_after_read)
    destination = tmp_path / "snapshot"
    manifest: list[dict[str, str]] = []

    isolation._copy_source_snapshot(
        destination,
        revision,
        project_root=root,
        python_source_roots=("app", "tests"),
        approved_files=("fixtures/required.json",),
        input_sha256={},
        manifest_entries=manifest,
    )

    assert (destination / "app" / "core.py").read_text() == "VALUE = 1\n"
    entry = next(item for item in manifest if item["path"] == "app/core.py")
    assert entry["source_sha256"] == hashlib.sha256(b"VALUE = 1\n").hexdigest()


def test_untracked_python_source_is_not_admitted_to_snapshot(tmp_path):
    root = _synthetic_repository(tmp_path)
    revision = _initialize_git_repository(root)
    (root / "app" / "untracked.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="do not match the expected Git revision"):
        isolation._copy_source_snapshot(
            tmp_path / "snapshot",
            revision,
            project_root=root,
            python_source_roots=("app", "tests"),
            approved_files=("fixtures/required.json",),
            input_sha256={},
        )


def test_source_preflight_accepts_exact_revision_and_clean_worktree(tmp_path):
    root = _synthetic_repository(tmp_path)
    revision = _initialize_git_repository(root)

    result = isolation._git_preflight(revision, project_root=root)

    assert result == {
        "expected_revision": revision,
        "actual_revision": revision,
        "worktree": "clean",
        "status": "passed",
    }


def test_source_preflight_rejects_wrong_expected_revision(tmp_path):
    root = _synthetic_repository(tmp_path)
    revision = _initialize_git_repository(root)

    with pytest.raises(RuntimeError, match="does not match expected"):
        isolation._git_preflight("a" * 40, project_root=root)


@pytest.mark.parametrize("change_kind", ["tracked", "untracked"])
def test_source_preflight_rejects_dirty_worktree(tmp_path, change_kind):
    root = _synthetic_repository(tmp_path)
    revision = _initialize_git_repository(root)
    if change_kind == "tracked":
        (root / "app" / "core.py").write_text("VALUE = 2\n", encoding="utf-8")
    else:
        (root / "tests" / "test_untracked.py").write_text(
            "VALUE = 3\n",
            encoding="utf-8",
        )

    with pytest.raises(RuntimeError, match="worktree is not clean"):
        isolation._git_preflight(revision, project_root=root)


def test_source_preflight_rejects_ignored_python_file_in_snapshot_roots(tmp_path):
    root = _synthetic_repository(tmp_path)
    (root / ".gitignore").write_text("tests/ignored.py\n", encoding="utf-8")
    revision = _initialize_git_repository(root)
    (root / "tests" / "ignored.py").write_text("VALUE = 4\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Ignored source"):
        isolation._git_preflight(revision, project_root=root)


@pytest.mark.parametrize("expected_revision", ["", "not-a-revision", None])
def test_source_preflight_requires_valid_expected_revision(
    tmp_path, expected_revision
):
    root = _synthetic_repository(tmp_path)
    _initialize_git_repository(root)

    with pytest.raises(RuntimeError, match="explicit valid expected"):
        isolation._git_preflight(expected_revision, project_root=root)
