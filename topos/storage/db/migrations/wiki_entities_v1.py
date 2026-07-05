"""Entity spine schema (P3 dense-intelligence upgrade).

entities        — resolved canonical entities (people, orgs, places, projects)
entity_mentions — provenance: every resolved surface occurrence
entity_edges    — typed, time-decayed relations between entities
entity_review   — low-margin resolution candidates awaiting owner confirmation
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "wiki_entities_v1"


def apply_wiki_entities_v1_up(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS entities (
            entity_id TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            canonical_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            aliases_json TEXT NOT NULL DEFAULT '[]',
            identifiers_json TEXT NOT NULL DEFAULT '[]',
            embedding_blob BLOB,
            is_self INTEGER NOT NULL DEFAULT 0,
            contact_id TEXT,
            first_seen TEXT,
            last_seen TEXT,
            mention_count INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_entities_normalized ON entities(normalized_name, entity_type);
        CREATE INDEX IF NOT EXISTS idx_entities_contact ON entities(contact_id) WHERE contact_id IS NOT NULL;

        CREATE TABLE IF NOT EXISTS entity_mentions (
            mention_id TEXT PRIMARY KEY,
            entity_id TEXT NOT NULL,
            record_id TEXT NOT NULL,
            source_id TEXT,
            canonical_table TEXT,
            surface_text TEXT,
            confidence REAL,
            event_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_entity_mentions_entity ON entity_mentions(entity_id);
        CREATE INDEX IF NOT EXISTS idx_entity_mentions_record ON entity_mentions(record_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_mentions_unique
            ON entity_mentions(entity_id, record_id, surface_text);

        CREATE TABLE IF NOT EXISTS entity_edges (
            edge_id TEXT PRIMARY KEY,
            src_entity_id TEXT NOT NULL,
            dst_entity_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 0,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            last_event_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (src_entity_id, dst_entity_id, edge_type)
        );
        CREATE INDEX IF NOT EXISTS idx_entity_edges_src ON entity_edges(src_entity_id, weight DESC);
        CREATE INDEX IF NOT EXISTS idx_entity_edges_dst ON entity_edges(dst_entity_id, weight DESC);

        CREATE TABLE IF NOT EXISTS entity_review (
            review_id TEXT PRIMARY KEY,
            surface_text TEXT NOT NULL,
            candidate_entity_id TEXT NOT NULL,
            score REAL NOT NULL,
            record_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_wiki_entities_v1_down(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DROP TABLE IF EXISTS entity_review;
        DROP TABLE IF EXISTS entity_edges;
        DROP TABLE IF EXISTS entity_mentions;
        DROP TABLE IF EXISTS entities;
        """
    )
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
