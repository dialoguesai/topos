"""Entity-mention lineage: the table stamp every mention must carry, and the
repair of what was written without it.

``entity_mentions`` is the lineage a per-record Off-limits exclusion travels
along (SCALABLE_GRANTS_DESIGN §3.4, D8): "withhold the rows that mention a
black-holed entity" is only as good as the rows the mentions can name. Three
defects were measured on a quarantined copy of a live node on 2026-09-17
(``CORPUS_MENTION_LINEAGE.md``, 128,663 canonical rows, 33,286 mentions):

1. **17,203 mentions carry no ``canonical_table``** — every one resolves,
   uniquely, to ``conversation_messages``, and every one was written AFTER the
   stamp-recovery migration (71) had run. A live writer still did not stamp:
   the local-sync lane (iMessage, Signal) hands the entities job message dicts
   that name no table, and the job wrote whatever it was given. A table-scoped
   read of ``conversation_messages`` saw 6.8% of that table's own mentions.
2. **11,637 + 2,751 + 894 records were extracted and never linked** — they
   have ``message_entities`` rows (the NER output landed) and no
   ``entity_mentions`` row (the spine link did not). The two were written in
   different transactions by different code paths, with the spine half wrapped
   in a ``try/except`` that logged and moved on. ``message_entities`` cannot
   stand in: none of its 63,473 ``entity_id`` values join the ``entities``
   spine, and no serving guard reads it.
3. **98 mentions stamped ``journal_entries`` cite rows that live in
   ``location_events``** (the fan-out child stamped with its parent's group
   before the 2026-08-27 stamp fix), and **7 stamped ``ai_chat_messages`` cite
   no canonical row at all**.

What this module holds:

* :func:`canonical_table_for_record` — the record kind -> canonical table map,
  in the shape ``features/signal/embed_context.py`` uses for the dimension
  map: a table name, a singular ``record_type``, or a profile record kind.
* :func:`require_canonical_table` and :class:`MentionLineageError` — the
  writer's refusal. ``EntityResolver.record_mention`` raises rather than
  writing a mention that names no table. A silent row that a table-scoped
  read cannot find is worse than a loud failure the batch rolls back on.
* :func:`repair_mention_lineage` — the idempotent backfill: stamp what is
  unstamped where the record resolves to exactly one table; re-stamp what is
  provably mis-stamped where the record resolves to exactly one OTHER table;
  quarantine (move to ``entity_mentions_quarantine``, never delete) what
  resolves to no table; and write the missing spine link for every
  ``message_entities`` row whose surface resolves to an EXISTING entity by the
  resolver's exact tiers (identifier, contact, name, alias). No fuzzy match
  and no minting: a backfill that invents entities from stale NER output is a
  different defect. Ambiguity fails toward leaving the row alone, every time.

Doors into the repair: the ``entity_mention_lineage`` ``derived_rebuild``
target (upgrade manifest — the node runs it on its first boot after the
upgrade), and ``python -m topos.features.entities.mention_lineage`` for a
node that is already on the build. Run the CLI only with the node STOPPED, or
from the installed package: opening the live database from a checkout whose
migration registry is ahead of the installed node stamps ``user_version``
past what the node knows and fences it out (2026-08-19, 2026-09-16).

Nothing here is a schema migration on purpose. The beta permissions lineage
already holds migrations 74-76 unpushed; a numbered migration on a main-based
branch would collide with them, and a repair that needs no DDL does not need a
number to be idempotent — the counters it returns are its ledger.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger("topos.features.entities.mention_lineage")

#: Canonical tables a mention may cite, and the column its ``record_id`` lives
#: in. A superset of the stamp-recovery migration's candidates: ``contacts``
#: and ``transcript_segments`` gained rows after it shipped.
CANONICAL_ID_COLUMNS: Dict[str, str] = {
    "conversation_messages": "message_id",
    "ai_chat_messages": "message_id",
    "activity_events": "event_id",
    "browser_visits": "visit_id",
    "journal_entries": "entry_id",
    "location_events": "event_id",
    "calendar_events": "event_id",
    "profile_records": "record_id",
    "financial_transactions": "transaction_id",
    "contacts": "contact_id",
    "transcript_segments": "segment_id",
}

#: Record kinds -> canonical table. Keys are the singular ``record_type``
#: values the ingest lanes emit and the ``profile_records.record_type`` kinds
#: observed live; a plural table name maps to itself through
#: :data:`CANONICAL_ID_COLUMNS`. ``browser_visit`` is deliberately absent: the
#: activity group canonicalizes visits into ``activity_events`` while a
#: ``browser_visits`` table also exists, and a kind that could name either is
#: not a stamp.
CANONICAL_TABLE_BY_RECORD_KIND: Dict[str, str] = {
    "conversation_message": "conversation_messages",
    "ai_chat_message": "ai_chat_messages",
    "activity_event": "activity_events",
    "journal_entry": "journal_entries",
    "location_event": "location_events",
    "calendar_event": "calendar_events",
    "profile_record": "profile_records",
    "experience": "profile_records",
    "education": "profile_records",
    "skill": "profile_records",
    "certification": "profile_records",
    "bio": "profile_records",
    "financial_transaction": "financial_transactions",
    "contact": "contacts",
    "transcript_segment": "transcript_segments",
}

#: Below this the live writer does not link a NER mention into the spine; the
#: backfill applies the same floor so it never links what the writer refuses.
MIN_RESOLVE_CONFIDENCE = 0.60

#: Where a mention goes when its record resolves to no canonical table at all.
#: A quarantined row keeps every column it had plus the reason, so the move is
#: reversible and the count is honest; it is never deleted.
QUARANTINE_TABLE = "entity_mentions_quarantine"


class MentionLineageError(ValueError):
    """A mention was about to be written without the table its record lives in."""


def normalize_table_kind(kind: Any) -> Optional[str]:
    """The canonical table a kind names, or None when it names nothing known."""
    key = str(kind or "").strip().lower()
    if not key:
        return None
    if key in CANONICAL_ID_COLUMNS:
        return key
    return CANONICAL_TABLE_BY_RECORD_KIND.get(key)


def canonical_table_for_record(
    msg: Optional[Dict[str, Any]], *, record_type: Optional[str] = None
) -> Optional[str]:
    """Canonical table for a record dict, or None — never a guess.

    Precedence is the one ``disclosure.field_registry.canonical_table_for_message``
    encodes — ``_table``, then the record's own ``canonical_table`` — with the
    record kind as the last resort, the way the embedding context header and
    the dimension map read it. An id's shape is not consulted: ``event_id`` is
    the key of four different tables.
    """
    if not isinstance(msg, dict):
        return normalize_table_kind(record_type)
    for kind in (
        msg.get("_table"),
        msg.get("canonical_table"),
        record_type,
        msg.get("record_type"),
    ):
        table = normalize_table_kind(kind)
        if table:
            return table
    return None


def require_canonical_table(value: Any) -> str:
    """The stamp a mention must carry, or :class:`MentionLineageError`."""
    table = normalize_table_kind(value)
    if table:
        return table
    if str(value or "").strip():
        raise MentionLineageError(
            f"entity mention names a table the lineage does not know: {value!r}"
        )
    raise MentionLineageError(
        "entity mention has no canonical_table; a mention that names no table is "
        "invisible to every table-scoped read and cannot be written"
    )


# --------------------------------------------------------------- record lookup


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _present_tables(conn: sqlite3.Connection) -> List[Tuple[str, str]]:
    return [(t, col) for t, col in CANONICAL_ID_COLUMNS.items() if _table_exists(conn, t)]


def resolve_record_tables(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    present: Optional[Sequence[Tuple[str, str]]] = None,
    stop_after: int = 2,
) -> List[str]:
    """Every present canonical table holding ``record_id`` (at most ``stop_after``).

    One answer is a recovered stamp; none or two is a row left alone.
    """
    rid = str(record_id or "")
    if not rid:
        return []
    matches: List[str] = []
    for table, col in present if present is not None else _present_tables(conn):
        try:
            hit = conn.execute(
                f"SELECT 1 FROM {table} WHERE {col}=? LIMIT 1", (rid,)
            ).fetchone()
        except sqlite3.Error:
            continue
        if hit:
            matches.append(table)
            if len(matches) >= stop_after:
                break
    return matches


# ------------------------------------------------------------ spine index


class _SpineIndex:
    """The resolver's exact tiers, loaded once, with no fuzzy tier and no mint.

    Mirrors ``EntityResolver.resolve`` up to and including its alias tier:
    identifier, contact-seeded person, exact normalized name or alias within
    the type, and the unique single-token person rule. Owner tombstones
    (``intelligence_exclusions``) and unbinds (``entity_review`` ``no_bind``)
    are honoured the same way. Built in one pass so the backfill is a dict
    lookup per row rather than a table scan per row.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        from .resolver import normalize_name

        self._normalize = normalize_name
        self.by_type_name: Dict[Tuple[str, str], str] = {}
        self.alias: Dict[Tuple[str, str], str] = {}
        self.identifiers: Dict[str, str] = {}
        self.persons: List[Tuple[str, str, Optional[str]]] = []
        rows = conn.execute(
            "SELECT entity_id, entity_type, normalized_name, aliases_json,"
            " identifiers_json, contact_id FROM entities ORDER BY entity_id"
        ).fetchall()
        for entity_id, etype, normalized, aliases_json, identifiers_json, contact_id in rows:
            eid = str(entity_id)
            etype = str(etype or "")
            normalized = str(normalized or "")
            self.by_type_name.setdefault((etype, normalized), eid)
            for alias in _json_list(aliases_json):
                self.alias.setdefault((etype, normalize_name(alias)), eid)
            for ident in _json_list(identifiers_json):
                self.identifiers.setdefault(str(ident).lower(), eid)
            if etype == "person":
                self.persons.append((eid, normalized, str(contact_id) if contact_id else None))
        self.excluded: Set[str] = set()
        try:
            self.excluded = {
                str(r[0])
                for r in conn.execute(
                    "SELECT artifact_key FROM intelligence_exclusions WHERE artifact_type='entity'"
                ).fetchall()
            }
        except sqlite3.OperationalError:
            pass
        self.no_bind: Dict[str, Set[str]] = {}
        try:
            for surface, candidate in conn.execute(
                "SELECT surface_text, candidate_entity_id FROM entity_review"
                " WHERE kind='no_bind' AND status='approved'"
            ).fetchall():
                if surface and candidate:
                    self.no_bind.setdefault(str(surface), set()).add(str(candidate))
        except sqlite3.OperationalError:
            pass

    def is_excluded(self, surface: str) -> bool:
        return self._normalize(surface) in self.excluded

    def _contact_person(self, normalized: str) -> Optional[str]:
        for eid, name, contact_id in self.persons:
            if name == normalized and contact_id:
                return eid
        if " " in normalized:
            return None
        contact_hit: Optional[str] = None
        matches = 0
        for eid, name, contact_id in self.persons:
            if normalized in name.split():
                matches += 1
                if contact_id:
                    contact_hit = eid
        return contact_hit if matches == 1 else None

    def resolve(self, surface: str, etype: str) -> Optional[str]:
        normalized = self._normalize(surface)
        if not normalized:
            return None
        blocked = self.no_bind.get(normalized, set())
        if "@" in surface or surface.startswith("+") or "." in normalized.replace(" ", ""):
            hit = self.identifiers.get(normalized.replace(" ", ""))
            if hit and hit not in blocked:
                return hit
        hit = self._contact_person(normalized)
        if hit and hit not in blocked:
            return hit
        hit = self.by_type_name.get((etype, normalized)) or self.alias.get((etype, normalized))
        if hit and hit not in blocked:
            return hit
        if etype == "person" and " " not in normalized:
            candidates = [eid for eid, name, _ in self.persons if normalized in name.split()]
            if len(candidates) == 1 and candidates[0] not in blocked:
                return candidates[0]
        return None


def _json_list(raw: Any) -> List[Any]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _json_dict(raw: Any) -> Dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _chunks(items: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), max(1, size)):
        yield items[start : start + max(1, size)]


# ------------------------------------------------------------ the repair


def _stamp_unstamped(
    conn: sqlite3.Connection,
    present: Sequence[Tuple[str, str]],
    *,
    dry_run: bool,
    batch_size: int,
) -> Dict[str, int]:
    """Defect 1. Same rule as migration 71 over the wider table list."""
    from ...storage.db.write_gate import batched_writes

    counts = {"scanned": 0, "stamped": 0, "ambiguous": 0, "unresolved": 0}
    rows = conn.execute(
        "SELECT mention_id, record_id FROM entity_mentions"
        " WHERE COALESCE(canonical_table,'') = '' AND COALESCE(record_id,'') <> ''"
        " ORDER BY mention_id"
    ).fetchall()
    updates: List[Tuple[str, str]] = []
    for mention_id, record_id in rows:
        counts["scanned"] += 1
        matches = resolve_record_tables(conn, str(record_id), present=present)
        if not matches:
            counts["unresolved"] += 1
        elif len(matches) > 1:
            counts["ambiguous"] += 1
        else:
            counts["stamped"] += 1
            updates.append((matches[0], str(mention_id)))
    if dry_run or not updates:
        return counts
    for chunk in _chunks(updates, batch_size):
        with batched_writes(conn):
            conn.executemany(
                "UPDATE entity_mentions SET canonical_table=? WHERE mention_id=?", chunk
            )
    return counts


def _ensure_quarantine_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {QUARANTINE_TABLE} (
            mention_id TEXT PRIMARY KEY,
            entity_id TEXT NOT NULL,
            record_id TEXT NOT NULL,
            source_id TEXT,
            canonical_table TEXT,
            surface_text TEXT,
            confidence REAL,
            event_at TEXT,
            created_at TEXT,
            authored_by_owner INTEGER,
            reason TEXT NOT NULL,
            quarantined_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


def _quarantine(conn: sqlite3.Connection, mention_ids: Sequence[str], *, reason: str) -> int:
    """Move mentions to the quarantine table, columns intact. Returns rows moved."""
    if not mention_ids:
        return 0
    _ensure_quarantine_table(conn)
    have = {r[1] for r in conn.execute("PRAGMA table_info(entity_mentions)").fetchall()}
    cols = [
        c
        for c in (
            "mention_id", "entity_id", "record_id", "source_id", "canonical_table",
            "surface_text", "confidence", "event_at", "created_at", "authored_by_owner",
        )
        if c in have
    ]
    col_list = ", ".join(cols)
    moved = 0
    for mention_id in mention_ids:
        conn.execute(
            f"INSERT OR REPLACE INTO {QUARANTINE_TABLE} ({col_list}, reason)"
            f" SELECT {col_list}, ? FROM entity_mentions WHERE mention_id=?",
            (reason, mention_id),
        )
        cursor = conn.execute("DELETE FROM entity_mentions WHERE mention_id=?", (mention_id,))
        moved += int(cursor.rowcount or 0)
    return moved


def _restamp_or_quarantine(
    conn: sqlite3.Connection,
    present: Sequence[Tuple[str, str]],
    *,
    dry_run: bool,
    batch_size: int,
) -> Dict[str, Any]:
    """Defect 3. A stamp that disagrees with the row is repaired only when the
    row resolves to exactly one other table; a row that resolves nowhere is
    quarantined; two answers leave the stamp as it is."""
    from ...storage.db.write_gate import batched_writes

    counts: Dict[str, Any] = {
        "scanned": 0, "restamped": 0, "quarantined": 0, "ambiguous": 0, "by_table": {},
    }
    present_names = {t for t, _ in present}
    orphans: List[Tuple[str, str, str]] = []
    # Stamped with a table that is present: the id must be in it.
    for table, col in present:
        rows = conn.execute(
            f"SELECT mention_id, record_id FROM entity_mentions"
            f" WHERE canonical_table=? AND COALESCE(record_id,'') <> ''"
            f" AND record_id NOT IN (SELECT {col} FROM {table} WHERE {col} IS NOT NULL)"
            f" ORDER BY mention_id",
            (table,),
        ).fetchall()
        orphans.extend((str(m), str(r), table) for m, r in rows)
    # Stamped with a name that is not a present canonical table at all.
    placeholders = ",".join("?" for _ in present_names) or "''"
    rows = conn.execute(
        "SELECT mention_id, record_id, canonical_table FROM entity_mentions"
        " WHERE COALESCE(canonical_table,'') <> '' AND COALESCE(record_id,'') <> ''"
        f" AND canonical_table NOT IN ({placeholders}) ORDER BY mention_id",
        tuple(sorted(present_names)),
    ).fetchall()
    orphans.extend((str(m), str(r), str(t)) for m, r, t in rows)

    restamps: List[Tuple[str, str]] = []
    quarantine: List[str] = []
    for mention_id, record_id, stamped in orphans:
        counts["scanned"] += 1
        per_table = counts["by_table"].setdefault(
            stamped, {"restamped": 0, "quarantined": 0, "ambiguous": 0}
        )
        matches = resolve_record_tables(conn, record_id, present=present)
        if len(matches) == 1:
            counts["restamped"] += 1
            per_table["restamped"] += 1
            restamps.append((matches[0], mention_id))
        elif not matches:
            counts["quarantined"] += 1
            per_table["quarantined"] += 1
            quarantine.append(mention_id)
        else:
            counts["ambiguous"] += 1
            per_table["ambiguous"] += 1
    if dry_run:
        return counts
    for chunk in _chunks(restamps, batch_size):
        with batched_writes(conn):
            conn.executemany(
                "UPDATE entity_mentions SET canonical_table=? WHERE mention_id=?", chunk
            )
    for chunk in _chunks(quarantine, batch_size):
        with batched_writes(conn):
            _quarantine(conn, chunk, reason="record resolves to no canonical table")
    return counts


def _relink_extracted(
    conn: sqlite3.Connection,
    present: Sequence[Tuple[str, str]],
    *,
    dry_run: bool,
    batch_size: int,
) -> Dict[str, int]:
    """Defect 2. Every ``message_entities`` row without a spine link for its
    record gets one — when its surface resolves to an existing entity."""
    from ...storage.db.write_gate import batched_writes
    from .resolver import (
        EntityResolver,
        clean_entity_surface,
        is_valid_entity_surface,
        map_ner_type,
    )

    counts = {
        "candidates": 0,
        "linked": 0,
        "already_linked": 0,
        "unresolved": 0,
        "unattributed": 0,
        "skipped_low_confidence": 0,
        "skipped_value_type": 0,
        "skipped_invalid_surface": 0,
        "skipped_excluded": 0,
    }
    if not _table_exists(conn, "message_entities"):
        return counts
    rows = conn.execute(
        """
        SELECT e.entity_id, e.record_id, e.source_id, e.entity_text, e.provider, e.payload_json
        FROM message_entities e
        WHERE COALESCE(e.record_id,'') <> ''
          AND TRIM(COALESCE(e.entity_text,'')) <> ''
          AND NOT EXISTS (
              SELECT 1 FROM entity_mentions m
              WHERE m.record_id = e.record_id AND m.surface_text = e.entity_text
          )
        ORDER BY e.record_id, e.entity_text
        """
    ).fetchall()
    counts["candidates"] = len(rows)
    if not rows:
        return counts

    index = _SpineIndex(conn)
    resolver = EntityResolver(conn)
    table_cache: Dict[str, Optional[str]] = {}

    def _table_for(record_id: str, payload: Dict[str, Any]) -> Optional[str]:
        table = canonical_table_for_record(payload)
        if table:
            return table
        if record_id not in table_cache:
            matches = resolve_record_tables(conn, record_id, present=present)
            table_cache[record_id] = matches[0] if len(matches) == 1 else None
        return table_cache[record_id]

    def _link_one(row: Tuple[Any, ...]) -> None:
        _row_id, record_id, source_id, entity_text, provider, payload_json = row
        record_id = str(record_id)
        payload = _json_dict(payload_json)
        provider = str(provider or payload.get("provider") or "")
        try:
            confidence = float(payload.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence < MIN_RESOLVE_CONFIDENCE:
            counts["skipped_low_confidence"] += 1
            return
        raw_type = payload.get("entity_type")
        if provider == "declared":
            etype = str(raw_type or "").strip() or None
        else:
            etype = map_ner_type(raw_type)
        if etype is None:
            counts["skipped_value_type"] += 1
            return
        surface = clean_entity_surface(str(entity_text))
        if not surface or not is_valid_entity_surface(surface):
            counts["skipped_invalid_surface"] += 1
            return
        if index.is_excluded(surface):
            counts["skipped_excluded"] += 1
            return
        table = _table_for(record_id, payload)
        if not table:
            counts["unattributed"] += 1
            return
        entity_id = index.resolve(surface, etype)
        if not entity_id:
            counts["unresolved"] += 1
            return
        linked = conn.execute(
            "SELECT 1 FROM entity_mentions WHERE record_id=? AND entity_id=? LIMIT 1",
            (record_id, entity_id),
        ).fetchone()
        if linked:
            counts["already_linked"] += 1
            return
        counts["linked"] += 1
        if dry_run:
            return
        resolver.record_mention(
            entity_id,
            record_id=record_id,
            surface_text=str(payload.get("surface_detail") or surface),
            source_id=source_id or payload.get("source_id"),
            canonical_table=table,
            confidence=confidence,
            event_at=payload.get("event_at"),
        )

    if dry_run:
        for row in rows:
            _link_one(row)
        return counts
    for chunk in _chunks(rows, batch_size):
        with batched_writes(conn):
            for row in chunk:
                _link_one(row)
    return counts


def repair_mention_lineage(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
    batch_size: int = 2000,
) -> Dict[str, Any]:
    """Stamp, re-stamp or quarantine, and relink. Idempotent: a second run
    reports zero writes. Holds the write gate per chunk, not for the whole
    pass, and commits per chunk; ``dry_run`` counts and writes nothing.
    """
    report: Dict[str, Any] = {"dry_run": bool(dry_run)}
    if not _table_exists(conn, "entity_mentions"):
        report["skipped"] = "no entity_mentions table"
        return report
    present = _present_tables(conn)
    report["stamp"] = _stamp_unstamped(conn, present, dry_run=dry_run, batch_size=batch_size)
    report["restamp"] = _restamp_or_quarantine(
        conn, present, dry_run=dry_run, batch_size=batch_size
    )
    report["relink"] = _relink_extracted(conn, present, dry_run=dry_run, batch_size=batch_size)
    report["entities_recounted"] = 0
    if not dry_run and report["restamp"]["quarantined"]:
        # A quarantined mention no longer counts toward its entity. One
        # definition of the count and the observation window — the scrub's.
        from ...storage.db.write_gate import batched_writes
        from ..lifecycle.derived_scrub import _recount_entity_mentions

        with batched_writes(conn):
            report["entities_recounted"] = int(_recount_entity_mentions(conn) or 0)
    logger.info(
        "entity mention lineage%s: stamped=%d restamped=%d quarantined=%d linked=%d "
        "(unresolved stamps=%d, unresolved links=%d, unattributed=%d)",
        " (dry run)" if dry_run else "",
        report["stamp"]["stamped"],
        report["restamp"]["restamped"],
        report["restamp"]["quarantined"],
        report["relink"]["linked"],
        report["stamp"]["unresolved"],
        report["relink"]["unresolved"],
        report["relink"]["unattributed"],
    )
    return report


def main(argv: Optional[List[str]] = None) -> int:
    """Repair entity-mention lineage on this node's database.

    Same pass as the ``entity_mention_lineage`` upgrade target. Run it with
    the node stopped, from the installed package — never from a checkout whose
    migrations are ahead of the installed node (see the module docstring).
    """
    import argparse

    from ...core.state import get_db_connection

    parser = argparse.ArgumentParser(
        description="Stamp, relink and quarantine entity mentions (idempotent)."
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="count what would change; write nothing"
    )
    parser.add_argument(
        "--batch-size", type=int, default=2000, help="rows per write-gate hold"
    )
    args = parser.parse_args(argv)
    conn = get_db_connection()
    if conn is None:
        print("no database connection", flush=True)
        return 1
    report = repair_mention_lineage(conn, dry_run=args.dry_run, batch_size=args.batch_size)
    print(json.dumps(report, indent=1, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
