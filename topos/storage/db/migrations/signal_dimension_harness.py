"""Signal dimension test harness: journal, profile, financial, location canonical tables."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "signal_dimension_harness"


def apply_signal_dimension_harness_up(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS journal_entries (
            entry_id TEXT PRIMARY KEY,
            entry_at TEXT,
            mood_tag TEXT,
            category TEXT,
            content TEXT,
            people TEXT,
            place_name TEXT,
            source_id TEXT NOT NULL,
            source_record_id TEXT,
            ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
            sync_batch_id TEXT,
            metadata_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_journal_entries_source ON journal_entries(source_id, entry_at);

        CREATE TABLE IF NOT EXISTS profile_records (
            record_id TEXT PRIMARY KEY,
            record_type TEXT NOT NULL,
            title TEXT,
            organization TEXT,
            start_date TEXT,
            end_date TEXT,
            description TEXT,
            source_id TEXT NOT NULL,
            source_record_id TEXT,
            ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
            sync_batch_id TEXT,
            metadata_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_profile_records_source ON profile_records(source_id, record_type);

        CREATE TABLE IF NOT EXISTS financial_transactions (
            transaction_id TEXT PRIMARY KEY,
            account_type TEXT NOT NULL,
            account_name TEXT,
            posted_at TEXT,
            amount REAL,
            currency TEXT DEFAULT 'USD',
            category TEXT,
            description TEXT,
            source_id TEXT NOT NULL,
            source_record_id TEXT,
            ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
            sync_batch_id TEXT,
            metadata_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_financial_transactions_source ON financial_transactions(source_id, account_type);

        CREATE TABLE IF NOT EXISTS location_events (
            event_id TEXT PRIMARY KEY,
            place_name TEXT,
            city TEXT,
            region TEXT,
            country TEXT,
            event_at TEXT,
            event_type TEXT,
            source_id TEXT NOT NULL,
            source_record_id TEXT,
            ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
            sync_batch_id TEXT,
            metadata_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_location_events_source ON location_events(source_id, event_at);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_signal_dimension_harness_down(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS location_events;
        DROP TABLE IF EXISTS financial_transactions;
        DROP TABLE IF EXISTS profile_records;
        DROP TABLE IF EXISTS journal_entries;
        """
    )
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
