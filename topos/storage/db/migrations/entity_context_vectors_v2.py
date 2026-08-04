"""Source diversity on mention-context centroids (PLAN §3.1a defect A).

``mention_sample`` counts distinct mentioned *records*, and that turned out not
to be a diversity measure at all: one browser page revisited three times is
three record ids carrying one document, so five people named on that page got
byte-identical centroids and a pairwise cosine of exactly 1.0000.

``source_sample`` counts distinct source *documents* — the thing the floor
actually needed to gate on. Kept as a separate column rather than a redefinition
of ``mention_sample`` so the two numbers stay legible side by side: their ratio
is precisely the re-read factor that hid the defect.

A NEW migration rather than an edit to ``entity_context_vectors_v1``: live nodes
have already applied 49 and 50, and rewriting an applied migration in place
leaves those databases unreconcilable with the registry.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "entity_context_vectors_v2"


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def apply_entity_context_vectors_v2_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    if not _has_column(conn, "entity_context_vectors", "source_sample"):
        conn.execute(
            "ALTER TABLE entity_context_vectors "
            "ADD COLUMN source_sample INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_entity_context_vectors_v2_down(conn: sqlite3.Connection) -> None:
    # Centroids are Layer-4 derived and rebuilt nightly, so dropping the column
    # by rebuilding the table loses nothing that cannot be recomputed.
    conn.execute("DROP TABLE IF EXISTS entity_context_vectors")
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id IN (?, ?)", (MIGRATION_ID, "entity_context_vectors_v1"))
    conn.commit()
