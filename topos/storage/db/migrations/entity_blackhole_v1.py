"""Entity black hole (M0): per-entity off-limits flag + rebuild notifications.

A black hole is the owner saying "this entity is mine alone". Unlike an owner
*exclusion* (`wiki_lifecycle_v1`), which tombstones and purges, a black hole is
strictly additive: the entity and all its content stay fully intact and fully
visible to the owner's own UI. What changes is who may see it — every non-owner
caller (the owner's own third-party MCP agents, routines, grantees, plugins) is
denied, and content mentioning it may only be processed by secure models.

Two tables:

  entity_blackholes      the flag itself. Keyed by normalized name (always
                         known, and survives an entity being re-minted after a
                         merge/scrub) with entity_id carried alongside for the
                         id-join hot path. A name may be black-holed before any
                         entity exists for it — pre-emptive protection — in
                         which case entity_id is '' until the resolver binds it.

  blackhole_notifications  D4's notify-first contract. Flipping the flag raises
                         a `rebuild_needed` notification *before* the rebuild
                         runs, so the owner is told the hide is not yet complete
                         across derived artifacts; it resolves when the rebuild
                         lands.

`processing_tier` carries D1: 'secure' admits the local adapters plus the Red
Pill TEE; 'local_only' is the stricter dial for entities that must never leave
the device at all.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "entity_blackhole_v1"

PROCESSING_TIERS = ("secure", "local_only")
REBUILD_STATES = ("pending", "running", "complete", "failed")


def apply_entity_blackhole_v1_up(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS entity_blackholes (
            blackhole_id TEXT PRIMARY KEY,
            entity_id TEXT NOT NULL DEFAULT '',
            normalized_name TEXT NOT NULL,
            canonical_name TEXT,
            aliases_json TEXT NOT NULL DEFAULT '[]',
            processing_tier TEXT NOT NULL DEFAULT 'secure',
            rebuild_state TEXT NOT NULL DEFAULT 'pending',
            note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            created_by TEXT NOT NULL DEFAULT 'owner'
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_blackholes_name
            ON entity_blackholes(normalized_name);
        CREATE INDEX IF NOT EXISTS idx_entity_blackholes_entity
            ON entity_blackholes(entity_id);
        CREATE INDEX IF NOT EXISTS idx_entity_blackholes_rebuild
            ON entity_blackholes(rebuild_state);

        CREATE TABLE IF NOT EXISTS blackhole_notifications (
            notification_id TEXT PRIMARY KEY,
            blackhole_id TEXT NOT NULL,
            entity_id TEXT NOT NULL DEFAULT '',
            normalized_name TEXT NOT NULL,
            kind TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'open',
            message TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            resolved_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_blackhole_notifications_state
            ON blackhole_notifications(state, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_blackhole_notifications_blackhole
            ON blackhole_notifications(blackhole_id);
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_entity_blackhole_v1_down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS blackhole_notifications")
    conn.execute("DROP TABLE IF EXISTS entity_blackholes")
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
