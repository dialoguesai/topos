"""A canonical database with NSFW tags in every shape the re-check meets. Synthetic rows only."""

from __future__ import annotations

import sqlite3

from topos.sanitization.nsfw_classifier import DEFAULT_NSFW_CLASSIFIER_MODEL, HEURISTIC_NSFW_SCORE
from topos.storage.db.migrations.canonical_nsfw_v1 import apply_canonical_nsfw_v1_up

MODEL = DEFAULT_NSFW_CLASSIFIER_MODEL
OTHER_MODEL = "example/other-nsfw-classifier"

# (table, id, content_nsfw, content_nsfw_score, content_nsfw_model) -> the outcome at a 0.91 cutoff.
ROWS = (
    ("journal_entries", "j-coinflip", 1, 0.502, MODEL, "below_threshold"),
    ("journal_entries", "j-at-cutoff", 1, 0.91, MODEL, "below_threshold"),
    ("journal_entries", "j-above", 1, 0.93, MODEL, "kept_above_threshold"),
    ("journal_entries", "j-heuristic", 1, HEURISTIC_NSFW_SCORE, MODEL, "kept_heuristic"),
    ("journal_entries", "j-other-model", 1, 0.6, OTHER_MODEL, "kept_other_model"),
    ("journal_entries", "j-no-model", 1, 0.6, None, "kept_other_model"),
    ("journal_entries", "j-no-score", 1, None, MODEL, "kept_no_score"),
    ("journal_entries", "j-safe-low", 0, 0.3, MODEL, None),
    ("journal_entries", "j-safe-high", 0, 0.99, MODEL, None),
    ("journal_entries", "j-untagged", 0, None, None, None),
    ("conversation_messages", "m-low", 1, 0.7, MODEL, "below_threshold"),
    ("conversation_messages", "m-high", 1, 0.97, MODEL, "kept_above_threshold"),
    ("conversation_messages", "m-safe", 0, 0.88, MODEL, None),
    ("ai_chat_messages", "a-low", 1, 0.55, MODEL, "below_threshold"),
)
ID_COLUMN = {"journal_entries": "entry_id", "conversation_messages": "message_id", "ai_chat_messages": "message_id"}
CLEARED_AT_091 = {(table, rid) for table, rid, *_rest, outcome in ROWS if outcome == "below_threshold"}


def build(conn: sqlite3.Connection, *, tables=("journal_entries", "conversation_messages", "ai_chat_messages")) -> None:
    for table in tables:
        conn.execute(
            f"CREATE TABLE {table} ({ID_COLUMN[table]} TEXT NOT NULL PRIMARY KEY, content TEXT, source_id TEXT)"
        )
    apply_canonical_nsfw_v1_up(conn)
    for table, rid, flag, score, model, _outcome in ROWS:
        if table not in tables:
            continue
        conn.execute(
            f"INSERT INTO {table} ({ID_COLUMN[table]}, content, source_id, content_nsfw, content_nsfw_score, "
            "content_nsfw_model) VALUES (?, ?, ?, ?, ?, ?)",
            (rid, "synthetic text", "src-1", flag, score, model),
        )
    conn.commit()


def dump(conn: sqlite3.Connection) -> dict:
    """Every row of every tagged table, keyed by (table, id)."""
    out = {}
    for table, id_col in ID_COLUMN.items():
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if exists is None:
            continue
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY {id_col}").fetchall():
            out[(table, row[0])] = tuple(row)
    return out


def tags(conn: sqlite3.Connection, table: str, rid: str) -> tuple:
    return tuple(conn.execute(
        f"SELECT content_nsfw, content_nsfw_score, content_nsfw_model FROM {table} WHERE {ID_COLUMN[table]}=?",
        (rid,),
    ).fetchone())
