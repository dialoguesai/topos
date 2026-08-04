"""C5 / Wave B3: drop empty ``signal_summaries`` zombie table.

``dimension_summary`` writes living-document briefs
(``signal_dimension_briefs``), never this table. GC already marked it
deprecated; this migration finishes the retirement so nothing creates a
zombie schema. Phase0 still creates the table for historical checksum
stability; this drop runs after phase0 on every fresh/live migrate.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "signal_summaries_drop_v1"


def apply_signal_summaries_drop_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS signal_summaries")
    try:
        conn.execute(
            "DELETE FROM wiki_table_catalog WHERE table_name=?",
            ("signal_summaries",),
        )
    except sqlite3.OperationalError:
        pass
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
