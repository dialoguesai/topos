"""Add ``duration_ms`` to ``query_artifacts`` so a turn's cost is recorded, not guessed.

§09 ships four quality metrics and no latency metric, and the artifact row — the one
durable per-turn record the query pipeline already writes — had no duration column
(``artifact_id, session_id, cache_key, public_result_json, retrieval_fingerprint,
game_layer_strategy, created_at``). The question "what did the redesign cost per
request" had no answer on disk to read, so it was answered "unmeasured".

A NEW migration rather than an edit to ``wiki_mvp_phase0``: that one is applied on
every live node, and editing an applied migration in place strands a database whose
``user_version`` is already past it — the column would simply never appear there.

Integer milliseconds, nullable. Nullable is the honest default: every row written
before this ships genuinely has no measurement, and a ``0`` or a ``-1`` would read as
one. The percentile script counts NULLs as absent rather than as zero for the same
reason.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "query_artifacts_duration_ms_v1"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    if not _table_exists(conn, table):
        return
    names = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in names:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def apply_query_artifacts_duration_ms_v1_up(conn: sqlite3.Connection) -> None:
    _add_column_if_missing(conn, "query_artifacts", "duration_ms", "INTEGER")
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_query_artifacts_duration_ms_v1_down(conn: sqlite3.Connection) -> None:
    # The column is left in place: SQLite's DROP COLUMN is recent and conditional, and
    # a nullable integer costs nothing to leave behind. Only the ledger stamp reverses.
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
