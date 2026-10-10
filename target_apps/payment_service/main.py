
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from uuid import uuid4
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="Kybernaut Target - Payment Service")

Instrumentator().instrument(app).expose(app, endpoint="/metrics")

payments = {}


class PaymentRequest(BaseModel):
    order_id: str
    amount: float


@app.get("/health")
def health():
    return {"status": "healthy", "service": "payment-service"}


@app.post("/payments")
def create_payment(payment: PaymentRequest):
    if payment.amount <= 0:
        raise HTTPException(
            status_code=400,
            detail="Amount must be greater than zero",
        )

    payment_id = str(uuid4())
    new_payment = {
        "payment_id": payment_id,
        "order_id": payment.order_id,
        "amount": payment.amount,
        "status": "processed",
    }
    payments[payment_id] = new_payment
    return new_payment


@app.get("/payments")
def list_payments():
    return list(payments.values())
