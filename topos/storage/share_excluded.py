"""``ai_chat_messages.share_excluded``: a row the owner keeps and nobody else gets (CONTRACTS.md §2.6).

Set by the AI-coding-tool sync for every assistant turn after a session touched a Topos surface
(§2.7). The owner's own reads keep the row; every enrichment, signal, embedding and fact
selection, and every grantee path, adds ``share_excluded = 0``. The column is added by migration
``ai_coding_sessions_v1`` and by the runner before its first write, so a database that predates it
is read as before: no column, no filter. A probe that cannot run raises; it never reads as "no column".
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

COLUMN = "share_excluded"
TABLE = "ai_chat_messages"


def share_excluded_present(conn: Any, table: str = TABLE) -> bool:
    """True when ``table`` carries the column; False when it does not, or the table does not exist (``PRAGMA
    table_info`` lists nothing): only then does a reader add no filter. A probe that cannot run (a closed or broken
    connection, a database error) raises, so it never reads as "no filter" (NR189 review, fail closed)."""
    return COLUMN in {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


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


def share_excluded_record_filter(conn: Any, *, record_column: str, present: bool | None = None) -> str:
    """`` AND NOT EXISTS (...)`` for a table keyed by a canonical record id that holds rows of every kind (an embedding,
    a topic-cluster member): leaves out a row whose id is a flagged ``ai_chat_messages`` row (a NULL counts as flagged)
    and keeps every row of any other kind. ``""`` when the column does not exist. ``record_column`` is the outer
    query's record-id column, spelled by the caller (a fixed name, never input)."""
    if not (share_excluded_present(conn) if present is None else present):
        return ""
    return (f" AND NOT EXISTS (SELECT 1 FROM {TABLE} flagged WHERE flagged.message_id = {record_column}"
            f" AND flagged.{COLUMN} IS NOT 0)")


def row_share_excluded(row: Any) -> bool:
    """Whether a row as stored (a ``SELECT *`` of the table, a ``sqlite3.Row`` or a mapping) carries the flag. For a
    read that already selects every column of one row, so it needs no probe at all: a member's re-check or an evidence
    load, once per member.

    Fail closed, as the SQL ``share_excluded = 0`` is: a row with the column reads as flagged unless the value is 0
    (NULL included). Only a row without the column (a database from before migration 85) reads as not flagged. A row of
    any other type cannot be read and raises ``TypeError``."""
    if isinstance(row, sqlite3.Row):
        if COLUMN not in row.keys():
            return False
    elif isinstance(row, Mapping):
        if COLUMN not in row:
            return False
    else:
        raise TypeError("row_share_excluded reads a sqlite3.Row or a mapping")
    value = row[COLUMN]
    return value is None or value != 0


def record_share_excluded(record: Any) -> bool:
    """Whether one canonical record is a row the owner keeps from every share: its ``share_excluded`` value, else
    the ``share_excluded`` the reader put in its ``metadata_json`` (a dict, or the JSON text the row stores). The
    same fact migration 85's triggers read at write time, for the paths that see the record before the row: flat
    (``share_excluded``, the bundled mapper) or nested (``_metadata.share_excluded``, the runtime-installed mapper a
    live node uses)."""
    if not isinstance(record, Mapping):
        return False
    if record.get(COLUMN) in (1, True, "1"):
        return True
    meta = record.get("metadata_json")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            return False
    if not isinstance(meta, dict):
        return False
    nested = meta.get("_metadata")
    return meta.get(COLUMN) is True or (isinstance(nested, dict) and nested.get(COLUMN) is True)


__all__ = ["COLUMN", "TABLE", "record_share_excluded", "row_share_excluded", "share_excluded_clause",
           "share_excluded_predicate", "share_excluded_present", "share_excluded_record_filter"]
