import json
import os
import secrets
import stat
import tempfile
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher, Type
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)

from app.core.jwt_auth import AuthenticatedOperator


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALLOWED_PERMISSIONS = {"owner", "read", "remediation"}
PASSWORD_HASHER = PasswordHasher(type=Type.ID)
_DUMMY_PASSWORD_HASH = PASSWORD_HASHER.hash(secrets.token_urlsafe(32))


class AuthenticationConfigurationError(RuntimeError):
    pass


def operator_config_path() -> Path:
    return Path(os.getenv(
        "KYBERNAUT_OPERATORS_FILE",
        str(PROJECT_ROOT / "config" / "operators.json"),
    ))


def password_hash_path() -> Path:
    return Path(os.getenv(
        "KYBERNAUT_OPERATOR_PASSWORD_FILE",
        str(PROJECT_ROOT / "config" / "operator_password_hashes.json"),
    ))


def load_operator_configuration() -> dict[str, Any]:
    path = operator_config_path()
    try:
        with path.open(encoding="utf-8") as file:
            configuration = json.load(file)
    except (OSError, json.JSONDecodeError):
        raise AuthenticationConfigurationError(
            "Operator configuration is unavailable or invalid"
        ) from None

    operators = configuration.get("operators") if isinstance(
        configuration, dict
    ) else None
    if not isinstance(operators, dict):
        raise AuthenticationConfigurationError(
            "Operator configuration must contain an operators object"
        )
    for operator_id, operator in operators.items():
        permissions = operator.get("permissions") if isinstance(
            operator, dict
        ) else None
        if (
            not isinstance(operator_id, str)
            or not operator_id.strip()
            or len(operator_id) > 128
            or not isinstance(permissions, list)
            or not permissions
            or not all(
                isinstance(permission, str)
                and permission in ALLOWED_PERMISSIONS
                for permission in permissions
            )
        ):
            raise AuthenticationConfigurationError(
                "Operator configuration contains an invalid account"
            )
    return operators


def load_password_hashes() -> dict[str, str]:
    path = password_hash_path()
    try:
        if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise AuthenticationConfigurationError(
                "Password verifier file permissions are too broad"
            )
        with path.open(encoding="utf-8") as file:
            configuration = json.load(file)
    except AuthenticationConfigurationError:
        raise
    except (OSError, json.JSONDecodeError):
        raise AuthenticationConfigurationError(
            "Password verifier configuration is unavailable or invalid"
        ) from None

    hashes = configuration.get("password_hashes") if isinstance(
        configuration, dict
    ) else None
    if (
        not isinstance(hashes, dict)
        or not all(
            isinstance(operator_id, str)
            and isinstance(password_hash, str)
            and password_hash.startswith("$argon2id$")
            for operator_id, password_hash in hashes.items()
        )
    ):
        raise AuthenticationConfigurationError(
            "Password verifier configuration contains an invalid account"
        )
    return hashes


def authenticate_operator(
    username: str,
    password: str,
) -> AuthenticatedOperator | None:
    operators = load_operator_configuration()
    password_hashes = load_password_hashes()
    operator = operators.get(username)
    password_hash = password_hashes.get(username)
    if operator is None or password_hash is None:
        try:
            PASSWORD_HASHER.verify(_DUMMY_PASSWORD_HASH, password)
        except VerifyMismatchError:
            pass
        return None

    try:
        PASSWORD_HASHER.verify(password_hash, password)
    except VerifyMismatchError:
        return None
    except (InvalidHashError, VerificationError):
        raise AuthenticationConfigurationError(
            "Password verifier configuration contains an invalid hash"
        ) from None

    permissions = tuple(sorted(set(operator["permissions"])))
    return AuthenticatedOperator(
        operator_id=username,
        permissions=permissions,
    )


def provision_operator_password(
    operator_id: str,
    password: str,
    *,
    replace_existing: bool = False,
) -> None:
    operators = load_operator_configuration()
    if operator_id not in operators:
        raise ValueError("Operator is not present in the trusted operator configuration")
    if len(password) < 12 or len(password) > 1024:
        raise ValueError("Password must contain between 12 and 1024 characters")

    path = password_hash_path()
    if path.exists():
        existing_hashes = load_password_hashes()
    else:
        existing_hashes = {}
    if operator_id in existing_hashes and not replace_existing:
        raise ValueError("Operator already has a password verifier; use --reset")

    updated_hashes = {**existing_hashes, operator_id: PASSWORD_HASHER.hash(password)}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            if os.name == "posix":
                os.fchmod(file.fileno(), 0o600)
            json.dump(
                {"password_hashes": updated_hashes},
                file,
                indent=2,
                sort_keys=True,
            )
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
