import os
import sqlite3
from pathlib import Path

DEFAULT_DATABASE_PATH = Path(__file__).resolve().parent.parent / "incidents.db"
DATABASE_PATH = Path(
    os.getenv("KYBERNAUT_DATABASE_PATH", str(DEFAULT_DATABASE_PATH))
)


def get_database_path() -> Path:
    """Resolve the configured database path without creating or opening it."""
    configured_path = os.getenv("KYBERNAUT_DATABASE_PATH")
    if configured_path:
        return Path(configured_path)
    return DATABASE_PATH


def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(get_database_path(), timeout=10.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    except sqlite3.Error:
        connection.close()
        raise
    if enabled != 1:
        connection.close()
        raise RuntimeError("SQLite foreign key enforcement is unavailable")
    return connection


def initialize_database() -> None:
    """Create database tables and indexes if they do not exist."""
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id TEXT NOT NULL,
                service TEXT NOT NULL,
                incident_type TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                opened_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                resolved_at TEXT,
                resolution_reason TEXT,
                remediation_count INTEGER NOT NULL DEFAULT 0,
                last_remediation_status TEXT,
                UNIQUE (incident_id, opened_at)
            )
            """
        )

        # Add lifecycle identity fields safely to existing databases.
        incident_columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(incidents)"
            ).fetchall()
        }

        if "fingerprint" not in incident_columns:
            connection.execute(
                "ALTER TABLE incidents ADD COLUMN fingerprint TEXT"
            )

        if "last_seen_at" not in incident_columns:
            connection.execute(
                "ALTER TABLE incidents ADD COLUMN last_seen_at TEXT"
            )

        if "source_timestamp" not in incident_columns:
            connection.execute(
                "ALTER TABLE incidents ADD COLUMN source_timestamp TEXT"
            )

        if "source_evidence_json" not in incident_columns:
            connection.execute(
                "ALTER TABLE incidents ADD COLUMN source_evidence_json TEXT"
            )

        if "resolution_reason" not in incident_columns:
            connection.execute(
                "ALTER TABLE incidents ADD COLUMN resolution_reason TEXT"
            )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_incidents_service_status
            ON incidents (service, status)
            """
        )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_incidents_fingerprint_status
            ON incidents (fingerprint, status)
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS incident_diagnoses (
                incident_row_id INTEGER PRIMARY KEY,
                evidence_digest TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                next_retry_at TEXT
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS incident_handoffs (
                incident_row_id INTEGER PRIMARY KEY,
                service TEXT NOT NULL,
                incident_id TEXT NOT NULL,
                fingerprint TEXT,
                evidence_timestamp TEXT,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                lease_owner TEXT,
                lease_token TEXT,
                lease_expires_at TEXT,
                idempotency_key TEXT NOT NULL UNIQUE,
                workflow_claim_id TEXT UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                retry_at TEXT,
                completed_at TEXT,
                last_error TEXT,
                FOREIGN KEY (incident_row_id) REFERENCES incidents(id)
            )
            """
        )
        if (
            os.getenv("KYBERNAUT_DEMO_MODE", "").strip().lower() == "true"
            and os.getenv("KYBERNAUT_ENVIRONMENT", "").strip().lower()
            == "local-demo"
        ):
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS demo_sessions (
                    session_id TEXT PRIMARY KEY,
                    operator_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_demo_sessions_expiry
                ON demo_sessions (expires_at)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS demo_incident_occurrences (
                    scheduled_slot TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL UNIQUE,
                    incident_row_id INTEGER,
                    scenario TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (incident_row_id) REFERENCES incidents(id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_demo_occurrences_created
                ON demo_incident_occurrences (created_at DESC)
                """
            )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_incident_handoff_dispatch
            ON incident_handoffs (status, retry_at, lease_expires_at)
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS console_investigations (
                investigation_id TEXT PRIMARY KEY,
                operator_id TEXT NOT NULL,
                incident_id TEXT,
                title TEXT NOT NULL,
                messages_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_console_investigations_updated
            ON console_investigations (updated_at DESC)
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS remediation_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                audit_id TEXT NOT NULL UNIQUE,
                incident_id TEXT NOT NULL,
                service TEXT NOT NULL,
                attempted_at TEXT NOT NULL,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                operator_id TEXT
            )
            """
        )

        # Safely add operator attribution to existing databases.
        columns = {
            row["name"]
            for row in connection.execute(
                "PRAGMA table_info(remediation_attempts)"
            ).fetchall()
        }

        if "operator_id" not in columns:
            connection.execute(
                "ALTER TABLE remediation_attempts ADD COLUMN operator_id TEXT"
            )

        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_remediation_service_incident
            ON remediation_attempts (service, incident_id, attempted_at)
            """
        )