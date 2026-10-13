import os
from dataclasses import dataclass

import jwt
from dotenv import load_dotenv
from fastapi import HTTPException, status


load_dotenv()

JWT_SECRET = os.getenv("KYBERNAUT_JWT_SECRET")
JWT_ISSUER = os.getenv("KYBERNAUT_JWT_ISSUER", "kybernaut")
JWT_AUDIENCE = os.getenv("KYBERNAUT_JWT_AUDIENCE", "kybernaut-api")

JWT_ALGORITHM = "HS256"


@dataclass(frozen=True)
class AuthenticatedOperator:
    operator_id: str
    permissions: tuple[str, ...]


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

    if "remediation" not in permissions:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Operator lacks remediation permission",
        )

    return AuthenticatedOperator(
        operator_id=operator_id.strip(),
        permissions=tuple(permissions),
    )
