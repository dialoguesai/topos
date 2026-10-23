"""Re-embed stored rows whose text changed after they were embedded.

The embeddings job seals every chunk it stores with ``content_hash``, the sha256 of
the record's embeddable text as the job saw it. A row rewritten since (an edit, a
re-capture, a re-import, a merged reply) keeps its old chunks and their stored
text, and nothing re-embeds it. Since lane 194 (1.5.16+) those chunks are refused
wherever they could speak for the row to someone else: a share's search index
embeds the row's current text again on every rebuild, within a per-build budget,
and keeps that vector only inside the index; below the owner's tier the vector
and recent lanes drop the row. On a copy of one live store on 22 Oct 2026, 309 of
4,000 sampled AI-chat rows and 168 of 597 journal rows were in that state. This
lane fixes the stored rows, so the index stops paying per rebuild and grantees'
lanes get the rows back.

One lane, one kind (``stale_text_embeddings_catchup``), on the catch-up worker
(``job_runner._CATCHUP_KINDS``) beside the two lanes of ``embeddings_catchup``.
Staleness cannot be read in SQL: a row is current when its stored hash equals
the hash of one of its current renderings (``retrieval._index_current_texts``,
the same rule the query gate and the index apply), and the renderings are
Python. So a job is one PAGE of a pass: it walks a source's embedded canonical
records in record-id order from a cursor the payload carries (``after``), reads
the rows, compares, re-embeds the stale ones through the ordinary embeddings
job (which writes the new ``content_hash`` and drops the model's superseded
chunks), prunes whatever chunk of those records still carries a hash that
matches no current rendering, and a finished page queues the next. A pass
ends at the last record; the next pass starts ``PASS_INTERVAL_SECONDS`` later,
on the next trigger (worker start, a finished ingest or page of the source), so
rows rewritten after a pass are caught by the next one. Nothing is enrolled by
the owner: a stored vector is one the owner already asked for when the source
was attached with ``embeddings``, the model is the same local one, and the work
is bounded by the rows that changed. The switch is ``TOPOS_STALE_TEXT_CATCHUP``.

A chunk with no hash at all (none is stored today) counts as stale too: the gate and
the index refuse it the same way, and one pass seals it.

Read, never re-embedded: a record whose canonical row is gone or cannot be
named (no ``record_type``, or a derived kind), a row the owner keeps from every
share (``share_excluded``), a row with no renderable text, and any row of a
source without ``embeddings`` attached. A record with one current chunk and a
stale leftover gets the leftover pruned and no model call. Payloads and results
hold record ids and counts only.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

JOB = "embeddings"
#: pipeline_jobs kind. A queued row outlives the build that wrote it, so it is never renamed.
KIND = "stale_text_embeddings_catchup"
SWITCH = "TOPOS_STALE_TEXT_CATCHUP"
#: Record ids one page reads and compares. Measured on a synthetic store (see the lane's commit):
#: the scan is reads and hashing, milliseconds per hundred rows; the model runs only for stale rows.
PAGE_SIZE = 2000
MAX_PAGE_SIZE = 10000
#: A finished pass over a source is repeated this long after it ended, on the next trigger.
PASS_INTERVAL_SECONDS = 24 * 3600
#: The source's own ingest goes first; a finished one, like a finished page, continues a pass.
INGEST_KINDS = ("file_ingestion", "enrichment_process_source", "inbox_deferred_enrichment", "local_sync")
CONTINUING_KINDS = frozenset((KIND, *INGEST_KINDS))
_OFF_VALUES = ("0", "false", "off", "no")
_ROW_CHUNK = 500


def enabled() -> bool:
    return os.environ.get(SWITCH, "on").strip().lower() not in _OFF_VALUES


def _attached(source_id: str) -> bool:
    """``embeddings`` runs for this source at ingest, in either lane; a detached source keeps its rows as they are."""
    from .embeddings_catchup import _attached_at_import, _attached_at_sync

    return _attached_at_import(source_id) or _attached_at_sync(source_id)


def _record_tables() -> Dict[str, str]:
    from ..query.retrieval import _RECORD_TYPE_TABLE

    return dict(_RECORD_TYPE_TABLE)


def _index_ready(conn: Any) -> bool:
    """The index exists and carries the two columns the comparison needs; without them nothing can be judged."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_embeddings'").fetchone() is None:
        return False
    columns = {row[1] for row in conn.execute("PRAGMA table_info(signal_embeddings)")}
    return {"content_hash", "record_type", "vector_blob"} <= columns


def _sources(conn: Any) -> List[str]:
    """Registered sources with ``embeddings`` attached and at least one embedded canonical row. One index probe per
    source (the registry holds the bundled and the runtime-installed definitions): a DISTINCT over the index read every
    row, 93 ms warm on 65k synthetic rows and over a second cold, and this runs at every finished page. A source with
    rows but no definition is never attached, so it is not listed."""
    from ..sources.registry import REGISTRY

    types = sorted(_record_tables())
    out = []
    for sid in sorted(REGISTRY):
        if not _attached(sid):
            continue
        present = conn.execute(
            "SELECT 1 FROM signal_embeddings WHERE source_id=? AND vector_blob IS NOT NULL "
            f"AND record_type IN ({','.join('?' for _ in types)}) LIMIT 1", (sid, *types),
        ).fetchone()
        if present:
            out.append(sid)
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_pass_id() -> str:
    """Names one pass over one source; every page of the pass carries it, and a new pass gets a new one."""
    return uuid.uuid4().hex[:16]


def _parse_clock(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _next_page(conn: Any, source_id: str) -> Optional[Dict[str, Any]]:
    """The payload of the page to queue for this source now, or None: a page or an ingest of the source is in
    flight, or its last pass ended less than ``PASS_INTERVAL_SECONDS`` ago. The cursor comes from the last finished
    page (``detail_json.last_id``); a failed page is queued again as it was."""
    kinds = (KIND, *INGEST_KINDS)
    active = conn.execute(
        "SELECT 1 FROM pipeline_jobs WHERE source_id=? AND status IN ('queued','running') "
        f"AND kind IN ({','.join('?' for _ in kinds)}) LIMIT 1", (source_id, *kinds),
    ).fetchone()
    if active:
        return None
    last = conn.execute(
        "SELECT status, payload_json, detail_json, finished_at FROM pipeline_jobs WHERE kind=? AND source_id=? "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1", (KIND, source_id),
    ).fetchone()
    if last is None:
        return {"source_id": source_id, "after": "", "pass_id": _new_pass_id(), "page": PAGE_SIZE}
    status, payload_json, detail_json = str(last[0]), last[1], last[2]
    try:
        payload = json.loads(payload_json or "{}")
    except ValueError:
        payload = {}
    try:
        detail = json.loads(detail_json or "{}")
    except ValueError:
        detail = {}
    pass_id = str(payload.get("pass_id") or "")
    if status != "done":
        # Failed (or any other final state): the same page again, under its own key.
        return {"source_id": source_id, "after": str(payload.get("after") or ""), "pass_id": pass_id, "page": PAGE_SIZE}
    if detail.get("end") or detail.get("skipped"):
        ended = _parse_clock(last[3]) or _now()
        if (_now() - ended).total_seconds() < PASS_INTERVAL_SECONDS:
            return None
        return {"source_id": source_id, "after": "", "pass_id": _new_pass_id(), "page": PAGE_SIZE}
    last_id = str(detail.get("last_id") or payload.get("after") or "")
    return {"source_id": source_id, "after": last_id, "pass_id": pass_id, "page": PAGE_SIZE}


def enqueue_catchup(conn: Any, source_id: Optional[str] = None) -> List[str]:
    """Queue the next page for each source with embedded canonical rows and nothing of its own in flight."""
    from ..pipeline.job_store import enqueue_job

    if not enabled() or not _index_ready(conn):
        return []
    queued = []
    for sid in _sources(conn):
        if source_id and sid != source_id:
            continue
        page = _next_page(conn, sid)
        if page is None:
            continue
        digest = hashlib.sha256(json.dumps([sid, page["pass_id"], page["after"]]).encode()).hexdigest()[:24]
        queued.append(enqueue_job(conn, kind=KIND, source_id=sid, idempotency_key=f"{KIND}:{sid}:{digest}",
                                  payload=page))
    return queued


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def scan_page(conn: Any, source_id: str, after: str, limit: int) -> Dict[str, Any]:
    """Read one page of a source's embedded canonical records and sort them. Returns counts, the cursor, the
    records to re-embed (prepared as the ingest hands them to the job) and, per stale record, the set of hashes
    its current renderings carry (every other chunk of the record is a leftover to prune). Never text."""
    from ..enrichment.jobs.canonical.brief_fallback import prepare_signal_record
    from ..query.retrieval import _index_current_texts
    from ..storage.adapters.sqlite.stores import _NATIVE_ID_COL
    from ..storage.share_excluded import row_share_excluded

    tables = _record_tables()
    types = sorted(tables)
    # `+source_id` keeps the planner off the source index, so the walk follows the record-id index from the cursor
    # and stops at the limit: 6 ms a page on 55k synthetic rows against 63 ms for a sort of the whole source, and the
    # cost no longer grows with the source. Either plan returns the same rows.
    page = conn.execute(
        "SELECT record_id, MIN(record_type), group_concat(DISTINCT content_hash) FROM signal_embeddings "
        f"WHERE +source_id=? AND vector_blob IS NOT NULL AND record_id > ? AND record_type IN ({','.join('?' for _ in types)}) "
        "GROUP BY record_id ORDER BY record_id LIMIT ?", (source_id, after, *types, limit),
    ).fetchall()
    out: Dict[str, Any] = {"scanned": len(page), "stale": 0, "reembed": [], "current_hashes": {},
                           "unreadable": 0, "share_excluded": 0, "no_text": 0,
                           "last_id": str(page[-1][0]) if page else after, "end": len(page) < limit}
    by_table: Dict[str, List[tuple]] = {}
    for record_id, record_type, hashes in page:
        table = tables.get(str(record_type or ""))
        if not table:
            out["unreadable"] += 1
            continue
        by_table.setdefault(table, []).append((str(record_id), str(record_type), hashes))
    for table, entries in by_table.items():
        id_col = _NATIVE_ID_COL[table]
        rows: Dict[str, Dict[str, Any]] = {}
        for start in range(0, len(entries), _ROW_CHUNK):
            chunk = entries[start:start + _ROW_CHUNK]
            cursor = conn.execute(
                f"SELECT * FROM {table} WHERE {id_col} IN ({','.join('?' for _ in chunk)})",
                [entry[0] for entry in chunk],
            )
            keys = [column[0] for column in cursor.description]
            for row in cursor.fetchall():
                record = dict(zip(keys, row))
                rows[str(record[id_col])] = record
        for record_id, record_type, hashes in entries:
            row = rows.get(record_id)
            if row is None:
                out["unreadable"] += 1
                continue
            if row_share_excluded(row):
                out["share_excluded"] += 1
                continue
            current = {_hash(text) for text in _index_current_texts(row)}
            if not current:
                out["no_text"] += 1
                continue
            stored = {h for h in str(hashes or "").split(",") if h}
            if stored and stored <= current:
                continue
            # Stale: a chunk sealed with text the row no longer holds, or (none stored today) no seal at all, which
            # the gate and the index refuse the same way.
            out["stale"] += 1
            out["current_hashes"][record_id] = current
            if stored & current:
                continue   # a current chunk exists: only the leftovers go
            prepared = prepare_signal_record(dict(row))
            prepared.update({"source_id": source_id, "_table": table, "record_type": record_type,
                             "record_id": record_id})
            if not prepared.get("ts"):
                prepared["ts"] = prepared.get("event_at") or prepared.get("entry_at") or prepared.get("occurred_at") \
                    or prepared.get("starts_at")
            out["reembed"].append(prepared)
    return out


def _prune_leftovers(conn: Any, current_hashes: Dict[str, set]) -> int:
    """Delete every chunk of these records whose hash matches none of the record's current renderings. Only after
    the page's re-embedding committed: a stale chunk is served to no one below the owner's tier and names text the
    row no longer holds, so nothing is lost; a failed page keeps everything."""
    from ..storage.adapters.sqlite.vector_search import delete_vec_rows
    from ..storage.db.write_gate import commit_connection, with_db_write

    if not current_hashes:
        return 0
    pruned = 0
    with with_db_write():
        for record_id, current in current_hashes.items():
            rows = conn.execute(
                "SELECT embedding_id, content_hash FROM signal_embeddings WHERE record_id=?", (record_id,)
            ).fetchall()
            ids = [str(row[0]) for row in rows if str(row[1] or "") not in current]
            if not ids:
                continue
            conn.execute(
                f"DELETE FROM signal_embeddings WHERE embedding_id IN ({','.join('?' for _ in ids)})", ids)
            delete_vec_rows(conn, ids)
            pruned += len(ids)
        commit_connection(conn)
    return pruned


async def execute_catchup(payload: Dict[str, Any]) -> Dict[str, Any]:
    from ..core.state import get_db_connection
    from ..enrichment.orchestrator import EnrichmentOrchestrator

    sid = str(payload.get("source_id") or "")
    after = payload.get("after")
    try:
        limit = int(payload.get("page") or 0)
    except (TypeError, ValueError):
        limit = 0
    if not sid or not isinstance(after, str) or not 0 < limit <= MAX_PAGE_SIZE:
        return {"status": "error", "error": "Invalid stale-text page"}
    if not enabled():
        # Done without work; discovery is off too, so nothing follows this page.
        return {"status": "ok", "skipped": f"{SWITCH} is off"}

    def load() -> Optional[Dict[str, Any]]:
        if not _attached(sid):
            return None
        conn = get_db_connection()
        if not _index_ready(conn):
            return {"scanned": 0, "stale": 0, "reembed": [], "current_hashes": {}, "unreadable": 0,
                    "share_excluded": 0, "no_text": 0, "last_id": after, "end": True}
        return scan_page(conn, sid, after, limit)

    scan = await asyncio.to_thread(load)
    if scan is None:
        return {"status": "ok", "skipped": "embeddings detached from source"}
    records = scan["reembed"]
    errors: List[str] = []
    created: Dict[str, Any] = {}
    if records:
        def progress(processed_count, total_count, job_name, job_percent, current_job_progress):
            update = payload.get("_progress_updater")
            if update:
                update({"status": "processing", "messages_processed": processed_count,
                        "messages_total": total_count, "current_job_name": job_name,
                        "current_job_progress_percent": current_job_progress})

        result = await EnrichmentOrchestrator().run_canonical(records, job_names=[JOB], progress_callback=progress)
        errors = list(result.get("errors") or [])
        created = result.get("records_created") or {}
    pruned = 0
    if not errors:
        # The connection is resolved inside the worker: one resolved on the loop must not be handed to another thread.
        pruned = await asyncio.to_thread(lambda: _prune_leftovers(get_db_connection(), scan["current_hashes"]))
    out = {"status": "error" if errors else "ok", "scanned": scan["scanned"], "stale": scan["stale"],
           "reembedded": len(records), "pruned_rows": pruned, "unreadable": scan["unreadable"],
           "share_excluded": scan["share_excluded"], "no_text": scan["no_text"],
           "last_id": scan["last_id"], "end": bool(scan["end"]) and not errors,
           "messages_processed": len(records), "records_created": created, "errors": errors}
    if errors:
        out["error"] = str(errors[:3])
    return out


def audit(conn: Any, source_id: Optional[str] = None, *, limit: Optional[int] = None) -> Dict[str, Dict[str, int]]:
    """Counts per source of what a full pass would find, with no writes and no model: how many embedded canonical
    records, how many stale, how many of those need the model, how many are skipped and why. For a measurement or a
    diagnosis; ``limit`` caps the records read per source. Counts only, never text."""
    out: Dict[str, Dict[str, int]] = {}
    if not _index_ready(conn):
        return out
    for sid in _sources(conn):
        if source_id and sid != source_id:
            continue
        totals = {"scanned": 0, "stale": 0, "needs_model": 0, "leftover_only": 0, "unreadable": 0,
                  "share_excluded": 0, "no_text": 0}
        after, remaining = "", limit
        while True:
            page = scan_page(conn, sid, after, min(PAGE_SIZE, remaining) if remaining is not None else PAGE_SIZE)
            for key in ("scanned", "stale", "unreadable", "share_excluded", "no_text"):
                totals[key] += page[key]
            totals["needs_model"] += len(page["reembed"])
            totals["leftover_only"] += page["stale"] - len(page["reembed"])
            after = page["last_id"]
            if remaining is not None:
                remaining -= page["scanned"]
            if page["end"] or (remaining is not None and remaining <= 0):
                break
        out[sid] = totals
    return out
