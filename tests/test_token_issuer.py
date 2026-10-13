
import json
import os
import subprocess
import sys
from pathlib import Path

import jwt


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ISSUER_SCRIPT = PROJECT_ROOT / "scripts" / "issue_operator_token.py"

TEST_SECRET = "test-only-secret-with-at-least-32-bytes"
TEST_ISSUER = "kybernaut-test"
TEST_AUDIENCE = "kybernaut-api-test"


def run_issuer(*args, secret=TEST_SECRET, operators=None):
    env = os.environ.copy()
    env["KYBERNAUT_JWT_SECRET"] = secret
    env["KYBERNAUT_JWT_ISSUER"] = TEST_ISSUER
    env["KYBERNAUT_JWT_AUDIENCE"] = TEST_AUDIENCE

    config_dir = PROJECT_ROOT / "config"
    operators_file = config_dir / "operators.json"
    original_content = (
        operators_file.read_text(encoding="utf-8")
        if operators_file.exists()
        else None
    )

    test_operators = operators or {
        "test-operator": {"permissions": ["read", "remediation"]},
        "read-only": {"permissions": ["read"]},
    }

    try:
        operators_file.write_text(
            json.dumps({"operators": test_operators}),
            encoding="utf-8",
        )

        return subprocess.run(
            [sys.executable, str(ISSUER_SCRIPT), *args],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        if original_content is None:
            operators_file.unlink(missing_ok=True)
        else:
            operators_file.write_text(
                original_content,
                encoding="utf-8",
            )


def test_issuer_generates_valid_operator_token():
    result = run_issuer(
        "--operator-id", "test-operator",
        "--minutes", "5",
    )

    assert result.returncode == 0
    token = result.stdout.strip().splitlines()[-1]

    payload = jwt.decode(
        token,
        TEST_SECRET,
        algorithms=["HS256"],
        issuer=TEST_ISSUER,
        audience=TEST_AUDIENCE,
    )

    assert payload["sub"] == "test-operator"
    assert set(payload["permissions"]) == {"read", "remediation"}


def test_issuer_grants_only_allowlisted_permissions():
    result = run_issuer("--operator-id", "read-only")

    assert result.returncode == 0
    token = result.stdout.strip().splitlines()[-1]

    payload = jwt.decode(
        token,
        TEST_SECRET,
        algorithms=["HS256"],
        issuer=TEST_ISSUER,
        audience=TEST_AUDIENCE,
    )

    assert payload["permissions"] == ["read"]


def test_issuer_rejects_unknown_operator():
    result = run_issuer("--operator-id", "unauthorized-user")

    assert result.returncode != 0
    assert "operator is not authorized" in result.stderr


def test_issuer_rejects_weak_secret():
    result = run_issuer("--operator-id", "test-operator", secret="too-short")

    assert result.returncode != 0
    assert "must contain at least 32 bytes" in result.stderr


def test_issuer_rejects_zero_minute_lifetime():
    result = run_issuer(
        "--operator-id", "test-operator",
        "--minutes", "0",
    )

    assert result.returncode != 0
    assert "token lifetime must be between 1 and 60 minutes" in result.stderr


def test_issuer_rejects_lifetime_over_sixty_minutes():
    result = run_issuer(
        "--operator-id", "test-operator",
        "--minutes", "61",
    )

    assert result.returncode != 0
    assert "token lifetime must be between 1 and 60 minutes" in result.stderr


def test_issuer_rejects_missing_permissions():
    result = run_issuer(
        "--operator-id", "test-operator",
        operators={"test-operator": {"permissions": []}},
    )

    assert result.returncode != 0
    assert "invalid permissions configuration" in result.stderr


def test_issuer_rejects_operator_id_over_128_characters():
    result = run_issuer(
        "--operator-id", "a" * 129,
    )

    assert result.returncode != 0
    assert "between 1 and 128 characters" in result.stderr
