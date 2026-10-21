
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from uuid import uuid4
from prometheus_fastapi_instrumentator import Instrumentator

app = FastAPI(title="Kybernaut Target - User Service")


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

users = {}


class UserRequest(BaseModel):
    name: str
    email: str


@app.get("/health")
def health():
    return {"status": "healthy", "service": "user-service"}


@app.post("/users")
def create_user(user: UserRequest):
    if not user.name.strip() or not user.email.strip():
        raise HTTPException(
            status_code=400,
            detail="Name and email are required",
        )

    user_id = str(uuid4())
    new_user = {
        "user_id": user_id,
        "name": user.name,
        "email": user.email,
    }
    users[user_id] = new_user
    return new_user


@app.get("/users")
def list_users():
    return list(users.values())

@app.get("/test-error")
def test_error():
    raise HTTPException(
        status_code=500,
        detail="Intentional error for Kybernaut monitoring test",
    )
