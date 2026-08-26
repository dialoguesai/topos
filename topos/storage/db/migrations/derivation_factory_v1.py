"""Training-data factory + self-gating state (W-B: owner decisions 2026-08-26).

Three tables:

``derivation_training_ledger`` — one row per parsed assertion the pipeline
judged, ACCEPTED OR NOT. The production job previously kept only accepted
facts; rejected assertions (with verifier reasons) are the hard negatives a
future classifier distillation needs, and owner verdicts on stored facts are
its gold. Quotes + record refs make span supervision recoverable. Prefilter
MISSES are deliberately absent: the prefilter is versioned and deterministic,
so routing negatives are reconstructible on demand at training time.

``pack_yield`` — per-pack daily counters (prefilter hits, LLM calls,
assertions, accepted, written). Powers: the lens catalog's cost estimates,
the dry-well nudge (calls without yield), and the self-gating trial trigger
(hits accumulating on a disabled pack).

``pack_offers`` — self-gating state machine: an enable offer minted by a
passed shadow trial, or a disable nudge minted by a dry well. The OWNER
decides; the node only ever offers.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "derivation_factory_v1"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def apply_derivation_factory_v1_up(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "wiki_schema_migrations"):
        conn.execute(
            "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
            (MIGRATION_ID,),
        )
    if not _table_exists(conn, "derivation_training_ledger"):
        conn.execute(
            """
            CREATE TABLE derivation_training_ledger (
                ledger_id TEXT PRIMARY KEY,
                ts TEXT NOT NULL DEFAULT (datetime('now')),
                stage TEXT NOT NULL DEFAULT 'ingest',     -- ingest | trial
                pack_id TEXT NOT NULL,
                pack_version TEXT,
                template_version TEXT,
                extract_model TEXT,
                verifier_model TEXT,
                source_table TEXT,
                record_id TEXT,
                actor_role TEXT,
                predicate TEXT NOT NULL,
                value_json TEXT,
                about TEXT,
                occurrence TEXT,
                quote TEXT,
                confidence REAL,
                vstatus TEXT,                              -- accepted|rejected|rerouted|skipped|error|grounding_reject
                vreason TEXT,
                written_object_id TEXT                     -- NULL when nothing was stored
            )
            """
        )
        conn.execute(
            "CREATE INDEX idx_dtl_pack_ts ON derivation_training_ledger(pack_id, ts)"
        )
        conn.execute(
            "CREATE INDEX idx_dtl_vstatus ON derivation_training_ledger(vstatus)"
        )
    if not _table_exists(conn, "pack_yield"):
        conn.execute(
            """
            CREATE TABLE pack_yield (
                pack_id TEXT NOT NULL,
                day TEXT NOT NULL,                         -- YYYY-MM-DD
                prefilter_hits INTEGER NOT NULL DEFAULT 0,
                llm_calls INTEGER NOT NULL DEFAULT 0,
                assertions INTEGER NOT NULL DEFAULT 0,
                accepted INTEGER NOT NULL DEFAULT 0,
                written INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (pack_id, day)
            )
            """
        )
    if not _table_exists(conn, "pack_offers"):
        conn.execute(
            """
            CREATE TABLE pack_offers (
                offer_id TEXT PRIMARY KEY,
                pack_id TEXT NOT NULL,
                kind TEXT NOT NULL,                        -- enable_offer | disable_nudge
                status TEXT NOT NULL DEFAULT 'pending',    -- pending | accepted | dismissed
                stats_json TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        conn.execute("CREATE INDEX idx_pack_offers_pack ON pack_offers(pack_id, status)")
    conn.commit()
