from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator
from uuid import uuid4
from prometheus_fastapi_instrumentator import Instrumentator
try:
    from common.local_failure_simulation import (
        install_local_failure_simulation, scenarios_for_service,
    )
except ModuleNotFoundError as exc:
    if exc.name != "common":
        raise
    from target_apps.common.local_failure_simulation import (
        install_local_failure_simulation, scenarios_for_service,
    )

app = FastAPI(title="Kybernaut Target - Order Service")


Instrumentator(
    should_round_latency_decimals=False,
).instrument(
    app,
    latency_lowr_buckets=(
        0.001,
        0.0025,
        0.005,
        0.0075,
        0.01,
        0.015,
        0.025,
        0.05,
        0.075,
        0.1,
        0.15,
        0.25,
        0.5,
        1.0,
    ),
).expose(app, endpoint="/metrics")

install_local_failure_simulation(
    app,
    service="order-service",
    scenarios=scenarios_for_service("order-service"),
)

orders = {}

class OrderRequest(BaseModel):
    product: str
    quantity: int

    @field_validator("quantity", mode="before")
    @classmethod
    def reject_boolean_quantity(cls, value):
        if isinstance(value, bool):
            raise ValueError("quantity must be an integer")
        return value


@app.get("/health")
def health():
    return {"status": "healthy", "service": "order-service"}


@app.post("/orders")
def create_order(order: OrderRequest):
    if order.quantity <= 0:
        raise HTTPException(
            status_code=400,
            detail="Quantity must be greater than zero",
        )

    order_id = str(uuid4())
    new_order = {
        "order_id": order_id,
        "product": order.product,
        "quantity": order.quantity,
        "status": "created",
    }
    orders[order_id] = new_order
    return new_order


@app.get("/orders")
def list_orders():
    return list(orders.values())
