"""Extraction artifacts migration (Layer 2.5)."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "explicit_signal_ia_extraction_artifacts_v1"


def apply_extraction_artifacts_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    if conn.execute(
        "SELECT 1 FROM wiki_schema_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    ).fetchone():
        return

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS extraction_artifacts (
            artifact_id TEXT PRIMARY KEY,
            artifact_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            dimension_affinity_json TEXT NOT NULL DEFAULT '[]',
            source_refs_json TEXT NOT NULL DEFAULT '[]',
            source_ref_hash TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            extracted_at TEXT NOT NULL,
            extractor_version TEXT NOT NULL DEFAULT 'v1'
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_extraction_artifacts_source_hash
            ON extraction_artifacts(artifact_type, source_ref_hash);
        CREATE INDEX IF NOT EXISTS idx_extraction_artifacts_type
            ON extraction_artifacts(artifact_type, extracted_at DESC);
        """
    )
    conn.execute(
        "INSERT INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
