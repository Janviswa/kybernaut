"""Read-only access to isolated synthetic demo databases."""

from __future__ import annotations

import json
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


DEMO_DATABASE_DIR = Path(tempfile.gettempdir()) / "kybernaut-demo"
DEMO_ID_PATTERN = re.compile(r"^DEMO-[a-f0-9]{12}$")
DATABASE_NAME_PATTERN = re.compile(r"^demo-([a-f0-9]{12})\.sqlite3$")


class DemoResultsError(RuntimeError):
    """The isolated demo result store cannot be read safely."""


def _latest_timestamp(values: list[str]) -> str | None:
    latest: tuple[datetime, str] | None = None
    for value in values:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise DemoResultsError(
                "Demo result store has an invalid timestamp"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise DemoResultsError(
                "Demo result store has an invalid timestamp"
            )
        instant = parsed.astimezone(timezone.utc)
        if latest is None or instant > latest[0]:
            latest = (instant, value)
    return latest[1] if latest is not None else None


def _database_path(incident_id: str) -> Path:
    if not DEMO_ID_PATTERN.fullmatch(incident_id):
        raise FileNotFoundError("Demo result not found")
    root = DEMO_DATABASE_DIR
    if root.is_symlink():
        raise DemoResultsError("Demo result store is unavailable")
    path = root / f"{incident_id.lower()}.sqlite3"
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("Demo result not found")
    try:
        if path.resolve(strict=True).parent != root.resolve(strict=True):
            raise DemoResultsError("Demo result store is unavailable")
    except OSError as exc:
        raise DemoResultsError("Demo result store is unavailable") from exc
    return path


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for key in ("policy_decision", "details", "proposed_action", "context"):
        value = result.get(key)
        if isinstance(value, str):
            try:
                result[key] = json.loads(value)
            except json.JSONDecodeError:
                pass
    return result


def _required_columns(connection: sqlite3.Connection) -> None:
    expected = {
        "audit_events": {
            "event_id", "incident_ref", "timestamp", "actor", "event_type",
            "summary", "policy_decision", "approval_id", "outcome", "details",
        },
        "approval_requests": {
            "approval_id", "incident_ref", "status", "proposed_action",
            "context", "requested_by", "requested_at", "expires_at",
            "decided_by", "decided_at", "decision_reason",
        },
    }
    for table, required in expected.items():
        present = {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if not required.issubset(present):
            raise DemoResultsError("Demo result store has an invalid schema")
    incident_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(incidents)")
    }
    if incident_columns and not {
        "incident_id", "service", "incident_type", "severity",
        "message", "status", "opened_at", "updated_at",
    }.issubset(incident_columns):
        raise DemoResultsError("Demo result store has an invalid schema")


def _read_result(path: Path, incident_id: str) -> dict[str, Any]:
    uri = f"file:{quote(path.as_posix(), safe='/')}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=3) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            _required_columns(connection)
            incident_row = None
            has_incident_table = connection.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type = 'table' AND name = 'incidents'"""
            ).fetchone()
            if has_incident_table:
                incident_row = connection.execute(
                    "SELECT * FROM incidents WHERE incident_id = ?",
                    (incident_id,),
                ).fetchone()
            events = connection.execute(
                """SELECT * FROM audit_events WHERE incident_ref = ?
                   ORDER BY id ASC""",
                (incident_id,),
            ).fetchall()
            approvals = connection.execute(
                """SELECT * FROM approval_requests WHERE incident_ref = ?
                   ORDER BY requested_at ASC""",
                (incident_id,),
            ).fetchall()
    except (sqlite3.Error, OSError) as exc:
        raise DemoResultsError("Demo result store is unreadable") from exc
    if incident_row is None and not events and not approvals:
        raise FileNotFoundError("Demo result not found")
    return {
        "synthetic": True,
        "incident": dict(incident_row) if incident_row else None,
        "audit_events": [_decode(row) for row in events],
        "approvals": [_decode(row) for row in approvals],
    }


def list_demo_results() -> dict[str, Any]:
    """List demos from the fixed local demo directory without changing app DB."""
    root = DEMO_DATABASE_DIR
    if root.is_symlink():
        raise DemoResultsError("Demo result store is unavailable")
    if not root.exists():
        return {"demos": [], "unavailable_count": 0}
    if not root.is_dir():
        raise DemoResultsError("Demo result store is unavailable")
    results = []
    unavailable_count = 0
    try:
        candidates = []
        for path in root.iterdir():
            match = DATABASE_NAME_PATTERN.fullmatch(path.name)
            if not match:
                continue
            if path.is_symlink() or not path.is_file():
                unavailable_count += 1
                continue
            try:
                modified_at = path.stat().st_mtime
            except OSError:
                unavailable_count += 1
                continue
            candidates.append((modified_at, path, match))
        candidates.sort(key=lambda entry: entry[0], reverse=True)
        for _, path, match in candidates:
            incident_id = f"DEMO-{match.group(1)}"
            try:
                result = _read_result(path, incident_id)
            except (DemoResultsError, FileNotFoundError):
                unavailable_count += 1
                continue
            result["incident_id"] = incident_id
            result["latest_at"] = _latest_timestamp(
                [event["timestamp"] for event in result["audit_events"]]
                + [approval["requested_at"] for approval in result["approvals"]]
                + ([result["incident"]["updated_at"]]
                   if result["incident"] else []),
            )
            results.append(result)
    except OSError as exc:
        raise DemoResultsError("Demo result store is unreadable") from exc
    return {"demos": results, "unavailable_count": unavailable_count}


def get_demo_result(incident_id: str) -> dict[str, Any]:
    path = _database_path(incident_id)
    result = _read_result(path, incident_id)
    result["incident_id"] = incident_id
    return result
