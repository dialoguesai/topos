"""Indexes for the permissions v2 read path, from the 17 Sep 2026 scaling assessment. [S1]

Four `CREATE INDEX IF NOT EXISTS`, each skipped while its table or column is absent,
so the step is idempotent and costs a schema lookup once the index exists. No row is
read or changed: an index is not on the surface an owner review hashes, so no review
goes stale and no pinned digest moves.

- ``entities(is_self) WHERE is_self=1``: the owner's self rows. ``legacy_owner_subjects``
  and ``self_entity_ids`` ask ``WHERE is_self=1`` on every permissions read and every
  attestation fold, and scanned the whole entity table for the handful of rows that
  answer. Partial, so it holds one entry per self row rather than one per entity.
- ``entity_merge_tombstones(merged_into)``: ``composition_revision`` asks which entities
  an attested entity absorbed, by ``merged_into``; the table's primary key is the
  absorbed side, so that was a scan per attested entity per read. The table is created
  on demand by the merge feature and by the protection clock, which is one reason this
  step is ``always_run``.
- ``conversation_messages`` and ``ai_chat_messages`` on ``CONTENT_KEY``: the exact-copy
  check behind ``independent_copy_lineage`` counts rows whose ``content`` equals a
  released message's, in both tables, once per cited message, and scanned all message
  text to do it -- about 0.2 s per message per million rows. ``content`` itself is not
  indexed: an index on the whole text would hold a second copy of every message. SQLite
  has no built-in hash function, and an expression index on an application-defined one
  would refuse every INSERT from a connection that had not registered it, which is any
  script or tool that opens the file. So the key is two built-in deterministic
  expressions -- the character length and the first 64 characters -- and the read path
  adds the full-text equality behind them. The predicate is spelled from ``CONTENT_KEY``
  in ``EvidenceResolver._known_copies`` so the planner matches the index expressions
  exactly; on a database that has not run this step, the same statement scans, as
  before, and answers the same.

``always_run``: the message tables are created by legacy DDL and the tombstone table on
demand, both possibly after this step has run once; the runner re-arms on any DDL, so a
table that appears later still gets its index.

Release note. Registering this stamps the database's schema version, and an engine that
knows fewer migrations refuses a newer stamp. Like specs 63, 69 and 75, it must land at
a release cut; do not run an engine built from a branch carrying it against a node
whose installed engine predates it.
"""
from __future__ import annotations

import sqlite3

MIGRATION_ID = "permissions_read_path_indexes_v1"

# The message-content key, in the spelling both the index and the read path use.
CONTENT_KEY: tuple[str, ...] = ("length(content)", "substr(content,1,64)")

# (table, columns the index needs, index name, DDL)
INDEXES: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    ("entities", ("is_self",), "idx_entities_is_self",
     "CREATE INDEX IF NOT EXISTS idx_entities_is_self ON entities(is_self) WHERE is_self=1"),
    ("entity_merge_tombstones", ("merged_into",), "idx_entity_merge_tombstones_merged_into",
     "CREATE INDEX IF NOT EXISTS idx_entity_merge_tombstones_merged_into ON entity_merge_tombstones(merged_into)"),
    ("conversation_messages", ("content",), "idx_conversation_messages_content_key",
     f"CREATE INDEX IF NOT EXISTS idx_conversation_messages_content_key ON conversation_messages({', '.join(CONTENT_KEY)})"),
    ("ai_chat_messages", ("content",), "idx_ai_chat_messages_content_key",
     f"CREATE INDEX IF NOT EXISTS idx_ai_chat_messages_content_key ON ai_chat_messages({', '.join(CONTENT_KEY)})"),
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def apply_permissions_read_path_indexes_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    for table, columns, _name, sql in INDEXES:
        if not _table_exists(conn, table):
            continue
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if not set(columns) <= present:
            continue
        conn.execute(sql)
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
