
from datetime import timedelta

import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import app.database as database
from app.database import get_connection
from app.services.remediation_repository import (
    MAX_ATTEMPTS_PER_INCIDENT,
    reserve_attempt,
    update_attempt_result,
    utc_now,
)


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """Give every test its own temporary SQLite database."""
    test_db = tmp_path / "test_incidents.db"
    monkeypatch.setattr(database, "DATABASE_PATH", test_db)
    database.initialize_database()


def test_first_reservation_is_allowed():
    result = reserve_attempt(
        audit_id="test-first-reservation-audit",
        incident_id="test-first-reservation",
        service="test-service",
    )

    assert result["allowed"] is True
    assert result["attempt_count"] == 1


def test_immediate_second_reservation_is_blocked():
    first = reserve_attempt(
        audit_id="test-cooldown-audit-1",
        incident_id="test-cooldown",
        service="test-service",
    )
    second = reserve_attempt(
        audit_id="test-cooldown-audit-2",
        incident_id="test-cooldown",
        service="test-service",
    )

    assert first["allowed"] is True
    assert second["allowed"] is False
    assert "Cooldown active" in second["reason"]


def test_retry_limit_blocks_fourth_attempt():
    service = "test-service"
    incident_id = "test-retry-limit"
    now = utc_now()

    with get_connection() as connection:
        for index, seconds_ago in enumerate((140, 70, 10), start=1):
            connection.execute(
                """
                INSERT INTO remediation_attempts (
                    audit_id, incident_id, service,
                    attempted_at, status, message
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    f"test-retry-seed-{index}",
                    incident_id,
                    service,
                    (now - timedelta(seconds=seconds_ago)).isoformat(),
                    "attempted",
                    "Automated test record",
                ),
            )

    result = reserve_attempt(
        audit_id="test-retry-fourth",
        incident_id=incident_id,
        service=service,
    )

    assert result["allowed"] is False
    assert result["attempt_count"] == MAX_ATTEMPTS_PER_INCIDENT
    assert "Maximum remediation attempts" in result["reason"]


def test_outcome_update_changes_database_record():
    service = "test-service"
    incident_id = "test-outcome-update"
    audit_id = "test-outcome-update-audit"

    reservation = reserve_attempt(
        audit_id=audit_id,
        incident_id=incident_id,
        service=service,
    )
    assert reservation["allowed"] is True

    updated = update_attempt_result(
        audit_id=audit_id,
        status="verified",
        message="Automated outcome test",
    )
    assert updated is True

    with get_connection() as connection:
        row = connection.execute(
            """
            SELECT status, message
            FROM remediation_attempts
            WHERE audit_id = ?
            """,
            (audit_id,),
        ).fetchone()

    assert row is not None
    assert row["status"] == "verified"
    assert row["message"] == "Automated outcome test"


def test_unknown_outcome_audit_id_returns_false():
    result = update_attempt_result(
        audit_id="nonexistent-test-audit",
        status="failed",
        message="This ID does not exist",
    )

    assert result is False


def test_concurrent_reservations_respect_cooldown():
    service = "test-service"
    incident_id = "test-concurrent-reservations"
    barrier = Barrier(2)

    def attempt(audit_id):
        barrier.wait()
        return reserve_attempt(
            audit_id=audit_id,
            incident_id=incident_id,
            service=service,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(attempt, "concurrent-audit-1"),
            executor.submit(attempt, "concurrent-audit-2"),
        ]
        results = [future.result() for future in futures]

    allowed = [result for result in results if result["allowed"]]
    blocked = [result for result in results if not result["allowed"]]

    assert len(allowed) == 1
    assert len(blocked) == 1
    assert "Cooldown active" in blocked[0]["reason"]
