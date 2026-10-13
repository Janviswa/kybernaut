
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPERATORS_FILE = PROJECT_ROOT / "config" / "operators.json"
ALLOWED_PERMISSIONS = {"read", "remediation"}


def main():
    load_dotenv(PROJECT_ROOT / ".env")

    secret = os.getenv("KYBERNAUT_JWT_SECRET")
    issuer = os.getenv("KYBERNAUT_JWT_ISSUER", "kybernaut")
    audience = os.getenv("KYBERNAUT_JWT_AUDIENCE", "kybernaut-api")

    if not secret or len(secret.encode("utf-8")) < 32:
        sys.exit(
            "Error: KYBERNAUT_JWT_SECRET must contain at least "
            "32 bytes. Configure a strong secret in .env."
        )

    parser = argparse.ArgumentParser(
        description="Issue a short-lived Kybernaut operator JWT."
    )
    parser.add_argument(
        "--operator-id",
        required=True,
        help="Identity of an operator listed in config/operators.json.",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=15,
        help="Token lifetime in minutes (default: 15; maximum: 60).",
    )
    args = parser.parse_args()

    operator_id = args.operator_id.strip()

    if not operator_id or len(operator_id) > 128:
        sys.exit("Error: operator ID must be between 1 and 128 characters.")

    if not 1 <= args.minutes <= 60:
        sys.exit("Error: token lifetime must be between 1 and 60 minutes.")

    try:
        with OPERATORS_FILE.open(encoding="utf-8") as file:
            config = json.load(file)
    except FileNotFoundError:
        sys.exit("Error: config/operators.json was not found.")
    except json.JSONDecodeError:
        sys.exit("Error: config/operators.json contains invalid JSON.")

    operators = config.get("operators")
    if not isinstance(operators, dict):
        sys.exit("Error: operator configuration must contain an operators object.")

    operator_config = operators.get(operator_id)
    if not isinstance(operator_config, dict):
        sys.exit("Error: operator is not authorized to receive a token.")

    permissions = operator_config.get("permissions")
    if (
        not isinstance(permissions, list)
        or not permissions
        or not all(
            isinstance(permission, str)
            and permission in ALLOWED_PERMISSIONS
            for permission in permissions
        )
    ):
        sys.exit("Error: operator has an invalid permissions configuration.")

    now = datetime.now(timezone.utc)
    payload = {
        "sub": operator_id,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + timedelta(minutes=args.minutes),
        "permissions": sorted(set(permissions)),
    }

    token = jwt.encode(payload, secret, algorithm="HS256")

    print("Operator token generated successfully.")
    print(f"Operator: {operator_id}")
    print(f"Permissions: {', '.join(payload['permissions'])}")
    print(f"Expires: {payload['exp'].isoformat()}")
    print("\nJWT (treat as a secret):")
    print(token)


if __name__ == "__main__":
    main()
