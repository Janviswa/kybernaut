
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import jwt
from dotenv import load_dotenv


def main():
    load_dotenv()

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
        help="Unique identity of the authorized operator.",
    )
    parser.add_argument(
        "--permission",
        action="append",
        choices=["read", "remediation"],
        default=[],
        help="Permission to grant. Can be supplied more than once.",
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

    if not args.permission:
        sys.exit("Error: specify at least one permission.")

    if not 1 <= args.minutes <= 60:
        sys.exit("Error: token lifetime must be between 1 and 60 minutes.")

    now = datetime.now(timezone.utc)

    payload = {
        "sub": operator_id,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + timedelta(minutes=args.minutes),
        "permissions": sorted(set(args.permission)),
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
