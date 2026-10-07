"""1.5.0: the mark of an Off-limits entry the upgrade carried, as a registered schema step.

`entity_blackholes.carried_waiting_json` says what of an entry is carried and waiting (`blackhole.WAITING_COLUMN`):
the entry itself, made by the step that carries the older per-person "exclude" choices into Off-limits and not yet
acted on by the owner, or the names that step added to an entry the owner had made. Only a build that knows the
mark reads such an entry rightly. An older build reads a carried entry as an ordinary one, with every handle and the
contact id as names: its read-time scan then withholds the owner's own messages from his own client, and a clean-up
he starts there deletes what the mark was written to keep (the second re-check, R3-M4).

The column was added in place by the first write that needed it, with no schema version, so nothing stopped an
older build from opening such a database. Registered here, it moves the schema version, and the existing downgrade
guard (`ensure_migrations_applied`) refuses to open the database on any build that predates it, as it does for
every earlier schema step. That refusal is this step's whole purpose.

One nullable column, PRAGMA-guarded. No row is read or changed: an entry has a mark only when the carry step
writes one. `always_run` like 75 to 80: the add is cheap and re-asserts if the table is made after this ran.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "off_limits_carried_waiting_v1"

TABLE = "entity_blackholes"
COLUMN = "carried_waiting_json"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def apply_off_limits_carried_waiting_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    if _table_exists(conn, TABLE):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})").fetchall()}
        if COLUMN not in columns:
            conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} TEXT")
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
