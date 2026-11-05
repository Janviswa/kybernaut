"""Disposable SQLite backup, validation, and restore helpers.

This module is intentionally restricted to paths beneath the system temporary
directory. It is not a production backup mechanism.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RecoveryError(Exception):
    """A safe, user-facing recovery-tool error."""


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_REPOSITORY_DATABASE = _REPOSITORY_ROOT / "incidents.db"
_METADATA_FORMAT_VERSION = 1
_METADATA_MAX_BYTES = 16 * 1024


def _temporary_root() -> Path:
    return Path(tempfile.gettempdir()).resolve()


def _safe_path(path: str | os.PathLike[str], *, label: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise RecoveryError(f"{label.capitalize()} cannot be a symlink.")
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise RecoveryError(f"Invalid {label} path.") from exc
    if not resolved.is_relative_to(_temporary_root()):
        raise RecoveryError(
            f"{label.capitalize()} must be beneath the system temporary directory."
        )
    return resolved


def _active_database_paths() -> set[Path]:
    configured = os.environ.get("KYBERNAUT_DATABASE_PATH")
    values = [_REPOSITORY_DATABASE]
    if configured:
        values.append(Path(configured).expanduser())
    result = set()
    for value in values:
        try:
            result.add(value.resolve(strict=False))
        except (OSError, RuntimeError):
            continue
    return result


def _refuse_active_database(path: Path, *, label: str) -> None:
    for active_path in _active_database_paths():
        if path == active_path:
            raise RecoveryError(
                f"{label.capitalize()} matches a configured application database path."
            )
        try:
            if path.exists() and active_path.exists() and path.samefile(active_path):
                raise RecoveryError(
                    f"{label.capitalize()} aliases a configured application database."
                )
        except OSError:
            raise RecoveryError(f"Could not safely compare the {label} path.")


def _require_regular_file(path: Path, *, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise RecoveryError(
            f"{label.capitalize()} does not exist or cannot be accessed."
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise RecoveryError(f"{label.capitalize()} must be a regular, non-symlink file.")


def _require_new_destination(path: Path, *, label: str) -> Path:
    parent = path.parent
    if not parent.is_dir():
        raise RecoveryError(f"{label.capitalize()} parent directory must already exist.")
    if path.exists() or path.is_symlink():
        raise RecoveryError(f"{label.capitalize()} already exists; refusing to overwrite.")
    return parent


def _open_readonly(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
            timeout=10.0,
        )
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error as exc:
        raise RecoveryError("Could not open SQLite database read-only.") from exc


def _copy_database(source: sqlite3.Connection, destination: sqlite3.Connection) -> None:
    source.backup(destination, pages=256, sleep=0.1)


def _application_revision() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=_REPOSITORY_ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = result.stdout.strip()
    return revision if re.fullmatch(r"[0-9a-fA-F]{40,64}", revision) else None


def _write_staged_file(path: Path, content: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(descriptor, "wb") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def _secure_staged_database(path: Path) -> None:
    os.chmod(path, 0o600)
    with path.open("rb") as database_file:
        os.fsync(database_file.fileno())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as database_file:
        for chunk in iter(lambda: database_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_snapshot_digest(path: Path, expected_digest: str) -> None:
    try:
        actual_digest = _sha256_file(path)
    except OSError as exc:
        raise RecoveryError("Could not verify the snapshot digest.") from exc
    if actual_digest != expected_digest:
        raise RecoveryError("Backup metadata does not match the snapshot digest.")


def _metadata_path(database_path: Path) -> Path:
    return database_path.with_name(database_path.name + ".metadata.json")


def _read_backup_metadata(database_path: Path) -> dict[str, Any]:
    metadata_path = _metadata_path(database_path)
    _require_regular_file(metadata_path, label="backup metadata")
    try:
        if metadata_path.stat().st_size > _METADATA_MAX_BYTES:
            raise RecoveryError("Backup metadata is too large.")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except RecoveryError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RecoveryError("Backup metadata is missing or malformed.") from exc

    expected_fields = {
        "format_version",
        "captured_at",
        "sqlite_version",
        "application_revision",
        "backup_filename",
        "capture_method",
        "snapshot_sha256",
    }
    if not isinstance(metadata, dict) or set(metadata) != expected_fields:
        raise RecoveryError("Backup metadata has an unsupported structure.")
    if (
        type(metadata["format_version"]) is not int
        or metadata["format_version"] != _METADATA_FORMAT_VERSION
    ):
        raise RecoveryError("Backup metadata version is unsupported.")
    if metadata["backup_filename"] != database_path.name:
        raise RecoveryError("Backup metadata does not match the snapshot.")
    if metadata["capture_method"] != "sqlite3.Connection.backup":
        raise RecoveryError("Backup metadata has an unsupported capture method.")
    if not isinstance(metadata["captured_at"], str):
        raise RecoveryError("Backup metadata has an invalid capture timestamp.")
    try:
        captured_at = datetime.fromisoformat(metadata["captured_at"])
    except ValueError as exc:
        raise RecoveryError("Backup metadata has an invalid capture timestamp.") from exc
    if captured_at.tzinfo is None:
        raise RecoveryError("Backup metadata has an invalid capture timestamp.")
    if (
        not isinstance(metadata["sqlite_version"], str)
        or not metadata["sqlite_version"]
    ):
        raise RecoveryError("Backup metadata has an invalid SQLite version.")
    revision = metadata["application_revision"]
    if revision is not None and (
        not isinstance(revision, str)
        or not re.fullmatch(r"[0-9a-fA-F]{40,64}", revision)
    ):
        raise RecoveryError("Backup metadata has an invalid application revision.")
    if not isinstance(metadata["snapshot_sha256"], str) or not re.fullmatch(
        r"[0-9a-f]{64}", metadata["snapshot_sha256"]
    ):
        raise RecoveryError("Backup metadata has an invalid snapshot digest.")
    _verify_snapshot_digest(database_path, metadata["snapshot_sha256"])
    return metadata


def _publish_without_overwrite(staged: Path, destination: Path) -> None:
    os.link(staged, destination)


def _remove_if_still_ours(staged: Path, published: Path) -> bool:
    try:
        info = published.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode):
        return False
    try:
        if not os.path.samefile(staged, published):
            return False
        published.unlink()
        return True
    except OSError:
        return False


def _remove_published_pair_if_owned(
    staged_database: Path,
    database_path: Path,
    staged_metadata: Path,
    metadata_path: Path,
) -> bool:
    metadata_removed = _remove_if_still_ours(staged_metadata, metadata_path)
    database_removed = _remove_if_still_ours(staged_database, database_path)
    return metadata_removed and database_removed


def create_backup(
    source_path: str | os.PathLike[str],
    destination_path: str | os.PathLike[str],
    *,
    create_backup: bool = False,
    confirm_disposable_source: bool = False,
) -> dict[str, Any]:
    """Create a consistent online backup in a caller-selected temp location."""
    if not create_backup or not confirm_disposable_source:
        raise RecoveryError(
            "Backup creation requires explicit backup and disposable-source confirmation."
        )
    source_path = _safe_path(source_path, label="source")
    destination_path = _safe_path(destination_path, label="destination")
    _refuse_active_database(source_path, label="source")
    _refuse_active_database(destination_path, label="destination")
    _require_regular_file(source_path, label="source")
    if source_path == destination_path:
        raise RecoveryError("Source and destination must be different paths.")
    destination_parent = _require_new_destination(
        destination_path, label="destination"
    )
    metadata_path = _metadata_path(destination_path)
    _refuse_active_database(metadata_path, label="metadata destination")
    _require_new_destination(metadata_path, label="metadata destination")

    with tempfile.TemporaryDirectory(
        dir=destination_parent, prefix=".kybernaut-recovery-"
    ) as staging_dir:
        staged_database = Path(staging_dir) / "snapshot.sqlite3"
        staged_metadata = Path(staging_dir) / "snapshot.metadata.json"
        source = _open_readonly(source_path)
        try:
            destination = sqlite3.connect(staged_database)
            try:
                _copy_database(source, destination)
            finally:
                destination.close()
            _secure_staged_database(staged_database)
        except sqlite3.Error as exc:
            raise RecoveryError("SQLite online backup failed.") from exc
        finally:
            source.close()

        provenance = {
            "format_version": _METADATA_FORMAT_VERSION,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "sqlite_version": sqlite3.sqlite_version,
            "application_revision": _application_revision(),
            "backup_filename": destination_path.name,
            "capture_method": "sqlite3.Connection.backup",
            "snapshot_sha256": _sha256_file(staged_database),
        }
        _write_staged_file(
            staged_metadata,
            (json.dumps(provenance, sort_keys=True, indent=2) + "\n").encode(),
        )

        try:
            _publish_without_overwrite(staged_database, destination_path)
            _publish_without_overwrite(staged_metadata, metadata_path)
        except FileExistsError as exc:
            if not _remove_published_pair_if_owned(
                staged_database,
                destination_path,
                staged_metadata,
                metadata_path,
            ):
                raise RecoveryError(
                    "Backup publication collision; partial cleanup could not be confirmed."
                ) from exc
            raise RecoveryError(
                "A destination appeared during backup; no overwrite was performed."
            ) from exc
        except OSError as exc:
            if not _remove_published_pair_if_owned(
                staged_database,
                destination_path,
                staged_metadata,
                metadata_path,
            ):
                raise RecoveryError(
                    "Backup publication failed; partial cleanup could not be confirmed."
                ) from exc
            raise RecoveryError(
                "Backup publication failed; partial artifacts were removed."
            ) from exc

    return {
        "backup_filename": destination_path.name,
        "metadata_filename": metadata_path.name,
        **provenance,
    }


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    escaped = table.replace('"', '""')
    return {
        row[1]
        for row in connection.execute(f'PRAGMA table_info("{escaped}")')
    }


def _count_if_table(connection: sqlite3.Connection, table: str) -> int:
    escaped = table.replace('"', '""')
    return int(
        connection.execute(f'SELECT COUNT(*) FROM "{escaped}"').fetchone()[0]
    )


def _json_dict(value: Any) -> dict | None:
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _has_columns(
    connection: sqlite3.Connection, table: str, required: set[str]
) -> bool:
    return required <= _table_columns(connection, table)


def _lifecycle_validation(
    connection: sqlite3.Connection, tables: set[str]
) -> dict:
    required_tables = {
        "incidents",
        "incident_handoffs",
        "workflow_claims",
        "action_claims",
        "approval_requests",
        "audit_events",
    }
    required_checks = {
        "handoff_incident_links",
        "workflow_incident_links",
        "handoff_workflow_links",
        "action_workflow_links",
        "approval_action_links",
        "approval_incident_links",
        "audit_action_links",
        "audit_workflow_links",
        "audit_approval_links",
        "verified_action_evidence",
        "verified_recovery_audit_links",
    }
    findings: list[str] = []
    unresolved_actions = 0
    unresolved_workflows = 0
    unresolved_handoffs = 0
    checked: list[str] = []
    not_checked: list[str] = []
    missing_tables = sorted(required_tables - tables)

    if (
        {"incidents", "incident_handoffs"} <= tables
        and _has_columns(connection, "incidents", {"id"})
        and _has_columns(
            connection, "incident_handoffs", {"incident_row_id"}
        )
    ):
        checked.append("handoff_incident_links")
        count = connection.execute(
            """SELECT COUNT(*) FROM incident_handoffs h
               LEFT JOIN incidents i ON i.id = h.incident_row_id
               WHERE i.id IS NULL"""
        ).fetchone()[0]
        if count:
            findings.append("orphan_handoff_incident")
        checked.append("handoff_incident_links")
    elif {"incidents", "incident_handoffs"} <= tables:
        not_checked.append("handoff_incident_links")

    if {"workflow_claims", "incidents"} <= tables:
        workflow_columns = _table_columns(connection, "workflow_claims")
        if (
            "incident_occurrence_id" in workflow_columns
            and _has_columns(connection, "incidents", {"id"})
        ):
            checked.append("workflow_incident_links")
            count = connection.execute(
                """SELECT COUNT(*) FROM workflow_claims w
                   LEFT JOIN incidents i ON i.id = w.incident_occurrence_id
                   WHERE w.incident_occurrence_id IS NOT NULL AND i.id IS NULL"""
            ).fetchone()[0]
            if count:
                findings.append("orphan_workflow_incident")
            checked.append("workflow_incident_links")
        else:
            not_checked.append("workflow_incident_links")

    if (
        {"workflow_claims", "incident_handoffs"} <= tables
        and _has_columns(connection, "workflow_claims", {"claim_id"})
        and _has_columns(
            connection, "incident_handoffs", {"workflow_claim_id"}
        )
    ):
        checked.append("handoff_workflow_links")
        count = connection.execute(
            """SELECT COUNT(*) FROM incident_handoffs h
               LEFT JOIN workflow_claims w ON w.claim_id = h.workflow_claim_id
               WHERE h.workflow_claim_id IS NOT NULL AND w.claim_id IS NULL"""
        ).fetchone()[0]
        if count:
            findings.append("orphan_handoff_workflow")
        checked.append("handoff_workflow_links")
    elif {"workflow_claims", "incident_handoffs"} <= tables:
        not_checked.append("handoff_workflow_links")

    if (
        {"action_claims", "workflow_claims"} <= tables
        and _has_columns(connection, "action_claims", {"workflow_claim_id"})
        and _has_columns(connection, "workflow_claims", {"claim_id"})
    ):
        checked.append("action_workflow_links")
        count = connection.execute(
            """SELECT COUNT(*) FROM action_claims a
               LEFT JOIN workflow_claims w ON w.claim_id = a.workflow_claim_id
               WHERE w.claim_id IS NULL"""
        ).fetchone()[0]
        if count:
            findings.append("orphan_action_workflow")
        checked.append("action_workflow_links")
    elif {"action_claims", "workflow_claims"} <= tables:
        not_checked.append("action_workflow_links")

    if {"action_claims", "approval_requests"} <= tables:
        if (
            _has_columns(
                connection, "action_claims", {"action_claim_id", "approval_id"}
            )
            and _has_columns(
                connection, "approval_requests",
                {"approval_id", "action_claim_id"},
            )
        ):
            checked.append("approval_action_links")
            mismatch = connection.execute(
                """SELECT COUNT(*) FROM action_claims a
                   JOIN approval_requests p ON p.approval_id = a.approval_id
                   WHERE a.approval_id IS NOT NULL
                     AND p.action_claim_id IS NOT NULL
                     AND p.action_claim_id != a.action_claim_id"""
            ).fetchone()[0]
            orphan = connection.execute(
                """SELECT COUNT(*) FROM action_claims a
                   LEFT JOIN approval_requests p ON p.approval_id = a.approval_id
                   WHERE a.approval_id IS NOT NULL AND p.approval_id IS NULL"""
            ).fetchone()[0]
            reverse_mismatch = connection.execute(
                """SELECT COUNT(*) FROM approval_requests p
                   JOIN action_claims a ON a.action_claim_id = p.action_claim_id
                   WHERE p.action_claim_id IS NOT NULL
                     AND a.approval_id IS NOT p.approval_id"""
            ).fetchone()[0]
            if mismatch or orphan or reverse_mismatch:
                findings.append("mismatched_approval_action")
            checked.append("approval_action_links")
        else:
            not_checked.append("approval_action_links")

    if "audit_events" in tables:
        audit_columns = _table_columns(connection, "audit_events")
        if (
            "action_claim_id" in audit_columns
            and "action_claims" in tables
            and _has_columns(connection, "action_claims", {"action_claim_id"})
        ):
            count = connection.execute(
                """SELECT COUNT(*) FROM audit_events e
                   LEFT JOIN action_claims a
                     ON a.action_claim_id = e.action_claim_id
                   WHERE e.action_claim_id IS NOT NULL
                     AND a.action_claim_id IS NULL"""
            ).fetchone()[0]
            if count:
                findings.append("orphan_audit_action")
            checked.append("audit_action_links")
        if (
            "workflow_claim_id" in audit_columns
            and "workflow_claims" in tables
            and _has_columns(connection, "workflow_claims", {"claim_id"})
        ):
            count = connection.execute(
                """SELECT COUNT(*) FROM audit_events e
                   LEFT JOIN workflow_claims w
                     ON w.claim_id = e.workflow_claim_id
                   WHERE e.workflow_claim_id IS NOT NULL
                     AND w.claim_id IS NULL"""
            ).fetchone()[0]
            if count:
                findings.append("orphan_audit_workflow")
            checked.append("audit_workflow_links")
        if (
            "approval_id" in audit_columns
            and "approval_requests" in tables
            and _has_columns(connection, "approval_requests", {"approval_id"})
        ):
            count = connection.execute(
                """SELECT COUNT(*) FROM audit_events e
                   LEFT JOIN approval_requests p ON p.approval_id = e.approval_id
                   WHERE e.approval_id IS NOT NULL AND p.approval_id IS NULL"""
            ).fetchone()[0]
            if count:
                findings.append("orphan_audit_approval")
            checked.append("audit_approval_links")

    if {"approval_requests", "action_claims"} <= tables:
        if (
            _has_columns(connection, "approval_requests", {"action_claim_id"})
            and _has_columns(connection, "action_claims", {"action_claim_id"})
        ):
            count = connection.execute(
                """SELECT COUNT(*) FROM approval_requests p
                   LEFT JOIN action_claims a ON a.action_claim_id = p.action_claim_id
                   WHERE p.action_claim_id IS NOT NULL AND a.action_claim_id IS NULL"""
            ).fetchone()[0]
            if count:
                findings.append("orphan_approval_action")

    if {
        "approval_requests", "action_claims", "workflow_claims", "incidents"
    } <= tables:
        occurrence_link_columns = (
            _has_columns(connection, "approval_requests", {
                "action_claim_id", "incident_ref",
            })
            and _has_columns(connection, "action_claims", {
                "action_claim_id", "workflow_claim_id",
            })
            and _has_columns(connection, "workflow_claims", {
                "claim_id", "incident_ref", "incident_occurrence_id",
            })
            and _has_columns(connection, "incidents", {"id", "incident_id"})
        )
        if occurrence_link_columns:
            checked.append("approval_incident_links")
            mismatch = connection.execute(
                """SELECT COUNT(*) FROM approval_requests p
                   JOIN action_claims a ON a.action_claim_id = p.action_claim_id
                   JOIN workflow_claims w ON w.claim_id = a.workflow_claim_id
                   LEFT JOIN incidents i ON i.id = w.incident_occurrence_id
                   WHERE p.action_claim_id IS NOT NULL
                     AND (
                         p.incident_ref != w.incident_ref
                         OR (
                             w.incident_occurrence_id IS NOT NULL
                             AND (i.id IS NULL OR i.incident_id != p.incident_ref)
                         )
                     )"""
            ).fetchone()[0]
            if mismatch:
                findings.append("approval_incident_mismatch")
            checked.append("approval_incident_links")
        else:
            not_checked.append("approval_incident_links")

    if "action_claims" in tables:
        action_columns = _table_columns(connection, "action_claims")
        if {"status", "execution_json", "verification_json"} <= action_columns:
            checked.append("verified_action_evidence")
            actions = connection.execute(
                """SELECT status, execution_json, verification_json
                   FROM action_claims WHERE status = 'VERIFIED'"""
            ).fetchall()
            for action in actions:
                execution = _json_dict(action["execution_json"])
                verification = _json_dict(action["verification_json"])
                if not (
                    execution
                    and execution.get("mode") == "live"
                    and execution.get("executed") is True
                    and execution.get("ok") is True
                    and verification
                    and verification.get("recovered") is True
                ):
                    findings.append("verified_action_missing_recovery_evidence")
                    break
            checked.append("verified_action_evidence")
        else:
            not_checked.append("verified_action_evidence")

        if "status" in action_columns:
            unresolved_actions = int(connection.execute(
                """SELECT COUNT(*) FROM action_claims
                   WHERE status IN ('EXECUTING', 'EXECUTION_UNKNOWN')"""
            ).fetchone()[0])

    if (
        "workflow_claims" in tables
        and _has_columns(connection, "workflow_claims", {"status"})
    ):
        unresolved_workflows = int(
            connection.execute(
                """SELECT COUNT(*) FROM workflow_claims
                   WHERE status = 'RECONCILIATION_REQUIRED'"""
            ).fetchone()[0]
        )
    if (
        "incident_handoffs" in tables
        and _has_columns(connection, "incident_handoffs", {"status"})
    ):
        unresolved_handoffs = int(
            connection.execute(
                """SELECT COUNT(*) FROM incident_handoffs
                   WHERE status IN ('DISPATCHING', 'RECONCILIATION_REQUIRED')"""
            ).fetchone()[0]
        )

    required_for_recovery = {
        "incidents",
        "incident_handoffs",
        "workflow_claims",
        "action_claims",
        "audit_events",
    }
    recovery_columns_available = (
        required_for_recovery <= tables
        and _has_columns(
            connection, "incidents",
            {"id", "incident_id", "status", "resolution_reason"},
        )
        and _has_columns(
            connection, "incident_handoffs",
            {"incident_row_id", "workflow_claim_id"},
        )
        and _has_columns(connection, "workflow_claims", {"claim_id"})
        and _has_columns(
            connection, "action_claims",
            {"workflow_claim_id", "action_claim_id", "status"},
        )
        and _has_columns(
            connection, "audit_events",
            {
                "incident_ref", "event_type", "workflow_claim_id",
                "action_claim_id",
            },
        )
    )
    if recovery_columns_available:
        checked.append("verified_recovery_audit_links")
        recoveries = connection.execute(
            """SELECT i.id, i.incident_id, h.workflow_claim_id,
                      a.action_claim_id
               FROM incidents i
               LEFT JOIN incident_handoffs h ON h.incident_row_id = i.id
               LEFT JOIN workflow_claims w ON w.claim_id = h.workflow_claim_id
               LEFT JOIN action_claims a
                 ON a.workflow_claim_id = w.claim_id AND a.status = 'VERIFIED'
               WHERE i.status = 'RESOLVED'
                 AND i.resolution_reason = 'verified_kubernetes_recovery'"""
        ).fetchall()
        for recovery in recoveries:
            if (
                not recovery["workflow_claim_id"]
                or not recovery["action_claim_id"]
            ):
                findings.append("verified_recovery_missing_linked_action")
                continue
            audit_columns = _table_columns(connection, "audit_events")
            if not {
                "workflow_claim_id", "action_claim_id"
            } <= audit_columns:
                findings.append("verified_recovery_audit_schema_incomplete")
                continue
            event = connection.execute(
                """SELECT 1 FROM audit_events
                   WHERE incident_ref = ?
                     AND event_type = 'incident_recovery_verified'
                     AND workflow_claim_id = ? AND action_claim_id = ?
                   LIMIT 1""",
                (
                    recovery["incident_id"],
                    recovery["workflow_claim_id"],
                    recovery["action_claim_id"],
                ),
            ).fetchone()
            if event is None:
                findings.append("verified_recovery_audit_event_missing")
        checked.append("verified_recovery_audit_links")
    elif required_for_recovery <= tables:
        not_checked.append("verified_recovery_audit_links")

    missing_checks = sorted(required_checks - set(checked))
    not_checked = sorted(set(not_checked) | set(missing_checks))
    coverage_complete = not missing_tables and not missing_checks
    return {
        "checked": sorted(set(checked)),
        "not_checked": not_checked,
        "missing_tables": missing_tables,
        "missing_checks": missing_checks,
        "coverage_complete": coverage_complete,
        "findings": sorted(set(findings)),
        "unresolved_execution_count": unresolved_actions,
        "unresolved_lifecycle_state_counts": {
            "workflow_reconciliation_required": unresolved_workflows,
            "handoff_dispatching_or_reconciliation_required": unresolved_handoffs,
        },
        "consistent": not findings,
    }


def validate_database(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read-only integrity, schema, aggregate, FK, and lifecycle validation."""
    database_path = _safe_path(path, label="database")
    _refuse_active_database(database_path, label="database")
    _require_regular_file(database_path, label="database")
    metadata = _read_backup_metadata(database_path)

    connection = _open_readonly(database_path)
    try:
        try:
            integrity_rows = connection.execute(
                "PRAGMA integrity_check"
            ).fetchall()
        except sqlite3.Error as exc:
            raise RecoveryError(
                "SQLite integrity validation could not be completed."
            ) from exc
        integrity_ok = len(integrity_rows) == 1 and integrity_rows[0][0] == "ok"

        tables = {
            row[0]
            for row in connection.execute(
                """SELECT name FROM sqlite_master
                   WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                   ORDER BY name"""
            )
        }
        table_counts = {
            table: _count_if_table(connection, table)
            for table in sorted(tables)
        }
        foreign_key_declarations = 0
        for table in tables:
            escaped = table.replace('"', '""')
            foreign_key_declarations += len(
                connection.execute(
                    f'PRAGMA foreign_key_list("{escaped}")'
                ).fetchall()
            )
        foreign_key_failures = (
            sum(1 for _ in connection.execute("PRAGMA foreign_key_check"))
            if foreign_key_declarations else 0
        )
        lifecycle = _lifecycle_validation(connection, tables)
    except sqlite3.Error as exc:
        raise RecoveryError("SQLite validation could not be completed.") from exc
    finally:
        connection.close()

    _verify_snapshot_digest(database_path, metadata["snapshot_sha256"])

    return {
        "filename": database_path.name,
        "integrity_ok": integrity_ok,
        "table_names": sorted(tables),
        "table_counts": table_counts,
        "foreign_key_check": (
            "not_applicable"
            if not foreign_key_declarations
            else "ok" if foreign_key_failures == 0 else "failed"
        ),
        "foreign_key_failure_count": foreign_key_failures,
        "lifecycle": lifecycle,
        "valid": (
            integrity_ok
            and foreign_key_failures == 0
            and lifecycle["coverage_complete"]
            and lifecycle["consistent"]
        ),
    }


def restore_disposable(
    backup_path: str | os.PathLike[str],
    destination_path: str | os.PathLike[str],
    *,
    confirm_disposable_destination: bool = False,
) -> dict[str, str]:
    """Materialize a backup at a new temporary path without launching Kybernaut."""
    if not confirm_disposable_destination:
        raise RecoveryError("Restore requires explicit disposable-destination confirmation.")
    backup_path = _safe_path(backup_path, label="backup")
    destination_path = _safe_path(destination_path, label="restore destination")
    _refuse_active_database(backup_path, label="backup")
    _refuse_active_database(destination_path, label="restore destination")
    _require_regular_file(backup_path, label="backup")
    metadata = _read_backup_metadata(backup_path)
    if backup_path == destination_path:
        raise RecoveryError("Backup and restore destination must be different paths.")
    destination_parent = _require_new_destination(
        destination_path, label="restore destination"
    )
    metadata_path = _metadata_path(destination_path)
    _refuse_active_database(metadata_path, label="restore metadata destination")
    _require_new_destination(metadata_path, label="restore metadata destination")

    with tempfile.TemporaryDirectory(
        dir=destination_parent, prefix=".kybernaut-restore-"
    ) as staging_dir:
        staged = Path(staging_dir) / "restored.sqlite3"
        staged_metadata = Path(staging_dir) / "restored.metadata.json"
        source = _open_readonly(backup_path)
        try:
            destination = sqlite3.connect(staged)
            try:
                _copy_database(source, destination)
            finally:
                destination.close()
            _secure_staged_database(staged)
        except sqlite3.Error as exc:
            raise RecoveryError("SQLite restore materialization failed.") from exc
        finally:
            source.close()
        _verify_snapshot_digest(backup_path, metadata["snapshot_sha256"])
        restored_metadata = {
            **metadata,
            "backup_filename": destination_path.name,
            "snapshot_sha256": _sha256_file(staged),
        }
        _write_staged_file(
            staged_metadata,
            (
                json.dumps(restored_metadata, sort_keys=True, indent=2) + "\n"
            ).encode(),
        )

        try:
            _publish_without_overwrite(staged, destination_path)
            _publish_without_overwrite(staged_metadata, metadata_path)
        except FileExistsError as exc:
            if not _remove_published_pair_if_owned(
                staged,
                destination_path,
                staged_metadata,
                metadata_path,
            ):
                raise RecoveryError(
                    "Restore publication failed; partial cleanup could not be confirmed."
                ) from exc
            raise RecoveryError(
                "A restore destination appeared during operation; no overwrite was performed."
            ) from exc
        except OSError as exc:
            if not _remove_published_pair_if_owned(
                staged,
                destination_path,
                staged_metadata,
                metadata_path,
            ):
                raise RecoveryError(
                    "Restore publication failed; partial cleanup could not be confirmed."
                ) from exc
            raise RecoveryError(
                "Restore publication failed; partial artifacts were removed."
            ) from exc

    return {
        "backup_filename": backup_path.name,
        "restore_filename": destination_path.name,
        "metadata_filename": metadata_path.name,
    }
