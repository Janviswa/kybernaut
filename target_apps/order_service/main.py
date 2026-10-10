from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from uuid import uuid4
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="Kybernaut Target - Order Service")

Instrumentator().instrument(app).expose(app, endpoint="/metrics")

orders = {}

class OrderRequest(BaseModel):
    product: str
    quantity: int


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
