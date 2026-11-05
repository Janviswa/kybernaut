
import math
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator
from uuid import uuid4
from prometheus_fastapi_instrumentator import Instrumentator
try:
    from payment_store import PaymentStoreError
    from payment_store import (
        add_payment, check_database, initialize,
        list_payments as load_payments,
    )
except ModuleNotFoundError as exc:
    if exc.name != "payment_store":
        raise
    from target_apps.payment_service.payment_store import PaymentStoreError
    from target_apps.payment_service.payment_store import (
        add_payment, check_database, initialize,
        list_payments as load_payments,
    )
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

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        initialize()
    except PaymentStoreError as exc:
        logger.error(
            "Payment history initialization failed (exception_type=%s)",
            type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__,
        )
        raise
    yield


app = FastAPI(title="Kybernaut Target - Payment Service", lifespan=lifespan)


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
    service="payment-service",
    scenarios=scenarios_for_service("payment-service"),
)

class PaymentRequest(BaseModel):
    order_id: str
    amount: float

    @field_validator("amount", mode="before")
    @classmethod
    def reject_boolean_amount(cls, value):
        if isinstance(value, bool):
            raise ValueError("amount must be numeric")
        return value


@app.get("/health")
def health():
    try:
        check_database()
    except PaymentStoreError as exc:
        logger.error(
            "Payment history health check failed (exception_type=%s)",
            type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__,
        )
        raise HTTPException(
            status_code=503, detail="Payment history storage is unavailable"
        ) from exc
    return {
        "status": "healthy",
        "service": "payment-service",
        "history_storage": "postgresql",
    }


@app.post("/payments")
def create_payment(payment: PaymentRequest):
    if not math.isfinite(payment.amount) or payment.amount <= 0:
        raise HTTPException(
            status_code=400,
            detail="Amount must be finite and greater than zero",
        )

    payment_id = str(uuid4())
    new_payment = {
        "payment_id": payment_id,
        "order_id": payment.order_id,
        "amount": payment.amount,
        "status": "processed",
    }
    try:
        add_payment(
            new_payment["payment_id"],
            new_payment["order_id"],
            new_payment["amount"],
            new_payment["status"],
        )
    except PaymentStoreError as exc:
        logger.error(
            "Payment history write failed (exception_type=%s)",
            type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__,
        )
        raise HTTPException(
            status_code=503, detail="Payment could not be persisted"
        ) from exc
    return new_payment


@app.get("/payments")
def list_payments():
    try:
        return load_payments()
    except PaymentStoreError as exc:
        logger.error(
            "Payment history read failed (exception_type=%s)",
            type(exc.__cause__).__name__ if exc.__cause__ else type(exc).__name__,
        )
        raise HTTPException(
            status_code=503, detail="Payment history is unavailable"
        ) from exc
