"""PostgreSQL persistence for payment records."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator


class PaymentStoreError(RuntimeError):
    """Payment history could not be read from or written to PostgreSQL."""


@contextmanager
def _connection() -> Iterator[object]:
    password = os.getenv("PAYMENT_DB_PASSWORD")
    if not password:
        raise PaymentStoreError("PAYMENT_DB_PASSWORD is required")
    try:
        import psycopg
    except ImportError as exc:
        raise PaymentStoreError(
            "The PostgreSQL driver is not installed"
        ) from exc

    try:
        with psycopg.connect(
            host=os.getenv("PAYMENT_DB_HOST", "localhost"),
            port=int(os.getenv("PAYMENT_DB_PORT", "5432")),
            dbname=os.getenv("PAYMENT_DB_NAME", "kybernaut_payments"),
            user=os.getenv("PAYMENT_DB_USER", "kybernaut_payment"),
            password=password,
            connect_timeout=3,
        ) as connection:
            yield connection
    except psycopg.Error as exc:
        raise PaymentStoreError(
            "Payment history PostgreSQL operation failed"
        ) from exc


def initialize() -> None:
    with _connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                sequence_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                payment_id TEXT NOT NULL UNIQUE,
                order_id TEXT NOT NULL,
                amount DOUBLE PRECISION NOT NULL CHECK (amount > 0),
                status TEXT NOT NULL
            )
            """
        )


def check_database() -> None:
    with _connection() as connection:
        row = connection.execute("SELECT 1").fetchone()
    if row != (1,):
        raise PaymentStoreError("Payment history database health check failed")


def add_payment(
    payment_id: str, order_id: str, amount: float, status: str
) -> None:
    with _connection() as connection:
        connection.execute(
            """
            INSERT INTO payments (payment_id, order_id, amount, status)
            VALUES (%s, %s, %s, %s)
            """,
            (payment_id, order_id, amount, status),
        )


def list_payments() -> list[dict]:
    with _connection() as connection:
        rows = connection.execute(
            """
            SELECT payment_id, order_id, amount, status
            FROM payments
            ORDER BY sequence_id
            """
        ).fetchall()
    return [
        {
            "payment_id": row[0],
            "order_id": row[1],
            "amount": float(row[2]),
            "status": row[3],
        }
        for row in rows
    ]


def import_payments(payments: list[dict]) -> int:
    imported = 0
    with _connection() as connection:
        for payment in payments:
            cursor = connection.execute(
                """
                INSERT INTO payments (payment_id, order_id, amount, status)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (payment_id) DO NOTHING
                """,
                (
                    payment["payment_id"],
                    payment["order_id"],
                    payment["amount"],
                    payment["status"],
                ),
            )
            imported += max(cursor.rowcount, 0)
    return imported
