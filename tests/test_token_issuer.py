
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import jwt


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ISSUER_SCRIPT = PROJECT_ROOT / "scripts" / "issue_operator_token.py"

TEST_SECRET = "test-only-secret-with-at-least-32-bytes"
TEST_ISSUER = "kybernaut-test"
TEST_AUDIENCE = "kybernaut-api-test"


def run_issuer(*args, secret=TEST_SECRET, operators=None):
    test_operators = operators or {
        "test-operator": {"permissions": ["read", "remediation"]},
        "read-only": {"permissions": ["read"]},
    }

    with tempfile.TemporaryDirectory(prefix="kybernaut-token-issuer-") as root:
        project_root = Path(root)
        config_dir = project_root / "config"
        scripts_dir = project_root / "scripts"
        config_dir.mkdir()
        scripts_dir.mkdir()
        issuer_script = scripts_dir / ISSUER_SCRIPT.name
        shutil.copyfile(ISSUER_SCRIPT, issuer_script)
        operators_file = config_dir / "operators.json"
        operators_file.write_text(
            json.dumps({"operators": test_operators}), encoding="utf-8"
        )
        env = {
            "KYBERNAUT_JWT_SECRET": secret,
            "KYBERNAUT_JWT_ISSUER": TEST_ISSUER,
            "KYBERNAUT_JWT_AUDIENCE": TEST_AUDIENCE,
        }
        return subprocess.run(
            [sys.executable, str(issuer_script), *args],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
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
