import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.core.jwt_auth import (
    ACCESS_TOKEN_LIFETIME_MINUTES,
    issue_operator_token,
)
from app.core.operator_accounts import (
    AuthenticationConfigurationError,
    authenticate_operator,
)


logger = logging.getLogger(__name__)
router = APIRouter(tags=["authentication"])


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: StrictStr = Field(min_length=1, max_length=128)
    password: StrictStr = Field(min_length=1, max_length=1024)


@router.post("/auth/login")
def login(body: LoginRequest):
    try:
        operator = authenticate_operator(body.username, body.password)
    except AuthenticationConfigurationError as exc:
        logger.error(
            "Operator authentication configuration unavailable "
            "(error_type=%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is temporarily unavailable.",
        ) from None

    if operator is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid username or password.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        access_token = issue_operator_token(operator)
    except RuntimeError as exc:
        logger.error(
            "Operator token issuance is unavailable (error_type=%s)",
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is temporarily unavailable.",
        ) from None

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_LIFETIME_MINUTES * 60,
        "operator_id": operator.operator_id,
        "permissions": list(operator.permissions),
    }
