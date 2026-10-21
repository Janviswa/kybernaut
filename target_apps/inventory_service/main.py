
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from prometheus_fastapi_instrumentator import Instrumentator

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

inventory = {
    "Laptop": 10,
    "Mouse": 25,
    "Keyboard": 15,
}


class StockRequest(BaseModel):
    product: str
    quantity: int


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
