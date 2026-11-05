import io
import json

import pytest

from scripts import migrate_payment_history as migration


def test_migration_accepts_only_loopback_payments_endpoint():
    migration._validate_source_url("http://127.0.0.1:8002/payments")
    migration._validate_source_url("http://localhost:8002/payments")

    with pytest.raises(ValueError, match="loopback"):
        migration._validate_source_url("http://example.test/payments")
    with pytest.raises(ValueError, match="loopback"):
        migration._validate_source_url("http://192.0.2.1/payments")


def test_migration_rejects_malformed_payment_records():
    with pytest.raises(ValueError, match="invalid payment record"):
        migration._validate_records([{
            "payment_id": "pay-1",
            "order_id": "order-1",
            "amount": True,
            "status": "processed",
        }])


def test_migration_imports_records_idempotently(monkeypatch):
    payments = [{
        "payment_id": "pay-1",
        "order_id": "order-1",
        "amount": 12.5,
        "status": "processed",
    }]
    calls = []

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "http://127.0.0.1:8002/payments"
            assert timeout == 5
            return io.BytesIO(json.dumps(payments).encode())

    monkeypatch.setattr(migration, "ProxyHandler", lambda _proxies: object())
    monkeypatch.setattr(migration, "build_opener", lambda _handler: Opener())
    monkeypatch.setattr(
        migration.payment_store, "initialize", lambda: calls.append("initialize")
    )
    monkeypatch.setattr(
        migration.payment_store, "import_payments",
        lambda records: calls.append(records) or 1,
    )

    assert migration.migrate("http://127.0.0.1:8002/payments") == 1
    assert calls == ["initialize", payments]
