"""Stamp the canonical table onto mentions that were written without one.

``entity_mentions.canonical_table`` is what every table-scoped read and delete
travels along. A mention without it is invisible to a table-scoped grant, a
table purge and a disclosure sweep alike — it is not withheld, it is simply not
found, which is the worse failure because nothing reports it.

Measured on a live node 2026-08-27: 619 unstamped mentions, from
``browser_visits`` (566), ``github_activity`` (52) and ``chatgpt`` (1) — none of
them the journal fan-out, so the ingest-time stamp fix could not reach them.
They come from connectors that never set the field.

No re-extraction is needed, which is the point of doing it here: the mention's
own ``record_id`` resolves against exactly one canonical table, so the stamp is
recovered rather than recomputed. On the same node all 619 resolved — 618 to
``activity_events`` and 1 to ``ai_chat_messages`` — with none ambiguous.

A mention whose record_id matches no canonical row, or matches more than one, is
left unstamped. An invented stamp would route a read to the wrong table, which
is worse than the absence this repairs.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

MIGRATION_ID = "mention_table_stamp_backfill_v1"

# Canonical tables and the column their primary key lives in.
_CANDIDATES = (
    ("activity_events", "event_id"),
    ("browser_visits", "visit_id"),
    ("ai_chat_messages", "message_id"),
    ("conversation_messages", "message_id"),
    ("journal_entries", "entry_id"),
    ("calendar_events", "event_id"),
    ("location_events", "event_id"),
    ("profile_records", "record_id"),
    ("financial_transactions", "transaction_id"),
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def backfill_mention_table_stamps(
    conn: sqlite3.Connection, *, dry_run: bool = False
) -> dict:
    counts = {"scanned": 0, "stamped": 0, "ambiguous": 0, "unresolved": 0}
    if not _table_exists(conn, "entity_mentions"):
        return counts
    live = [(t, col) for t, col in _CANDIDATES if _table_exists(conn, t)]
    rows = conn.execute(
        "SELECT mention_id, record_id FROM entity_mentions"
        " WHERE COALESCE(canonical_table,'') = '' AND COALESCE(record_id,'') <> ''"
    ).fetchall()

    updates = []
    for mention_id, record_id in rows:
        counts["scanned"] += 1
        matches = []
        for table, id_col in live:
            try:
                hit = conn.execute(
                    f"SELECT 1 FROM {table} WHERE {id_col}=? LIMIT 1", (str(record_id),)
                ).fetchone()
            except sqlite3.Error:
                continue
            if hit:
                matches.append(table)
                if len(matches) > 1:
                    break
        if not matches:
            counts["unresolved"] += 1
        elif len(matches) > 1:
            # Two canonical tables claiming one id is an identity problem of its
            # own; guessing between them would route reads to the wrong one.
            counts["ambiguous"] += 1
        else:
            counts["stamped"] += 1
            updates.append((matches[0], str(mention_id)))

    if dry_run or not updates:
        return counts
    conn.executemany(
        "UPDATE entity_mentions SET canonical_table=? WHERE mention_id=?", updates
    )
    return counts


def apply_mention_table_stamp_backfill_v1_up(conn: sqlite3.Connection) -> None:
    counts = backfill_mention_table_stamps(conn)
    if counts["stamped"]:
        logger.info(
            "mention table stamps: %d of %d recovered (%d ambiguous, %d unresolved)",
            counts["stamped"], counts["scanned"], counts["ambiguous"], counts["unresolved"],
        )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_mention_table_stamp_backfill_v1_down(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
