import math

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from target_apps.common.local_failure_simulation import (
    install_local_failure_simulation, scenarios_for_service,
)
from target_apps.inventory_service import main as inventory_service
from target_apps.order_service import main as order_service
from target_apps.payment_service import main as payment_service
from target_apps.user_service import main as user_service


@pytest.fixture(autouse=True)
def clear_service_state(monkeypatch):
    order_service.orders.clear()
    user_service.users.clear()
    inventory_service.inventory.clear()
    inventory_service.inventory.update({
        "Laptop": 10,
        "Mouse": 25,
        "Keyboard": 15,
    })
    payment_rows = {}

    def add_payment(payment_id, order_id, amount, status):
        payment_rows[payment_id] = {
            "payment_id": payment_id,
            "order_id": order_id,
            "amount": amount,
            "status": status,
        }

    monkeypatch.setattr(payment_service, "initialize", lambda: None)
    monkeypatch.setattr(payment_service, "check_database", lambda: None)
    monkeypatch.setattr(payment_service, "add_payment", add_payment)
    monkeypatch.setattr(
        payment_service, "load_payments",
        lambda: list(payment_rows.values()),
    )
    yield payment_rows
    order_service.orders.clear()
    user_service.users.clear()
    inventory_service.inventory.clear()
    inventory_service.inventory.update({
        "Laptop": 10,
        "Mouse": 25,
        "Keyboard": 15,
    })


@pytest.mark.parametrize(
    ("app", "service"),
    [
        (order_service.app, "order-service"),
        (payment_service.app, "payment-service"),
        (user_service.app, "user-service"),
        (inventory_service.app, "inventory-service"),
    ],
)
def test_each_service_exposes_its_health_contract(app, service):
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    expected = {"status": "healthy", "service": service}
    if service == "payment-service":
        expected["history_storage"] = "postgresql"
    assert response.json() == expected


def test_order_contract_and_boolean_quantity_validation():
    client = TestClient(order_service.app)
    created = client.post(
        "/orders", json={"product": "Laptop", "quantity": 2}
    )

    assert created.status_code == 200
    order = created.json()
    assert order == {
        "order_id": order["order_id"],
        "product": "Laptop",
        "quantity": 2,
        "status": "created",
    }
    assert client.get("/orders").json() == [order]

    invalid = client.post(
        "/orders", json={"product": "Laptop", "quantity": True}
    )
    assert invalid.status_code == 422
    assert client.get("/orders").json() == [order]

    non_positive = client.post(
        "/orders", json={"product": "Laptop", "quantity": 0}
    )
    assert non_positive.status_code == 400
    assert client.get("/orders").json() == [order]


@pytest.mark.parametrize("amount", [math.nan, math.inf, -math.inf])
def test_payment_rejects_non_finite_amounts_without_side_effects(amount):
    client = TestClient(payment_service.app)

    response = client.post(
        "/payments",
        content=f'{{"order_id":"order-1","amount":"{amount}"}}',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Amount must be finite and greater than zero"
    )
    assert payment_service.load_payments() == []


def test_payment_accepts_positive_finite_amount(clear_service_state):
    client = TestClient(payment_service.app)

    response = client.post(
        "/payments", json={"order_id": "order-1", "amount": 12.5}
    )

    assert response.status_code == 200
    assert response.json() == {
        "payment_id": response.json()["payment_id"],
        "order_id": "order-1",
        "amount": 12.5,
        "status": "processed",
    }
    assert clear_service_state == {
        response.json()["payment_id"]: response.json()
    }
    assert client.get("/payments").json() == [response.json()]


def test_payment_rejects_boolean_amount_without_side_effects():
    client = TestClient(payment_service.app)

    response = client.post(
        "/payments", json={"order_id": "order-1", "amount": True}
    )

    assert response.status_code == 422
    assert payment_service.load_payments() == []


def test_payment_service_initializes_postgres_before_startup(monkeypatch):
    initialized = []
    monkeypatch.setattr(
        payment_service, "initialize", lambda: initialized.append(True)
    )
    with TestClient(payment_service.app):
        pass
    assert initialized == [True]


def test_payment_health_reports_database_unavailability(monkeypatch):
    def unavailable():
        raise payment_service.PaymentStoreError("database down")

    monkeypatch.setattr(payment_service, "check_database", unavailable)
    response = TestClient(payment_service.app).get("/health")
    assert response.status_code == 503
    assert response.json() == {
        "detail": "Payment history storage is unavailable"
    }


def test_payment_write_failure_is_not_reported_as_success(monkeypatch):
    def unavailable(*_args):
        raise payment_service.PaymentStoreError("database down")

    monkeypatch.setattr(payment_service, "add_payment", unavailable)
    response = TestClient(payment_service.app).post(
        "/payments", json={"order_id": "order-1", "amount": 12.5}
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Payment could not be persisted"}


def test_user_contract_rejects_blank_required_fields_without_side_effects():
    client = TestClient(user_service.app)
    created = client.post(
        "/users", json={"name": "Ada", "email": "ada@example.test"}
    )

    assert created.status_code == 200
    assert created.json()["name"] == "Ada"
    assert created.json()["email"] == "ada@example.test"
    assert created.json()["user_id"] in user_service.users

    invalid = client.post(
        "/users", json={"name": "  ", "email": "ada@example.test"}
    )
    assert invalid.status_code == 400
    assert len(user_service.users) == 1


def test_inventory_contract_preserves_stock_on_invalid_reservations():
    client = TestClient(inventory_service.app)

    assert client.get("/inventory").json() == {
        "Laptop": 10,
        "Mouse": 25,
        "Keyboard": 15,
    }

    reserved = client.post(
        "/inventory/reserve",
        json={"product": "Laptop", "quantity": 3},
    )
    assert reserved.status_code == 200
    assert reserved.json() == {
        "product": "Laptop",
        "reserved_quantity": 3,
        "remaining_stock": 7,
        "status": "reserved",
    }

    for payload, expected_status in (
        ({"product": "Laptop", "quantity": True}, 422),
        ({"product": "Unknown", "quantity": 1}, 404),
        ({"product": "Laptop", "quantity": 8}, 409),
        ({"product": "Laptop", "quantity": 0}, 400),
    ):
        response = client.post("/inventory/reserve", json=payload)
        assert response.status_code == expected_status
        assert inventory_service.inventory["Laptop"] == 7


@pytest.mark.parametrize(
    ("service", "scenario", "expected_status", "health_status"),
    [
        ("order-service", "high_latency", 200, 200),
        ("payment-service", "http_5xx", 503, 503),
        ("user-service", "service_unavailable", 503, 503),
        ("inventory-service", "request_failure", 500, 500),
    ],
)
def test_local_failure_simulation_is_token_gated_and_clearable(
    monkeypatch, service, scenario, expected_status, health_status,
):
    monkeypatch.setenv("KYBERNAUT_ENVIRONMENT", "local-demo")
    monkeypatch.setenv("KYBERNAUT_FAILURE_SIMULATION_TOKEN", "offline-test-token")
    app = FastAPI()
    install_local_failure_simulation(
        app,
        service=service,
        scenarios={scenario: "offline test scenario"},
    )

    @app.get("/health")
    def health():
        return {"status": "healthy"}

    @app.get("/business")
    def business():
        return {"status": "ok"}

    @app.get("/metrics")
    def metrics():
        return "metrics"

    client = TestClient(app)
    control = "/__kybernaut/simulation"
    payload = {
        "action": "start",
        "scenario": scenario,
        "duration_seconds": 30,
    }

    denied = client.post(control, json=payload)
    assert denied.status_code == 403

    started = client.post(
        control,
        json=payload,
        headers={"X-Kybernaut-Simulation-Token": "offline-test-token"},
    )
    assert started.status_code == 200
    assert started.json()["active"] is True
    assert client.get("/business").status_code == expected_status
    assert client.get("/health").status_code == health_status
    assert client.get("/metrics").status_code == expected_status

    cleared = client.post(
        control,
        json={"action": "clear"},
        headers={"X-Kybernaut-Simulation-Token": "offline-test-token"},
    )
    assert cleared.status_code == 200
    assert cleared.json()["active"] is False
    assert client.get("/business").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/metrics").status_code == 200


def test_every_target_service_accepts_every_rotating_failure_type(monkeypatch):
    monkeypatch.setenv("KYBERNAUT_ENVIRONMENT", "local-demo")
    monkeypatch.setenv("KYBERNAUT_FAILURE_SIMULATION_TOKEN", "offline-test-token")
    services = (
        ("order-service", order_service.app),
        ("payment-service", payment_service.app),
        ("user-service", user_service.app),
        ("inventory-service", inventory_service.app),
    )
    headers = {"X-Kybernaut-Simulation-Token": "offline-test-token"}

    for service, app in services:
        client = TestClient(app)
        for scenario in scenarios_for_service(service):
            started = client.post(
                "/__kybernaut/simulation",
                headers=headers,
                json={
                    "action": "start",
                    "scenario": scenario,
                    "duration_seconds": 30,
                },
            )
            assert started.status_code == 200
            assert started.json()["scenario"] == scenario

            cleared = client.post(
                "/__kybernaut/simulation",
                headers=headers,
                json={"action": "clear", "duration_seconds": 30},
            )
            assert cleared.status_code == 200
            assert cleared.json()["active"] is False
