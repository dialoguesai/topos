"""Content-free, node-local measurements of pack model calls. No historic backfill."""
import sqlite3

MIGRATION_ID = 'derivation_processing_v1'


def apply_derivation_processing_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute('''CREATE TABLE IF NOT EXISTS derivation_processing (
        call_id TEXT PRIMARY KEY,
        pack_id TEXT NOT NULL,
        model TEXT NOT NULL,
        phase TEXT NOT NULL CHECK (phase IN ('extraction', 'verification')),
        mode TEXT NOT NULL,
        started_at TEXT NOT NULL,
        elapsed_ms REAL NOT NULL CHECK (elapsed_ms >= 0),
        failed INTEGER NOT NULL CHECK (failed IN (0, 1)),
        prompt_tokens INTEGER,
        completion_tokens INTEGER,
        load_ms REAL,
        prefill_ms REAL,
        decode_ms REAL
    )''')
    conn.execute('CREATE INDEX IF NOT EXISTS derivation_processing_time ON derivation_processing(started_at, pack_id)')
    conn.execute('''CREATE TABLE IF NOT EXISTS derivation_processing_state (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        tracking_since TEXT NOT NULL
    )''')
    conn.execute("INSERT OR IGNORE INTO derivation_processing_state VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ','now'))")
    conn.execute('INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)', (MIGRATION_ID,))
    conn.commit()
