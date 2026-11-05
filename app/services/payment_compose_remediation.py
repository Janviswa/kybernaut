"""Narrowly scoped local-Compose migration for payment-service history."""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import time
from pathlib import Path
from threading import Lock
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

from scripts.migrate_payment_history import _validate_records, _validate_source_url

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = PROJECT_ROOT / "docker-compose.app-services.yml"
PAYMENT_SERVICE_URL = "http://127.0.0.1:8002"
PAYMENT_SOURCE_URL = f"{PAYMENT_SERVICE_URL}/payments"
PAYMENT_DATABASE_NAME = "kybernaut_payments"
PAYMENT_DATABASE_USER = "kybernaut_payment"
_EXECUTION_LOCK = Lock()


class PaymentComposeRemediationError(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


def execution_unavailable_reason() -> str | None:
    if os.getenv("KYBERNAUT_ENVIRONMENT", "").strip().lower() != "local-demo":
        return "Set KYBERNAUT_ENVIRONMENT=local-demo for this local-only action."
    if shutil.which("docker") is None:
        return "The Docker CLI is not available to the API process."
    return None


def _sql_text(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _payment_sql(payments: list[dict]) -> str:
    statements = [
        "BEGIN;",
        """
        CREATE TABLE IF NOT EXISTS payments (
            sequence_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            payment_id TEXT NOT NULL UNIQUE,
            order_id TEXT NOT NULL,
            amount DOUBLE PRECISION NOT NULL CHECK (amount > 0),
            status TEXT NOT NULL
        );
        """,
    ]
    values = [
        "("
        + ", ".join((
            _sql_text(payment["payment_id"]),
            _sql_text(payment["order_id"]),
            repr(float(payment["amount"])),
            _sql_text(payment["status"]),
        ))
        + ")"
        for payment in payments
    ]
    if values:
        value_rows = ",\n".join(values)
        statements.extend((
            f"""
            DO $kybernaut$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM payments AS stored
                    JOIN (VALUES {value_rows})
                        AS imported(payment_id, order_id, amount, status)
                        USING (payment_id)
                    WHERE stored.order_id IS DISTINCT FROM imported.order_id
                       OR stored.amount IS DISTINCT FROM imported.amount
                       OR stored.status IS DISTINCT FROM imported.status
                ) THEN
                    RAISE EXCEPTION
                        'Existing payment identifier conflicts with source record';
                END IF;
            END;
            $kybernaut$;
            """,
            f"""
            INSERT INTO payments (payment_id, order_id, amount, status)
            VALUES {value_rows}
            ON CONFLICT (payment_id) DO NOTHING;
            """,
        ))
    statements.extend((
        "SELECT 'KYBERNAUT_PAYMENT_COUNT=' || COUNT(*)::text FROM payments;",
        "COMMIT;",
    ))
    return "\n".join(statements)


def _run(
    docker: str,
    args: list[str],
    *,
    stage: str,
    timeout: int,
    input_text: str | None = None,
) -> str:
    try:
        result = subprocess.run(
            [docker, "compose", "-f", str(COMPOSE_FILE), *args],
            cwd=PROJECT_ROOT,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PaymentComposeRemediationError(
            stage, f"Payment remediation stopped during {stage}."
        ) from exc
    if result.returncode:
        raise PaymentComposeRemediationError(
            stage, f"Payment remediation stopped during {stage}."
        )
    return result.stdout


def _fetch_current_payments() -> list[dict]:
    _validate_source_url(PAYMENT_SOURCE_URL)
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(
            Request(PAYMENT_SOURCE_URL, headers={"Accept": "application/json"}),
            timeout=5,
        ) as response:
            return _validate_records(json.load(response))
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        raise PaymentComposeRemediationError(
            "read_payment_history",
            "Could not read and validate the current payment history.",
        ) from exc


def _wait_for_database(docker: str) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            _run(
                docker,
                [
                    "exec", "-T", "payment-postgres", "pg_isready",
                    "-U", PAYMENT_DATABASE_USER, "-d", PAYMENT_DATABASE_NAME,
                ],
                stage="wait_for_payment_database",
                timeout=8,
            )
            return
        except PaymentComposeRemediationError:
            time.sleep(1)
    raise PaymentComposeRemediationError(
        "wait_for_payment_database",
        "Payment PostgreSQL did not become ready in time.",
    )


def _read_json(docker: str, path: str, stage: str) -> object:
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(f"{PAYMENT_SERVICE_URL}{path}", timeout=5) as response:
            return json.load(response)
    except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
        raise PaymentComposeRemediationError(
            stage, f"Payment-service verification failed during {stage}."
        ) from exc


def _wait_for_payment_health(docker: str) -> None:
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            health = _read_json(docker, "/health", "verify_payment_health")
        except PaymentComposeRemediationError:
            health = None
        if (
            isinstance(health, dict)
            and health.get("status") == "healthy"
            and health.get("history_storage") == "postgresql"
        ):
            return
        time.sleep(1)
    raise PaymentComposeRemediationError(
        "verify_payment_health",
        "The replacement did not become healthy with PostgreSQL-backed storage.",
    )


def _execute_payment_history_remediation() -> dict:
    """Migrate the old service's records, replace only it, and verify storage."""
    reason = execution_unavailable_reason()
    if reason:
        raise PaymentComposeRemediationError("preflight", reason)
    docker = shutil.which("docker")
    if docker is None:
        raise PaymentComposeRemediationError(
            "preflight", "The Docker CLI is not available to the API process."
        )

    _run(
        docker,
        ["up", "-d", "payment-postgres"],
        stage="start_payment_database",
        timeout=120,
    )
    _wait_for_database(docker)

    payments = _fetch_current_payments()
    if any(
        not math.isfinite(float(payment["amount"]))
        for payment in payments
    ):
        raise PaymentComposeRemediationError(
            "read_payment_history",
            "The payment history contains a non-finite amount.",
        )
    sql_output = _run(
        docker,
        [
            "exec", "-T", "payment-postgres", "psql",
            "-v", "ON_ERROR_STOP=1", "-A", "-t", "-q",
            "-U", PAYMENT_DATABASE_USER, "-d", PAYMENT_DATABASE_NAME,
        ],
        stage="migrate_payment_history",
        timeout=120,
        input_text=_payment_sql(payments),
    )
    count_line = next(
        (
            line for line in sql_output.splitlines()
            if line.startswith("KYBERNAUT_PAYMENT_COUNT=")
        ),
        None,
    )
    if count_line is None:
        raise PaymentComposeRemediationError(
            "migrate_payment_history",
            "The database did not confirm the imported payment count.",
        )
    try:
        database_count = int(count_line.partition("=")[2])
    except ValueError as exc:
        raise PaymentComposeRemediationError(
            "migrate_payment_history",
            "The database returned an invalid payment count.",
        ) from exc
    if database_count < len(payments):
        raise PaymentComposeRemediationError(
            "migrate_payment_history",
            "The database payment count is lower than the source history.",
        )

    _run(
        docker,
        ["build", "payment-service"],
        stage="build_payment_service",
        timeout=600,
    )
    _run(
        docker,
        ["up", "-d", "--no-deps", "payment-service"],
        stage="replace_payment_service",
        timeout=180,
    )
    _wait_for_payment_health(docker)
    history = _read_json(docker, "/payments", "verify_payment_history")
    if not isinstance(history, list) or len(history) != database_count:
        raise PaymentComposeRemediationError(
            "verify_payment_history",
            "The replacement did not return the complete migrated payment history.",
        )

    return {
        "source_payment_count": len(payments),
        "database_payment_count": database_count,
        "health_verified": True,
        "history_verified": True,
    }


def execute_payment_history_remediation() -> dict:
    if not _EXECUTION_LOCK.acquire(blocking=False):
        raise PaymentComposeRemediationError(
            "already_running",
            "Another payment remediation is already running.",
        )
    try:
        return _execute_payment_history_remediation()
    finally:
        _EXECUTION_LOCK.release()
