"""OD-52 P1: activity_events records the door that wrote each row.

Browser visits and the other activity rows carried no writer column at all, so a
visit an approved third-party app wrote could not be told from one the owner's own
browser plugin captured (JOURNAL_AND_BROWSER_SOURCES_DESIGN.md §4.3). This adds
the three writer columns ``ai_chat_messages`` already has:

- ``writer_class``: the door that wrote the row (``features/provenance/writer_class.py``);
- ``writer_app_id``: the capture app behind a stamped ``owner_app`` relay write;
- ``writer_dataset_id``: the dataset the door wrote into (an activity row has no
  dataset column of its own).

``canonical_store.WRITER_CLASS_TABLES`` names the table. With the switch
``TOPOS_ACTIVITY_WRITER_CLASS`` on (the default since October 2026), every write goes through
``_upsert_recording_writer``: a non-owner write cannot replace a row an owner door
wrote, and an owner door takes over a row a non-owner wrote first. The columns
exist either way; off, nothing writes them.

No backfill, on purpose. The door that wrote an existing row is recorded nowhere,
and writing a class onto it now would forge provenance (the rule #68 applied to
``actor_role``). Existing rows keep NULL, which the store and the role gate read as
"no door recorded".

``always_run`` and PRAGMA-guarded like activity_events_content_v1: the adds are
cheap, and they re-assert after the legacy ``CREATE TABLE IF NOT EXISTS
activity_events`` DDL. Registering it stamps the schema version past any engine
that predates it, so it lands at a release cut like 75, 76, 78 and 79.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "activity_writer_columns_v1"

WRITER_COLUMNS = (
    ("writer_class", "TEXT"),
    ("writer_app_id", "TEXT"),
    ("writer_dataset_id", "TEXT"),
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def apply_activity_writer_columns_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    if _table_exists(conn, "activity_events"):
        columns = {row[1] for row in conn.execute("PRAGMA table_info(activity_events)").fetchall()}
        for column, col_type in WRITER_COLUMNS:
            if column not in columns:
                conn.execute(f"ALTER TABLE activity_events ADD COLUMN {column} {col_type}")
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
