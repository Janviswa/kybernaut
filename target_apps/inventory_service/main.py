
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator
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

app = FastAPI(title="Kybernaut Target - Inventory Service")


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
    service="inventory-service",
    scenarios=scenarios_for_service("inventory-service"),
)

inventory = {
    "Laptop": 10,
    "Mouse": 25,
    "Keyboard": 15,
}


class StockRequest(BaseModel):
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
    return {"status": "healthy", "service": "inventory-service"}


@app.get("/inventory")
def get_inventory():
    return inventory


@app.post("/inventory/reserve")
def reserve_stock(request: StockRequest):
    if request.quantity <= 0:
        raise HTTPException(
            status_code=400,
            detail="Quantity must be greater than zero",
        )

    if request.product not in inventory:
        raise HTTPException(
            status_code=404,
            detail="Product not found",
        )

    if inventory[request.product] < request.quantity:
        raise HTTPException(
            status_code=409,
            detail="Insufficient stock",
        )

    inventory[request.product] -= request.quantity

    return {
        "product": request.product,
        "reserved_quantity": request.quantity,
        "remaining_stock": inventory[request.product],
        "status": "reserved",
    }
