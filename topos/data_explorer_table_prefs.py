from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Dict, Optional

from topos.core.state import get_engine_config_value, set_engine_config_value
from topos.storage.db.write_gate import commit_connection, with_db_write

logger = logging.getLogger("topos.data_explorer_table_prefs")

PREFS_VERSION = 1
MAX_TABLE_NAME_LEN = 256
MAX_USER_ID_LEN = 256
MAX_PREFS_BYTES = 32 * 1024
MIN_COLUMN_WIDTH = 80
MAX_COLUMN_WIDTH = 480


def build_table_prefs_config_key(user_id: str, table_name: str) -> str:
    uid = str(user_id or "").strip()
    name = str(table_name or "").strip()
    return f"data_explorer_table_prefs:v1:{uid}:{name}"


def _clamp_column_width(width: Any, fallback: int = 160) -> int:
    try:
        n = float(width)
    except (TypeError, ValueError):
        n = float(fallback)
    if n != n or n < 0:
        n = float(fallback)
    return int(min(MAX_COLUMN_WIDTH, max(MIN_COLUMN_WIDTH, n)))


def normalize_table_prefs_payload(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("INVALID_PREFS")
    column_widths_raw = raw.get("columnWidths")
    if not isinstance(column_widths_raw, dict):
        raise ValueError("INVALID_PREFS")
    column_widths: Dict[str, int] = {}
    for key, width in column_widths_raw.items():
        col = str(key).strip()
        if not col:
            continue
        column_widths[col] = _clamp_column_width(width)
    prefs: Dict[str, Any] = {"columnWidths": column_widths}
    sort = raw.get("sort")
    if sort is not None:
        if not isinstance(sort, dict):
            raise ValueError("INVALID_SORT")
        column_id = str(sort.get("columnId") or "").strip()
        direction = str(sort.get("direction") or "").strip().lower()
        if not column_id or direction not in {"asc", "desc"}:
            raise ValueError("INVALID_SORT")
        prefs["sort"] = {"columnId": column_id, "direction": direction}
    for field in ("hiddenColumns", "columnOrder"):
        values = raw.get(field)
        if values is None:
            continue
        if not isinstance(values, list):
            raise ValueError("INVALID_PREFS")
        cleaned = [str(v).strip() for v in values if str(v).strip()]
        if cleaned:
            prefs[field] = cleaned
    envelope = {"version": PREFS_VERSION, **prefs}
    serialized = json.dumps(envelope, separators=(",", ":"), default=str)
    if len(serialized.encode("utf-8")) > MAX_PREFS_BYTES:
        raise ValueError("PREFS_TOO_LARGE")
    return envelope


def _validate_table_name(table_name: str) -> str:
    name = str(table_name or "").strip()
    if not name or len(name) > MAX_TABLE_NAME_LEN:
        raise ValueError("INVALID_TABLE_NAME")
    return name


def _validate_user_id(user_id: str) -> str:
    uid = str(user_id or "").strip()
    if not uid or len(uid) > MAX_USER_ID_LEN:
        raise ValueError("INVALID_USER_ID")
    return uid


def get_table_prefs(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    table_name: str,
) -> Optional[Dict[str, Any]]:
    uid = _validate_user_id(user_id)
    name = _validate_table_name(table_name)
    raw = get_engine_config_value(conn, build_table_prefs_config_key(uid, name))
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    try:
        return normalize_table_prefs_payload(parsed)
    except ValueError:
        return None


def put_table_prefs(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    table_name: str,
    prefs: Dict[str, Any],
) -> Dict[str, Any]:
    uid = _validate_user_id(user_id)
    name = _validate_table_name(table_name)
    normalized = normalize_table_prefs_payload(prefs)
    set_engine_config_value(conn, build_table_prefs_config_key(uid, name), json.dumps(normalized))
    logger.info(
        "data_explorer_table_prefs_saved user_id=%s table_name=%s columns=%d",
        uid[:8],
        name,
        len(normalized.get("columnWidths") or {}),
    )
    return normalized


def delete_table_prefs(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    table_name: str,
) -> bool:
    uid = _validate_user_id(user_id)
    name = _validate_table_name(table_name)
    key = build_table_prefs_config_key(uid, name)
    existing = get_engine_config_value(conn, key)
    if not existing:
        return False
    with with_db_write():
        conn.execute("DELETE FROM engine_config WHERE key = ?", (key,))
        commit_connection(conn)
    logger.info(
        "data_explorer_table_prefs_deleted user_id=%s table_name=%s",
        uid[:8],
        name,
    )
    return True
