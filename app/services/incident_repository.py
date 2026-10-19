from datetime import datetime, timezone

from app.database import get_connection


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record_incident(incident: dict) -> dict:
    """Create an incident or refresh its matching active record."""
    now = utc_now()
    fingerprint = incident.get("fingerprint")

    with get_connection() as connection:
        if fingerprint:
            existing = connection.execute(
                """
                SELECT *
                FROM incidents
                WHERE fingerprint = ?
                  AND status IN ('OPEN', 'REMEDIATING')
                ORDER BY id DESC
                LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
        else:
            # Preserve the original behavior for incidents without fingerprints.
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
                    last_seen_at = ?,
                    severity = ?,
                    message = ?,
                    fingerprint = COALESCE(?, fingerprint)
                WHERE id = ?
                """,
                (
                    now,
                    now,
                    incident["severity"],
                    incident["message"],
                    fingerprint,
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
                    incident_id,
                    service,
                    incident_type,
                    severity,
                    message,
                    status,
                    opened_at,
                    updated_at,
                    fingerprint,
                    last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?)
                """,
                (
                    incident["incident_id"],
                    incident["service"],
                    incident["type"],
                    incident["severity"],
                    incident["message"],
                    now,
                    now,
                    fingerprint,
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
                SELECT *
                FROM incidents
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


def resolve_open_incidents(
    service: str,
    incident_type: str | None = None,
) -> int:
    """Resolve active incidents for a service, optionally by type."""
    now = utc_now()

    with get_connection() as connection:
        if incident_type is None:
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
        else:
            cursor = connection.execute(
                """
                UPDATE incidents
                SET status = 'RESOLVED',
                    updated_at = ?,
                    resolved_at = ?
                WHERE service = ?
                  AND incident_type = ?
                  AND status IN ('OPEN', 'REMEDIATING')
                """,
                (now, now, service, incident_type),
            )

        return cursor.rowcount



def resolve_incident_fingerprint(fingerprint: str) -> int:
    """Resolve one active incident by its exact fingerprint."""
    now = utc_now()

    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE incidents
            SET status = 'RESOLVED',
                updated_at = ?,
                resolved_at = ?
            WHERE fingerprint = ?
              AND status IN ('OPEN', 'REMEDIATING')
            """,
            (now, now, fingerprint),
        )
        return cursor.rowcount
