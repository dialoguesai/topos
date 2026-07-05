"""Incremental statistics engine schema (P2 dense-intelligence upgrade).

stat_definitions — registry of statistics folded over canonical batches
stat_state      — associative-mergeable state per (stat, group, daily bucket)
stat_seen       — recent record-id dedup guard against batch replays
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "wiki_stats_v1"


def apply_wiki_stats_v1_up(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS stat_definitions (
            stat_id TEXT PRIMARY KEY,
            canonical_table TEXT NOT NULL,
            group_by TEXT NOT NULL DEFAULT 'none',
            stat_kind TEXT NOT NULL,
            value_expr TEXT,
            dimension TEXT NOT NULL DEFAULT 'memory',
            decay_half_life_days REAL,
            windows_json TEXT NOT NULL DEFAULT '["all"]',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS stat_state (
            stat_id TEXT NOT NULL,
            group_key TEXT NOT NULL DEFAULT '',
            bucket_date TEXT NOT NULL DEFAULT '',
            state_json TEXT NOT NULL,
            watermark TEXT,
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (stat_id, group_key, bucket_date)
        );
        CREATE INDEX IF NOT EXISTS idx_stat_state_stat ON stat_state(stat_id, bucket_date);

        CREATE TABLE IF NOT EXISTS stat_seen (
            stat_id TEXT NOT NULL,
            record_id TEXT NOT NULL,
            seen_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (stat_id, record_id)
        );
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_wiki_stats_v1_down(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS stat_seen;
        DROP TABLE IF EXISTS stat_state;
        DROP TABLE IF EXISTS stat_definitions;
        """
    )
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
