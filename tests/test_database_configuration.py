import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import app.database as database


def test_connections_enable_and_enforce_foreign_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("KYBERNAUT_DATABASE_PATH", raising=False)
    monkeypatch.setattr(database, "DATABASE_PATH", tmp_path / "foreign-keys.db")
    database.initialize_database()

    with database.get_connection() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(
            sqlite3.IntegrityError,
            match="FOREIGN KEY constraint failed",
        ):
            connection.execute(
                """INSERT INTO incident_handoffs (
                    incident_row_id, service, incident_id, status,
                    idempotency_key, created_at, updated_at
                ) VALUES (999, 'order-service', 'INC-missing', 'PENDING',
                    'missing-parent', '2026-10-26T00:00:00+00:00',
                    '2026-10-26T00:00:00+00:00')"""
            )


def test_environment_path_is_resolved_when_requested(tmp_path, monkeypatch):
    configured = tmp_path / "environment.sqlite3"
    monkeypatch.setenv("KYBERNAUT_DATABASE_PATH", str(configured))

    assert database.get_database_path() == configured
    assert not configured.exists()


def test_missing_environment_uses_deterministic_repository_default(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("KYBERNAUT_DATABASE_PATH", raising=False)
    monkeypatch.setattr(database, "DATABASE_PATH", database.DEFAULT_DATABASE_PATH)

    assert database.get_database_path() == (
        Path(database.__file__).resolve().parent.parent / "incidents.db"
    )
    assert not (tmp_path / "incidents.db").exists()


def test_connection_uses_existing_database_path_test_hook(tmp_path, monkeypatch):
    configured = tmp_path / "monkeypatched.sqlite3"
    monkeypatch.delenv("KYBERNAUT_DATABASE_PATH", raising=False)
    monkeypatch.setattr(database, "DATABASE_PATH", configured)

    with database.get_connection() as connection:
        connection.execute("CREATE TABLE synthetic (value TEXT)")

    assert configured.exists()


def test_main_loads_dotenv_before_importing_database_and_does_not_initialize(
    tmp_path,
):
    configured = tmp_path / "from-dotenv.sqlite3"
    (tmp_path / ".env").write_text(
        f"KYBERNAUT_DATABASE_PATH={configured}\n",
        encoding="utf-8",
    )
    repo_root = Path(__file__).resolve().parents[1]
    child_env = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(repo_root),
        "PYTHONNOUSERSITE": "1",
    }
    code = (
        "import app.main\n"
        "from app import database\n"
        "from pathlib import Path\n"
        "import os\n"
        "assert database.DATABASE_PATH == "
        "Path(os.environ['KYBERNAUT_DATABASE_PATH'])\n"
        "assert database.get_database_path() == database.DATABASE_PATH\n"
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert not configured.exists()


def test_process_environment_overrides_dotenv_before_main_import(tmp_path):
    dotenv_path = tmp_path / "dotenv.sqlite3"
    process_path = tmp_path / "process.sqlite3"
    (tmp_path / ".env").write_text(
        f"KYBERNAUT_DATABASE_PATH={dotenv_path}\n",
        encoding="utf-8",
    )
    repo_root = Path(__file__).resolve().parents[1]
    child_env = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(repo_root),
        "PYTHONNOUSERSITE": "1",
        "KYBERNAUT_DATABASE_PATH": str(process_path),
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import app.main; from app.database import get_database_path; "
            "assert get_database_path().as_posix() == "
            f"{str(process_path.as_posix())!r}",
        ],
        cwd=tmp_path,
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert not dotenv_path.exists()
    assert not process_path.exists()
