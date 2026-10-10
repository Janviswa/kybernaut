
from datetime import datetime, timezone

from app.database import get_connection


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_incident(incident: dict) -> dict:
    """Create an incident or refresh its existing open record."""
    now = utc_now()

    with get_connection() as connection:
        existing = connection.execute(
            """
            SELECT *
            FROM incidents
            WHERE incident_id = ?
              AND status IN ('OPEN', 'REMEDIATING')
            ORDER BY id DESC
            LIMIT 1
            """,
            (incident["incident_id"],),
        ).fetchone()

        if existing:
            connection.execute(
                """
                UPDATE incidents
                SET updated_at = ?,
                    severity = ?,
                    message = ?
                WHERE id = ?
                """,
                (
                    now,
                    incident["severity"],
                    incident["message"],
                    existing["id"],
                ),
            )

            row = connection.execute(
                "SELECT * FROM incidents WHERE id = ?",
                (existing["id"],),
            ).fetchone()
        else:
            cursor = connection.execute(
                """
                INSERT INTO incidents (
                    incident_id, service, incident_type,
                    severity, message, status, opened_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?)
                """,
                (
                    incident["incident_id"],
                    incident["service"],
                    incident["type"],
                    incident["severity"],
                    incident["message"],
                    now,
                    now,
                ),
            )

            row = connection.execute(
                "SELECT * FROM incidents WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()

        return dict(row)


def list_incidents(status: str | None = None) -> list[dict]:
    """Return incidents, optionally filtered by lifecycle status."""
    with get_connection() as connection:
        if status:
            rows = connection.execute(
                """
                SELECT * FROM incidents
                WHERE status = ?
                ORDER BY id DESC
                """,
                (status,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM incidents ORDER BY id DESC"
            ).fetchall()

        return [dict(row) for row in rows]


def resolve_open_incidents(service: str) -> int:
    """Resolve open incidents for a service after recovery is confirmed."""
    now = utc_now()

    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE incidents
            SET status = 'RESOLVED',
                updated_at = ?,
                resolved_at = ?
            WHERE service = ?
              AND status IN ('OPEN', 'REMEDIATING')
            """,
            (now, now, service),
        )

        return cursor.rowcount
