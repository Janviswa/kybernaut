#!/usr/bin/env python3
import argparse
import getpass
import secrets
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from app.core.operator_accounts import provision_operator_password


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Provision an Argon2id password verifier for a configured operator."
    )
    parser.add_argument(
        "--operator-id",
        required=True,
        help="Identity of an operator already listed in the trusted operator configuration.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Replace the existing password verifier for this operator.",
    )
    arguments = parser.parse_args()

    password = getpass.getpass("New operator password (input hidden): ")
    confirmation = getpass.getpass("Confirm operator password: ")
    if not secrets.compare_digest(password, confirmation):
        sys.stderr.write("Error: passwords did not match.\n")
        return 2
    try:
        provision_operator_password(
            arguments.operator_id.strip(),
            password,
            replace_existing=arguments.reset,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 2

    sys.stdout.write(
        f"Password verifier provisioned for {arguments.operator_id.strip()}.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
