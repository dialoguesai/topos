"""Attention triage verdict storage (PLAN_ATTENTION_TRIAGE.md M2.4).

One row per (day, record) triage verdict: the 2x2 quadrant outcome plus the
formal instruments that grounded it (S10: compression novelty, kNN surprisal,
per-item Bayesian surprise, PPR attachment). `experienced`/`engagement_kind`
double as the uniform experience marker (M2.2) until a dedicated canonical
annotation exists. Verdicts are run-time judgments, versioned by
`routine_version` — never ingest-time facts.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "attention_triage_v1"


def apply_attention_triage_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS triage_verdicts (
            day TEXT NOT NULL,
            record_id TEXT NOT NULL,
            canonical_table TEXT,
            source_id TEXT,
            verdict TEXT NOT NULL,
            aligned INTEGER,
            experienced INTEGER,
            engagement_kind TEXT,
            floor_reason TEXT,
            comp_novelty REAL,
            knn_surprisal REAL,
            item_kl REAL,
            attachment REAL,
            gen_score REAL,
            align_mass REAL,
            visit_count INTEGER DEFAULT 1,
            grounds_json TEXT,
            routine_version TEXT NOT NULL DEFAULT 'triage_v1',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (day, record_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_triage_verdicts_day ON triage_verdicts(day, verdict)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
