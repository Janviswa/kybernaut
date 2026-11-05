import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt
from dotenv import load_dotenv
from fastapi import HTTPException, status


load_dotenv()

JWT_SECRET = os.getenv("KYBERNAUT_JWT_SECRET")
JWT_ISSUER = os.getenv("KYBERNAUT_JWT_ISSUER", "kybernaut")
JWT_AUDIENCE = os.getenv("KYBERNAUT_JWT_AUDIENCE", "kybernaut-api")

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_LIFETIME_MINUTES = 15


@dataclass(frozen=True)
class AuthenticatedOperator:
    operator_id: str
    permissions: tuple[str, ...]


def issue_operator_token(operator: AuthenticatedOperator) -> str:
    """Issue a short-lived access token using the existing JWT claims."""
    secret = os.getenv("KYBERNAUT_JWT_SECRET")
    issuer = os.getenv("KYBERNAUT_JWT_ISSUER", "kybernaut")
    audience = os.getenv("KYBERNAUT_JWT_AUDIENCE", "kybernaut-api")
    if not secret or len(secret.encode("utf-8")) < 32:
        raise RuntimeError("KYBERNAUT_JWT_SECRET is not securely configured")
    now = datetime.now(timezone.utc)
    payload = {
        "sub": operator.operator_id,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_LIFETIME_MINUTES),
        "permissions": list(operator.permissions),
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def verify_operator_token(token: str) -> AuthenticatedOperator:
    """Verify a signed JWT and return its authenticated operator claims."""
    if not JWT_SECRET:
        raise RuntimeError("KYBERNAUT_JWT_SECRET is not configured")

    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            issuer=JWT_ISSUER,
            audience=JWT_AUDIENCE,
            options={
                "require": ["exp", "iat", "sub", "iss", "aud"],
            },
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired operator token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None

    operator_id = payload.get("sub")
    permissions = payload.get("permissions", [])

    if not isinstance(operator_id, str) or not operator_id.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid operator identity",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if (
        not isinstance(permissions, list)
        or not all(isinstance(permission, str) for permission in permissions)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid operator permissions",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return AuthenticatedOperator(
        operator_id=operator_id.strip(),
        permissions=tuple(permissions),
    )

def require_permission(
    operator: AuthenticatedOperator,
    permission: str,
) -> None:
    """Raise 403 when an authenticated operator lacks a permission."""
    if permission not in operator.permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Operator lacks {permission} permission",
        )
