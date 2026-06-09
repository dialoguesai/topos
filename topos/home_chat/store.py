"""SQLite store for home chat session blobs (functional storage, not canonical AIChat)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from .schema import ensure_home_chat_schema

MAX_HISTORY_BYTES = 1_048_576


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_history_json(history: Any) -> Dict[str, Any]:
    if not isinstance(history, dict):
        raise ValueError("INVALID_HISTORY")
    if history.get("version") != 3:
        raise ValueError("INVALID_HISTORY")
    if not isinstance(history.get("messages"), dict):
        raise ValueError("INVALID_HISTORY")
    serialized = json.dumps(history, separators=(",", ":"), default=str)
    if len(serialized.encode("utf-8")) > MAX_HISTORY_BYTES:
        raise ValueError("HISTORY_TOO_LARGE")
    return history


def _row_data(row: sqlite3.Row) -> Dict[str, Any]:
    return dict(row)


def _row_to_meta(row: sqlite3.Row) -> Dict[str, Any]:
    data = _row_data(row)
    return {
        "sessionId": data["id"],
        "title": data.get("title") or "New chat",
        "engineId": data.get("engine_id") or "",
        "userId": data.get("user_id") or "",
        "traceSessionId": data.get("trace_session_id") or "",
        "createdAt": int(data.get("created_at_ms") or 0),
        "updatedAt": int(data.get("updated_at_ms") or 0),
        "revision": int(data.get("revision") or 0),
    }


def _row_to_blob(row: sqlite3.Row) -> Dict[str, Any]:
    meta = _row_to_meta(row)
    history_raw = _row_data(row).get("history_json") or "{}"
    try:
        history = json.loads(history_raw) if isinstance(history_raw, str) else history_raw
    except json.JSONDecodeError as exc:
        raise ValueError("INVALID_HISTORY") from exc
    _validate_history_json(history)
    return {**meta, "history": history}


def list_sessions(conn: sqlite3.Connection, *, user_id: str, engine_id: str) -> List[Dict[str, Any]]:
    ensure_home_chat_schema(conn)
    uid = str(user_id).strip()
    eid = str(engine_id).strip()
    cur = conn.execute(
        """
        SELECT id, user_id, engine_id, title, trace_session_id, revision, created_at_ms, updated_at_ms
        FROM home_chat_sessions
        WHERE user_id = ? AND engine_id = ?
        ORDER BY updated_at_ms DESC
        """,
        (uid, eid),
    )
    return [_row_to_meta(row) for row in cur.fetchall()]


def get_session(conn: sqlite3.Connection, *, user_id: str, session_id: str) -> Optional[Dict[str, Any]]:
    ensure_home_chat_schema(conn)
    cur = conn.execute(
        "SELECT * FROM home_chat_sessions WHERE id = ? AND user_id = ?",
        (str(session_id).strip(), str(user_id).strip()),
    )
    row = cur.fetchone()
    if not row:
        return None
    return _row_to_blob(row)


def upsert_session(conn: sqlite3.Connection, *, user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    ensure_home_chat_schema(conn)
    sid = str(payload.get("sessionId") or payload.get("id") or "").strip()
    if not sid:
        raise ValueError("INVALID_SESSION_ID")
    try:
        UUID(sid)
    except ValueError as exc:
        raise ValueError("INVALID_SESSION_ID") from exc
    engine_id = str(payload.get("engineId") or "").strip()
    if not engine_id:
        raise ValueError("INVALID_ENGINE_ID")
    history = _validate_history_json(payload.get("history"))
    uid = str(user_id).strip()
    revision = int(payload.get("revision") or 0)
    now = _now_iso()
    created_at_ms = int(payload.get("createdAt") or 0) or int(datetime.now(timezone.utc).timestamp() * 1000)
    updated_at_ms = int(payload.get("updatedAt") or 0) or int(datetime.now(timezone.utc).timestamp() * 1000)

    cur = conn.execute("SELECT * FROM home_chat_sessions WHERE id = ?", (sid,))
    existing = cur.fetchone()
    if existing:
        existing_data = _row_data(existing)
        if existing_data.get("user_id") != uid:
            raise PermissionError("FORBIDDEN")
        if existing_data.get("engine_id") != engine_id:
            raise ValueError("INVALID_ENGINE_ID")
        stored_revision = int(existing_data.get("revision") or 0)
        if revision and stored_revision > revision:
            raise ValueError("STALE_REVISION")
        next_revision = max(revision, stored_revision + 1) if revision else stored_revision + 1
        created_at_ms = int(existing_data.get("created_at_ms") or created_at_ms)
    else:
        next_revision = max(revision, 1)

    history_json = json.dumps(history, separators=(",", ":"), default=str)
    conn.execute(
        """
        INSERT INTO home_chat_sessions (
            id, user_id, engine_id, title, trace_session_id, history_json, revision,
            created_at_ms, updated_at_ms, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            engine_id = excluded.engine_id,
            title = excluded.title,
            trace_session_id = excluded.trace_session_id,
            history_json = excluded.history_json,
            revision = excluded.revision,
            updated_at_ms = excluded.updated_at_ms,
            updated_at = excluded.updated_at
        """,
        (
            sid,
            uid,
            engine_id,
            str(payload.get("title") or "New chat").strip() or "New chat",
            str(payload.get("traceSessionId") or payload.get("trace_session_id") or ""),
            history_json,
            next_revision,
            created_at_ms,
            updated_at_ms,
            now,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM home_chat_sessions WHERE id = ?", (sid,)).fetchone()
    if not row:
        raise RuntimeError("Failed to persist session")
    return _row_to_blob(row)


def delete_session(conn: sqlite3.Connection, *, user_id: str, session_id: str) -> bool:
    ensure_home_chat_schema(conn)
    cur = conn.execute(
        "DELETE FROM home_chat_sessions WHERE id = ? AND user_id = ?",
        (str(session_id).strip(), str(user_id).strip()),
    )
    conn.commit()
    return cur.rowcount > 0
