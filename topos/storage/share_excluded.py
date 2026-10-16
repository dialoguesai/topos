"""``ai_chat_messages.share_excluded``: a row the owner keeps and nobody else gets (CONTRACTS.md §2.6).

Set by the AI-coding-tool sync for every assistant turn after a session touched a Topos surface
(§2.7). The owner's own reads keep the row; every enrichment, signal, embedding and fact
selection, and every grantee path, adds ``share_excluded = 0``. The column is added by migration
``ai_coding_sessions_v1`` and by the runner before its first write, so a database that predates it
is read as before: no column, no filter, never an error.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

COLUMN = "share_excluded"
TABLE = "ai_chat_messages"


def share_excluded_present(conn: Any, table: str = TABLE) -> bool:
    """True when ``table`` carries the column. A probe that cannot run reads as absent."""
    try:
        return COLUMN in {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except (sqlite3.Error, AttributeError, TypeError):
        return False


def share_excluded_predicate(conn: Any, *, alias: str = "", table: str = TABLE, present: bool | None = None) -> str:
    """``[alias.]share_excluded = 0`` when the column exists, else ``""``: one predicate for a WHERE list.

    ``present``: what the caller already knows of the column (a ``PRAGMA table_info`` it ran on this read anyway);
    None probes here."""
    if not (share_excluded_present(conn, table) if present is None else present):
        return ""
    prefix = f"{alias}." if alias else ""
    return f"{prefix}{COLUMN} = 0"


def share_excluded_clause(conn: Any, *, alias: str = "", table: str = TABLE, present: bool | None = None) -> str:
    """`` AND [alias.]share_excluded = 0`` when the column exists, else ``""``; appends to an existing WHERE."""
    predicate = share_excluded_predicate(conn, alias=alias, table=table, present=present)
    return f" AND {predicate}" if predicate else ""


def row_share_excluded(row: Any) -> bool:
    """Whether a row as stored (a ``SELECT *`` of the table, a ``sqlite3.Row`` or a dict) carries the flag: its column
    holds anything but 0. A row from a database without the column never does. For a read that already selects every
    column of one row, so it needs no probe at all: a member's re-check or an evidence load, once per member."""
    try:
        value = row[COLUMN]
    except (KeyError, IndexError, TypeError):
        return False
    return value not in (None, 0)


def record_share_excluded(record: Any) -> bool:
    """Whether one canonical record is a row the owner keeps from every share: its ``share_excluded`` value, else
    the ``share_excluded`` the reader put in its ``metadata_json`` (a dict, or the JSON text the row stores). The
    same fact migration 85's triggers read at write time, for the paths that see the record before the row."""
    if not isinstance(record, dict):
        return False
    if record.get(COLUMN) in (1, True, "1"):
        return True
    meta = record.get("metadata_json")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            return False
    return isinstance(meta, dict) and meta.get(COLUMN) is True


__all__ = ["COLUMN", "TABLE", "record_share_excluded", "row_share_excluded", "share_excluded_clause",
           "share_excluded_predicate", "share_excluded_present"]
