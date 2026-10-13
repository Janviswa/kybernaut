from datetime import datetime, timedelta, timezone

from app.database import get_connection


COOLDOWN_SECONDS = 60
MAX_ATTEMPTS_PER_INCIDENT = 3


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def record_attempt(
    audit_id: str,
    incident_id: str,
    service: str,
    status: str,
    message: str,
    operator_id: str | None = None,
) -> None:
    """Persist a remediation attempt, outcome, and operator attribution."""
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO remediation_attempts (
                audit_id, incident_id, service,
                attempted_at, status, message, operator_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                incident_id,
                service,
                utc_now().isoformat(),
                status,
                message,
                operator_id,
            ),
        )


def get_attempt_count(service: str, incident_id: str) -> int:
    """Count attempts within the rolling retry window."""
    cutoff = (
        utc_now()
        - timedelta(seconds=COOLDOWN_SECONDS * MAX_ATTEMPTS_PER_INCIDENT)
    ).isoformat()

    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT COUNT(*) AS total
            FROM remediation_attempts
            WHERE service = ?
              AND incident_id = ?
              AND attempted_at >= ?
            """,
            (service, incident_id, cutoff),
        ).fetchone()

    return int(row["total"])


def get_last_attempt_time(
    service: str,
    incident_id: str,
) -> datetime | None:
    """Return the most recent attempt timestamp."""
    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT attempted_at
            FROM remediation_attempts
            WHERE service = ?
              AND incident_id = ?
            ORDER BY attempted_at DESC, id DESC
            LIMIT 1
            """,
            (service, incident_id),
        ).fetchone()

    if row is None:
        return None

    return datetime.fromisoformat(row["attempted_at"])


def get_guardrail_status(service: str, incident_id: str) -> dict:
    """Check cooldown and retry limits without reserving an attempt."""
    now = utc_now()
    last_attempt = get_last_attempt_time(service, incident_id)
    attempt_count = get_attempt_count(service, incident_id)

    if attempt_count >= MAX_ATTEMPTS_PER_INCIDENT:
        return {
            "allowed": False,
            "reason": "Maximum remediation attempts reached for this incident.",
            "attempt_count": attempt_count,
        }

    if last_attempt is not None:
        elapsed = (now - last_attempt).total_seconds()

        if elapsed < COOLDOWN_SECONDS:
            return {
                "allowed": False,
                "reason": (
                    f"Cooldown active. Retry in "
                    f"{int(COOLDOWN_SECONDS - elapsed) + 1} seconds."
                ),
                "attempt_count": attempt_count,
            }

    return {
        "allowed": True,
        "reason": "Remediation guardrails passed.",
        "attempt_count": attempt_count,
    }


def reserve_attempt(
    audit_id: str,
    incident_id: str,
    service: str,
    operator_id: str | None = None,
) -> dict:
    """Atomically check guardrails and reserve an attributed attempt."""
    now = utc_now()

    cutoff = (
        now
        - timedelta(seconds=COOLDOWN_SECONDS * MAX_ATTEMPTS_PER_INCIDENT)
    ).isoformat()

    with get_connection() as connection:
        # Acquire the SQLite write lock before reading the attempt history.
        connection.execute("BEGIN IMMEDIATE")

        rows = connection.execute(
            """
            SELECT attempted_at
            FROM remediation_attempts
            WHERE service = ?
              AND incident_id = ?
              AND attempted_at >= ?
            ORDER BY attempted_at DESC, id DESC
            """,
            (service, incident_id, cutoff),
        ).fetchall()

        attempt_count = len(rows)

        if attempt_count >= MAX_ATTEMPTS_PER_INCIDENT:
            return {
                "allowed": False,
                "reason": "Maximum remediation attempts reached for this incident.",
                "attempt_count": attempt_count,
            }

        if rows:
            last_attempt = datetime.fromisoformat(rows[0]["attempted_at"])
            elapsed = (now - last_attempt).total_seconds()

            if elapsed < COOLDOWN_SECONDS:
                return {
                    "allowed": False,
                    "reason": (
                        f"Cooldown active. Retry in "
                        f"{int(COOLDOWN_SECONDS - elapsed) + 1} seconds."
                    ),
                    "attempt_count": attempt_count,
                }

        connection.execute(
            """
            INSERT INTO remediation_attempts (
                audit_id, incident_id, service,
                attempted_at, status, message, operator_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                incident_id,
                service,
                now.isoformat(),
                "attempted",
                "Approved remediation attempt reserved.",
                operator_id,
            ),
        )

        return {
            "allowed": True,
            "reason": "Remediation attempt reserved.",
            "attempt_count": attempt_count + 1,
        }


def update_attempt_result(
    audit_id: str,
    status: str,
    message: str,
) -> bool:
    """Update the outcome without overwriting operator attribution."""
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE remediation_attempts
            SET status = ?, message = ?
            WHERE audit_id = ?
            """,
            (status, message, audit_id),
        )
        return cursor.rowcount == 1
