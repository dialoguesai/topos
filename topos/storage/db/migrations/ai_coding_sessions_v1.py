"""AI-coding-tool sessions (CONTRACTS.md §2.6): ``ai_chat_messages.share_excluded`` and ``ai_chat_message_facets``.

``share_excluded INTEGER NOT NULL DEFAULT 0`` is additive: every row already stored reads as
shareable, and only the AI-tool sync sets it (``ingestion/ai_tool_sync.py``), for assistant turns
after a session touched a Topos surface. Readers that must not carry such a row add
``share_excluded = 0`` through ``storage/share_excluded.py``.

``ai_chat_message_facets(message_id PRIMARY KEY, tool, project_key, updated_at)`` is owner-only
storage for the project facet (the repo folder's basename and nothing more). It is never joined
into a grantee path and never written into ``metadata_json``, which reaches grantees raw.

Always-run, like ``actor_role_v1``: the ai_chat tables are created lazily by the first import,
possibly after the migrations ran once, so the column add must re-run at every boot. Both DDL
steps are PRAGMA-guarded and cheap. Registering the step moves ``PRAGMA user_version`` past every
engine that predates it, as 75-82 did at their release cuts.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Dict, Optional

logger = logging.getLogger("topos.storage.db.migrations")

MIGRATION_ID = "ai_coding_sessions_v1"

MESSAGES_TABLE = "ai_chat_messages"
COLUMN = "share_excluded"
FACETS_TABLE = "ai_chat_message_facets"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return bool(row)


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


TRIGGER_ON_INSERT = f"{MESSAGES_TABLE}_{COLUMN}_on_insert"
TRIGGER_ON_METADATA = f"{MESSAGES_TABLE}_{COLUMN}_on_metadata"
#: The flag travels WITH the write (CONTRACTS §2.6): a reader marks a record ``_metadata.share_excluded``, the
#: mapper carries that into ``metadata_json``, and the two triggers set the column inside the same statement as
#: the INSERT (or a later rewrite of the metadata), so no window and no later UPDATE separates a stored tainted
#: row from its flag. The column only ever goes 0 → 1 here; nothing clears it. JSON1 is part of every SQLite the
#: node ships with (``json_extract`` is already read by signal_features).
#:
#: Two places, because two mappers write the row. The bundled ``ai_coding_tool`` mapper merges the reader's
#: ``_metadata`` flat (``$.share_excluded``). A runtime install of the source registers
#: ``RuntimeInstalledStorageMapper`` over it (``sources/runtime_install.py``), which keeps it nested
#: (``$._metadata.share_excluded``), and that is what a live node stores. Read only the first and the triggers
#: never fire there.
_FLAG_PATHS = ("$.share_excluded", "$._metadata.share_excluded")
_TRIGGER_WHEN = (
    "NEW.metadata_json IS NOT NULL AND json_valid(NEW.metadata_json) AND ("
    + " OR ".join(f"json_extract(NEW.metadata_json, '{path}') IN (1, 'true')" for path in _FLAG_PATHS)
    + ")"
)
_TRIGGER_BODY = f"""
        WHEN {_TRIGGER_WHEN}
        BEGIN
            UPDATE {MESSAGES_TABLE} SET {COLUMN} = 1 WHERE message_id = NEW.message_id AND {COLUMN} = 0;
        END"""
_TRIGGERS = {
    TRIGGER_ON_INSERT: f"CREATE TRIGGER {TRIGGER_ON_INSERT} AFTER INSERT ON {MESSAGES_TABLE}{_TRIGGER_BODY}",
    TRIGGER_ON_METADATA: (
        f"CREATE TRIGGER {TRIGGER_ON_METADATA} AFTER UPDATE OF metadata_json ON {MESSAGES_TABLE}{_TRIGGER_BODY}"
    ),
}


def _same_sql(a: str, b: str) -> bool:
    return " ".join(a.split()) == " ".join(b.split())


def ensure_share_excluded_triggers(conn: sqlite3.Connection) -> bool:
    """Create the two triggers when the table and the column exist. True when they are in place.

    A trigger stored with another definition (the one that read ``$.share_excluded`` only) is dropped and created
    again in one savepoint, so no write lands between the two without a trigger. One that matches is left alone."""
    if not _table_exists(conn, MESSAGES_TABLE) or COLUMN not in _columns(conn, MESSAGES_TABLE):
        return False
    stored = dict(
        conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (MESSAGES_TABLE,)
        ).fetchall()
    )
    stale = [name for name, sql in _TRIGGERS.items() if not _same_sql(str(stored.get(name) or ""), sql)]
    if not stale:
        return True
    conn.execute("SAVEPOINT share_excluded_triggers")
    try:
        for name in stale:
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            conn.execute(_TRIGGERS[name])
    except BaseException:
        conn.execute("ROLLBACK TO share_excluded_triggers")
        conn.execute("RELEASE share_excluded_triggers")
        raise
    conn.execute("RELEASE share_excluded_triggers")
    return True


def _ensure_marker_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


#: Recorded in ``wiki_schema_migrations`` once the backfill below has run on a table with the column.
BACKFILL_ID = f"{MIGRATION_ID}_unmarked_flag_backfill"
_UNMARKED_WHERE = (
    f"{COLUMN} = 0 AND metadata_json IS NOT NULL AND json_valid(metadata_json) AND ("
    + " OR ".join(f"json_extract(metadata_json, '{path}') IN (1, 'true')" for path in _FLAG_PATHS)
    + ")"
)


def backfill_unmarked_share_excluded(conn: sqlite3.Connection) -> Optional[Dict[str, int]]:
    """Set the column on rows whose own metadata carries the reader's flag but whose column is still 0, and take
    their timeline rows out, as ``_mark_share_excluded`` would have. These are rows written while the triggers read
    ``$.share_excluded`` only, by a sync whose post-write mark did not run. Fail closed on the row's own metadata: a
    touch repair sets the metadata flag false before it clears the column, so a repaired row is never flagged again.

    Embeddings are left in place. Every grantee read already drops a flagged row's embedding by the column
    (``share_excluded_record_filter``), and deleting them needs the ANN-row cleanup the attribution scrub does.

    Once per node: the scan parses every row's JSON (0.5-4.4 s over ~68k rows, warm to cold), and the fixed
    triggers cover every write after it. Nothing is recorded while the table or the column is missing, so the boot that first sees a
    lazily created table still runs it. Returns the counts, or None when it did not run."""
    if not _table_exists(conn, MESSAGES_TABLE) or not {COLUMN, "metadata_json"} <= _columns(conn, MESSAGES_TABLE):
        return None
    _ensure_marker_table(conn)
    if conn.execute("SELECT 1 FROM wiki_schema_migrations WHERE migration_id = ?", (BACKFILL_ID,)).fetchone():
        return None
    ids = [row[0] for row in conn.execute(f"SELECT message_id FROM {MESSAGES_TABLE} WHERE {_UNMARKED_WHERE}")]
    flagged = timeline_removed = 0
    has_timeline = _table_exists(conn, "timeline")
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        marks = ",".join("?" for _ in chunk)
        flagged += conn.execute(
            f"UPDATE {MESSAGES_TABLE} SET {COLUMN} = 1 WHERE message_id IN ({marks}) AND {COLUMN} = 0", chunk
        ).rowcount
        if has_timeline:
            timeline_removed += conn.execute(
                f"DELETE FROM timeline WHERE canonical_table = '{MESSAGES_TABLE}' AND record_id IN ({marks})", chunk
            ).rowcount
    conn.execute("INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)", (BACKFILL_ID,))
    counts = {"flagged": flagged, "timeline_removed": timeline_removed}
    if flagged or timeline_removed:
        logger.warning("[MIGRATION:%s] unmarked share_excluded rows backfilled: %s", MIGRATION_ID, counts)
    return counts


def ensure_share_excluded_column(conn: sqlite3.Connection) -> bool:
    """Add the column to ``ai_chat_messages`` if the table exists and lacks it, with its index and the two
    triggers that set it at write time. True when the column is there."""
    if not _table_exists(conn, MESSAGES_TABLE):
        return False
    if COLUMN not in _columns(conn, MESSAGES_TABLE):
        conn.execute(f"ALTER TABLE {MESSAGES_TABLE} ADD COLUMN {COLUMN} INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{MESSAGES_TABLE}_{COLUMN} ON {MESSAGES_TABLE}({COLUMN}) WHERE {COLUMN} = 1"
    )
    ensure_share_excluded_triggers(conn)
    return True


def ensure_facets_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {FACETS_TABLE} (
            message_id TEXT PRIMARY KEY,
            tool TEXT,
            project_key TEXT,
            updated_at TEXT
        )
        """
    )


def apply_ai_coding_sessions_v1_up(conn: sqlite3.Connection) -> None:
    _ensure_marker_table(conn)
    ensure_share_excluded_column(conn)
    backfill_unmarked_share_excluded(conn)
    # The facets table only beside the ai_chat tables: a node that never made them (messenger only) keeps no
    # ai_chat% table at all (tests/core/test_routine_lane_row_tools.py); the sync runner creates it before its
    # first write (ai_tool_sync._ensure_storage), after the canonical tables exist.
    if _table_exists(conn, MESSAGES_TABLE):
        ensure_facets_table(conn)
    conn.execute("INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)", (MIGRATION_ID,))
    conn.commit()
