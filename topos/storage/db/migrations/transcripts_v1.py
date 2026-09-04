"""Transcripts canonical lane — session + speakers + segments.

VoxTerm was parked on ``conversation_messages`` as a stopgap (speaker labels
became contacts; enrichment never ran). This adds a first-class ``transcripts``
group so YouTube captions and later meeting tools land as ambient speech
without implying the owner talked or that Speaker N is a person.

A brand-new table appears for free on every node via
``CREATE TABLE IF NOT EXISTS`` — no ALTER, no backfill required.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "transcripts_v1"


def apply_transcripts_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS transcripts (
            transcript_id        TEXT PRIMARY KEY,
            dataset_id           TEXT,
            title                TEXT,
            origin_url           TEXT,
            origin_kind          TEXT,
            started_at           TEXT,
            ended_at             TEXT,
            duration_sec         REAL,
            language_code        TEXT,
            asr_model            TEXT,
            asr_quality          TEXT,
            is_generated         INTEGER,
            media_ref            TEXT,
            participation_mode   TEXT NOT NULL DEFAULT 'ambient',
            source_id            TEXT NOT NULL,
            source_record_id     TEXT,
            ingested_at          TEXT NOT NULL DEFAULT (datetime('now')),
            sync_batch_id        TEXT,
            metadata_json        TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_transcripts_source
            ON transcripts(source_id, started_at);

        CREATE TABLE IF NOT EXISTS transcript_speakers (
            speaker_id               TEXT PRIMARY KEY,
            transcript_id            TEXT NOT NULL,
            dataset_id               TEXT,
            label                    TEXT,
            display_name             TEXT,
            contact_id               TEXT,
            is_owner                 INTEGER NOT NULL DEFAULT 0,
            attribution_source       TEXT,
            attribution_confidence   REAL,
            source_id                TEXT NOT NULL,
            source_record_id         TEXT,
            ingested_at              TEXT NOT NULL DEFAULT (datetime('now')),
            sync_batch_id            TEXT,
            metadata_json            TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_transcript_speakers_transcript
            ON transcript_speakers(transcript_id);

        CREATE TABLE IF NOT EXISTS transcript_segments (
            segment_id       TEXT PRIMARY KEY,
            transcript_id    TEXT NOT NULL,
            dataset_id       TEXT,
            speaker_id       TEXT,
            speaker_label    TEXT,
            content          TEXT,
            start_sec        REAL,
            duration_sec     REAL,
            event_at         TEXT,
            actor_role       TEXT NOT NULL DEFAULT 'ambient',
            is_from_self     INTEGER NOT NULL DEFAULT 0,
            asr_confidence   REAL,
            source_id        TEXT NOT NULL,
            source_record_id TEXT,
            ingested_at      TEXT NOT NULL DEFAULT (datetime('now')),
            sync_batch_id    TEXT,
            metadata_json    TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_transcript_segments_transcript
            ON transcript_segments(transcript_id, start_sec);
        CREATE INDEX IF NOT EXISTS idx_transcript_segments_source
            ON transcript_segments(source_id, event_at);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
