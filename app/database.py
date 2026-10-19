import os
import sqlite3
from pathlib import Path

DATABASE_PATH = Path(
    os.getenv(
        "KYBERNAUT_DATABASE_PATH",
        str(Path(__file__).resolve().parent.parent / "incidents.db"),
    )
)


def get_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=10.0)
    connection.row_factory = sqlite3.Row
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
