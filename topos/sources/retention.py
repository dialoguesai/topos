"""Keep one source's data only from a date onward: the source's retention floor.

The owner names a source and a date (``keep_since``). From then on the node holds
none of that source's rows dated before it:

* **No re-import.** The floor is stored per source (``source_retention_floors``,
  created on first write) and every door that writes the source's messages reads
  it: ``ConversationsTablesManager.upsert_message_batch`` drops older records
  before it creates a conversation, participant or contact for them, the canonical
  store's conversation writer refuses them (``REFUSED_RETENTION_FLOOR``), and the
  iMessage sync drops them right after the read, before the raw copy and before
  enrichment (``local_sync``). Every sync mode goes through those doors: since-last,
  full history, the bounded windows and the held-row retry.
* **Removal of what is already stored** (``apply_retention``): the source's
  ``conversation_messages`` rows dated before the floor, and everything derived
  from them, in bounded write-gate batches. Each batch is one transaction, so an
  interruption loses nothing and leaves nothing half-removed; running it again
  continues where it stopped. The aggregates that cannot be subtracted (entity
  counts, statistics, topic clusters, dimension profiles, messenger analytics) are
  recomputed from what remains once, at the end, from flags the batches persist
  with the floor, so a run interrupted before that step finishes it next time.
* **Dry run first** (``plan_retention``): counts per table and per derived
  artefact, the effect on recent rows a grant can index, and the bytes the removal
  would free once the file is compacted. Reads only.

What a removed row takes with it, and what it deliberately leaves:

====================================  =================================================
removed with the row                  how
====================================  =================================================
``conversation_messages``             the row itself, last in its batch
``raw_chat_messages_<source>``        the raw copy, by ``source_record_id``
``canonical_source_mappings``         by ``source_id`` + ``source_record_id``
every table with a ``record_id`` or   rows naming a removed id (timeline, embeddings
``message_id`` column, except the     with their ANN rows and FTS rows, entity
kept list below                       mentions, message entities/emotions/topics/
                                      sentiment, enrichment progress, triage, stat_seen,
                                      signal facts/scores/tags, topic cluster members,
                                      cluster candidates, goals, entity review, the
                                      derivation ledger, and any table added later)
``signal_objects``                    refs to removed rows trimmed, with the matching
                                      ``payload.evidence[]`` items and their quoted
                                      text; an object left with no ref is deleted (its
                                      permissions_v2 fact keys go by trigger)
``extraction_artifacts``              trimmed or deleted the same way
``conversations``                     a conversation the removal emptied, with its
                                      participants and its ``graph_nodes``/``graph_edges``
                                      projection; a contact node left with no edge
entities, entity edges, dossiers      recounted; entities left with no mention, contact
                                      anchor or edge removed (black-holed ones never; one
                                      whose last edge the rebuild drops goes next pass),
                                      edges rebuilt, dossiers refreshed
``stat_state``                        refolded from the remainder, only when a removed
                                      row had been folded
topic clusters, dimension profiles    recomputed, only when a removed row fed them
messenger analytics                   periods before the floor dropped for every scope
                                      that includes the source; every such scope and the
                                      dyad lifetime rollup recomputed from the remainder
====================================  =================================================

Kept on purpose:

* A row with an owner-attested provenance link (``ingest_provenance_records``) is
  not removed, and the link is never deleted here. The link table is covered by the
  provenance store's authority digest: a raw delete makes every attested row on the
  node refuse (``ingest_ledger_rollback``). Links older than 32 days are retired by
  the provenance refresh itself; a later run removes the row then.
* ``owner_only_records`` and ``intelligence_exclusions`` rows: owner decisions keyed
  by record id. Deleting one advances the protection clock and stales every signed
  authority; a marker for an absent record is inert and still holds if the record
  ever returned.
* Main-database policy tables (``permissions_v2_*``, ``ingest_provenance_*``,
  capture receipts) are kept by their own services and triggers, never edited row
  by row; capture receipts are immutable and never cover this table.
* Owner-attested native captures (``permissions-v2/ingest-snapshots``) are pinned
  by their enrollment; the provenance lane discards one when no enrollment names it.
* Contacts and their identifiers: identity, not message content. A full source
  scrub removes them.
* ``reply_to_message_id`` on kept rows is a native pointer; a reply whose parent is
  older than the floor reads like a reply whose parent was never synced.
* Ingestion bookkeeping (``episodes``, ``ingest_audit``, ``pipeline_jobs``,
  checkpoints): when batches were ingested, never their content.
* The permissions-v2 side stores (``evidence-reviews.db``, ``ledger.db``) refuse
  direct deletes by design; a review or audit sample naming a removed row reads
  ``evidence_missing`` / ``row_missing`` for that row only.

Only SQLite nodes, and only sources in ``SUPPORTED_SOURCES``: a source qualifies
when its rows live in ``conversation_messages`` alone and its sync honours the floor
at read time.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ..storage.db.write_gate import batched_writes, commit_connection, with_db_write

logger = logging.getLogger("topos.sources.retention")

TABLE = "source_retention_floors"
#: Sources whose canonical rows live in ``conversation_messages`` only and whose sync
#: drops rows older than the floor before it writes anything.
SUPPORTED_SOURCES = frozenset({"imessage"})

STATE_REMOVING = "removing"
STATE_DONE = "done"

DEFAULT_BATCH_SIZE = 1000
MIN_BATCH_SIZE = 50
MAX_BATCH_SIZE = 5000
MAX_PAUSE_SECONDS = 30.0
#: Bound parameters per statement, under every SQLite build's variable limit.
_CHUNK = 500

_SCHEMA = (
    f"CREATE TABLE IF NOT EXISTS {TABLE} ("
    "source_id TEXT PRIMARY KEY, "
    "keep_since TEXT NOT NULL, "
    "set_at TEXT NOT NULL, "
    f"state TEXT NOT NULL CHECK (state IN ('{STATE_REMOVING}','{STATE_DONE}')), "
    "pending_json TEXT NOT NULL DEFAULT '{}', "
    "applied_at TEXT, "
    "report_json TEXT)"
)

_ID_COLUMNS = ("record_id", "message_id")

#: Tables with a record_id/message_id column whose rows naming a removed row stay.
KEPT_REFERENCE_TABLES = {
    "conversation_messages": "the canonical rows, removed last in each batch",
    "ingest_provenance_records": "owner-attested links; digest-covered, never deleted here",
    "owner_only_records": "owner decision; deleting advances the protection clock",
    "intelligence_exclusions": "owner decision; deleting advances the protection clock",
    "capture_receipt_rows": "immutable by trigger",
    "ai_chat_capture_receipt_rows": "immutable by trigger",
    "ai_chat_messages": "another canonical table",
    TABLE: "this store",
}

#: Policy stores kept by their own services and triggers; never edited row by row here.
KEPT_REFERENCE_PREFIXES = ("permissions_v2_", "ingest_provenance_", "capture_receipt", "ai_chat_capture_receipt")

#: Derived tables whose removal makes an aggregate stale; the flag names the recompute.
_FLAG_BY_TABLE = {
    "entity_mentions": "entities",
    "stat_seen": "stats",
    "topic_cluster_members": "topics",
    "signal_facts": "profiles",
    "signal_scores": "profiles",
    "signal_tags": "profiles",
    "signal_embeddings": "orphans",
}

_MESSENGER_PERIOD_TABLES = ("messenger_social_edges", "messenger_participant_importance", "messenger_communities")
_MESSENGER_DIRECTED = "messenger_directed_edges"
_MONTH_GLOB = "[0-9][0-9][0-9][0-9]-[0-9][0-9]"

#: Free space a compaction must leave after its worst case, on top of it.
COMPACTION_MARGIN_BYTES = 256 * 1024 * 1024


class RetentionError(ValueError):
    """A refused request. ``code`` is safe to return to the owner; no row data in it."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------------- the floor


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _iso(dt: datetime) -> str:
    return _utc(dt).replace(microsecond=0).isoformat()


def parse_instant(value: Any) -> Optional[datetime]:
    """An aware UTC datetime from ISO 8601 text, a date, or Unix seconds; None when unreadable.

    A date alone is midnight UTC. Text without an offset is read as UTC, the way the
    canonical writers store native times.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if len(text) == 10:
            return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)
        return _utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        return None


def normalize_keep_since(value: Any, *, now: Optional[datetime] = None) -> str:
    """The floor as stored: an ISO 8601 UTC instant. Refuses unreadable and future dates.

    A floor in the future would remove data that is still current, so it is refused
    rather than read as "everything".
    """
    if not isinstance(value, str):
        raise RetentionError("keep_since_invalid")
    instant = parse_instant(value)
    if instant is None:
        raise RetentionError("keep_since_invalid")
    current = _utc(now) if now is not None else datetime.now(timezone.utc)
    if instant > current:
        raise RetentionError("keep_since_in_future")
    return _iso(instant)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')]


def retention_floors(conn: Any) -> Dict[str, str]:
    """Every source's floor: ``{source_id: keep_since}``. Empty when none is set.

    Read on every write of a supported source. An unreadable store reads as no floor
    and says so in the log: a missed floor costs a re-run of the removal, a refused
    write would cost the owner their sync.
    """
    if not isinstance(conn, sqlite3.Connection):
        return {}
    try:
        if not _table_exists(conn, TABLE):
            return {}
        return {str(r[0]): str(r[1]) for r in conn.execute(f"SELECT source_id, keep_since FROM {TABLE}")}
    except sqlite3.Error as exc:
        logger.warning("retention floors unreadable; writing without them: %s", type(exc).__name__)
        return {}


def retention_floor(conn: Any, source_id: str) -> Optional[str]:
    return retention_floors(conn).get(str(source_id or ""))


def retention_floor_unix(conn: Any, source_id: str) -> Optional[float]:
    floor = parse_instant(retention_floor(conn, source_id))
    return floor.timestamp() if floor is not None else None


def is_below_floor(event_at: Any, floor: Any) -> bool:
    """True only when both instants read and the event is strictly older.

    An undated or unreadable time is never below a floor: the floor removes what is
    known to be older, not what cannot be dated.
    """
    when = parse_instant(event_at)
    limit = floor if isinstance(floor, datetime) else parse_instant(floor)
    return when is not None and limit is not None and when < limit


def record_event_time(record: Dict[str, Any]) -> Any:
    """The native time a canonical message record carries, or None when the writer filled it."""
    if record.get("_event_time_substituted") is True:
        return None
    return record.get("event_at") or record.get("ts")


def split_below_floor(
    conn: Any, source_id: str, records: Sequence[Dict[str, Any]]
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """``(kept, message ids below the source's floor)``. No floor: everything is kept."""
    floor = parse_instant(retention_floor(conn, source_id))
    if floor is None:
        return list(records), []
    kept: List[Dict[str, Any]] = []
    below: List[str] = []
    for record in records:
        if is_below_floor(record_event_time(record), floor):
            below.append(str(record.get("message_id") or ""))
        else:
            kept.append(record)
    return kept, below


def describe_floors(conn: Any) -> List[Dict[str, Any]]:
    """Every floor with its state, for the owner. No row data."""
    if conn is None or not _table_exists(conn, TABLE):
        return []
    out = []
    for source_id, keep_since, set_at, state, pending, applied_at, report in conn.execute(
        f"SELECT source_id, keep_since, set_at, state, pending_json, applied_at, report_json FROM {TABLE} ORDER BY source_id"
    ):
        out.append({
            "source_id": source_id, "keep_since": keep_since, "set_at": set_at, "state": state,
            "pending": _load_json(pending), "applied_at": applied_at, "last_report": _load_json(report),
        })
    return out


def _load_json(text: Any) -> Dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _ensure_table(conn: sqlite3.Connection) -> None:
    with with_db_write():
        conn.execute(_SCHEMA)
        commit_connection(conn)


def set_retention_floor(conn: sqlite3.Connection, source_id: str, keep_since: str) -> None:
    """Persist the floor before any row goes, so no sync re-imports while the removal runs."""
    _ensure_table(conn)
    now = _iso(datetime.now(timezone.utc))
    with with_db_write():
        conn.execute(
            f"INSERT INTO {TABLE} (source_id, keep_since, set_at, state) VALUES (?,?,?,?) "
            "ON CONFLICT(source_id) DO UPDATE SET keep_since=excluded.keep_since, set_at=excluded.set_at, "
            "state=excluded.state",
            (source_id, keep_since, now, STATE_REMOVING),
        )
        commit_connection(conn)


def clear_retention_floor(conn: sqlite3.Connection, source_id: str) -> bool:
    """Lift the floor. Removed rows do not come back by themselves: a later sync of an
    older window (``mode="custom"`` with an early ``start_date``) reads them again."""
    if not _table_exists(conn, TABLE):
        return False
    with with_db_write():
        cursor = conn.execute(f"DELETE FROM {TABLE} WHERE source_id=?", (source_id,))
        commit_connection(conn)
    return bool(cursor.rowcount)


def _check_request(conn: Any, source_id: str) -> str:
    from ..config.settings import settings

    sid = str(source_id or "").strip()
    if sid not in SUPPORTED_SOURCES:
        raise RetentionError("retention_source_unsupported")
    if not isinstance(conn, sqlite3.Connection) or settings.topos_database_mode == "postgres":
        raise RetentionError("retention_sqlite_only")
    return sid


# --------------------------------------------------------- what the floor removes


def _chunks(items: Sequence[Any], size: int = _CHUNK) -> Iterable[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _attested_ids(conn: sqlite3.Connection) -> Set[str]:
    if not _table_exists(conn, "ingest_provenance_records"):
        return set()
    return {str(r[0]) for r in conn.execute("SELECT message_id FROM ingest_provenance_records")}


def _candidates(conn: sqlite3.Connection, source_id: str, cutoff: str, *, limit: Optional[int] = None) -> List[Tuple]:
    """``(message_id, source_record_id, conversation_id, dataset_id, event_at)`` below the floor.

    Compared as instants (``julianday``), not as text: a stored offset other than UTC
    still compares correctly, and a time SQLite cannot read never qualifies. The text
    bound only lets the event-time index narrow the scan; it is a day wider than the
    floor so no offset falls outside it.
    """
    if not _table_exists(conn, "conversation_messages"):
        return []
    wide = _iso(parse_instant(cutoff) + timedelta(days=1))
    sql = ("SELECT message_id, source_record_id, conversation_id, dataset_id, event_at FROM conversation_messages "
           "WHERE source_id=? AND event_at < ? AND julianday(event_at) < julianday(?) ORDER BY event_at, message_id")
    args: List[Any] = [source_id, wide, cutoff]
    if limit is not None:
        sql += " LIMIT ?"
        args.append(int(limit))
    return [tuple(r) for r in conn.execute(sql, args)]


def _reference_columns(conn: sqlite3.Connection) -> List[Tuple[str, str]]:
    """Every ``(table, column)`` whose rows name a canonical record by id, minus the kept list."""
    out: List[Tuple[str, str]] = []
    for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "AND sql NOT LIKE 'CREATE VIRTUAL TABLE%' ORDER BY name"
    ):
        table = str(name)
        if (table in KEPT_REFERENCE_TABLES or table.startswith(("raw_", *KEPT_REFERENCE_PREFIXES))
                or not table.replace("_", "").isalnum()):
            continue
        try:
            columns = _columns(conn, table)
        except sqlite3.Error:
            continue
        for column in _ID_COLUMNS:
            if column in columns:
                out.append((table, column))
    return out


def _raw_table(conn: sqlite3.Connection, source_id: str) -> Optional[str]:
    from ..storage.raw.raw_tables_manager import RawTablesManager

    name = RawTablesManager(conn).get_raw_table_name(source_id, "chat_messages")
    if not name or not name.replace("_", "").isalnum() or not _table_exists(conn, name):
        return None
    if not {"source_system", "source_record_id"} <= set(_columns(conn, name)):
        return None
    return name


def _ref_record_id(ref: Any) -> Optional[str]:
    from ..features.lifecycle.derived_scrub import ref_record_key

    key = ref_record_key(ref)
    return str(key[1]) if key and key[1] else None


def _trim_refs(refs: Any, removed: Set[str]) -> Tuple[Optional[List[Any]], bool]:
    """``(surviving refs, changed)``; None when the value is not a ref list.

    A ref the reader cannot resolve to a record is never evidence for removal (the
    rule ``purge_derived_for_records`` uses), so it survives.
    """
    if not isinstance(refs, list):
        return None, False
    surviving = [ref for ref in refs if _ref_record_id(ref) not in removed]
    return surviving, len(surviving) != len(refs)


def _trim_payload_evidence(payload: Any, removed: Set[str]) -> Tuple[Any, int]:
    """Drop ``evidence[]`` items naming a removed record, with the text they quote."""
    if not isinstance(payload, dict) or not isinstance(payload.get("evidence"), list):
        return payload, 0
    kept = [item for item in payload["evidence"]
            if not (isinstance(item, dict) and str(item.get("record_id") or "") in removed)]
    dropped = len(payload["evidence"]) - len(kept)
    return ({**payload, "evidence": kept} if dropped else payload), dropped


def _object_index(conn: sqlite3.Connection, ids: Set[str]) -> Dict[str, Dict[str, Set[str]]]:
    """``{table: {record_id: {object ids}}}`` for derived objects citing any of ``ids``.

    One pass at the start of a run, outside the gate; each batch then re-reads only
    the objects its rows feed.
    """
    index: Dict[str, Dict[str, Set[str]]] = {"signal_objects": {}, "extraction_artifacts": {}}
    for table, key in (("signal_objects", "object_id"), ("extraction_artifacts", "artifact_id")):
        if not _table_exists(conn, table):
            continue
        for object_id, refs_json in conn.execute(f"SELECT {key}, source_refs_json FROM {table}"):
            try:
                refs = json.loads(refs_json or "[]")
            except (TypeError, ValueError):
                continue
            if not isinstance(refs, list):
                continue
            for ref in refs:
                record_id = _ref_record_id(ref)
                if record_id in ids:
                    index[table].setdefault(record_id, set()).add(str(object_id))
    return index


def _object_outcomes(conn: sqlite3.Connection, removed: Set[str]) -> Dict[str, int]:
    """What the trims would do to every derived object, judged against the whole removal."""
    out = {"signal_objects_deleted": 0, "signal_objects_trimmed": 0, "payload_evidence_items_removed": 0,
           "extraction_artifacts_deleted": 0, "extraction_artifacts_trimmed": 0}
    if _table_exists(conn, "signal_objects"):
        for refs_json, payload_json in conn.execute("SELECT source_refs_json, payload_json FROM signal_objects"):
            refs = _load_list(refs_json)
            surviving, changed = _trim_refs(refs, removed)
            if not changed:
                continue
            if surviving:
                out["signal_objects_trimmed"] += 1
                out["payload_evidence_items_removed"] += _trim_payload_evidence(_load_any(payload_json), removed)[1]
            else:
                out["signal_objects_deleted"] += 1
    if _table_exists(conn, "extraction_artifacts"):
        for (refs_json,) in conn.execute("SELECT source_refs_json FROM extraction_artifacts"):
            surviving, changed = _trim_refs(_load_list(refs_json), removed)
            if changed:
                out["extraction_artifacts_trimmed" if surviving else "extraction_artifacts_deleted"] += 1
    return out


def _load_list(text: Any) -> Any:
    value = _load_any(text if text is not None else "[]")
    return value if isinstance(value, list) else None


def _load_any(text: Any) -> Any:
    try:
        return json.loads(text or "null")
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------- dry run


def plan_retention(
    conn: Any,
    source_id: str,
    keep_since: str,
    *,
    window_days: int = 90,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Counts only: what ``apply_retention`` would remove, keep, recompute and free. Reads only.

    ``window_days`` is the recent window a grant indexes; the plan reports how many
    rows inside it the removal would touch indirectly (an exact-copy count or a
    review context that changes), since those are what makes a grant index rebuild.
    """
    sid = _check_request(conn, source_id)
    cutoff = normalize_keep_since(keep_since, now=now)
    current = _utc(now) if now is not None else datetime.now(timezone.utc)
    rows = _candidates(conn, sid, cutoff)
    attested = _attested_ids(conn)
    removed = {r[0] for r in rows if r[0] not in attested}
    removed_rows = [r for r in rows if r[0] in removed]
    tables: Dict[str, int] = {"conversation_messages": len(removed)}

    raw = _raw_table(conn, sid)
    if raw:
        source_records = {str(r[1] or r[0]) for r in removed_rows}
        tables[raw] = sum(1 for (value,) in conn.execute(
            f'SELECT source_record_id FROM "{raw}" WHERE source_system=?', (sid,)) if value in source_records)
    if _table_exists(conn, "canonical_source_mappings"):
        source_records = {str(r[1] or r[0]) for r in removed_rows}
        tables["canonical_source_mappings"] = sum(1 for (value,) in conn.execute(
            "SELECT source_record_id FROM canonical_source_mappings WHERE source_id=?", (sid,)) if value in source_records)
    for table, columns in _group_columns(_reference_columns(conn)).items():
        selected = ", ".join(f'"{column}"' for column in columns)
        count = sum(1 for row in conn.execute(f'SELECT {selected} FROM "{table}"') if any(v in removed for v in row))
        if count:
            tables[table] = count
    if "signal_embeddings" in tables:
        tables["vector_index"] = tables["signal_embeddings"]

    by_conversation: Dict[Tuple[str, str], int] = {}
    for r in removed_rows:
        by_conversation[(r[2], r[3])] = by_conversation.get((r[2], r[3]), 0) + 1
    totals = {(r[0], r[1]): int(r[2]) for r in conn.execute(
        "SELECT conversation_id, dataset_id, count(*) FROM conversation_messages WHERE source_id=? GROUP BY 1, 2", (sid,))}
    parents = {(r[0], r[1]) for r in conn.execute(
        "SELECT conversation_id, dataset_id FROM conversations WHERE source_id=?", (sid,))} \
        if _table_exists(conn, "conversations") else set()
    emptied = sum(1 for key, n in by_conversation.items() if totals.get(key) == n and key in parents)

    entity_mentions = _entity_mention_effect(conn, removed)
    objects = _object_outcomes(conn, removed)
    window = _grant_window_effect(conn, sid, removed_rows, current, window_days)
    newest = max((r[4] for r in removed_rows), default=None)
    oldest = min((r[4] for r in removed_rows), default=None)
    datasets = sorted({r[3] for r in removed_rows})
    return {
        "source_id": sid,
        "keep_since": cutoff,
        "dry_run": True,
        "rows": {
            "below_floor": len(rows),
            "to_remove": len(removed),
            "kept_attested": len(rows) - len(removed),
            "remaining": _count(conn, "SELECT count(*) FROM conversation_messages WHERE source_id=?", (sid,)) - len(removed),
            "oldest_removed_day": oldest[:10] if oldest else None,
            "newest_removed_day": newest[:10] if newest else None,
            "datasets": len(datasets),
        },
        "tables": dict(sorted((k, v) for k, v in tables.items() if v or k == "conversation_messages")),
        "derived": {
            **objects,
            "conversations_emptied": emptied,
            "entities_losing_every_mention": entity_mentions,
            "stats_refold": bool(tables.get("stat_seen")),
            "topic_clusters_recompute": bool(tables.get("topic_cluster_members")),
            "dimension_profiles_recompute": any(tables.get(t) for t in ("signal_facts", "signal_scores", "signal_tags")),
            "messenger_period_rows_dropped": _messenger_rows_before(conn, datasets, sid, cutoff),
        },
        "grant_window": window,
        "bytes": _bytes_estimate(conn, tables),
        "kept": dict(sorted(KEPT_REFERENCE_TABLES.items())),
    }


def _group_columns(pairs: Sequence[Tuple[str, str]]) -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = {}
    for table, column in pairs:
        grouped.setdefault(table, []).append(column)
    return grouped


def _count(conn: sqlite3.Connection, sql: str, args: Tuple = ()) -> int:
    try:
        return int(conn.execute(sql, args).fetchone()[0] or 0)
    except sqlite3.Error:
        return 0


def _entity_mention_effect(conn: sqlite3.Connection, removed: Set[str]) -> int:
    if not removed or not _table_exists(conn, "entity_mentions"):
        return 0
    totals: Dict[str, List[int]] = {}
    for entity_id, record_id in conn.execute("SELECT entity_id, record_id FROM entity_mentions"):
        pair = totals.setdefault(str(entity_id), [0, 0])
        pair[0] += 1
        pair[1] += int(record_id in removed)
    return sum(1 for total, gone in totals.values() if gone and gone == total)


def _grant_window_effect(conn, source_id, removed_rows, now, window_days) -> Dict[str, Any]:
    """Kept rows inside a grant's window that the removal changes without deleting.

    Counts two things the message-search index fingerprints: a kept row whose exact
    text also sits in a removed row (its independent-copy count drops; at 1 the copy
    floor stops withholding it), and a kept row whose two nearest earlier messages in
    its conversation include a removed one (its machine-review context changes, so
    the review goes stale until re-reviewed).
    """
    days = max(1, int(window_days or 90))
    lower_at = now - timedelta(days=days)
    lower = _iso(lower_at)
    removed = {r[0] for r in removed_rows}
    in_window_removed = sum(1 for r in removed_rows if (parse_instant(r[4]) or lower_at) >= lower_at
                            and parse_instant(r[4]) is not None)
    copies: Dict[str, int] = {}
    removed_copies: Dict[str, int] = {}
    for table in ("conversation_messages", "ai_chat_messages"):
        if not _table_exists(conn, table):
            continue
        for message_id, content in conn.execute(f"SELECT message_id, content FROM {table}"):
            if isinstance(content, str):
                copies[content] = copies.get(content, 0) + 1
                if table == "conversation_messages" and message_id in removed:
                    removed_copies[content] = removed_copies.get(content, 0) + 1
    changed = unique = 0
    recent = conn.execute(
        "SELECT message_id, content, conversation_id, dataset_id, event_at, source_id FROM conversation_messages "
        "WHERE julianday(event_at) >= julianday(?)", (lower,)).fetchall()
    for message_id, content, *_ in recent:
        if message_id in removed or not isinstance(content, str) or not removed_copies.get(content):
            continue
        changed += 1
        if copies[content] > 1 and copies[content] - removed_copies[content] <= 1:
            unique += 1
    context = 0
    for message_id, _content, conversation_id, dataset_id, event_at, row_source in recent:
        # A review's context is its own conversation in its own source (automatic_message_review.context_for).
        if message_id in removed or row_source != source_id:
            continue
        before = conn.execute(
            "SELECT message_id FROM conversation_messages WHERE conversation_id=? AND source_id=? AND dataset_id=? "
            "AND (event_at, message_id) < (?, ?) ORDER BY event_at DESC, message_id DESC LIMIT 2",
            (conversation_id, source_id, dataset_id, event_at, message_id)).fetchall()
        context += int(any(r[0] in removed for r in before))
    return {
        "window_days": days,
        "removed_inside_window": in_window_removed,
        "kept_rows_copy_count_changes": changed,
        "kept_rows_become_unique": unique,
        "kept_rows_review_context_changes": context,
        "index_rebuild": bool(in_window_removed or changed or context),
    }


def _messenger_rows_before(conn: sqlite3.Connection, datasets: Sequence[str], source_id: str, cutoff: str) -> int:
    total = 0
    period = cutoff[:7]
    for dataset_id in datasets:
        for table, scopes in _messenger_scopes(conn, dataset_id, source_id).items():
            for scope in scopes:
                total += _count(conn, f"SELECT count(*) FROM {table} WHERE dataset_id=? AND source_scope=? "
                                      f"AND period_key < ? AND period_key GLOB '{_MONTH_GLOB}'",
                                (dataset_id, scope, period))
        if _table_exists(conn, _MESSENGER_DIRECTED):
            total += _count(conn, f"SELECT count(*) FROM {_MESSENGER_DIRECTED} WHERE dataset_id=? AND connector=? "
                                  f"AND period_key < ? AND period_key GLOB '{_MONTH_GLOB}'",
                            (dataset_id, source_id, period))
    return total


def _messenger_scopes(conn: sqlite3.Connection, dataset_id: str, source_id: str) -> Dict[str, List[str]]:
    """``{table: [source_scope, ...]}``: the scopes a source's messages feed (its own, any
    list naming it, and ``all``)."""
    out: Dict[str, List[str]] = {}
    for table in _MESSENGER_PERIOD_TABLES:
        if not _table_exists(conn, table):
            continue
        scopes = [str(r[0]) for r in conn.execute(f"SELECT DISTINCT source_scope FROM {table} WHERE dataset_id=?",
                                                  (dataset_id,))]
        out[table] = sorted(s for s in scopes if s == "all" or source_id in s.split(","))
    return out


def _bytes_estimate(conn: sqlite3.Connection, tables: Dict[str, int]) -> Dict[str, Any]:
    """Bytes the removal frees once compacted: each touched b-tree's size times the share
    of its rows removed, from ``dbstat``. An estimate: rows are assumed equal in size."""
    page_size = _count(conn, "PRAGMA page_size")
    freelist = _count(conn, "PRAGMA freelist_count") * page_size
    file_bytes = _count(conn, "PRAGMA page_count") * page_size
    estimate = 0
    available = True
    for label, removed in tables.items():
        table = label.split(".")[0]
        if not removed or not _table_exists(conn, table):
            continue
        total = _count(conn, f'SELECT count(*) FROM "{table}"')
        if total <= 0:
            continue
        names = [table] + [str(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,))]
        try:
            size = sum(int(conn.execute("SELECT pgsize FROM dbstat WHERE name=? AND aggregate=1", (name,)).fetchone()[0] or 0)
                       for name in names)
        except (sqlite3.Error, TypeError):
            available = False
            break
        estimate += int(size * min(1.0, removed / total))
    return {
        "database_file_bytes": file_bytes,
        "free_pages_bytes_now": freelist,
        "estimated_freed_bytes": estimate if available else None,
        "estimated_reclaimable_after_compaction": (estimate + freelist) if available else None,
        "method": "dbstat_share_of_rows" if available else "unavailable",
    }


# ------------------------------------------------------------------- the removal


def _merge_pending(conn: sqlite3.Connection, source_id: str, flags: Set[str], datasets: Set[str]) -> None:
    row = conn.execute(f"SELECT pending_json FROM {TABLE} WHERE source_id=?", (source_id,)).fetchone()
    pending = _load_json(row[0] if row else "{}")
    for flag in flags:
        pending[flag] = True
    if datasets:
        pending["datasets"] = sorted(set(pending.get("datasets") or []) | datasets)
    conn.execute(f"UPDATE {TABLE} SET pending_json=? WHERE source_id=?", (json.dumps(pending, sort_keys=True), source_id))


class _Run:
    """What one ``apply_retention`` call has removed so far. Counts and object ids only."""

    def __init__(self) -> None:
        self.tables: Dict[str, int] = {}
        self.trimmed: Dict[str, Set[str]] = {"signal_objects": set(), "extraction_artifacts": set()}
        self.deleted: Dict[str, Set[str]] = {"signal_objects": set(), "extraction_artifacts": set()}
        self.evidence_items = 0

    def add(self, label: str, n: Any) -> None:
        if n:
            self.tables[label] = self.tables.get(label, 0) + int(n)

    def derived(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for table in ("signal_objects", "extraction_artifacts"):
            out[f"{table}_deleted"] = len(self.deleted[table])
            # Trimmed in one batch and deleted in a later one counts once, as deleted.
            out[f"{table}_trimmed"] = len(self.trimmed[table] - self.deleted[table])
        out["payload_evidence_items_removed"] = self.evidence_items
        return out


def _remove_batch(
    conn: sqlite3.Connection,
    source_id: str,
    batch: List[Tuple],
    *,
    ref_columns: List[Tuple[str, str]],
    raw_table: Optional[str],
    objects: Dict[str, Dict[str, Set[str]]],
    run: _Run,
) -> None:
    """Remove one batch of rows and everything that names them, in one transaction."""
    from ..storage.adapters.sqlite.stores import SQLiteVectorIndex

    ids = [r[0] for r in batch]
    id_set = set(ids)
    source_records = sorted({str(r[1] or r[0]) for r in batch})
    embedding_ids: List[str] = []
    if ("signal_embeddings", "record_id") in ref_columns:
        for chunk in _chunks(ids):
            embedding_ids += [str(r[0]) for r in conn.execute(
                f"SELECT embedding_id FROM signal_embeddings WHERE record_id IN ({','.join('?' * len(chunk))})", chunk)]
    flags: Set[str] = set()
    add = run.add
    with batched_writes(conn):
        for table, column in ref_columns:
            removed = 0
            for chunk in _chunks(ids):
                removed += int(conn.execute(
                    f'DELETE FROM "{table}" WHERE "{column}" IN ({",".join("?" * len(chunk))})', chunk).rowcount or 0)
            add(table, removed)
            if removed and table in _FLAG_BY_TABLE:
                flags.add(_FLAG_BY_TABLE[table])
        if embedding_ids:
            add("vector_index", SQLiteVectorIndex(conn).delete_embeddings(embedding_ids))
        for chunk in _chunks(source_records):
            marks = ",".join("?" * len(chunk))
            if raw_table:
                add(raw_table, conn.execute(
                    f'DELETE FROM "{raw_table}" WHERE source_system=? AND source_record_id IN ({marks})',
                    (source_id, *chunk)).rowcount)
            if _table_exists(conn, "canonical_source_mappings"):
                add("canonical_source_mappings", conn.execute(
                    f"DELETE FROM canonical_source_mappings WHERE source_id=? AND source_record_id IN ({marks})",
                    (source_id, *chunk)).rowcount)
        if _trim_objects(conn, id_set, objects, run):
            flags.add("orphans")
        for chunk in _chunks(ids):
            add("conversation_messages", conn.execute(
                f"DELETE FROM conversation_messages WHERE message_id IN ({','.join('?' * len(chunk))})", chunk).rowcount)
        _drop_emptied_conversations(conn, source_id, {(r[2], r[3]) for r in batch}, run)
        _merge_pending(conn, source_id, flags, {str(r[3]) for r in batch})


def _trim_objects(conn: sqlite3.Connection, removed: Set[str], index: Dict[str, Dict[str, Set[str]]],
                  run: _Run) -> bool:
    touched = False
    for table, key, has_payload in (("signal_objects", "object_id", True), ("extraction_artifacts", "artifact_id", False)):
        object_ids = sorted({oid for rid in removed for oid in index.get(table, {}).get(rid, ())})
        for object_id in object_ids:
            columns = "source_refs_json, payload_json" if has_payload else "source_refs_json"
            row = conn.execute(f"SELECT {columns} FROM {table} WHERE {key}=?", (object_id,)).fetchone()
            if row is None:
                continue
            surviving, changed = _trim_refs(_load_list(row[0]), removed)
            if not changed:
                continue
            touched = True
            if not surviving:
                conn.execute(f"DELETE FROM {table} WHERE {key}=?", (object_id,))
                run.deleted[table].add(object_id)
                continue
            if has_payload:
                payload, dropped = _trim_payload_evidence(_load_any(row[1]), removed)
                conn.execute(
                    "UPDATE signal_objects SET source_refs_json=?, payload_json=?, updated_at=datetime('now') "
                    "WHERE object_id=?",
                    (json.dumps(surviving), json.dumps(payload) if dropped else row[1], object_id))
                run.evidence_items += dropped
            else:
                conn.execute(f"UPDATE {table} SET source_refs_json=? WHERE {key}=?", (json.dumps(surviving), object_id))
            run.trimmed[table].add(object_id)
    return touched


def _drop_emptied_conversations(conn: sqlite3.Connection, source_id: str, keys: Set[Tuple[str, str]],
                                run: _Run) -> None:
    """A conversation this batch emptied goes, with its participants and graph projection."""
    graph = _table_exists(conn, "graph_nodes") and _table_exists(conn, "graph_edges")
    for conversation_id, dataset_id in sorted(keys):
        # A conversation row belongs to one source; this probe reads the covering
        # (source, conversation, dataset, time, id) index, never a dataset-wide scan.
        if conn.execute("SELECT 1 FROM conversation_messages WHERE source_id=? AND conversation_id=? AND dataset_id=? "
                        "LIMIT 1", (source_id, conversation_id, dataset_id)).fetchone():
            continue
        gone = conn.execute("DELETE FROM conversations WHERE conversation_id=? AND dataset_id=? AND source_id=?",
                            (conversation_id, dataset_id, source_id)).rowcount
        if not gone:
            continue
        run.add("conversations", gone)
        if _table_exists(conn, "conversation_participants"):
            run.add("conversation_participants", conn.execute(
                "DELETE FROM conversation_participants WHERE conversation_id=? AND dataset_id=? AND source_id=?",
                (conversation_id, dataset_id, source_id)).rowcount)
        if graph:
            node = f"conversation:{conversation_id}"
            if conn.execute("SELECT 1 FROM conversation_messages WHERE conversation_id=? LIMIT 1",
                            (conversation_id,)).fetchone():
                continue  # the same thread id still has messages in another dataset
            ends = [str(r[0]) for r in conn.execute(
                "SELECT src_node_id FROM graph_edges WHERE dst_node_id=? UNION SELECT dst_node_id FROM graph_edges WHERE src_node_id=?",
                (node, node))]
            run.add("graph_edges", conn.execute(
                "DELETE FROM graph_edges WHERE src_node_id=? OR dst_node_id=?", (node, node)).rowcount)
            run.add("graph_nodes", conn.execute("DELETE FROM graph_nodes WHERE node_id=?", (node,)).rowcount)
            for end in ends:
                if end.startswith("contact:") and not conn.execute(
                        "SELECT 1 FROM graph_edges WHERE src_node_id=? OR dst_node_id=? LIMIT 1", (end, end)).fetchone():
                    run.add("graph_nodes", conn.execute(
                        "DELETE FROM graph_nodes WHERE node_id=? AND source_id=?", (end, source_id)).rowcount)


def _finish(conn: sqlite3.Connection, source_id: str, cutoff: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Recompute what the batches flagged. Returns ``(report, pending still due)``.

    Each step clears its own flag only when it succeeds, so a failed step is retried
    by the next run and the floor stays ``removing`` until every one has.
    """
    row = conn.execute(f"SELECT pending_json FROM {TABLE} WHERE source_id=?", (source_id,)).fetchone()
    pending = _load_json(row[0] if row else "{}")
    report: Dict[str, Any] = {}

    def step(flag: str, fn) -> None:
        if not pending.get(flag):
            return
        try:
            report[flag] = fn()
            pending.pop(flag, None)
        except Exception as exc:  # noqa: BLE001 -- kept due; the next run retries it
            logger.warning("retention finish step %s failed: %s", flag, type(exc).__name__)
            report[flag] = {"status": "failed", "error": type(exc).__name__}

    from ..features.lifecycle import derived_scrub as derived

    def entities() -> Dict[str, Any]:
        with batched_writes(conn):
            recounted = derived._recount_entity_mentions(conn)
            removed = derived._delete_orphan_entities(conn)
        out = {"entities_recounted": recounted, "entities_removed": len(removed),
               "entities_retained_protected": len(getattr(derived._delete_orphan_entities, "last_retained_protected", []) or []),
               "edges_rebuilt": derived._rebuild_entity_edges(conn)}
        from ..features.entities.dossier import refresh_dossiers

        out["dossiers_refreshed"] = refresh_dossiers(conn)
        pending["orphans"] = True
        return out

    def stats() -> Dict[str, Any]:
        out = derived.refold_statistics(conn)
        out.update(derived.repromote_stat_insights(conn))
        return out

    def topics() -> Dict[str, Any]:
        from ..config.settings import settings
        from ..features.signal.topic_clustering import recompute_topic_clusters, write_top_topics_signal_facts
        from ..storage.adapters.factory import AdapterFactory

        result = recompute_topic_clusters(conn, min_records=int(getattr(settings, "scrub_min_embeddings_for_recluster", 3) or 3))
        if result.get("status") == "completed":
            write_top_topics_signal_facts(AdapterFactory.create("local_database", conn=conn), conn)
        return {"status": result.get("status")}

    def profiles() -> Dict[str, Any]:
        from ..features.signal.dimension_profiles import DimensionProfileUpdater
        from ..storage.adapters.factory import AdapterFactory

        DimensionProfileUpdater(AdapterFactory.create("local_database", conn=conn), conn).upsert_all()
        return {"status": "updated"}

    def orphans() -> Dict[str, Any]:
        return derived.sweep_orphans(conn)

    def messenger() -> Dict[str, Any]:
        return _refresh_messenger(conn, list(pending.get("datasets") or []), source_id, cutoff)

    step("entities", entities)
    step("stats", stats)
    step("topics", topics)
    step("profiles", profiles)
    step("orphans", orphans)
    if pending.get("datasets"):
        pending["messenger"] = True
    step("messenger", messenger)
    if "messenger" not in pending:
        pending.pop("datasets", None)
    return report, pending


def _refresh_messenger(conn: sqlite3.Connection, datasets: List[str], source_id: str, cutoff: str) -> Dict[str, Any]:
    """Drop every period before the floor for the scopes the source feeds, then recompute
    those scopes from what remains (the recompute rewrites only periods that still have
    messages, so a period the removal emptied would otherwise keep its old rows)."""
    from ..analytics.messenger_communities import compute_and_persist_messenger_analytics

    period = cutoff[:7]
    out: Dict[str, Any] = {"periods_rows_dropped": 0, "recomputed_scopes": 0}
    for dataset_id in datasets:
        scope_map = _messenger_scopes(conn, dataset_id, source_id)
        scopes = sorted({s for values in scope_map.values() for s in values})
        with batched_writes(conn):
            for table, values in scope_map.items():
                for scope in values:
                    out["periods_rows_dropped"] += int(conn.execute(
                        f"DELETE FROM {table} WHERE dataset_id=? AND source_scope=? AND period_key < ? "
                        f"AND period_key GLOB '{_MONTH_GLOB}'", (dataset_id, scope, period)).rowcount or 0)
            if _table_exists(conn, _MESSENGER_DIRECTED):
                out["periods_rows_dropped"] += int(conn.execute(
                    f"DELETE FROM {_MESSENGER_DIRECTED} WHERE dataset_id=? AND connector=? AND period_key < ? "
                    f"AND period_key GLOB '{_MONTH_GLOB}'", (dataset_id, source_id, period)).rowcount or 0)
        # The source's own scope last: it is what the post-sync refresh writes, so the
        # dataset's lifetime dyad rollup ends as a sync would leave it.
        ordered = [s for s in scopes if s != source_id] + [source_id]
        for scope in ordered:
            compute_and_persist_messenger_analytics(
                dataset_id=dataset_id, conn=conn,
                source_ids=None if scope == "all" else scope.split(","), period_granularity="month")
            out["recomputed_scopes"] += 1
    return out


def _bounded(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:
        return default
    return max(lo, min(hi, number))


def apply_retention(
    conn: Any,
    source_id: str,
    keep_since: str,
    *,
    batch_size: Any = DEFAULT_BATCH_SIZE,
    pause_seconds: Any = 0.0,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Set the floor, then remove the source's rows below it with their derived data.

    Bounded: each batch of ``batch_size`` rows is one write-gate transaction, and
    ``pause_seconds`` between batches lets waiting writers in. Resumable and safe to
    re-run: the floor is persisted first, a batch commits whole or not at all, and
    the recompute flags live with the floor until their step succeeds. Refuses while
    a sync or a scrub of the source runs.
    """
    from ..ingestion.local_sync import exclusive_sync
    from .scrub_service import ScrubInProgressError, _acquire_scrub_lock, _assert_scrub_allowed, _release_scrub_lock

    sid = _check_request(conn, source_id)
    cutoff = normalize_keep_since(keep_since, now=now)
    try:
        _assert_scrub_allowed()
    except RuntimeError:
        raise RetentionError("retention_pooled_unavailable") from None
    size = int(_bounded(batch_size, DEFAULT_BATCH_SIZE, MIN_BATCH_SIZE, MAX_BATCH_SIZE))
    pause = _bounded(pause_seconds, 0.0, 0.0, MAX_PAUSE_SECONDS)
    started = time.perf_counter()
    try:
        _acquire_scrub_lock(sid)
    except ScrubInProgressError:
        raise RetentionError("retention_source_busy") from None
    try:
        datasets = sorted({str(r[0]) for r in conn.execute(
            "SELECT DISTINCT dataset_id FROM conversation_messages WHERE source_id=?", (sid,))}) \
            if _table_exists(conn, "conversation_messages") else []
        with ExitStack() as stack:
            for dataset_id in datasets:
                if not stack.enter_context(exclusive_sync(sid, dataset_id)):
                    raise RetentionError("retention_sync_in_progress")
            set_retention_floor(conn, sid, cutoff)
            attested = _attested_ids(conn)
            first = [r for r in _candidates(conn, sid, cutoff) if r[0] not in attested]
            objects = _object_index(conn, {r[0] for r in first})
            ref_columns = _reference_columns(conn)
            raw_table = _raw_table(conn, sid)
            run = _Run()
            batches = 0
            pending_rows = first
            while pending_rows:
                batch = pending_rows[:size]
                _remove_batch(conn, sid, batch, ref_columns=ref_columns, raw_table=raw_table,
                              objects=objects, run=run)
                batches += 1
                pending_rows = pending_rows[size:]
                if not pending_rows:
                    # A row written between the first read and now (a sync already past
                    # its floor check) is caught here rather than left behind.
                    pending_rows = [r for r in _candidates(conn, sid, cutoff, limit=size) if r[0] not in attested]
                    if pending_rows:
                        objects = _object_index(conn, {r[0] for r in pending_rows})
                if pending_rows and pause:
                    time.sleep(pause)
            recompute, due = _finish(conn, sid, cutoff)
        report = {
            "source_id": sid,
            "keep_since": cutoff,
            "dry_run": False,
            "batches": batches,
            "rows_removed": run.tables.get("conversation_messages", 0),
            "kept_attested": len([r for r in _candidates(conn, sid, cutoff) if r[0] in attested]),
            "tables": dict(sorted(run.tables.items())),
            "derived": run.derived(),
            "recompute": recompute,
            "pending": due,
            "state": STATE_DONE if not due else STATE_REMOVING,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
        with with_db_write():
            conn.execute(
                f"UPDATE {TABLE} SET state=?, pending_json=?, applied_at=?, report_json=? WHERE source_id=?",
                (report["state"], json.dumps(due, sort_keys=True), _iso(datetime.now(timezone.utc)),
                 json.dumps({k: report[k] for k in ("batches", "rows_removed", "kept_attested", "tables", "derived")},
                            sort_keys=True),
                 sid))
            commit_connection(conn)
        return report
    finally:
        _release_scrub_lock(sid)


# ----------------------------------------------------------------- compaction


def _database_path(conn: sqlite3.Connection) -> Optional[str]:
    for _seq, name, path in conn.execute("PRAGMA database_list"):
        if name == "main":
            return str(path) if path else None
    return None


def compaction_status(conn: Any) -> Dict[str, Any]:
    """What a compaction would reclaim and whether the volume has room for it. Reads only.

    ``VACUUM`` rewrites the whole database: it builds the compacted copy, then writes
    it back through the journal (in WAL mode the WAL grows to the compacted size until
    the checkpoint). The worst case needs about twice the compacted size free, plus a
    margin. An ``auto_vacuum=INCREMENTAL`` database instead releases free pages in
    place (``PRAGMA incremental_vacuum``) and needs no headroom.
    """
    if not isinstance(conn, sqlite3.Connection):
        raise RetentionError("retention_sqlite_only")
    page_size = _count(conn, "PRAGMA page_size")
    pages = _count(conn, "PRAGMA page_count")
    free_pages = _count(conn, "PRAGMA freelist_count")
    auto_vacuum = _count(conn, "PRAGMA auto_vacuum")
    path = _database_path(conn)
    try:
        free_disk = shutil.disk_usage(os.path.dirname(path)).free if path else None
    except OSError:
        free_disk = None
    compacted = (pages - free_pages) * page_size
    needed = 0 if auto_vacuum == 2 else 2 * compacted + COMPACTION_MARGIN_BYTES
    mode = "incremental_vacuum" if auto_vacuum == 2 else "vacuum"
    refusal = None
    if path is None:
        refusal = "compaction_in_memory_database"
    elif free_pages == 0:
        refusal = "compaction_nothing_to_reclaim"
    elif free_disk is None:
        refusal = "compaction_free_space_unknown"
    elif free_disk < needed:
        refusal = "compaction_insufficient_free_space"
    return {
        "mode": mode,
        "database_file_bytes": pages * page_size,
        "reclaimable_bytes": free_pages * page_size,
        "compacted_bytes": compacted,
        "free_disk_bytes": free_disk,
        "required_free_bytes": needed,
        "can_compact": refusal is None,
        "refusal": refusal,
    }


def compact_database(conn: Any, *, dry_run: bool = True) -> Dict[str, Any]:
    """Owner-triggered compaction that checks the volume first and refuses when it is short.

    Holds the write gate for the whole rewrite, so the node's writers wait (on the
    order of a minute for a database of a gigabyte or two); readers on other
    connections keep their snapshots. Checkpoints and truncates the WAL afterwards so
    the space it grew into is returned too.
    """
    status = compaction_status(conn)
    status["dry_run"] = bool(dry_run)
    if dry_run or not status["can_compact"]:
        return status
    started = time.perf_counter()
    before = status["database_file_bytes"]
    with with_db_write():
        commit_connection(conn)
        try:
            if status["mode"] == "incremental_vacuum":
                conn.execute("PRAGMA incremental_vacuum")
            else:
                conn.execute("VACUUM")
        except sqlite3.OperationalError as exc:
            status.update(can_compact=False, refusal="compaction_busy", error=type(exc).__name__)
            return status
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        except sqlite3.Error:
            pass
    after = _count(conn, "PRAGMA page_count") * _count(conn, "PRAGMA page_size")
    status.update(compacted=True, database_file_bytes_after=after, freed_bytes=max(0, before - after),
                  duration_ms=int((time.perf_counter() - started) * 1000))
    return status


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "KEPT_REFERENCE_TABLES",
    "RetentionError",
    "SUPPORTED_SOURCES",
    "TABLE",
    "apply_retention",
    "clear_retention_floor",
    "compact_database",
    "compaction_status",
    "describe_floors",
    "is_below_floor",
    "normalize_keep_since",
    "plan_retention",
    "retention_floor",
    "retention_floor_unix",
    "retention_floors",
    "split_below_floor",
]
