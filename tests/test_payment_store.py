from contextlib import contextmanager

import pytest

from target_apps.payment_service import payment_store


class Cursor:
    def __init__(self, rows=(), rowcount=0):
        self._rows = list(rows)
        self.rowcount = rowcount

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class FakeConnection:
    def __init__(self):
        self.rows = []
        self.statements = []

    def execute(self, query, parameters=()):
        self.statements.append((query, parameters))
        if query.strip().startswith("INSERT INTO payments"):
            payment_id, order_id, amount, status = parameters
            if any(row[0] == payment_id for row in self.rows):
                return Cursor(rowcount=0)
            self.rows.append((payment_id, order_id, amount, status))
            return Cursor(rowcount=1)
        if query.strip().startswith("SELECT 1"):
            return Cursor(rows=[(1,)])
        if query.strip().startswith("SELECT payment_id"):
            return Cursor(rows=self.rows)
        return Cursor()


@pytest.fixture
def fake_connection(monkeypatch):
    connection = FakeConnection()

    @contextmanager
    def connect():
        yield connection

    monkeypatch.setattr(payment_store, "_connection", connect)
    return connection


def test_initialize_creates_durable_payment_table(fake_connection):
    payment_store.initialize()

    statement = fake_connection.statements[0][0]
    assert "CREATE TABLE IF NOT EXISTS payments" in statement
    assert "payment_id TEXT NOT NULL UNIQUE" in statement
    assert "GENERATED ALWAYS AS IDENTITY" in statement


def test_payment_records_round_trip_in_insert_order(fake_connection):
    payment_store.add_payment("pay-1", "order-1", 10.5, "processed")
    payment_store.add_payment("pay-2", "order-2", 7.25, "processed")

    assert payment_store.list_payments() == [
        {
            "payment_id": "pay-1",
            "order_id": "order-1",
            "amount": 10.5,
            "status": "processed",
        },
        {
            "payment_id": "pay-2",
            "order_id": "order-2",
            "amount": 7.25,
            "status": "processed",
        },
    ]
    assert any("%s" in query for query, _ in fake_connection.statements)


def test_import_is_idempotent_by_payment_id(fake_connection):
    records = [{
        "payment_id": "pay-1",
        "order_id": "order-1",
        "amount": 10.5,
        "status": "processed",
    }]

    assert payment_store.import_payments(records) == 1
    assert payment_store.import_payments(records) == 0
    assert payment_store.list_payments() == records


def test_database_password_is_required(monkeypatch):
    monkeypatch.delenv("PAYMENT_DB_PASSWORD", raising=False)

    with pytest.raises(payment_store.PaymentStoreError, match="required"):
        payment_store.initialize()


def test_health_check_rejects_unexpected_database_result(fake_connection):
    fake_connection.execute = lambda *_args: Cursor(rows=[(0,)])

    with pytest.raises(payment_store.PaymentStoreError, match="health check"):
        payment_store.check_database()
