"""Opt-in persistence for explicitly authorized lab-run provenance.

Schema creation is never performed during application startup. Callers must
provide a database path, explicitly initialize the schema, and configure an
authorizer; the default authorizer denies every operation.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import re
import sqlite3
from pathlib import Path
from typing import Iterator, Protocol

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_SCHEMA_VERSION = 1


def _valid_utc_timestamp_sql(column: str) -> str:
    return f"""
        length({column}) = 27
        AND {column} GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]Z'
        AND CAST(substr({column}, 1, 4) AS INTEGER) BETWEEN 1 AND 9999
        AND CAST(substr({column}, 6, 2) AS INTEGER) BETWEEN 1 AND 12
        AND CAST(substr({column}, 9, 2) AS INTEGER) BETWEEN 1 AND
            CASE CAST(substr({column}, 6, 2) AS INTEGER)
                WHEN 2 THEN CASE
                    WHEN CAST(substr({column}, 1, 4) AS INTEGER) % 4 = 0
                     AND (
                         CAST(substr({column}, 1, 4) AS INTEGER) % 100 != 0
                         OR CAST(substr({column}, 1, 4) AS INTEGER) % 400 = 0
                     )
                    THEN 29 ELSE 28 END
                WHEN 4 THEN 30
                WHEN 6 THEN 30
                WHEN 9 THEN 30
                WHEN 11 THEN 30
                ELSE 31
            END
        AND CAST(substr({column}, 12, 2) AS INTEGER) BETWEEN 0 AND 23
        AND CAST(substr({column}, 15, 2) AS INTEGER) BETWEEN 0 AND 59
        AND CAST(substr({column}, 18, 2) AS INTEGER) BETWEEN 0 AND 59
    """


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE lab_provenance_schema_version (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        schema_version INTEGER NOT NULL
    )
    """,
    f"""
    CREATE TABLE lab_runs (
        lab_run_id TEXT PRIMARY KEY,
        environment_id TEXT NOT NULL,
        started_at_utc TEXT NOT NULL,
        completed_at_utc TEXT,
        capture_method TEXT NOT NULL,
        source_revision TEXT,
        artifact_sha256 TEXT,
        evidence_ref TEXT,
        status TEXT NOT NULL
            CHECK (status IN ('running', 'completed', 'aborted')),
        created_by TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,
        finalized_by TEXT,
        CHECK (
            artifact_sha256 IS NULL
            OR (
                length(artifact_sha256) = 64
                AND artifact_sha256 NOT GLOB '*[^0-9a-f]*'
            )
        ),
        CHECK (
            evidence_ref IS NULL
            OR (
                length(evidence_ref) BETWEEN 1 AND 256
                AND evidence_ref GLOB '[A-Za-z0-9]*'
                AND evidence_ref NOT GLOB '*[^A-Za-z0-9._:/-]*'
                AND instr(evidence_ref, '://') = 0
            )
        ),
        CHECK (
            {_valid_utc_timestamp_sql("started_at_utc")}
        ),
        CHECK (
            {_valid_utc_timestamp_sql("created_at_utc")}
        ),
        CHECK (
            completed_at_utc IS NULL
            OR ({_valid_utc_timestamp_sql("completed_at_utc")})
        ),
        CHECK (
            (status = 'running' AND completed_at_utc IS NULL)
            OR (status IN ('completed', 'aborted')
                AND completed_at_utc IS NOT NULL
                AND completed_at_utc >= started_at_utc)
        )
    )
    """,
    """
    CREATE TABLE lab_run_scenarios (
        lab_run_id TEXT NOT NULL
            REFERENCES lab_runs(lab_run_id) ON DELETE RESTRICT,
        scenario_id TEXT NOT NULL,
        PRIMARY KEY (lab_run_id, scenario_id)
    )
    """,
    f"""
    CREATE TABLE lab_run_occurrences (
        occurrence_id INTEGER PRIMARY KEY AUTOINCREMENT,
        lab_run_id TEXT NOT NULL
            REFERENCES lab_runs(lab_run_id) ON DELETE RESTRICT,
        event_key TEXT NOT NULL,
        event_digest TEXT NOT NULL,
        incident_row_id INTEGER NOT NULL
            REFERENCES incidents(id) ON DELETE RESTRICT,
        source_timestamp_utc TEXT,
        observed_at_utc TEXT NOT NULL,
        UNIQUE (lab_run_id, event_key),
        CHECK (
            length(event_digest) = 64
            AND event_digest NOT GLOB '*[^0-9a-f]*'
        ),
        CHECK (
            source_timestamp_utc IS NULL
            OR ({_valid_utc_timestamp_sql("source_timestamp_utc")})
        ),
        CHECK (
            {_valid_utc_timestamp_sql("observed_at_utc")}
        )
    )
    """,
    """
    CREATE TRIGGER lab_runs_insert_guard
    BEFORE INSERT ON lab_runs
    WHEN NEW.status != 'running' OR NEW.completed_at_utc IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, 'lab runs must begin in running state');
    END
    """,
    """
    CREATE TRIGGER lab_runs_update_guard
    BEFORE UPDATE ON lab_runs
    WHEN OLD.status != 'running'
      OR NEW.status NOT IN ('completed', 'aborted')
      OR NEW.lab_run_id IS NOT OLD.lab_run_id
      OR NEW.environment_id IS NOT OLD.environment_id
      OR NEW.started_at_utc IS NOT OLD.started_at_utc
      OR NEW.capture_method IS NOT OLD.capture_method
      OR NEW.source_revision IS NOT OLD.source_revision
      OR NEW.artifact_sha256 IS NOT OLD.artifact_sha256
      OR NEW.evidence_ref IS NOT OLD.evidence_ref
      OR NEW.created_by IS NOT OLD.created_by
      OR NEW.created_at_utc IS NOT OLD.created_at_utc
      OR NEW.finalized_by IS NULL
      OR NEW.completed_at_utc IS NULL
      OR NEW.completed_at_utc < OLD.started_at_utc
    BEGIN
        SELECT RAISE(ABORT, 'invalid lab run transition');
    END
    """,
    """
    CREATE TRIGGER lab_runs_delete_guard
    BEFORE DELETE ON lab_runs
    BEGIN
        SELECT RAISE(ABORT, 'lab runs are immutable');
    END
    """,
    """
    CREATE TRIGGER lab_run_scenarios_insert_guard
    BEFORE INSERT ON lab_run_scenarios
    WHEN (SELECT status FROM lab_runs
          WHERE lab_run_id = NEW.lab_run_id) != 'running'
    BEGIN
        SELECT RAISE(ABORT, 'scenarios may only be added to running runs');
    END
    """,
    """
    CREATE TRIGGER lab_run_scenarios_update_guard
    BEFORE UPDATE ON lab_run_scenarios
    BEGIN
        SELECT RAISE(ABORT, 'lab run scenarios are immutable');
    END
    """,
    """
    CREATE TRIGGER lab_run_scenarios_delete_guard
    BEFORE DELETE ON lab_run_scenarios
    BEGIN
        SELECT RAISE(ABORT, 'lab run scenarios are immutable');
    END
    """,
    """
    CREATE TRIGGER lab_run_occurrences_insert_guard
    BEFORE INSERT ON lab_run_occurrences
    WHEN NOT EXISTS (
        SELECT 1 FROM lab_runs
        WHERE lab_run_id = NEW.lab_run_id
          AND status = 'completed'
          AND (artifact_sha256 IS NOT NULL OR evidence_ref IS NOT NULL)
          AND NEW.observed_at_utc >= started_at_utc
          AND NEW.observed_at_utc <= completed_at_utc
          AND (
              NEW.source_timestamp_utc IS NULL
              OR (
                  NEW.source_timestamp_utc >= started_at_utc
                  AND NEW.source_timestamp_utc <= completed_at_utc
              )
          )
    )
    BEGIN
        SELECT RAISE(
            ABORT,
            'occurrence timestamps must be within a completed lab run'
        );
    END
    """,
    """
    CREATE TRIGGER lab_run_occurrences_update_guard
    BEFORE UPDATE ON lab_run_occurrences
    BEGIN
        SELECT RAISE(ABORT, 'lab run occurrences are immutable');
    END
    """,
    """
    CREATE TRIGGER lab_run_occurrences_delete_guard
    BEFORE DELETE ON lab_run_occurrences
    BEGIN
        SELECT RAISE(ABORT, 'lab run occurrences are immutable');
    END
    """,
)

_SCHEMA_OBJECT_NAMES = {
    "lab_provenance_schema_version",
    "lab_runs",
    "lab_run_scenarios",
    "lab_run_occurrences",
    "lab_runs_insert_guard",
    "lab_runs_update_guard",
    "lab_runs_delete_guard",
    "lab_run_scenarios_insert_guard",
    "lab_run_scenarios_update_guard",
    "lab_run_scenarios_delete_guard",
    "lab_run_occurrences_insert_guard",
    "lab_run_occurrences_update_guard",
    "lab_run_occurrences_delete_guard",
}


class LabRunError(Exception):
    """Base error for lab-run provenance operations."""


class LabRunAuthorizationError(LabRunError):
    """The configured authorizer did not approve the operation."""


class LabRunSchemaError(LabRunError):
    """The selected database is not initialized for lab provenance."""


class LabRunNotFoundError(LabRunError):
    """The requested lab run does not exist."""


class LabRunStateError(LabRunError):
    """The lab run is ineligible for the requested operation."""


class LabRunConflictError(LabRunError):
    """A run or event key conflicts with previously recorded state."""


class LabRunAuthorizer(Protocol):
    def authorize(
        self,
        *,
        action: str,
        actor_id: str,
        lab_run_id: str,
    ) -> bool:
        """Return whether the actor may perform this lab-run operation."""


class DenyAllLabRunAuthorizer:
    def authorize(
        self,
        *,
        action: str,
        actor_id: str,
        lab_run_id: str,
    ) -> bool:
        return False


def _identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"{field} must be a non-empty opaque identifier")
    return value


def _utc_timestamp(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a timezone-aware timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"{field} must be a timezone-aware timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be a timezone-aware timestamp")
    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _optional_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError("artifact_sha256 must be 64 lowercase hex characters")
    return value


def _optional_evidence_reference(value: str | None) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not _EVIDENCE_REFERENCE.fullmatch(value)
        or "://" in value
    ):
        raise ValueError("evidence_ref must be an opaque controlled reference")
    return value


class LabRunRepository:
    """Explicitly initialized lab-run store using a caller-selected SQLite file."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        authorizer: LabRunAuthorizer | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self._authorizer = authorizer or DenyAllLabRunAuthorizer()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=10.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            enabled = connection.execute(
                "PRAGMA foreign_keys"
            ).fetchone()[0]
            if enabled != 1:
                raise LabRunSchemaError(
                    "SQLite foreign-key enforcement could not be enabled"
                )
            yield connection
        finally:
            connection.close()

    def _authorize(
        self,
        *,
        action: str,
        actor_id: str,
        lab_run_id: str,
    ) -> None:
        _identifier(actor_id, "actor_id")
        if self._authorizer.authorize(
            action=action,
            actor_id=actor_id,
            lab_run_id=lab_run_id,
        ) is not True:
            raise LabRunAuthorizationError(
                f"Lab-run operation is not authorized: {action}"
            )

    def initialize_schema(self, *, actor_id: str) -> None:
        """Create provenance tables explicitly; never called at app startup."""
        self._authorize(
            action="initialize_schema",
            actor_id=actor_id,
            lab_run_id="schema-v1",
        )
        with self._connection() as connection, connection:
            incident_table = connection.execute(
                """
                SELECT 1
                FROM sqlite_master
                WHERE type = 'table' AND name = 'incidents'
                """
            ).fetchone()
            if incident_table is None:
                raise LabRunSchemaError(
                    "The selected database must already contain incidents"
                )
            incident_id = next(
                (
                    row
                    for row in connection.execute(
                        "PRAGMA table_info(incidents)"
                    )
                    if row["pk"] > 0
                ),
                None,
            )
            incident_columns = connection.execute(
                "PRAGMA table_info(incidents)"
            ).fetchall()
            if (
                incident_id is None
                or len([row for row in incident_columns if row["pk"] > 0]) != 1
                or incident_id["name"] != "id"
                or incident_id["type"].strip().upper() != "INTEGER"
            ):
                raise LabRunSchemaError(
                    "The incidents table must have a single INTEGER PRIMARY "
                    "KEY column named id"
                )

            existing_objects = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE substr(name, 1, 4) = 'lab_' "
                    "AND type IN ('table', 'trigger', 'index', 'view')"
                )
            }
            if existing_objects:
                if existing_objects != _SCHEMA_OBJECT_NAMES:
                    raise LabRunSchemaError(
                        "Existing lab provenance schema is partial or "
                        "incompatible; refusing repair"
                    )
                self._validate_schema(connection)
                return

            for statement in _SCHEMA_STATEMENTS[1:]:
                connection.execute(statement)
            connection.execute(_SCHEMA_STATEMENTS[0])
            connection.execute(
                """
                INSERT INTO lab_provenance_schema_version
                    (singleton, schema_version)
                VALUES (1, ?)
                """,
                (_SCHEMA_VERSION,),
            )
            self._validate_schema(connection)

    @staticmethod
    def _normalize_sql(statement: str | None) -> str:
        return " ".join((statement or "").lower().split())

    def _validate_schema(self, connection: sqlite3.Connection) -> None:
        marker = connection.execute(
            """
            SELECT schema_version
            FROM lab_provenance_schema_version
            WHERE singleton = 1
            """
        ).fetchall()
        if len(marker) != 1 or marker[0]["schema_version"] != _SCHEMA_VERSION:
            raise LabRunSchemaError(
                "Unsupported or invalid lab provenance schema version"
            )

        for statement in _SCHEMA_STATEMENTS:
            match = re.match(
                r"\s*create\s+(table|trigger)\s+([a-z_][a-z0-9_]*)",
                statement,
                flags=re.IGNORECASE,
            )
            if match is None:
                raise LabRunSchemaError("Invalid internal schema definition")
            object_type, object_name = match.groups()
            object_type = object_type.lower()
            object_name = object_name.lower()
            stored = connection.execute(
                """
                SELECT sql FROM sqlite_master
                WHERE type = ? AND name = ?
                """,
                (object_type, object_name),
            ).fetchone()
            if stored is None or self._normalize_sql(stored["sql"]) != (
                self._normalize_sql(statement)
            ):
                raise LabRunSchemaError(
                    f"Incompatible lab provenance definition: {object_name}"
                )

    def create_run(
        self,
        *,
        actor_id: str,
        lab_run_id: str,
        environment_id: str,
        scenario_ids: tuple[str, ...] | list[str],
        started_at: str,
        capture_method: str,
        source_revision: str | None = None,
        artifact_sha256: str | None = None,
        evidence_ref: str | None = None,
    ) -> dict:
        lab_run_id = _identifier(lab_run_id, "lab_run_id")
        environment_id = _identifier(environment_id, "environment_id")
        capture_method = _identifier(capture_method, "capture_method")
        scenarios = tuple(
            _identifier(scenario_id, "scenario_id")
            for scenario_id in scenario_ids
        )
        if not scenarios or len(set(scenarios)) != len(scenarios):
            raise ValueError("scenario_ids must be non-empty and unique")
        started_at_utc = _utc_timestamp(started_at, "started_at")
        artifact_sha256 = _optional_sha256(artifact_sha256)
        evidence_ref = _optional_evidence_reference(evidence_ref)
        if source_revision is not None:
            source_revision = _identifier(source_revision, "source_revision")
        created_at_utc = datetime.now(timezone.utc).isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z")

        self._authorize(
            action="create_run",
            actor_id=actor_id,
            lab_run_id=lab_run_id,
        )
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO lab_runs (
                    lab_run_id, environment_id, started_at_utc,
                    capture_method, source_revision, artifact_sha256,
                    evidence_ref, status, created_by, created_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
                """,
                (
                    lab_run_id,
                    environment_id,
                    started_at_utc,
                    capture_method,
                    source_revision,
                    artifact_sha256,
                    evidence_ref,
                    actor_id,
                    created_at_utc,
                ),
            )
            connection.executemany(
                """
                INSERT INTO lab_run_scenarios (lab_run_id, scenario_id)
                VALUES (?, ?)
                """,
                [(lab_run_id, scenario_id) for scenario_id in scenarios],
            )
            return self._load_run(connection, lab_run_id)

    def finalize_run(
        self,
        *,
        actor_id: str,
        lab_run_id: str,
        status: str,
        completed_at: str,
    ) -> dict:
        lab_run_id = _identifier(lab_run_id, "lab_run_id")
        if status not in {"completed", "aborted"}:
            raise ValueError("status must be completed or aborted")
        completed_at_utc = _utc_timestamp(completed_at, "completed_at")
        self._authorize(
            action="finalize_run",
            actor_id=actor_id,
            lab_run_id=lab_run_id,
        )

        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT * FROM lab_runs WHERE lab_run_id = ?",
                (lab_run_id,),
            ).fetchone()
            if run is None:
                raise LabRunNotFoundError("Lab run does not exist")
            if run["status"] != "running":
                raise LabRunStateError("Only running lab runs can be finalized")
            if completed_at_utc < run["started_at_utc"]:
                raise ValueError("completed_at must not precede started_at")
            connection.execute(
                """
                UPDATE lab_runs
                SET status = ?, completed_at_utc = ?, finalized_by = ?
                WHERE lab_run_id = ? AND status = 'running'
                """,
                (status, completed_at_utc, actor_id, lab_run_id),
            )
            return self._load_run(connection, lab_run_id)

    def provenance_review_eligibility(
        self,
        *,
        actor_id: str,
        lab_run_id: str,
    ) -> str:
        """Report whether a completed run has a locator for later review."""
        lab_run_id = _identifier(lab_run_id, "lab_run_id")
        self._authorize(
            action="review_eligibility",
            actor_id=actor_id,
            lab_run_id=lab_run_id,
        )
        with self._connection() as connection:
            run = connection.execute(
                """
                SELECT status, artifact_sha256, evidence_ref
                FROM lab_runs WHERE lab_run_id = ?
                """,
                (lab_run_id,),
            ).fetchone()
            if run is None:
                raise LabRunNotFoundError("Lab run does not exist")
            if run["status"] != "completed":
                return "ineligible_run_not_completed"
            if run["artifact_sha256"] is None and run["evidence_ref"] is None:
                return "ineligible_missing_evidence"
            return "eligible_for_later_review"

    def record_occurrence(
        self,
        *,
        actor_id: str,
        lab_run_id: str,
        event_key: str,
        event_digest: str,
        incident: dict,
        observed_at: str,
        source_timestamp: str | None = None,
    ) -> dict:
        """Fail closed until occurrence-to-summary identity is approved."""
        raise LabRunStateError(
            "Occurrence recording is disabled pending an approved identity "
            "design"
        )

    @staticmethod
    def _load_run(connection: sqlite3.Connection, lab_run_id: str) -> dict:
        run = connection.execute(
            "SELECT * FROM lab_runs WHERE lab_run_id = ?",
            (lab_run_id,),
        ).fetchone()
        scenarios = connection.execute(
            """
            SELECT scenario_id FROM lab_run_scenarios
            WHERE lab_run_id = ? ORDER BY scenario_id
            """,
            (lab_run_id,),
        ).fetchall()
        result = dict(run)
        result["scenario_ids"] = [row["scenario_id"] for row in scenarios]
        return result
