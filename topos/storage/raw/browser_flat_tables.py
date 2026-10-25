"""Flat tables for browser plugin data: one row per event, one column per field.

Stored in SQLite with explicit columns so DuckDB (or any SQL engine) can query
without parsing JSON. Raw format = one row per event; good rows = flat columns
for analytics (e.g. SELECT url, visited_at, hostname FROM browser_visits).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

from ..db.write_gate import commit_connection, with_db_write
from ...features.provenance.browser_actor import browser_actor

logger = logging.getLogger("topos.storage.raw.browser_flat_tables")

BROWSER_VISITS_TABLE = "browser_visits"
BROWSER_EVENTS_TABLE = "browser_events"
# Normalized raw retention table for browser_visits (architecture layer).
RAW_BROWSER_VISITS_TABLE = "raw_chat_messages_browservisits"


def _to_sql_value(val: Any) -> Optional[str]:
    """Convert Python value to SQL-friendly value (string or None)."""
    if val is None:
        return None
    if isinstance(val, bool):
        return "1" if val else "0"
    if isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False)
    return str(val)


def _ensure_actor_columns(conn, table):
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name in ("actor_kind", "actor_provider", "parent_conversation_source", "parent_conversation_id", "actor_json"):
        if name not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} TEXT")


def _retain_actor(conn, table, record_id, payload):
    actor = browser_actor(payload)
    if actor["method"] == "unresolved":
        previous = conn.execute(f"SELECT actor_json FROM {table} WHERE record_id=?", (record_id,)).fetchone()
        if previous and previous[0]:
            actor = browser_actor({"actor_json": previous[0]})
    return {**payload, "actor": actor}


def _write_actor(conn, table, record_id, payload):
    actor = browser_actor(payload)
    conn.execute(f"UPDATE {table} SET actor_kind=?, actor_provider=?, parent_conversation_source=?, "
                 "parent_conversation_id=?, actor_json=? WHERE record_id=?",
                 (actor["kind"], actor.get("provider"), actor.get("conversation_source"), actor.get("conversation_id"),
                  json.dumps(actor), record_id))


def ensure_browser_visits_table(conn) -> None:
    """Create browser_visits table with flat columns if not exists."""
    # DDL takes SQLite's write lock at execute time — gate it with the commit
    # (write_gate lock-order inversion); ingest calls this on its write path.
    with with_db_write():
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {BROWSER_VISITS_TABLE} (
                record_id TEXT PRIMARY KEY,
                dataset_id TEXT,
                url TEXT NOT NULL,
                visited_at TEXT NOT NULL,
                title TEXT,
                favicon_url TEXT,
                hostname TEXT,
                device_name TEXT,
                tab_id INTEGER,
                window_id INTEGER,
                incognito INTEGER,
                transition_type TEXT,
                pinned INTEGER,
                audible INTEGER,
                muted INTEGER,
                opener_tab_id INTEGER,
                referred_by TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{BROWSER_VISITS_TABLE}_visited_at
            ON {BROWSER_VISITS_TABLE}(visited_at)
        """)
        conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{BROWSER_VISITS_TABLE}_hostname
            ON {BROWSER_VISITS_TABLE}(hostname)
        """)
        conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{BROWSER_VISITS_TABLE}_transition_type
            ON {BROWSER_VISITS_TABLE}(transition_type)
        """)
        _ensure_actor_columns(conn, BROWSER_VISITS_TABLE)
        commit_connection(conn)
    logger.debug("Ensured table %s exists", BROWSER_VISITS_TABLE)


def ensure_browser_events_table(conn) -> None:
    """Create browser_events table with flat columns if not exists."""
    with with_db_write():
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {BROWSER_EVENTS_TABLE} (
                record_id TEXT PRIMARY KEY,
                dataset_id TEXT,
                event_type TEXT NOT NULL,
                url TEXT,
                visited_at TEXT,
                title TEXT,
                favicon_url TEXT,
                hostname TEXT,
                device_name TEXT,
                transition_type TEXT,
                content TEXT,
                tab_id INTEGER,
                window_id INTEGER,
                incognito INTEGER,
                pinned INTEGER,
                audible INTEGER,
                muted INTEGER,
                opener_tab_id INTEGER,
                starred_at TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{BROWSER_EVENTS_TABLE}_event_type
            ON {BROWSER_EVENTS_TABLE}(event_type)
        """)
        conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{BROWSER_EVENTS_TABLE}_visited_at
            ON {BROWSER_EVENTS_TABLE}(visited_at)
        """)
        _ensure_actor_columns(conn, BROWSER_EVENTS_TABLE)
        commit_connection(conn)
    logger.debug("Ensured table %s exists", BROWSER_EVENTS_TABLE)


def backfill_browser_visits_from_raw_retention(conn) -> int:
    """Copy visits from raw retention into browser_visits when the flat table is empty.

    Fresh Topos installs ingest into raw_chat_messages_browservisits first; this
    one-time catch-up makes browser_visits visible in Data Explorer without manual SQL.
    """
    ensure_browser_visits_table(conn)
    flat_count = conn.execute(f"SELECT COUNT(*) FROM {BROWSER_VISITS_TABLE}").fetchone()[0]
    if flat_count:
        return 0
    raw_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (RAW_BROWSER_VISITS_TABLE,),
    ).fetchone()
    if not raw_exists:
        return 0
    raw_count = conn.execute(f"SELECT COUNT(*) FROM {RAW_BROWSER_VISITS_TABLE}").fetchone()[0]
    if not raw_count:
        return 0
    with with_db_write():
        conn.execute(f"""
            INSERT OR REPLACE INTO {BROWSER_VISITS_TABLE}
            (record_id, dataset_id, url, visited_at, title, favicon_url, hostname, device_name,
             tab_id, window_id, incognito, transition_type, pinned, audible, muted, opener_tab_id, referred_by)
            SELECT
                COALESCE(NULLIF(TRIM(record_id), ''), source_record_id),
                dataset_id,
                url,
                visited_at,
                title,
                favicon_url,
                hostname,
                device_name,
                tab_id,
                window_id,
                incognito,
                transition_type,
                pinned,
                audible,
                muted,
                opener_tab_id,
                referred_by
            FROM {RAW_BROWSER_VISITS_TABLE}
            WHERE url IS NOT NULL AND TRIM(url) != ''
        """)
        raw_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({RAW_BROWSER_VISITS_TABLE})")}
        if "actor" in raw_columns or "actor_json" in raw_columns:
            actor_col = "actor" if "actor" in raw_columns else "actor_json"
            for row in conn.execute(f"SELECT COALESCE(NULLIF(TRIM(record_id), ''), source_record_id), {actor_col} FROM {RAW_BROWSER_VISITS_TABLE}").fetchall():
                _write_actor(conn, BROWSER_VISITS_TABLE, row[0], {"actor_json": row[1]})
        commit_connection(conn)
    copied = conn.execute(f"SELECT COUNT(*) FROM {BROWSER_VISITS_TABLE}").fetchone()[0]
    logger.info(
        "[PIPELINE:RAW] Backfilled %d browser visit row(s) from %s into %s",
        copied,
        RAW_BROWSER_VISITS_TABLE,
        BROWSER_VISITS_TABLE,
    )
    return int(copied)


def write_browser_visit(conn, payload: Dict[str, Any]) -> None:
    """Insert or replace one row in browser_visits (flat columns)."""
    ensure_browser_visits_table(conn)
    record_id = payload.get("record_id") or ""
    url = _to_sql_value(payload.get("url")) or ""
    visited_at = _to_sql_value(payload.get("visited_at")) or ""
    with with_db_write():
        payload = _retain_actor(conn, BROWSER_VISITS_TABLE, record_id, payload)
        conn.execute(f"""
            INSERT OR REPLACE INTO {BROWSER_VISITS_TABLE}
            (record_id, dataset_id, url, visited_at, title, favicon_url, hostname, device_name,
             tab_id, window_id, incognito, transition_type, pinned, audible, muted, opener_tab_id, referred_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record_id,
            _to_sql_value(payload.get("dataset_id")),
            url,
            visited_at,
            _to_sql_value(payload.get("title")),
            _to_sql_value(payload.get("favicon_url")),
            _to_sql_value(payload.get("hostname")),
            _to_sql_value(payload.get("device_name")),
            payload.get("tab_id") if isinstance(payload.get("tab_id"), (int, type(None))) else None,
            payload.get("window_id") if isinstance(payload.get("window_id"), (int, type(None))) else None,
            1 if payload.get("incognito") is True else (0 if payload.get("incognito") is False else None),
            _to_sql_value(payload.get("transition_type")),
            1 if payload.get("pinned") is True else (0 if payload.get("pinned") is False else None),
            1 if payload.get("audible") is True else (0 if payload.get("audible") is False else None),
            1 if payload.get("muted") is True else (0 if payload.get("muted") is False else None),
            payload.get("opener_tab_id") if isinstance(payload.get("opener_tab_id"), (int, type(None))) else None,
            _to_sql_value(payload.get("referred_by")),
        ))
        _write_actor(conn, BROWSER_VISITS_TABLE, record_id, payload)
        commit_connection(conn)
    logger.info("[PIPELINE:RAW] Wrote flat row to %s: record_id=%s", BROWSER_VISITS_TABLE, record_id[:24] if record_id else None)


def write_browser_event(conn, payload: Dict[str, Any]) -> None:
    """Insert or replace one row in browser_events (flat columns)."""
    ensure_browser_events_table(conn)
    record_id = payload.get("record_id") or ""
    event_type = _to_sql_value(payload.get("event_type")) or "unknown"
    with with_db_write():
        payload = _retain_actor(conn, BROWSER_EVENTS_TABLE, record_id, payload)
        conn.execute(f"""
            INSERT OR REPLACE INTO {BROWSER_EVENTS_TABLE}
            (record_id, dataset_id, event_type, url, visited_at, title, favicon_url, hostname, device_name,
             transition_type, content, tab_id, window_id, incognito, pinned, audible, muted, opener_tab_id, starred_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            record_id,
            _to_sql_value(payload.get("dataset_id")),
            event_type,
            _to_sql_value(payload.get("url")),
            _to_sql_value(payload.get("visited_at")),
            _to_sql_value(payload.get("title")),
            _to_sql_value(payload.get("favicon_url")),
            _to_sql_value(payload.get("hostname")),
            _to_sql_value(payload.get("device_name")),
            _to_sql_value(payload.get("transition_type")),
            _to_sql_value(payload.get("content")),
            payload.get("tab_id") if isinstance(payload.get("tab_id"), (int, type(None))) else None,
            payload.get("window_id") if isinstance(payload.get("window_id"), (int, type(None))) else None,
            1 if payload.get("incognito") is True else (0 if payload.get("incognito") is False else None),
            1 if payload.get("pinned") is True else (0 if payload.get("pinned") is False else None),
            1 if payload.get("audible") is True else (0 if payload.get("audible") is False else None),
            1 if payload.get("muted") is True else (0 if payload.get("muted") is False else None),
            payload.get("opener_tab_id") if isinstance(payload.get("opener_tab_id"), (int, type(None))) else None,
            _to_sql_value(payload.get("starred_at")),
        ))
        _write_actor(conn, BROWSER_EVENTS_TABLE, record_id, payload)
        commit_connection(conn)
    logger.info("[PIPELINE:RAW] Wrote flat row to %s: record_id=%s event_type=%s", BROWSER_EVENTS_TABLE, record_id[:24] if record_id else None, event_type)

