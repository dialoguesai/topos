"""Where a journal entry was written: four nullable columns on journal_entries (DISCLOSURE_RULES DR-77).

Additive only. Each column is added when missing and nothing else happens: no row is read, rewritten or deleted,
and an existing entry reads NULL until its app sends the entry again with a position. ``always_run`` like the other
journal column steps (people, place_name, duration), because a database can create ``journal_entries`` after this
step first ran.

The values are owner-only; see ``topos.disclosure.coordinates`` for who reads them.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "journal_entries_coordinates_v1"

#: Column -> SQLite type. Names match the incoming ``grow_journal`` record fields.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("latitude", "REAL"),
    ("longitude", "REAL"),
    ("location_accuracy_m", "REAL"),
    ("location_taken_at", "TEXT"),
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def apply_journal_entries_coordinates_v1_up(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "journal_entries"):
        present = {row[1] for row in conn.execute("PRAGMA table_info(journal_entries)").fetchall()}
        for column, col_type in COLUMNS:
            if column not in present:
                conn.execute(f"ALTER TABLE journal_entries ADD COLUMN {column} {col_type}")
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
