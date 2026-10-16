"""One content-free timing row per owner home-chat turn (HT lane, 15 Oct 2026).

The table's shape lives here, frozen with the migration's checksum; ``topos/home_chat/turn_timings.py`` reuses
``create_home_chat_turn_timings`` so a node whose ledger never ran still gets exactly this table on its first write.
Every column is a number, an epoch-millisecond time, a closed-set word, a random id, a model tag, a version, or a
JSON list of the same; the validator in ``turn_timings.py`` is what keeps it so.
"""
import sqlite3

MIGRATION_ID = "home_chat_turn_timings_v1"

DDL = (
    """CREATE TABLE IF NOT EXISTS home_chat_turn_timings (
        turn_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        submitted_at INTEGER NOT NULL,
        recorded_at INTEGER NOT NULL,
        ttft_ms INTEGER,
        total_ms INTEGER NOT NULL,
        outcome TEXT NOT NULL,
        error_class TEXT,
        model TEXT,
        provider TEXT,
        route TEXT,
        llm_calls INTEGER NOT NULL DEFAULT 0,
        llm_ms INTEGER,
        tool_calls INTEGER NOT NULL DEFAULT 0,
        tool_ms INTEGER,
        scope_queries INTEGER NOT NULL DEFAULT 0,
        scope_query_ms_total INTEGER,
        answer_chars INTEGER,
        answer_sentences INTEGER,
        node_gate_wait_ms INTEGER,
        app_build TEXT,
        node_version TEXT NOT NULL,
        llm_json TEXT NOT NULL DEFAULT '[]',
        tools_json TEXT NOT NULL DEFAULT '[]',
        scope_queries_json TEXT NOT NULL DEFAULT '[]'
    )""",
    "CREATE INDEX IF NOT EXISTS idx_home_chat_turn_timings_submitted ON home_chat_turn_timings(submitted_at)",
    "CREATE INDEX IF NOT EXISTS idx_home_chat_turn_timings_recorded ON home_chat_turn_timings(recorded_at)",
)


def create_home_chat_turn_timings(conn: sqlite3.Connection) -> None:
    for statement in DDL:
        conn.execute(statement)


def apply_home_chat_turn_timings_v1_up(conn: sqlite3.Connection) -> None:
    create_home_chat_turn_timings(conn)
    conn.execute("INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)", (MIGRATION_ID,))
    conn.commit()
