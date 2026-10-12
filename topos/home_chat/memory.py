"""Explicit, source-backed home-chat memories. Never learns from assistant output.

Functional node storage for milestone one. No query-session/embedding cache: every
read revalidates its source, so an edit, branch change or deletion withdraws it.
"""
from __future__ import annotations

import json
import re
from uuid import UUID
from .schema import ensure_home_chat_schema
from .store import _now_iso
from ..storage.db.write_gate import with_db_write, commit_connection


def source_message(history, message_id):
    """Only completed owner messages on the active branch are evidence."""
    if not isinstance(history, dict) or not isinstance(history.get("messages"), dict):
        return None
    messages = history["messages"]
    current = history.get("currentId")
    seen, evidence = set(), None
    while isinstance(current, str) and current not in seen:
        seen.add(current)
        message = messages.get(current)
        if not isinstance(message, dict):
            return None
        # A grant's release must not become a new unrestricted memory source.
        if message.get("sharedGrantAnswer"):
            return None
        if current == message_id:
            if message.get("role") == "user" and message.get("done") and not message.get("streaming"):
                evidence = str(message.get("content") or "")
        current = message.get("parentId")
    return evidence if current is None else None


def withdraw_changed_sources(conn, session_id, history):
    rows = conn.execute("SELECT id, source_message_id, source_content FROM home_chat_memories WHERE source_session_id=? AND deleted=0", (session_id,)).fetchall()
    for row in rows:
        if source_message(history, row["source_message_id"]) != row["source_content"]:
            conn.execute("UPDATE home_chat_memories SET deleted=1, text='', source_content='', revision=revision+1 WHERE id=?", (row["id"],))


def _visible(conn, user_id, engine_id):
    rows = conn.execute("""SELECT m.*, s.title, s.history_json FROM home_chat_memories m
        JOIN home_chat_sessions s ON s.id=m.source_session_id
        WHERE m.user_id=? AND m.engine_id=? AND m.deleted=0
          AND s.user_id=m.user_id AND s.engine_id=m.engine_id
        ORDER BY m.updated_at DESC, m.id""", (user_id, engine_id)).fetchall()
    result = []
    for r in rows:
        if source_message(json.loads(r["history_json"]), r["source_message_id"]) != r["source_content"]:
            continue
        result.append({"id": r["id"], "text": r["text"], "revision": r["revision"],
            "sourceSessionId": r["source_session_id"], "sourceMessageId": r["source_message_id"],
            "sourceTitle": r["title"], "updatedAt": r["updated_at"]})
    return result


def memory_action(conn, *, user_id, engine_id, action, payload):
    ensure_home_chat_schema(conn)
    if action == "search":
        rows = _visible(conn, user_id, engine_id)
        terms = set(re.findall(r"\w+", str(payload.get("query") or "").lower()))
        if terms:
            rows = [r for r in rows if any(t in r["text"].lower() for t in terms)]
            rows.sort(key=lambda r: sum(t in r["text"].lower() for t in terms), reverse=True)
        return {"memories": rows[:50], "truncated": len(rows) > 50}
    if action not in {"remember", "correct", "forget"}:
        raise ValueError("INVALID_ACTION")
    mid = str(payload.get("id") or "")
    try:
        UUID(mid)
    except ValueError as exc:
        raise ValueError("INVALID_MEMORY_ID") from exc
    with with_db_write():
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT * FROM home_chat_memories WHERE id=?", (mid,)).fetchone()
            if existing and (existing["user_id"] != user_id or existing["engine_id"] != engine_id):
                raise ValueError("MEMORY_NOT_FOUND")
            if action != "remember":
                if not existing or existing["deleted"]:
                    raise ValueError("MEMORY_NOT_FOUND")
                if existing["revision"] != payload.get("revision"):
                    raise ValueError("STALE_REVISION")
            elif existing:
                # Idempotency: never resurrect a forgotten record on a retry.
                if existing["deleted"]:
                    raise ValueError("MEMORY_NOT_FOUND")
                if existing["text"] != str(payload.get("text") or "").strip():
                    raise ValueError("STALE_REVISION")
                visible = next((r for r in _visible(conn, user_id, engine_id) if r["id"] == mid), None)
                if visible is None:
                    raise ValueError("MEMORY_NOT_FOUND")
                commit_connection(conn)
                return {"memory": visible}
            if action == "forget":
                conn.execute("UPDATE home_chat_memories SET deleted=1, text='', source_content='', revision=revision+1 WHERE id=?", (mid,))
                commit_connection(conn)
                return {"forgotten": True, "id": mid}
            sid = str(payload.get("sourceSessionId") or "")
            message_id = str(payload.get("sourceMessageId") or "")
            row = conn.execute("SELECT history_json FROM home_chat_sessions WHERE id=? AND user_id=? AND engine_id=?", (sid, user_id, engine_id)).fetchone()
            content = source_message(json.loads(row[0]), message_id) if row else None
            text = str(payload.get("text") or "").strip()
            if not text or len(text) > 1000 or content is None or text not in content:
                raise ValueError("INVALID_MEMORY_SOURCE")
            if action == "remember":
                conn.execute("INSERT INTO home_chat_memories(id,user_id,engine_id,origin_session_id,source_session_id,source_message_id,source_content,text,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (mid,user_id,engine_id,sid,sid,message_id,content,text,_now_iso()))
            else:
                conn.execute("UPDATE home_chat_memories SET source_session_id=?,source_message_id=?,source_content=?,text=?,revision=revision+1,updated_at=? WHERE id=?", (sid,message_id,content,text,_now_iso(),mid))
            commit_connection(conn)
            return {"memory": next(r for r in _visible(conn, user_id, engine_id) if r["id"] == mid)}
        except Exception:
            conn.rollback()
            raise
