"""
Wiki MVP Phase 0 SQLite schema migration.

Graph storage: dedicated graph_nodes/graph_edges tables (not unified relationship_edges)
for Phase 3 graph UI; relationship_edges remains a signal dimension object table.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "wiki_mvp_phase0"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, col_type: str) -> None:
    if not _table_exists(conn, table):
        return
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def up(conn: sqlite3.Connection) -> None:
    apply_wiki_mvp_phase0_up(conn)


def down(conn: sqlite3.Connection) -> None:
    apply_wiki_mvp_phase0_down(conn)


def apply_wiki_mvp_phase0_up(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS wiki_canonical_records (
            record_id TEXT PRIMARY KEY,
            table_name TEXT NOT NULL,
            source_id TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS signal_facts (
            fact_id TEXT PRIMARY KEY,
            dimension TEXT,
            source_id TEXT,
            record_id TEXT,
            model TEXT,
            provider TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_signal_facts_source_created ON signal_facts(source_id, created_at);

        CREATE TABLE IF NOT EXISTS signal_scores (
            score_id TEXT PRIMARY KEY,
            dimension TEXT,
            source_id TEXT,
            record_id TEXT,
            model TEXT,
            provider TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS signal_tags (
            tag_id TEXT PRIMARY KEY,
            dimension TEXT,
            source_id TEXT,
            record_id TEXT,
            tag TEXT,
            model TEXT,
            provider TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS signal_summaries (
            summary_id TEXT PRIMARY KEY,
            dimension TEXT,
            source_id TEXT,
            record_id TEXT,
            model TEXT,
            provider TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS signal_dimension_profiles (
            profile_id TEXT PRIMARY KEY,
            signal_dimension TEXT NOT NULL,
            source_id TEXT,
            profile_json TEXT NOT NULL,
            model TEXT,
            provider TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_signal_dimension_profiles_dimension ON signal_dimension_profiles(signal_dimension);

        CREATE TABLE IF NOT EXISTS message_entities (
            entity_id TEXT PRIMARY KEY,
            record_id TEXT,
            source_id TEXT,
            entity_text TEXT,
            model TEXT,
            provider TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS message_topics (
            topic_id TEXT PRIMARY KEY,
            record_id TEXT,
            source_id TEXT,
            topic TEXT,
            model TEXT,
            provider TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS message_emotions (
            emotion_id TEXT PRIMARY KEY,
            record_id TEXT,
            source_id TEXT,
            model TEXT,
            provider TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS message_sentiment (
            sentiment_id TEXT PRIMARY KEY,
            record_id TEXT,
            source_id TEXT,
            model TEXT,
            provider TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS user_goals (
            goal_id TEXT PRIMARY KEY,
            record_id TEXT,
            source_id TEXT,
            goal_text TEXT,
            model TEXT,
            provider TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS relationship_edges (
            edge_id TEXT PRIMARY KEY,
            src_node_id TEXT,
            dst_node_id TEXT,
            source_id TEXT,
            model TEXT,
            provider TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS data_health_dimension (
            health_id TEXT PRIMARY KEY,
            signal_dimension TEXT,
            source_id TEXT,
            score REAL,
            model TEXT,
            provider TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS signal_embeddings (
            embedding_id TEXT PRIMARY KEY,
            record_id TEXT,
            source_id TEXT,
            signal_dimension TEXT,
            model TEXT,
            provider TEXT,
            dims INTEGER,
            text_preview TEXT,
            provenance_json TEXT,
            vector_blob BLOB,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_signal_embeddings_source_created ON signal_embeddings(source_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_signal_embeddings_dimension ON signal_embeddings(signal_dimension);

        CREATE TABLE IF NOT EXISTS graph_nodes (
            node_id TEXT PRIMARY KEY,
            node_type TEXT,
            label TEXT,
            metadata_json TEXT NOT NULL,
            source_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS graph_edges (
            edge_id TEXT PRIMARY KEY,
            src_node_id TEXT NOT NULL,
            dst_node_id TEXT NOT NULL,
            edge_type TEXT,
            weight REAL,
            metadata_json TEXT NOT NULL,
            source_id TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS query_sessions (
            session_id TEXT PRIMARY KEY,
            requester_id TEXT,
            intent_hash TEXT,
            envelope_json TEXT NOT NULL,
            ttl_expires_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS query_artifacts (
            artifact_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            cache_key TEXT,
            public_result_json TEXT,
            retrieval_fingerprint TEXT,
            game_layer_strategy TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (session_id) REFERENCES query_sessions(session_id)
        );
        CREATE INDEX IF NOT EXISTS idx_query_artifacts_session ON query_artifacts(session_id);

        CREATE TABLE IF NOT EXISTS query_audit_events (
            event_id TEXT PRIMARY KEY,
            session_id TEXT,
            event_type TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_query_audit_session ON query_audit_events(session_id);
        """
    )

    for table in ("ai_chat_messages", "conversation_messages", "activity_events"):
        _add_column_if_missing(conn, table, "sync_batch_id", "TEXT")

    conn.execute("INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)", (MIGRATION_ID,))
    conn.commit()


def apply_wiki_mvp_phase0_down(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS query_artifacts;
        DROP TABLE IF EXISTS query_sessions;
        DROP TABLE IF EXISTS query_audit_events;
        DROP TABLE IF EXISTS graph_edges;
        DROP TABLE IF EXISTS graph_nodes;
        DROP TABLE IF EXISTS signal_embeddings;
        DROP TABLE IF EXISTS data_health_dimension;
        DROP TABLE IF EXISTS relationship_edges;
        DROP TABLE IF EXISTS user_goals;
        DROP TABLE IF EXISTS message_sentiment;
        DROP TABLE IF EXISTS message_emotions;
        DROP TABLE IF EXISTS message_topics;
        DROP TABLE IF EXISTS message_entities;
        DROP TABLE IF EXISTS signal_dimension_profiles;
        DROP TABLE IF EXISTS signal_summaries;
        DROP TABLE IF EXISTS signal_tags;
        DROP TABLE IF EXISTS signal_scores;
        DROP TABLE IF EXISTS signal_facts;
        DROP TABLE IF EXISTS wiki_canonical_records;
        """
    )
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
