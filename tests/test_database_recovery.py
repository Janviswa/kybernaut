import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from pathlib import Path

import pytest

from app.services import database_recovery as recovery
from scripts.database_recovery import main as recovery_cli


@pytest.fixture(autouse=True)
def isolate_active_path(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "KYBERNAUT_DATABASE_PATH", str(tmp_path / "not-the-test-database.sqlite3")
    )


def create_database(path, statements=()):
    with sqlite3.connect(path) as connection:
        for statement in statements:
            connection.execute(statement)


def attach_test_metadata(path, *, filename=None, digest=None):
    metadata = {
        "format_version": 1,
        "captured_at": "2026-01-01T00:00:00+00:00",
        "sqlite_version": sqlite3.sqlite_version,
        "application_revision": None,
        "backup_filename": filename or path.name,
        "capture_method": "sqlite3.Connection.backup",
        "snapshot_sha256": digest or hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    path.with_name(path.name + ".metadata.json").write_text(
        json.dumps(metadata),
        encoding="utf-8",
    )


def database_content_digest(path):
    digest = hashlib.sha256()
    with closing(sqlite3.connect(path)) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                   WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                   ORDER BY name"""
            )
        ]
        for table in tables:
            escaped = table.replace('"', '""')
            rows = connection.execute(
                f'SELECT * FROM "{escaped}" ORDER BY rowid'
            ).fetchall()
            digest.update(json.dumps([table, rows], sort_keys=True).encode())
    return digest.hexdigest()


def make_consistent_lifecycle_database(path, *, resolved=False, audit=True):
    create_database(
        path,
        [
            """CREATE TABLE incidents (
                id INTEGER PRIMARY KEY, incident_id TEXT, status TEXT,
                resolution_reason TEXT
            )""",
            """CREATE TABLE incident_handoffs (
                incident_row_id INTEGER, workflow_claim_id TEXT, status TEXT
            )""",
            """CREATE TABLE workflow_claims (
                claim_id TEXT PRIMARY KEY, incident_occurrence_id INTEGER,
                incident_ref TEXT, status TEXT
            )""",
            """CREATE TABLE action_claims (
                action_claim_id TEXT PRIMARY KEY, workflow_claim_id TEXT,
                status TEXT, execution_json TEXT, verification_json TEXT,
                approval_id TEXT
            )""",
            """CREATE TABLE approval_requests (
                approval_id TEXT PRIMARY KEY, action_claim_id TEXT,
                incident_ref TEXT
            )""",
            """CREATE TABLE audit_events (
                event_id TEXT PRIMARY KEY, incident_ref TEXT, event_type TEXT,
                workflow_claim_id TEXT, action_claim_id TEXT, approval_id TEXT
            )""",
        ],
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO incidents VALUES (1, 'INC-SYNTHETIC', ?, ?)",
            (
                "RESOLVED" if resolved else "OPEN",
                "verified_kubernetes_recovery" if resolved else None,
            ),
        )
        connection.execute(
            "INSERT INTO incident_handoffs VALUES (1, 'wf-1', ?)",
            ("COMPLETED" if resolved else "RECONCILIATION_REQUIRED",),
        )
        connection.execute(
            "INSERT INTO workflow_claims VALUES ('wf-1', 1, 'INC-SYNTHETIC', ?)",
            ("COMPLETED" if resolved else "RECONCILIATION_REQUIRED",),
        )
        execution = (
            {"mode": "live", "executed": True, "ok": True}
            if resolved else {"outcome_unknown": True}
        )
        verification = {"recovered": True} if resolved else None
        connection.execute(
            """INSERT INTO action_claims VALUES (
                'ac-1', 'wf-1', ?, ?, ?, 'approval-1'
            )""",
            (
                "VERIFIED" if resolved else "EXECUTION_UNKNOWN",
                json.dumps(execution),
                json.dumps(verification) if verification is not None else None,
            ),
        )
        connection.execute(
            """INSERT INTO approval_requests
               VALUES ('approval-1', 'ac-1', 'INC-SYNTHETIC')"""
        )
        if audit:
            connection.execute(
                """INSERT INTO audit_events VALUES (
                    'event-1', 'INC-SYNTHETIC',
                    'incident_recovery_verified', 'wf-1', 'ac-1', 'approval-1'
                )"""
            )


def test_online_backup_and_read_only_validation_while_source_is_open(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    source_connection = sqlite3.connect(source)
    source_connection.execute("CREATE TABLE synthetic (id INTEGER PRIMARY KEY)")
    source_connection.execute("INSERT INTO synthetic VALUES (1)")
    source_connection.commit()
    original_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    try:
        provenance = recovery.create_backup(
            source,
            backup,
            create_backup=True,
            confirm_disposable_source=True,
        )
    finally:
        source_connection.close()

    report = recovery.validate_database(backup)
    metadata = json.loads((tmp_path / "backup.sqlite3.metadata.json").read_text())

    assert provenance["capture_method"] == "sqlite3.Connection.backup"
    assert provenance["sqlite_version"]
    assert provenance["backup_filename"] == "backup.sqlite3"
    assert metadata["backup_filename"] == "backup.sqlite3"
    assert metadata["snapshot_sha256"] == hashlib.sha256(
        backup.read_bytes()
    ).hexdigest()
    assert report["valid"] is False
    assert report["integrity_ok"] is True
    assert report["table_counts"] == {"synthetic": 1}
    assert report["lifecycle"]["coverage_complete"] is False
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_digest
    assert backup.stat().st_mode & 0o077 == 0
    assert (tmp_path / "backup.sqlite3.metadata.json").stat().st_mode & 0o077 == 0


def test_online_backup_is_consistent_during_concurrent_commits(
    tmp_path, monkeypatch
):
    source = tmp_path / "concurrent-source.sqlite3"
    backup = tmp_path / "concurrent-backup.sqlite3"
    during_backup = threading.Event()
    writer_finished = threading.Event()
    writer_errors = []
    writer_row = (10_000, "writer-committed-during-backup")
    after_backup_row = (10_001, "writer-committed-after-backup")

    def checksum(payload):
        return hashlib.sha256(payload.encode()).hexdigest()

    before_rows = [
        (sequence, f"seed-{sequence}-" + ("x" * 2048))
        for sequence in range(128)
    ]
    with closing(sqlite3.connect(source)) as connection:
        with connection:
            connection.execute(
                """CREATE TABLE synthetic_rows (
                    sequence INTEGER PRIMARY KEY,
                    payload TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL
                )"""
            )
            connection.executemany(
                "INSERT INTO synthetic_rows VALUES (?, ?, ?)",
                [
                    (sequence, payload, checksum(payload))
                    for sequence, payload in before_rows
                ],
            )

    def writer():
        try:
            if not during_backup.wait(timeout=5):
                raise AssertionError("backup did not reach a progress callback")
            payload = writer_row[1]
            with closing(sqlite3.connect(source, timeout=3)) as connection:
                with connection:
                    connection.execute(
                        "INSERT INTO synthetic_rows VALUES (?, ?, ?)",
                        (writer_row[0], payload, checksum(payload)),
                    )
        except Exception as exc:
            writer_errors.append(exc)
        finally:
            writer_finished.set()

    backup_progress_seen = threading.Event()

    def backup_with_progress(source_connection, destination_connection):
        def progress(_status, _remaining, _total):
            if not backup_progress_seen.is_set():
                backup_progress_seen.set()
                during_backup.set()
                if not writer_finished.wait(timeout=5):
                    raise AssertionError("concurrent writer did not finish")

        source_connection.backup(
            destination_connection,
            pages=1,
            progress=progress,
        )

    monkeypatch.setattr(recovery, "_copy_database", backup_with_progress)
    writer_thread = threading.Thread(target=writer, name="synthetic-db-writer")
    writer_thread.start()
    try:
        recovery.create_backup(
            source,
            backup,
            create_backup=True,
            confirm_disposable_source=True,
        )
    finally:
        during_backup.set()
        writer_thread.join(timeout=8)

    assert not writer_thread.is_alive(), "synthetic writer did not terminate"
    assert writer_errors == []
    assert backup_progress_seen.is_set()

    with closing(sqlite3.connect(source)) as connection:
        with connection:
            connection.execute(
                "INSERT INTO synthetic_rows VALUES (?, ?, ?)",
                (
                    after_backup_row[0],
                    after_backup_row[1],
                    checksum(after_backup_row[1]),
                ),
            )
        source_rows = connection.execute(
            """SELECT sequence, payload, payload_sha256
               FROM synthetic_rows ORDER BY sequence"""
        ).fetchall()

    report = recovery.validate_database(backup)
    assert report["integrity_ok"] is True
    with closing(sqlite3.connect(backup)) as connection:
        snapshot_rows = connection.execute(
            """SELECT sequence, payload, payload_sha256
               FROM synthetic_rows ORDER BY sequence"""
        ).fetchall()

    expected_payloads = {
        sequence: payload for sequence, payload in before_rows
    }
    expected_payloads.update(
        {writer_row[0]: writer_row[1], after_backup_row[0]: after_backup_row[1]}
    )
    assert {row[0] for row in source_rows} == set(expected_payloads)
    assert all(row[1] == expected_payloads[row[0]] for row in source_rows)
    assert all(row[2] == checksum(row[1]) for row in source_rows)

    snapshot_by_sequence = {row[0]: row for row in snapshot_rows}
    assert set(sequence for sequence, _payload in before_rows) <= set(
        snapshot_by_sequence
    )
    assert after_backup_row[0] not in snapshot_by_sequence
    assert all(
        row[1] == expected_payloads[row[0]] and row[2] == checksum(row[1])
        for row in snapshot_rows
    )


def test_online_backup_validates_synthetic_wal_database_while_open(tmp_path):
    source = tmp_path / "wal-source.sqlite3"
    backup = tmp_path / "wal-backup.sqlite3"
    source_connection = sqlite3.connect(source)
    try:
        journal_mode = source_connection.execute(
            "PRAGMA journal_mode=WAL"
        ).fetchone()[0]
        assert journal_mode.lower() == "wal"
        source_connection.execute(
            """CREATE TABLE synthetic_rows (
                sequence INTEGER PRIMARY KEY,
                payload TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL
            )"""
        )
        source_connection.commit()

        payloads = {
            1: "wal-before-backup",
            2: "wal-committed-before-snapshot",
        }
        with closing(sqlite3.connect(source)) as writer_connection:
            with writer_connection:
                for sequence, payload in payloads.items():
                    writer_connection.execute(
                        "INSERT INTO synthetic_rows VALUES (?, ?, ?)",
                        (
                            sequence,
                            payload,
                            hashlib.sha256(payload.encode()).hexdigest(),
                        ),
                    )

        recovery.create_backup(
            source,
            backup,
            create_backup=True,
            confirm_disposable_source=True,
        )

        report = recovery.validate_database(backup)
        assert report["integrity_ok"] is True
        with closing(sqlite3.connect(source)) as connection:
            source_rows = connection.execute(
                """SELECT sequence, payload, payload_sha256
                   FROM synthetic_rows ORDER BY sequence"""
            ).fetchall()
        with closing(sqlite3.connect(backup)) as connection:
            backup_rows = connection.execute(
                """SELECT sequence, payload, payload_sha256
                   FROM synthetic_rows ORDER BY sequence"""
            ).fetchall()

        assert source.resolve() != backup.resolve()
        assert backup_rows == source_rows
        assert all(
            row[2] == hashlib.sha256(row[1].encode()).hexdigest()
            for row in backup_rows
        )
        metadata = json.loads(
            backup.with_name(backup.name + ".metadata.json").read_text(
                encoding="utf-8"
            )
        )
        assert metadata["snapshot_sha256"] == hashlib.sha256(
            backup.read_bytes()
        ).hexdigest()

        restored = tmp_path / "wal-restored.sqlite3"
        recovery.restore_disposable(
            backup,
            restored,
            confirm_disposable_destination=True,
        )
        assert recovery.validate_database(restored)["integrity_ok"] is True
        with closing(sqlite3.connect(restored)) as connection:
            restored_rows = connection.execute(
                """SELECT sequence, payload, payload_sha256
                   FROM synthetic_rows ORDER BY sequence"""
            ).fetchall()
        assert restored_rows == source_rows
        assert database_content_digest(restored) == database_content_digest(backup)
    finally:
        source_connection.close()


def test_backup_requires_both_explicit_confirmations(tmp_path):
    source = tmp_path / "source.sqlite3"
    create_database(source)

    with pytest.raises(recovery.RecoveryError, match="explicit"):
        recovery.create_backup(source, tmp_path / "backup.sqlite3")


def test_missing_source_invalid_database_and_existing_destination_fail(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(recovery.RecoveryError, match="does not exist"):
        recovery.create_backup(
            missing,
            tmp_path / "backup.sqlite3",
            create_backup=True,
            confirm_disposable_source=True,
        )

    invalid = tmp_path / "invalid.sqlite3"
    invalid.write_bytes(b"not sqlite")
    with pytest.raises(recovery.RecoveryError):
        recovery.create_backup(
            invalid,
            tmp_path / "bad-backup.sqlite3",
            create_backup=True,
            confirm_disposable_source=True,
        )

    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "existing.sqlite3"
    create_database(source)
    destination.write_bytes(b"preserve")
    with pytest.raises(recovery.RecoveryError, match="already exists"):
        recovery.create_backup(
            source,
            destination,
            create_backup=True,
            confirm_disposable_source=True,
        )
    assert destination.read_bytes() == b"preserve"


def test_existing_symlink_sidecar_is_preserved(tmp_path):
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    sidecar = tmp_path / "backup.sqlite3.metadata.json"
    target = tmp_path / "pre-existing.json"
    create_database(source)
    target.write_text("preserve")
    sidecar.symlink_to(target)

    with pytest.raises(recovery.RecoveryError, match="already exists"):
        recovery.create_backup(
            source,
            destination,
            create_backup=True,
            confirm_disposable_source=True,
        )

    assert not destination.exists()
    assert sidecar.is_symlink()
    assert sidecar.resolve() == target
    assert target.read_text() == "preserve"


def test_source_destination_alias_and_symlink_are_rejected(tmp_path):
    source = tmp_path / "source.sqlite3"
    create_database(source)
    with pytest.raises(recovery.RecoveryError, match="different"):
        recovery.create_backup(
            source,
            source,
            create_backup=True,
            confirm_disposable_source=True,
        )

    link = tmp_path / "source-link.sqlite3"
    link.symlink_to(source)
    with pytest.raises(recovery.RecoveryError, match="symlink"):
        recovery.create_backup(
            link,
            tmp_path / "link-backup.sqlite3",
            create_backup=True,
            confirm_disposable_source=True,
        )


def test_active_and_outside_temporary_paths_are_rejected(tmp_path, monkeypatch):
    active = tmp_path / "active.sqlite3"
    create_database(active)
    monkeypatch.setenv("KYBERNAUT_DATABASE_PATH", str(active))
    with pytest.raises(recovery.RecoveryError, match="configured application"):
        recovery.validate_database(active)

    with pytest.raises(recovery.RecoveryError, match="temporary directory"):
        recovery.validate_database("/etc/passwd")

    alias = tmp_path / "active-alias.sqlite3"
    alias.hardlink_to(active)
    with pytest.raises(recovery.RecoveryError, match="aliases"):
        recovery.validate_database(alias)


def test_backup_failure_cleans_only_its_own_partial_artifacts(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.sqlite3"
    sentinel = tmp_path / "keep.me"
    backup = tmp_path / "backup.sqlite3"
    create_database(source)
    sentinel.write_text("pre-existing")

    def fail_copy(_source, _destination):
        raise sqlite3.OperationalError("synthetic failure")

    monkeypatch.setattr(recovery, "_copy_database", fail_copy)
    with pytest.raises(recovery.RecoveryError, match="online backup failed"):
        recovery.create_backup(
            source,
            backup,
            create_backup=True,
            confirm_disposable_source=True,
        )

    assert not backup.exists()
    assert not (tmp_path / "backup.sqlite3.metadata.json").exists()
    assert sentinel.read_text() == "pre-existing"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "keep.me",
        "source.sqlite3",
    ]


def test_corrupt_database_is_not_reported_as_valid(tmp_path):
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_bytes(b"broken sqlite")
    attach_test_metadata(corrupt)

    with pytest.raises(recovery.RecoveryError):
        recovery.validate_database(corrupt)


def test_empty_schema_fails_closed_with_explicit_missing_coverage(tmp_path):
    database = tmp_path / "empty-schema.sqlite3"
    create_database(database)
    attach_test_metadata(database)

    report = recovery.validate_database(database)

    assert report["integrity_ok"] is True
    assert report["valid"] is False
    assert report["lifecycle"]["coverage_complete"] is False
    assert report["lifecycle"]["missing_tables"] == [
        "action_claims",
        "approval_requests",
        "audit_events",
        "incident_handoffs",
        "incidents",
        "workflow_claims",
    ]
    assert report["lifecycle"]["missing_checks"]
    assert set(report["lifecycle"]["missing_checks"]) <= set(
        report["lifecycle"]["not_checked"]
    )


def test_unrelated_schema_reports_missing_lifecycle_tables_and_checks(tmp_path):
    database = tmp_path / "unrelated-schema.sqlite3"
    create_database(database, ["CREATE TABLE unrelated (value TEXT)"])
    attach_test_metadata(database)

    report = recovery.validate_database(database)

    assert report["valid"] is False
    assert report["table_names"] == ["unrelated"]
    assert report["lifecycle"]["missing_tables"]
    assert report["lifecycle"]["missing_checks"]
    assert report["lifecycle"]["checked"] == []


def test_supported_synthetic_schema_has_complete_lifecycle_coverage(tmp_path):
    database = tmp_path / "supported-schema.sqlite3"
    make_consistent_lifecycle_database(database)
    attach_test_metadata(database)

    report = recovery.validate_database(database)

    assert report["valid"] is True
    assert report["lifecycle"]["coverage_complete"] is True
    assert report["lifecycle"]["missing_tables"] == []
    assert report["lifecycle"]["missing_checks"] == []


def test_declared_foreign_key_violations_fail_validation(tmp_path):
    database = tmp_path / "foreign-key.sqlite3"
    create_database(
        database,
        [
            "CREATE TABLE parent (id INTEGER PRIMARY KEY)",
            """CREATE TABLE child (
                parent_id INTEGER REFERENCES parent(id)
            )""",
        ],
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("INSERT INTO child VALUES (404)")
    attach_test_metadata(database)

    report = recovery.validate_database(database)
    assert report["foreign_key_check"] == "failed"
    assert report["foreign_key_failure_count"] == 1
    assert report["valid"] is False


def test_restore_materializes_fresh_database_and_preserves_backup(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    restored = tmp_path / "restored.sqlite3"
    create_database(source, ["CREATE TABLE synthetic (value TEXT)"])
    with sqlite3.connect(source) as connection:
        connection.execute("INSERT INTO synthetic VALUES ('safe fixture')")

    recovery.create_backup(
        source,
        backup,
        create_backup=True,
        confirm_disposable_source=True,
    )
    original_digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    recovery.restore_disposable(
        backup,
        restored,
        confirm_disposable_destination=True,
    )

    restored_report = recovery.validate_database(restored)
    assert restored_report["integrity_ok"] is True
    assert restored_report["valid"] is False
    assert restored_report["lifecycle"]["coverage_complete"] is False
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == original_digest
    active = tmp_path / "active-target.sqlite3"
    monkeypatch.setenv("KYBERNAUT_DATABASE_PATH", str(active))
    with pytest.raises(recovery.RecoveryError, match="configured application"):
        recovery.restore_disposable(
            backup,
            active,
            confirm_disposable_destination=True,
        )
    with pytest.raises(recovery.RecoveryError, match="already exists"):
        recovery.restore_disposable(
            backup,
            restored,
            confirm_disposable_destination=True,
        )


def test_unresolved_execution_is_reported_without_being_promoted(tmp_path):
    database = tmp_path / "unresolved.sqlite3"
    make_consistent_lifecycle_database(database)
    attach_test_metadata(database)

    report = recovery.validate_database(database)

    assert report["valid"] is True
    assert report["lifecycle"]["unresolved_execution_count"] == 1
    assert report["lifecycle"]["unresolved_lifecycle_state_counts"] == {
        "workflow_reconciliation_required": 1,
        "handoff_dispatching_or_reconciliation_required": 1,
    }
    assert report["table_counts"]["action_claims"] == 1
    with sqlite3.connect(database) as connection:
        status = connection.execute(
            "SELECT status FROM action_claims"
        ).fetchone()[0]
    assert status == "EXECUTION_UNKNOWN"


def test_verified_recovery_requires_linked_audit_event(tmp_path):
    valid = tmp_path / "verified.sqlite3"
    make_consistent_lifecycle_database(valid, resolved=True)
    attach_test_metadata(valid)
    assert recovery.validate_database(valid)["valid"] is True

    missing_audit = tmp_path / "missing-audit.sqlite3"
    make_consistent_lifecycle_database(
        missing_audit, resolved=True, audit=False
    )
    attach_test_metadata(missing_audit)
    report = recovery.validate_database(missing_audit)
    assert report["valid"] is False
    assert "verified_recovery_audit_event_missing" in report["lifecycle"]["findings"]


def test_approval_must_match_linked_incident_occurrence(tmp_path):
    database = tmp_path / "approval-mismatch.sqlite3"
    make_consistent_lifecycle_database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE approval_requests SET incident_ref = 'OTHER-INCIDENT'"
        )
    attach_test_metadata(database)

    report = recovery.validate_database(database)

    assert report["valid"] is False
    assert "approval_incident_mismatch" in report["lifecycle"]["findings"]


@pytest.mark.parametrize(
    "metadata_case",
    [
        "missing",
        "malformed",
        "unsupported",
        "naive_timestamp",
        "digest_mismatch",
        "other_snapshot",
    ],
)
def test_validation_and_restore_reject_invalid_artifact_pairs(
    tmp_path, metadata_case, capsys
):
    snapshot = tmp_path / f"{metadata_case}-snapshot.sqlite3"
    make_consistent_lifecycle_database(snapshot, resolved=True)
    attach_test_metadata(snapshot)
    metadata_path = snapshot.with_name(snapshot.name + ".metadata.json")

    if metadata_case == "missing":
        metadata_path.unlink()
    elif metadata_case == "malformed":
        metadata_path.write_text("{not-json", encoding="utf-8")
    elif metadata_case == "unsupported":
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["format_version"] = 999
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    elif metadata_case == "naive_timestamp":
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["captured_at"] = "2026-01-01T00:00:00"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    elif metadata_case == "digest_mismatch":
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["snapshot_sha256"] = "0" * 64
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    else:
        other_snapshot = tmp_path / "different-snapshot.sqlite3"
        make_consistent_lifecycle_database(other_snapshot, resolved=True)
        with sqlite3.connect(other_snapshot) as connection:
            connection.execute(
                "INSERT INTO audit_events VALUES "
                "('event-2', 'INC-SYNTHETIC', 'extra', 'wf-1', 'ac-1', 'approval-1')"
            )
        attach_test_metadata(other_snapshot)
        other_metadata = json.loads(
            other_snapshot.with_name(
                other_snapshot.name + ".metadata.json"
            ).read_text(encoding="utf-8")
        )
        other_metadata["backup_filename"] = snapshot.name
        metadata_path.write_text(json.dumps(other_metadata), encoding="utf-8")

    with pytest.raises(recovery.RecoveryError):
        recovery.validate_database(snapshot)
    assert recovery_cli(["validate", "--database", str(snapshot)]) == 2
    cli_error = capsys.readouterr().err
    assert "database-recovery:" in cli_error
    assert str(snapshot) not in cli_error

    restored = tmp_path / f"{metadata_case}-restored.sqlite3"
    with pytest.raises(recovery.RecoveryError):
        recovery.restore_disposable(
            snapshot,
            restored,
            confirm_disposable_destination=True,
        )
    assert not restored.exists()
    assert not restored.with_name(restored.name + ".metadata.json").exists()
    assert recovery_cli([
        "restore",
        "--backup", str(snapshot),
        "--destination", str(restored),
        "--confirm-disposable-destination",
    ]) == 2
    cli_error = capsys.readouterr().err
    assert "database-recovery:" in cli_error
    assert str(snapshot) not in cli_error
    assert not restored.exists()


def test_matching_metadata_pair_validates_and_restores(tmp_path):
    snapshot = tmp_path / "matching-snapshot.sqlite3"
    make_consistent_lifecycle_database(snapshot, resolved=True)
    attach_test_metadata(snapshot)
    metadata = json.loads(
        snapshot.with_name(snapshot.name + ".metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["snapshot_sha256"] == hashlib.sha256(
        snapshot.read_bytes()
    ).hexdigest()
    assert recovery.validate_database(snapshot)["valid"] is True

    restored = tmp_path / "matching-restored.sqlite3"
    recovery.restore_disposable(
        snapshot,
        restored,
        confirm_disposable_destination=True,
    )
    restored_metadata_path = restored.with_name(
        restored.name + ".metadata.json"
    )
    restored_metadata = json.loads(
        restored_metadata_path.read_text(encoding="utf-8")
    )
    assert restored_metadata["backup_filename"] == restored.name
    assert restored_metadata["snapshot_sha256"] == hashlib.sha256(
        restored.read_bytes()
    ).hexdigest()
    assert recovery.validate_database(restored)["valid"] is True


def test_successful_backup_validation_restore_lifecycle_preserves_content(
    tmp_path,
):
    source = tmp_path / "lifecycle-source.sqlite3"
    backup = tmp_path / "lifecycle-backup.sqlite3"
    restored = tmp_path / "lifecycle-restored.sqlite3"
    make_consistent_lifecycle_database(source, resolved=True)
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    content_digest = database_content_digest(source)

    provenance = recovery.create_backup(
        source,
        backup,
        create_backup=True,
        confirm_disposable_source=True,
    )
    metadata_path = backup.with_name(backup.name + ".metadata.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["snapshot_sha256"] == hashlib.sha256(
        backup.read_bytes()
    ).hexdigest()
    assert provenance["snapshot_sha256"] == metadata["snapshot_sha256"]
    assert database_content_digest(backup) == content_digest
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest
    assert recovery.validate_database(backup)["valid"] is True

    backup_digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    metadata_digest = hashlib.sha256(metadata_path.read_bytes()).hexdigest()
    recovery.restore_disposable(
        backup,
        restored,
        confirm_disposable_destination=True,
    )

    restored_metadata_path = restored.with_name(
        restored.name + ".metadata.json"
    )
    restored_metadata = json.loads(
        restored_metadata_path.read_text(encoding="utf-8")
    )
    assert database_content_digest(restored) == content_digest
    assert restored_metadata["snapshot_sha256"] == hashlib.sha256(
        restored.read_bytes()
    ).hexdigest()
    assert recovery.validate_database(restored)["valid"] is True
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == backup_digest
    assert hashlib.sha256(metadata_path.read_bytes()).hexdigest() == metadata_digest


def test_unresolved_state_survives_backup_validation_and_restore(tmp_path):
    source = tmp_path / "unresolved-source.sqlite3"
    backup = tmp_path / "unresolved-backup.sqlite3"
    restored = tmp_path / "unresolved-restored.sqlite3"
    make_consistent_lifecycle_database(source)
    source_digest = database_content_digest(source)

    recovery.create_backup(
        source,
        backup,
        create_backup=True,
        confirm_disposable_source=True,
    )
    before_restore = recovery.validate_database(backup)
    assert before_restore["valid"] is True
    assert before_restore["lifecycle"]["unresolved_execution_count"] == 1

    recovery.restore_disposable(
        backup,
        restored,
        confirm_disposable_destination=True,
    )
    after_restore = recovery.validate_database(restored)
    assert after_restore["valid"] is True
    assert after_restore["lifecycle"]["unresolved_execution_count"] == 1
    assert after_restore["lifecycle"]["unresolved_lifecycle_state_counts"] == (
        before_restore["lifecycle"]["unresolved_lifecycle_state_counts"]
    )
    assert database_content_digest(source) == source_digest
    with closing(sqlite3.connect(restored)) as connection:
        assert connection.execute(
            "SELECT status FROM action_claims"
        ).fetchone()[0] == "EXECUTION_UNKNOWN"


def test_restore_refuses_existing_metadata_symlink(tmp_path):
    source = tmp_path / "restore-source.sqlite3"
    backup = tmp_path / "restore-backup.sqlite3"
    destination = tmp_path / "restore-target.sqlite3"
    existing_target = tmp_path / "keep-existing-metadata.json"
    create_database(source)
    recovery.create_backup(
        source,
        backup,
        create_backup=True,
        confirm_disposable_source=True,
    )
    existing_target.write_text("preserve", encoding="utf-8")
    destination_metadata = destination.with_name(
        destination.name + ".metadata.json"
    )
    destination_metadata.symlink_to(existing_target)

    with pytest.raises(recovery.RecoveryError, match="already exists"):
        recovery.restore_disposable(
            backup,
            destination,
            confirm_disposable_destination=True,
        )

    assert not destination.exists()
    assert destination_metadata.is_symlink()
    assert destination_metadata.resolve() == existing_target
    assert existing_target.read_text(encoding="utf-8") == "preserve"


def test_restore_sidecar_publication_failure_cleans_its_snapshot(
    tmp_path, monkeypatch
):
    source = tmp_path / "restore-failure-source.sqlite3"
    backup = tmp_path / "restore-failure-backup.sqlite3"
    destination = tmp_path / "restore-failure-target.sqlite3"
    sentinel = tmp_path / "restore-failure-sentinel.txt"
    create_database(source)
    recovery.create_backup(
        source,
        backup,
        create_backup=True,
        confirm_disposable_source=True,
    )
    backup_digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    sentinel.write_text("preserve", encoding="utf-8")
    _fail_on_sidecar_publication(monkeypatch)

    with pytest.raises(
        recovery.RecoveryError, match="partial artifacts were removed"
    ):
        recovery.restore_disposable(
            backup,
            destination,
            confirm_disposable_destination=True,
        )

    assert not destination.exists()
    assert not destination.with_name(destination.name + ".metadata.json").exists()
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == backup_digest
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def _fail_on_first_publication(monkeypatch):
    def publish(_staged, _destination):
        raise OSError("synthetic snapshot publication failure")

    monkeypatch.setattr(recovery, "_publish_without_overwrite", publish)


def test_snapshot_publication_failure_leaves_no_backup_artifacts(
    tmp_path, monkeypatch
):
    source = tmp_path / "snapshot-failure-source.sqlite3"
    destination = tmp_path / "snapshot-failure-backup.sqlite3"
    sentinel = tmp_path / "snapshot-failure-sentinel.txt"
    create_database(source)
    sentinel.write_text("preserve", encoding="utf-8")
    _fail_on_first_publication(monkeypatch)

    with pytest.raises(
        recovery.RecoveryError, match="partial artifacts were removed"
    ):
        recovery.create_backup(
            source,
            destination,
            create_backup=True,
            confirm_disposable_source=True,
        )

    assert not destination.exists()
    assert not destination.with_name(destination.name + ".metadata.json").exists()
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_backup_cleanup_uncertainty_reports_metadata_only_orphan(
    tmp_path, monkeypatch
):
    source = tmp_path / "uncertain-backup-source.sqlite3"
    destination = tmp_path / "uncertain-backup.sqlite3"
    metadata = destination.with_name(destination.name + ".metadata.json")
    sentinel = tmp_path / "uncertain-backup-sentinel.txt"
    create_database(source)
    sentinel.write_text("preserve", encoding="utf-8")
    _raise_after_sidecar_publication(monkeypatch)
    original_remove = recovery._remove_if_still_ours

    def fail_metadata_cleanup(staged, published):
        if published == metadata:
            return False
        return original_remove(staged, published)

    monkeypatch.setattr(recovery, "_remove_if_still_ours", fail_metadata_cleanup)

    with pytest.raises(
        recovery.RecoveryError, match="partial cleanup could not be confirmed"
    ):
        recovery.create_backup(
            source,
            destination,
            create_backup=True,
            confirm_disposable_source=True,
        )

    assert not destination.exists()
    assert metadata.is_file()
    with pytest.raises(recovery.RecoveryError):
        recovery.validate_database(destination)
    with pytest.raises(recovery.RecoveryError, match="already exists"):
        recovery.create_backup(
            source,
            destination,
            create_backup=True,
            confirm_disposable_source=True,
        )
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_restore_cleanup_uncertainty_reports_metadata_only_orphan(
    tmp_path, monkeypatch
):
    source = tmp_path / "uncertain-restore-source.sqlite3"
    backup = tmp_path / "uncertain-restore-backup.sqlite3"
    destination = tmp_path / "uncertain-restore-target.sqlite3"
    sentinel = tmp_path / "uncertain-restore-sentinel.txt"
    create_database(source)
    recovery.create_backup(
        source,
        backup,
        create_backup=True,
        confirm_disposable_source=True,
    )
    backup_digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    sentinel.write_text("preserve", encoding="utf-8")
    _raise_after_sidecar_publication(monkeypatch)
    metadata = destination.with_name(destination.name + ".metadata.json")
    original_remove = recovery._remove_if_still_ours

    def fail_metadata_cleanup(staged, published):
        if published == metadata:
            return False
        return original_remove(staged, published)

    monkeypatch.setattr(recovery, "_remove_if_still_ours", fail_metadata_cleanup)

    with pytest.raises(
        recovery.RecoveryError, match="partial cleanup could not be confirmed"
    ):
        recovery.restore_disposable(
            backup,
            destination,
            confirm_disposable_destination=True,
        )

    assert not destination.exists()
    assert metadata.is_file()
    with pytest.raises(recovery.RecoveryError):
        recovery.validate_database(destination)
    with pytest.raises(recovery.RecoveryError, match="already exists"):
        recovery.restore_disposable(
            backup,
            destination,
            confirm_disposable_destination=True,
        )
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == backup_digest
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_restore_snapshot_publication_failure_returns_sanitized_cli_error(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "cli-restore-failure-source.sqlite3"
    backup = tmp_path / "cli-restore-failure-backup.sqlite3"
    destination = tmp_path / "cli-restore-failure-target.sqlite3"
    create_database(source)
    recovery.create_backup(
        source,
        backup,
        create_backup=True,
        confirm_disposable_source=True,
    )
    _fail_on_first_publication(monkeypatch)

    result = recovery_cli([
        "restore",
        "--backup", str(backup),
        "--destination", str(destination),
        "--confirm-disposable-destination",
    ])
    captured = capsys.readouterr()

    assert result == 2
    assert captured.out == ""
    assert "Restore publication failed" in captured.err
    assert "synthetic snapshot publication failure" not in captured.err
    assert str(backup) not in captured.err
    assert not destination.exists()
    assert not destination.with_name(destination.name + ".metadata.json").exists()


def _fail_on_sidecar_publication(monkeypatch):
    original_publish = recovery._publish_without_overwrite
    calls = 0

    def publish(staged, destination):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic failure at /private/test/path")
        original_publish(staged, destination)

    monkeypatch.setattr(recovery, "_publish_without_overwrite", publish)


def _raise_after_sidecar_publication(monkeypatch):
    original_publish = recovery._publish_without_overwrite
    calls = 0

    def publish(staged, destination):
        nonlocal calls
        calls += 1
        original_publish(staged, destination)
        if calls == 2:
            raise OSError("synthetic failure after sidecar link")

    monkeypatch.setattr(recovery, "_publish_without_overwrite", publish)
    return original_publish


def test_backup_cleans_both_links_when_sidecar_publish_raises_after_link(
    tmp_path, monkeypatch
):
    source = tmp_path / "post-link-source.sqlite3"
    destination = tmp_path / "post-link-backup.sqlite3"
    metadata = destination.with_name(destination.name + ".metadata.json")
    sentinel = tmp_path / "post-link-sentinel.txt"
    create_database(source)
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    sentinel.write_text("preserve", encoding="utf-8")
    original_publish = _raise_after_sidecar_publication(monkeypatch)

    with pytest.raises(
        recovery.RecoveryError, match="partial artifacts were removed"
    ):
        recovery.create_backup(
            source,
            destination,
            create_backup=True,
            confirm_disposable_source=True,
        )

    assert not destination.exists()
    assert not metadata.exists()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_digest
    assert sentinel.read_text(encoding="utf-8") == "preserve"

    monkeypatch.setattr(recovery, "_publish_without_overwrite", original_publish)
    recovery.create_backup(
        source,
        destination,
        create_backup=True,
        confirm_disposable_source=True,
    )
    assert recovery.validate_database(destination)["integrity_ok"] is True
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_restore_cleans_both_links_when_sidecar_publish_raises_after_link(
    tmp_path, monkeypatch
):
    source = tmp_path / "restore-post-link-source.sqlite3"
    backup = tmp_path / "restore-post-link-backup.sqlite3"
    destination = tmp_path / "restore-post-link-target.sqlite3"
    metadata = destination.with_name(destination.name + ".metadata.json")
    sentinel = tmp_path / "restore-post-link-sentinel.txt"
    create_database(source)
    recovery.create_backup(
        source,
        backup,
        create_backup=True,
        confirm_disposable_source=True,
    )
    backup_digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    sentinel.write_text("preserve", encoding="utf-8")
    original_publish = _raise_after_sidecar_publication(monkeypatch)

    with pytest.raises(
        recovery.RecoveryError, match="partial artifacts were removed"
    ):
        recovery.restore_disposable(
            backup,
            destination,
            confirm_disposable_destination=True,
        )

    assert not destination.exists()
    assert not metadata.exists()
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == backup_digest
    assert sentinel.read_text(encoding="utf-8") == "preserve"

    monkeypatch.setattr(recovery, "_publish_without_overwrite", original_publish)
    recovery.restore_disposable(
        backup,
        destination,
        confirm_disposable_destination=True,
    )
    assert recovery.validate_database(destination)["integrity_ok"] is True
    assert sentinel.read_text(encoding="utf-8") == "preserve"


def test_sidecar_publication_failure_removes_only_published_snapshot(
    tmp_path, monkeypatch
):
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    sentinel = tmp_path / "keep.me"
    create_database(source, ["CREATE TABLE synthetic (value TEXT)"])
    sentinel.write_text("preserve")
    _fail_on_sidecar_publication(monkeypatch)

    with pytest.raises(
        recovery.RecoveryError, match="partial artifacts were removed"
    ):
        recovery.create_backup(
            source,
            destination,
            create_backup=True,
            confirm_disposable_source=True,
        )

    assert not destination.exists()
    assert not (tmp_path / "backup.sqlite3.metadata.json").exists()
    assert sentinel.read_text() == "preserve"


def test_cli_reports_sidecar_publication_failure_without_raw_error(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "source.sqlite3"
    destination = tmp_path / "backup.sqlite3"
    create_database(source)
    _fail_on_sidecar_publication(monkeypatch)

    result = recovery_cli([
        "backup",
        "--source", str(source),
        "--destination", str(destination),
        "--confirm-disposable-source",
        "--create-backup",
    ])
    captured = capsys.readouterr()

    assert result == 2
    assert captured.out == ""
    assert "partial artifacts were removed" in captured.err
    assert "/private/test/path" not in captured.err
    assert "Traceback" not in captured.err
    assert not destination.exists()
    assert not (tmp_path / "backup.sqlite3.metadata.json").exists()


def test_process_interruption_between_publications_is_rejected_by_consumers(
    tmp_path, capsys
):
    source = tmp_path / "interruption-source.sqlite3"
    snapshot = tmp_path / "interrupted-backup.sqlite3"
    sidecar = tmp_path / "interrupted-backup.sqlite3.metadata.json"
    sentinel = tmp_path / "unrelated-sentinel.txt"
    make_consistent_lifecycle_database(source, resolved=True)
    sentinel.write_text("preserve")

    child_program = """
import os
import sys

from app.services import database_recovery as recovery

publish = recovery._publish_without_overwrite
publication_count = 0

def interrupt_after_snapshot(staged, destination):
    global publication_count
    publish(staged, destination)
    publication_count += 1
    if publication_count == 1:
        os._exit(73)

recovery._publish_without_overwrite = interrupt_after_snapshot
recovery.create_backup(
    sys.argv[1],
    sys.argv[2],
    create_backup=True,
    confirm_disposable_source=True,
)
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            child_program,
            str(source),
            str(snapshot),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert completed.returncode == 73
    assert snapshot.is_file()
    assert not sidecar.exists()
    assert sentinel.read_text() == "preserve"
    staging_dirs = list(tmp_path.glob(".kybernaut-recovery-*"))
    assert len(staging_dirs) == 1
    assert (staging_dirs[0] / "snapshot.metadata.json").is_file()
    orphan_digest = hashlib.sha256(snapshot.read_bytes()).hexdigest()

    with pytest.raises(recovery.RecoveryError, match="already exists"):
        recovery.create_backup(
            source,
            snapshot,
            create_backup=True,
            confirm_disposable_source=True,
        )
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == orphan_digest
    assert not sidecar.exists()
    assert sentinel.read_text() == "preserve"

    with pytest.raises(recovery.RecoveryError, match="metadata"):
        recovery.validate_database(snapshot)
    assert recovery_cli(["validate", "--database", str(snapshot)]) == 2
    assert "database-recovery:" in capsys.readouterr().err

    restored = tmp_path / "restored-from-orphan.sqlite3"
    with pytest.raises(recovery.RecoveryError, match="metadata"):
        recovery.restore_disposable(
            snapshot,
            restored,
            confirm_disposable_destination=True,
        )
    assert not restored.exists()
    assert recovery_cli([
        "restore",
        "--backup", str(snapshot),
        "--destination", str(tmp_path / "cli-restored-from-orphan.sqlite3"),
        "--confirm-disposable-destination",
    ]) == 2
    assert "database-recovery:" in capsys.readouterr().err
    assert sentinel.read_text() == "preserve"


def test_cli_backup_validate_and_restore_use_explicit_disposable_paths(
    tmp_path, capsys
):
    source = tmp_path / "cli-source.sqlite3"
    backup = tmp_path / "cli-backup.sqlite3"
    restored = tmp_path / "cli-restored.sqlite3"
    create_database(source, ["CREATE TABLE synthetic (id INTEGER)"])

    assert recovery_cli([
        "backup",
        "--source", str(source),
        "--destination", str(backup),
        "--confirm-disposable-source",
        "--create-backup",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["backup_filename"] == backup.name

    assert recovery_cli(["validate", "--database", str(backup)]) == 2
    unsupported_report = json.loads(capsys.readouterr().out)
    assert unsupported_report["integrity_ok"] is True
    assert unsupported_report["valid"] is False
    assert unsupported_report["lifecycle"]["missing_tables"]

    assert recovery_cli([
        "restore",
        "--backup", str(backup),
        "--destination", str(restored),
        "--confirm-disposable-destination",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["restore_filename"] == restored.name
    assert recovery_cli(["validate", "--database", str(restored)]) == 2
    assert json.loads(capsys.readouterr().out)["valid"] is False

    broken = tmp_path / "broken-cli.sqlite3"
    broken.write_bytes(b"not sqlite")
    assert recovery_cli(["validate", "--database", str(broken)]) == 2
    assert "database-recovery:" in capsys.readouterr().err
