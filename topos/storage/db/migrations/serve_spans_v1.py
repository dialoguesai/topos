"""Content-free query serve spans (migration 89).

Durations and closed labels only. ``topos/serve/spans.py`` refuses anything else, including a
time-to-first-token on a database span and a duration copied into that field by a caller.
"""
import sqlite3

MIGRATION_ID = "serve_spans_v1"

DDL = (
    """CREATE TABLE IF NOT EXISTS serve_spans (
        span_id TEXT PRIMARY KEY,
        parent_span_id TEXT,
        recorded_at TEXT NOT NULL,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        duration_ms INTEGER NOT NULL,
        ttfb_ms INTEGER,
        outcome TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_serve_spans_recorded_at ON serve_spans(recorded_at)",
)


def create_serve_spans(conn: sqlite3.Connection) -> None:
    for statement in DDL:
        conn.execute(statement)


def apply_serve_spans_v1_up(conn: sqlite3.Connection) -> None:
    create_serve_spans(conn)
    conn.execute("INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)", (MIGRATION_ID,))
    conn.commit()
