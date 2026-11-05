"""Persistence for saved Console conversations (investigation history)."""

from __future__ import annotations

import json
import re
import uuid
from contextlib import closing
from datetime import datetime, timezone

from app.database import get_connection

MAX_MESSAGES = 400
MAX_BYTES = 750_000
ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")


class InvestigationError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def new_id() -> str:
    return f"chat-{uuid.uuid4().hex[:16]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _summary(row) -> dict:
    messages = json.loads(row["messages_json"])
    return {
        "investigation_id": row["investigation_id"],
        "operator_id": row["operator_id"],
        "incident_id": row["incident_id"],
        "title": row["title"],
        "message_count": len(messages),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def save_investigation(
    investigation_id: str, operator_id: str, *, incident_id: str | None,
    title: str, messages: list,
) -> dict:
    if not ID_PATTERN.fullmatch(investigation_id):
        raise InvestigationError("invalid_id", "Invalid investigation id")
    if not isinstance(messages, list) or len(messages) > MAX_MESSAGES:
        raise InvestigationError("invalid_messages", "Too many messages")
    if not all(isinstance(item, dict) for item in messages):
        raise InvestigationError("invalid_messages", "Messages must be objects")
    encoded = json.dumps(messages, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_BYTES:
        raise InvestigationError("too_large", "Conversation is too large")
    title = (title or "Untitled investigation").strip()[:160] or "Untitled investigation"
    now = _now()
    with closing(get_connection()) as conn, conn:
        row = conn.execute(
            "SELECT operator_id FROM console_investigations "
            "WHERE investigation_id = ?", (investigation_id,),
        ).fetchone()
        if row is not None and row["operator_id"] != operator_id:
            raise InvestigationError("forbidden", "Not your investigation")
        if row is None:
            conn.execute(
                "INSERT INTO console_investigations (investigation_id, "
                "operator_id, incident_id, title, messages_json, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?)",
                (investigation_id, operator_id, incident_id, title,
                 encoded, now, now),
            )
        else:
            conn.execute(
                "UPDATE console_investigations SET incident_id = ?, title = ?, "
                "messages_json = ?, updated_at = ? WHERE investigation_id = ?",
                (incident_id, title, encoded, now, investigation_id),
            )
        saved = conn.execute(
            "SELECT * FROM console_investigations WHERE investigation_id = ?",
            (investigation_id,),
        ).fetchone()
    return _summary(saved)


def list_investigations(limit: int = 200) -> list[dict]:
    with closing(get_connection()) as conn:
        rows = conn.execute(
            "SELECT * FROM console_investigations "
            "ORDER BY updated_at DESC LIMIT ?", (min(max(limit, 1), 500),),
        ).fetchall()
    return [_summary(row) for row in rows]


def delete_investigation(
    investigation_id: str, operator_id: str, *, is_owner: bool = False,
) -> bool:
    """Delete one saved conversation. Incident records and audit are untouched.

    Only the operator who saved the conversation (or an ``owner`` operator)
    may delete it. Returns False when it does not exist.
    """
    if not ID_PATTERN.fullmatch(investigation_id):
        raise InvestigationError("invalid_id", "Invalid investigation id")
    with closing(get_connection()) as conn, conn:
        row = conn.execute(
            "SELECT operator_id FROM console_investigations "
            "WHERE investigation_id = ?", (investigation_id,),
        ).fetchone()
        if row is None:
            return False
        if row["operator_id"] != operator_id and not is_owner:
            raise InvestigationError(
                "forbidden", "Only the operator who saved this conversation "
                "(or an owner) can delete it")
        conn.execute(
            "DELETE FROM console_investigations WHERE investigation_id = ?",
            (investigation_id,),
        )
    return True


def get_investigation(investigation_id: str) -> dict | None:
    with closing(get_connection()) as conn:
        row = conn.execute(
            "SELECT * FROM console_investigations WHERE investigation_id = ?",
            (investigation_id,),
        ).fetchone()
    if row is None:
        return None
    return {**_summary(row), "messages": json.loads(row["messages_json"])}