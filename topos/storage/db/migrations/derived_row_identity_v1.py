"""Collapse duplicate derived rows and re-key the survivors to a stable id.

The derived-table writers keyed each row on a fresh ``uuid4()``, so the same
fact re-derived on a later pass was INSERTed beside its predecessor instead of
replacing it (``topos/storage/derived_row_identity.py`` has the mechanism and
the measurement). Two populations of junk came out of that:

  * **twins from one write** — two writers minting different ids for the same
    row, one typed and one empty. Fixed at the write in 2026-08-27; the rows
    already on disk stayed.
  * **duplicates across runs** — every re-sync appending another full set.

Both are the same shape to repair: rows that resolve to one identity should be
one row. On the node this was built against, ``message_entities`` held 50,049
rows over far fewer real mentions.

Re-keying rather than only deleting is the point. If the survivors kept their
random ids, the next pass would compute the stable id, find no conflict, and
insert a duplicate of every surviving row — the repair would last exactly one
enrichment cycle. After this migration a row's id IS its identity, so the next
write of it is the REPLACE the statement always claimed to be.

The identity comes from ``identity_from_row``, the same function the writer
uses, over a row dict assembled as ``payload_json`` overlaid by the real
columns. That matters: ``entity_type`` is not a column on ``message_entities``,
it lives in the payload, and a migration that resolved identity its own way
would collapse a different set of rows than the writer goes on to produce.

Safety rules, in order of how much they would have cost to get wrong:

  * a row whose REQUIRED identity fields are missing is **left completely
    alone** — not deleted, not re-keyed. Unidentifiable is not the same as
    duplicate.
  * among rows sharing an identity the survivor is the one carrying the most
    content, so a typed row always beats its empty twin; ties break to the
    newest ``created_at``, then the highest rowid.
  * deletes happen before the re-key, so the UPDATE can never collide with a
    row this migration is about to remove.
  * ``message_entities.entity_id`` was checked against every other table
    carrying an ``entity_id`` column before this was written: ``entities``
    (4,039 rows), ``entity_mentions`` (1,037) and ``embedding_entities`` all
    share **zero** ids with it. It is a private namespace and re-keying it
    breaks no join. The same holds for ``goal_id``, ``topic_id`` and
    ``sentiment_id``, which no other table carries at all.

Idempotent: a second run finds every row already at its stable id, groups of
one, and changes nothing.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

MIGRATION_ID = "derived_row_identity_v1"

#: The derived tables that carry their own id column. ``message_emotions`` is
#: deliberately absent: its live schema is keyed ``PRIMARY KEY (message_id,
#: model_name)`` with no id column, so its writes already conflict-update and it
#: never accumulated duplicates.
_TABLES: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    # (table, id column, columns that count as "content" when picking a survivor)
    ("message_entities", "entity_id", ("entity_text", "model", "provider", "source_id")),
    ("user_goals", "goal_id", ("goal_text", "model", "provider", "source_id")),
    ("message_topics", "topic_id", ("topic", "model", "provider", "source_id")),
    ("message_sentiment", "sentiment_id", ("model", "provider", "source_id")),
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _row_dict(cols: List[str], row: Tuple[Any, ...]) -> Dict[str, Any]:
    """The row as the writer would have seen it: payload first, real columns on top."""
    raw = dict(zip(cols, row))
    merged: Dict[str, Any] = {}
    payload = raw.get("payload_json")
    if payload:
        try:
            parsed = json.loads(payload)
            if isinstance(parsed, dict):
                merged.update(parsed)
        except (ValueError, TypeError):
            pass
    for key, value in raw.items():
        if key == "payload_json":
            continue
        if value is not None and str(value).strip():
            merged[key] = value
    return merged


def _content_score(raw: Dict[str, Any], content_cols: Tuple[str, ...]) -> int:
    return sum(
        1
        for col in content_cols
        if raw.get(col) is not None and str(raw.get(col)).strip()
    )


def collapse_derived_rows(
    conn: sqlite3.Connection, *, dry_run: bool = False
) -> Dict[str, Dict[str, int]]:
    """Collapse each derived table to one row per identity and re-key survivors."""
    from ...derived_row_identity import derived_row_id, identity_from_row

    report: Dict[str, Dict[str, int]] = {}

    for table, id_col, content_cols in _TABLES:
        counts = {"scanned": 0, "unidentifiable": 0, "deleted": 0, "rekeyed": 0}
        if not _table_exists(conn, table):
            report[table] = counts
            continue
        cols = _columns(conn, table)
        if id_col not in cols:
            report[table] = counts
            continue

        select = ", ".join(["rowid"] + cols)
        groups: Dict[str, List[Tuple[int, str, int, Any]]] = {}
        for row in conn.execute(f"SELECT {select} FROM {table}").fetchall():
            counts["scanned"] += 1
            rowid = int(row[0])
            raw = dict(zip(cols, row[1:]))
            merged = _row_dict(cols, row[1:])
            identity = identity_from_row(table, merged)
            if identity is None:
                counts["unidentifiable"] += 1
                continue
            new_id = derived_row_id(table, identity)
            groups.setdefault(new_id, []).append(
                (rowid, str(raw.get(id_col) or ""), _content_score(raw, content_cols), raw.get("created_at"))
            )

        for new_id, members in groups.items():
            # Richest row wins; then newest; then the row written last.
            members.sort(key=lambda m: (m[2], str(m[3] or ""), m[0]), reverse=True)
            survivor = members[0]
            losers = [m[0] for m in members[1:]]
            if losers:
                counts["deleted"] += len(losers)
                if not dry_run:
                    conn.executemany(
                        f"DELETE FROM {table} WHERE rowid=?", [(r,) for r in losers]
                    )
            if survivor[1] != new_id:
                counts["rekeyed"] += 1
                if not dry_run:
                    conn.execute(
                        f"UPDATE {table} SET {id_col}=? WHERE rowid=?", (new_id, survivor[0])
                    )

        report[table] = counts
        logger.info(
            "%s: %s scanned=%d deleted=%d rekeyed=%d unidentifiable=%d",
            MIGRATION_ID,
            table,
            counts["scanned"],
            counts["deleted"],
            counts["rekeyed"],
            counts["unidentifiable"],
        )

    return report


def apply_derived_row_identity_v1_up(conn: sqlite3.Connection) -> None:
    """Migration entry point.

    Recording the id in ``wiki_schema_migrations`` is what makes this run ONCE.
    ``apply_all_migrations`` calls every ``_up`` unconditionally and does not
    record anything itself, so a migration that skips this stays forever in
    ``pending_ledger_migrations`` — which means every boot takes a
    pre-migration backup for a step that has already run, and re-scans four
    tables to do nothing. ``test_apply_all_stamps_user_version`` is what caught
    the omission here.
    """
    collapse_derived_rows(conn)
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_derived_row_identity_v1_down(conn: sqlite3.Connection) -> None:
    """Forget that this ran. The collapsed rows are not restored — they were
    duplicates, and the survivor carries their content."""
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
