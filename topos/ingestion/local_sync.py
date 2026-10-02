"""Local sync ingestion: iMessage, Signal (read from local DB, write to conversation_messages)."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional

from .checkpoints.checkpoint_store import CheckpointStore, IngestionCheckpoint
from .checkpoints.sqlite_checkpoint_store import SqliteCheckpointStore
from .parsers import PARSER_REGISTRY
from .sources.base import RawRecord
from ..storage.db.write_gate import commit_connection, with_db_write

logger = logging.getLogger("topos.ingestion.local_sync")

IMESSAGE_SCHEMA_ID = "imessage.messages.v1"
SOURCE_ID_IMESSAGE = "imessage"

#: Resume after the newest message any earlier sync of this dataset read, and
#: nothing older. What the app's "since last" means.
MODE_SINCE_LAST = "since_last"
#: Resume from the cursor of the last unbounded scan, so history a bounded sync
#: skipped is read too. For iMessage this is what ``"all"`` meant until the app's
#: "since last" button, which has always sent ``"all"``, re-read 39,867 messages
#: that were already stored, on one click. It is now asked for by name.
MODE_FULL_HISTORY = "full_history"
#: iMessage modes that mean since-last. ``"all"`` stays one because every app
#: already shipped sends it for "since last".
_SINCE_LAST_MODES = frozenset({MODE_SINCE_LAST, "all", ""})


def stamp_conversation_table(canonical_messages: List[Dict[str, Any]]) -> None:
    """Stamp ``_table`` on local-sync records that name no table.

    Every local-sync source (iMessage, Signal) canonicalizes into
    ``conversation_messages``; a record that already declares a table keeps it.
    """
    for message in canonical_messages:
        if isinstance(message, dict):
            message.setdefault("_table", "conversation_messages")


def _run_local_sync_enrichment_if_enabled(
    *,
    db_conn: Any,
    source_id: str,
    canonical_messages: List[Dict[str, Any]],
) -> None:
    """Run canonical enrichment for local_sync sources when trigger is automatic."""
    if not canonical_messages:
        return
    from ..features.timeline_projection import project_timeline_rows

    # The lineage stamp, on the records enrichment will see — not on a copy.
    # These dicts are built from the local database's staging rows and name
    # no table; the timeline projection below has always stamped a COPY, so
    # the entities job received unstamped messages and wrote 17,203
    # `entity_mentions` rows with no `canonical_table` on one node (measured
    # 2026-09-17, every one resolving to this table). The job now refuses a
    # mention it cannot attribute, so an unstamped lane here would extract
    # nothing rather than something invisible.
    stamp_conversation_table(canonical_messages)
    timeline_rows = [dict(message) for message in canonical_messages]
    # Timeline is a lightweight canonical projection, not optional enrichment.
    # Let failures propagate so the sync checkpoint is not advanced past a gap.
    project_timeline_rows(db_conn, timeline_rows)

    try:
        from ..sources.registry import REGISTRY
        source_def = REGISTRY.get(source_id)
        if not source_def:
            return
        if getattr(source_def, "enrichment_trigger", "manual") != "automatic":
            return
        job_names = list(getattr(source_def, "canonical_enrichment_jobs", []) or [])
        if not job_names:
            return
        from ..enrichment.derived_tables import DerivedTablesManager
        from ..enrichment.orchestrator import EnrichmentOrchestrator
        import asyncio as _asyncio

        orchestrator = EnrichmentOrchestrator(tables_manager=DerivedTablesManager(conn=db_conn))
        _asyncio.run(orchestrator.run_canonical(canonical_messages, job_names=job_names))
    except Exception as e:
        logger.warning(
            "[PIPELINE:ENRICHMENT] local_sync enrichment failed (non-fatal): source_id=%s error=%s",
            source_id,
            e,
            exc_info=True,
        )


def _as_bool(value: Any, *, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _resolve_exclude_spam(
    options: Optional[Dict[str, Any]],
    *,
    db_conn: Any,
    dataset_id: str,
) -> bool:
    """Default on. sync_options.exclude_spam wins over the stored source setting."""
    if isinstance(options, dict) and "exclude_spam" in options:
        return _as_bool(options.get("exclude_spam"), default=True)
    try:
        from ..storage.source_settings import get_source_settings

        settings = get_source_settings(db_conn, dataset_id, SOURCE_ID_IMESSAGE) or {}
        if "exclude_spam" in settings:
            return _as_bool(settings.get("exclude_spam"), default=True)
    except Exception:
        logger.debug("exclude_spam setting lookup failed; defaulting to skip spam", exc_info=True)
    return True


def _resolve_sync_start_unix(options: Optional[Dict[str, Any]]) -> tuple[Optional[float], Optional[str]]:
    """Resolve sync start timestamp from sync options."""
    if not options:
        return None, None
    mode = str(options.get("mode") or "all").strip().lower()
    if mode in {"", "all", MODE_SINCE_LAST, MODE_FULL_HISTORY}:
        return None, None
    now = datetime.now(timezone.utc)
    if mode == "1m":
        return (now - timedelta(days=30)).timestamp(), None
    if mode == "3m":
        return (now - timedelta(days=90)).timestamp(), None
    if mode == "6m":
        return (now - timedelta(days=180)).timestamp(), None
    if mode == "1y":
        return (now - timedelta(days=365)).timestamp(), None
    if mode == "5y":
        return (now - timedelta(days=365 * 5)).timestamp(), None
    if mode == "custom":
        start_raw = options.get("start_date")
        if not start_raw:
            return None, "start_date is required for custom sync mode"
        try:
            start_text = str(start_raw).strip()
            if len(start_text) == 10:
                dt = datetime.fromisoformat(start_text).replace(tzinfo=timezone.utc)
            else:
                dt = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    dt = dt.astimezone(timezone.utc)
            return dt.timestamp(), None
        except Exception:
            return None, f"invalid start_date: {start_raw}"
    return None, f"unknown sync mode: {mode}"


def _map_normalized_records_with_canonical_mapper(
    normalized_records: List[Any],
    *,
    source_id: str,
) -> List[Dict[str, Any]]:
    """Map normalized records through source canonical mapper (with fallback)."""
    try:
        from ..canonicalization.mappers import MAPPER_REGISTRY
        from ..sources.registry import REGISTRY

        source_def = REGISTRY.get(source_id)
        mapper_id = getattr(source_def, "canonical_mapper_id", None) if source_def else None
        mapper_cls = MAPPER_REGISTRY.get(mapper_id) if mapper_id else None
        if not mapper_cls:
            raise ValueError(f"No canonical mapper registered for source_id={source_id} mapper_id={mapper_id}")
        mapper = mapper_cls()
        out: List[Dict[str, Any]] = []
        for norm in normalized_records:
            canonical = mapper.map(norm)
            payload = dict(canonical.payload or {})
            payload["source_id"] = source_id
            out.append(payload)
        return out
    except Exception as e:
        logger.warning(
            "[PIPELINE:CANONICAL] local_sync mapper unavailable for source_id=%s, using fallback payload mapping: %s",
            source_id,
            e,
        )
        out: List[Dict[str, Any]] = []
        for norm in normalized_records:
            p = dict(getattr(norm, "payload", {}) or {})
            if not p.get("message_id"):
                p["message_id"] = getattr(norm, "record_id", None)
            if not p.get("conversation_id"):
                p["conversation_id"] = p.get("thread_id")
            p["source_id"] = source_id
            out.append(p)
        return out


def _signal_reply_source_key_to_seconds(source_key: Any) -> Optional[int]:
    """Normalize Signal reply source key variants to Unix seconds for lookup."""
    if source_key is None:
        return None
    text = str(source_key).strip()
    if not text:
        return None
    if text.startswith("signal:"):
        parts = text.split(":")
        if len(parts) >= 3:
            try:
                return int(float(parts[-1]))
            except Exception:
                return None
    try:
        value = int(float(text))
    except Exception:
        return None
    # Common Signal quote.id style is milliseconds.
    if abs(value) >= 1_000_000_000_000:
        return int(value / 1000)
    return value


def _resolve_signal_reply_links(
    *,
    db_conn: Any,
    dataset_id: str,
    staging_records: List[Dict[str, Any]],
) -> None:
    """Resolve Signal reply source keys to canonical message_id when possible.

    This mutates staging_records in-place:
    - preserves original source reply key in _metadata.reply_to_source_key
    - updates reply_to_message_id to canonical message_id when matched
    """
    if not staging_records:
        return

    # Build in-batch lookup by (conversation/thread id, sent_at_seconds) -> message_id.
    batch_lookup: Dict[tuple[str, int], str] = {}
    for rec in staging_records:
        message_id = str(rec.get("message_id") or "")
        thread_id = str(rec.get("thread_id") or rec.get("conversation_id") or "")
        if not message_id or not thread_id:
            continue
        sec = _signal_reply_source_key_to_seconds(message_id)
        if sec is not None:
            batch_lookup[(thread_id, sec)] = message_id

    for rec in staging_records:
        source_key = rec.get("reply_to_message_id")
        if source_key is None:
            continue

        # Always preserve source-native linkage in metadata for traceability.
        if "_metadata" not in rec or not isinstance(rec.get("_metadata"), dict):
            rec["_metadata"] = {}
        rec["_metadata"]["reply_to_source_key"] = source_key

        source_key_text = str(source_key).strip()
        if not source_key_text:
            rec["reply_to_message_id"] = None
            continue

        # Already canonical format.
        if source_key_text.startswith("signal:"):
            rec["reply_to_message_id"] = source_key_text
            continue

        thread_id = str(rec.get("thread_id") or rec.get("conversation_id") or "")
        sec = _signal_reply_source_key_to_seconds(source_key_text)
        resolved: Optional[str] = None

        if sec is not None and thread_id:
            resolved = batch_lookup.get((thread_id, sec))

        # Fallback lookup in already-ingested canonical rows.
        if resolved is None and sec is not None and thread_id and db_conn is not None:
            like_suffix = f"%:{sec}"
            row = db_conn.execute(
                """
                SELECT message_id
                FROM conversation_messages
                WHERE dataset_id = ?
                  AND source_id = 'signal'
                  AND conversation_id = ?
                  AND message_id LIKE ?
                ORDER BY event_at DESC
                LIMIT 1
                """,
                (dataset_id, thread_id, like_suffix),
            ).fetchone()
            if row:
                resolved = row[0]

        # Store canonical link when matched; otherwise keep source key for now.
        if resolved:
            rec["reply_to_message_id"] = resolved
        else:
            rec["reply_to_message_id"] = source_key_text


def _backfill_signal_reply_links_in_db(*, db_conn: Any, dataset_id: str) -> int:
    """Resolve persisted Signal reply keys (ms/sec source keys -> canonical message_id)."""
    if db_conn is None:
        return 0
    updated = 0
    rows = db_conn.execute(
        """
        SELECT message_id, conversation_id, reply_to_message_id
        FROM conversation_messages
        WHERE dataset_id = ?
          AND source_id = 'signal'
          AND reply_to_message_id IS NOT NULL
          AND reply_to_message_id != ''
          AND reply_to_message_id NOT LIKE 'signal:%'
        """,
        (dataset_id,),
    ).fetchall()
    # Read-only pass first: resolve every target, then apply the updates in a
    # short gated write pass (per-row lookups stay off the write gate).
    pending: List[tuple[str, str]] = []
    for row in rows:
        row_message_id, conversation_id, reply_key = row
        sec = _signal_reply_source_key_to_seconds(reply_key)
        if sec is None:
            continue
        target = db_conn.execute(
            """
            SELECT message_id
            FROM conversation_messages
            WHERE dataset_id = ?
              AND source_id = 'signal'
              AND conversation_id = ?
              AND message_id LIKE ?
            ORDER BY event_at DESC
            LIMIT 1
            """,
            (dataset_id, conversation_id, f"%:{sec}"),
        ).fetchone()
        if not target:
            continue
        resolved_message_id = target[0]
        if not resolved_message_id or resolved_message_id == row_message_id:
            continue
        pending.append((resolved_message_id, row_message_id))
    if not pending:
        return 0
    with with_db_write():
        for resolved_message_id, row_message_id in pending:
            db_conn.execute(
                """
                UPDATE conversation_messages
                SET reply_to_message_id = ?
                WHERE message_id = ?
                """,
                (resolved_message_id, row_message_id),
            )
            updated += 1
        commit_connection(db_conn)
    return updated


def _emit_sync_progress(
    progress_cb: Optional[Callable[[Dict[str, Any]], None]],
    *,
    batch_num: int,
    records_processed: int,
    records_skipped: int,
    last_record_id: str,
) -> None:
    """Publish one batch's progress, if anyone is listening.

    Called only after ``save_checkpoint`` has made the batch durable, so a
    reported count is always a count that survives a crash — a progress number
    ahead of the checkpoint would re-run on restart and count twice.

    Never raises: a sync that has already written its rows must not fail
    because a progress sink did. The callback runs on the sync's worker thread
    (never the event loop), which is what lets it take the write gate.
    """
    if progress_cb is None:
        return
    try:
        progress_cb({
            "status": "processing",
            "batch_num": batch_num,
            "messages_processed": records_processed,
            "messages_skipped": records_skipped,
            "records_processed": records_processed,
            "records_skipped": records_skipped,
            "last_record_id": last_record_id,
        })
    except Exception as exc:  # noqa: BLE001 — progress is best-effort
        logger.debug("sync progress callback failed: %s", exc)


#: Checkpoint metadata naming the cursor of the last UNBOUNDED sync. A bounded
#: sync (``mode="3m"`` and friends) scans ROWID 0 upward through a date filter,
#: so the cursor it saves says nothing about older rows; ``last_record_id`` alone
#: cannot tell the two apart. Only this key lets ``mode="all"`` resume.
UNBOUNDED_CURSOR_KEY = "unbounded_last_record_id"
#: The spam policy that unbounded cursor ran under (iMessage only).
UNBOUNDED_EXCLUDE_SPAM_KEY = "unbounded_exclude_spam"
#: Set on the unbounded cursor when it was adopted from a checkpoint written before
#: coverage was recorded (Signal only), so an unproven cursor is never
#: indistinguishable from a proven one. Kept for as long as that cursor lineage is.
UNBOUNDED_INHERITED_KEY = "unbounded_inherited_legacy"
#: Written on every checkpoint save since coverage was recorded. Its absence is
#: what marks a checkpoint as legacy; a missing cursor alone does not, because a
#: bounded sync on a fresh node also saves none.
COVERAGE_RECORDED_KEY = "coverage_recorded"
#: Scanned iMessage ROWIDs behind the cursor that were not written: ROWID -> reason.
HELD_ROWIDS_KEY = "held_rowids"


def _unbounded_coverage(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """The unbounded-cursor keys of a checkpoint's metadata, for carrying forward."""
    return {
        k: metadata[k]
        for k in (UNBOUNDED_CURSOR_KEY, UNBOUNDED_EXCLUDE_SPAM_KEY, UNBOUNDED_INHERITED_KEY)
        if k in metadata
    }


def _adopt_legacy_signal_cursor(metadata: Dict[str, Any], last_record_id: str) -> Dict[str, Any]:
    """Treat a pre-coverage Signal checkpoint's cursor as unbounded, and say so.

    Signal and iMessage differ in what their legacy checkpoints probably are. With
    no sync options, Signal syncs ``mode="all"``, so a legacy Signal cursor almost
    certainly covers all history, and rescanning it would re-enrich every message
    to recover nothing. iMessage defaults to ``mode="3m"`` at both doors, so its
    legacy cursor is not adopted (see ``_resume_cursor``). The cost of trusting is
    that a Signal checkpoint an owner did write from a bounded sync keeps its gap;
    ``mode="custom"`` with an early ``start_date`` always rescans from the start.
    """
    if metadata.get(COVERAGE_RECORDED_KEY) or UNBOUNDED_CURSOR_KEY in metadata:
        return metadata
    if not last_record_id or last_record_id == "0":
        return metadata
    return {**metadata, UNBOUNDED_CURSOR_KEY: last_record_id, UNBOUNDED_INHERITED_KEY: True}


def _resume_cursor(
    metadata: Dict[str, Any],
    *,
    start_unix: Optional[float],
    exclude_spam: Optional[bool] = None,
) -> str:
    """Where a sync starts: the saved unbounded cursor only if it covers this request.

    A bounded sync always rescans its window from ROWID 0, as it always has (the
    rescan also heals bodies inside the window). An unbounded sync resumes only
    from a cursor an unbounded sync saved, under a spam policy that skipped no
    more than this one does. A legacy iMessage checkpoint cannot say which it was,
    and both doors default iMessage to a bounded sync, so it is rescanned once;
    Signal's legacy cursor is adopted before this is called.
    """
    if start_unix is not None:
        return "0"
    cursor = metadata.get(UNBOUNDED_CURSOR_KEY)
    if not isinstance(cursor, str) or not cursor:
        return "0"
    if exclude_spam is False and _as_bool(metadata.get(UNBOUNDED_EXCLUDE_SPAM_KEY), default=True):
        return "0"
    return cursor


#: Checkpoint metadata: the highest chat.db ROWID any sync of this dataset has
#: scanned and saved, whatever its mode. Only ever raised. The last batch cursor
#: cannot serve: an all-history rescan that is stopped part-way saves its own,
#: LOWER cursor over it, and a since-last sync resuming there re-reads everything
#: in between.
HIGH_WATER_KEY = "high_water_rowid"
#: The native time (ISO 8601, UTC) of the message at the high-water mark.
HIGH_WATER_AT_KEY = "high_water_at"

#: ``outcome`` of a since-last run that wrote nothing because its plan needs the
#: owner: there was no checkpoint to resume from, or chat.db no longer reaches it.
OUTCOME_NEEDS_CONFIRMATION = "needs_confirmation"
#: ``outcome`` of ``dry_run``: the plan, and nothing else.
OUTCOME_PREVIEW = "preview"
OUTCOME_IMPORTED = "imported"
OUTCOME_UP_TO_DATE = "up_to_date"

#: A sync refused because another run of the same dataset holds it.
SYNC_IN_PROGRESS = "sync_in_progress"
#: A sync refused because this node's iMessage is enrolled for another dataset.
DATASET_NOT_ENROLLED = "dataset_not_enrolled"
#: Every reader contract an owner-attested iMessage enrollment can name, read without
#: importing the permissions package into every sync: the snapshot lane's
#: (``ingest_protocol.IMESSAGE_READER_CONTRACT``) and the existing-row comparison's
#: (``imessage_reconciliation.RECONCILIATION_CONTRACTS``), which is the lane an owner's
#: recovery and refresh enroll. A test pins this set to those constants.
IMESSAGE_ENROLLMENT_CONTRACTS = frozenset({
    "imessage-owner-snapshot/v1",
    "imessage-existing-comparison/v2",
    "imessage-existing-comparison/v3",
})

#: Bounds a caller may set on one run through ``sync_options``. A scheduled run
#: uses small batches and a pause, so each write-gate section stays short and a
#: recipient search waiting on the gate gets in between batches.
MIN_BATCH_SIZE = 50
MAX_BATCH_SIZE = 5000
MAX_PAUSE_SECONDS = 30.0


def _rowid(cursor: Any) -> Optional[int]:
    """The positive ROWID in an ``imessage:<n>`` cursor or a bare number, else None."""
    if cursor is None or isinstance(cursor, bool):
        return None
    if isinstance(cursor, int):
        return cursor if cursor > 0 else None
    tail = str(cursor).strip().split(":")[-1]
    try:
        value = int(tail)
    except ValueError:
        return None
    return value if value > 0 else None


def imessage_high_water(last_record_id: Any, metadata: Optional[Dict[str, Any]]) -> Optional[int]:
    """The ROWID a since-last sync resumes after, or None when no checkpoint names one.

    The highest of what a checkpoint says was read: the recorded high-water mark,
    the last batch cursor and the unbounded cursor. A checkpoint saved before the
    mark was recorded still has the other two.
    """
    md = metadata or {}
    found = [
        _rowid(last_record_id),
        _rowid(md.get(UNBOUNDED_CURSOR_KEY)),
        _rowid(md.get(HIGH_WATER_KEY)),
    ]
    return max((value for value in found if value), default=None)


def _bounded_int(value: Any, *, default: int, lo: int, hi: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


def _bounded_float(value: Any, *, default: float, lo: float, hi: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return max(lo, min(hi, number))


def sync_batch_size(sync_options: Optional[Dict[str, Any]], default: int = MAX_BATCH_SIZE) -> int:
    """The batch size a run asked for, clamped to what the sync supports."""
    return _bounded_int((sync_options or {}).get("batch_size"), default=default, lo=MIN_BATCH_SIZE, hi=MAX_BATCH_SIZE)


def _iso(unix_ts: Optional[float]) -> Optional[str]:
    if unix_ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(unix_ts), tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _imessage_mode(sync_options: Optional[Dict[str, Any]]) -> str:
    """``since_last``, ``full_history`` or ``bounded``. No options at all is since-last."""
    if not sync_options:
        return MODE_SINCE_LAST
    mode = str(sync_options.get("mode") or "").strip().lower()
    if mode in _SINCE_LAST_MODES:
        return MODE_SINCE_LAST
    if mode == MODE_FULL_HISTORY:
        return MODE_FULL_HISTORY
    return "bounded"


_DATASET_LOCKS: Dict[tuple, threading.Lock] = {}
_DATASET_LOCKS_GUARD = threading.Lock()


@contextmanager
def exclusive_sync(source_id: str, dataset_id: str) -> Iterator[bool]:
    """Hold this dataset's sync for the block, or yield False if a run already holds it.

    The job lane already runs one sync at a time and refuses to enqueue a second
    for a dataset with one queued or running. This covers every other way in: a
    direct call, a second lane, a script. It never waits. A second run that queued
    behind the first would re-read, in the same process, whatever the first had
    not saved yet.
    """
    with _DATASET_LOCKS_GUARD:
        lock = _DATASET_LOCKS.setdefault((source_id, dataset_id), threading.Lock())
    acquired = lock.acquire(blocking=False)
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


def enrolled_imessage_datasets(conn: Any) -> frozenset:
    """Datasets holding an ACTIVE owner-attested iMessage provenance enrollment.

    Read-only. A node with no enrollment table has none. A table that exists but
    cannot be read raises: the guard that uses this fails closed.
    """
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingest_provenance_enrollments'"
    ).fetchone()
    if not found:
        return frozenset()
    enrolled = set()
    for dataset_id, snapshot_json in conn.execute(
        "SELECT dataset_id, snapshot_json FROM ingest_provenance_enrollments WHERE state='active'"
    ).fetchall():
        try:
            contract = (json.loads(snapshot_json) or {}).get("reader_contract")
        except (TypeError, ValueError):
            contract = None
        if isinstance(contract, str) and contract in IMESSAGE_ENROLLMENT_CONTRACTS and dataset_id:
            enrolled.add(str(dataset_id))
    return frozenset(enrolled)


def enrolled_dataset_refusal(
    conn: Any, dataset_id: str, sync_options: Optional[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Refuse an iMessage sync into a dataset other than the enrolled one, or None.

    Message ids are ``imessage:<ROWID>`` and unique across datasets, and a write
    that meets an existing id leaves it where it is. So a sync into another dataset
    claims every new message for that dataset for good; the enrolled dataset can
    never receive them. And that dataset's first receipt INSERTs a
    ``user_ingestion_sources`` row, which moves the source clock and stales the
    enrollment. ``sync_options.allow_unenrolled_dataset`` overrides, deliberately.
    """
    if _as_bool((sync_options or {}).get("allow_unenrolled_dataset"), default=False):
        return None
    try:
        enrolled = enrolled_imessage_datasets(conn)
    except sqlite3.Error as exc:
        return {
            "status": "error",
            "code": DATASET_NOT_ENROLLED,
            "error": f"Could not read the iMessage enrollment, so the sync did not start: {exc}",
            "records_processed": 0,
            "records_skipped": 0,
        }
    if not enrolled or dataset_id in enrolled:
        return None
    return {
        "status": "error",
        "code": DATASET_NOT_ENROLLED,
        "error": (
            "iMessage on this node is enrolled for a different dataset. Syncing this one would "
            "file new messages outside the enrolled dataset for good, and its first sync would "
            "add a source row that stales the enrollment. Switch to the enrolled dataset, or "
            "send allow_unenrolled_dataset to sync here anyway."
        ),
        "records_processed": 0,
        "records_skipped": 0,
    }


def _stored_rowids(conn: Any, rowids: List[int]) -> set:
    """Which of these chat.db ROWIDs already have a canonical row, in any dataset."""
    if not rowids:
        return set()
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_messages'"
    ).fetchone()
    if not found:
        return set()
    stored: set = set()
    chunk = 500
    for start in range(0, len(rowids), chunk):
        ids = [f"imessage:{rowid}" for rowid in rowids[start:start + chunk]]
        for (message_id,) in conn.execute(
            f"SELECT message_id FROM conversation_messages WHERE message_id IN ({','.join('?' * len(ids))})",
            ids,
        ).fetchall():
            parsed = _rowid(message_id)
            if parsed is not None:
                stored.add(parsed)
    return stored


def plan_imessage_since_last(
    *,
    db_conn: Any,
    high_water: Optional[int],
    high_water_at: Optional[str],
    held_count: int,
    chat_db_path: Any,
    exclude_spam: bool,
) -> Dict[str, Any]:
    """What a since-last sync would do now: where it starts, and what it would read.

    Counts only. Trusted when a checkpoint names a high-water mark that chat.db
    still reaches; otherwise the plan starts from the beginning and a run needs
    the owner to confirm it, with these numbers in front of them.
    """
    from .sources.imessage_reader import inspect_imessage_backlog

    start = high_water or 0
    backlog = inspect_imessage_backlog(start, chat_db_path=chat_db_path, exclude_spam=exclude_spam)
    reason = "checkpoint"
    if high_water is None:
        reason = "no_checkpoint"
    elif backlog.chat_db_max_rowid is not None and high_water > backlog.chat_db_max_rowid:
        # chat.db's newest message is older than what was already read: it was
        # reset or replaced. Resuming would read nothing, silently, forever.
        reason = "checkpoint_beyond_chat_db"
        start = 0
        backlog = inspect_imessage_backlog(0, chat_db_path=chat_db_path, exclude_spam=exclude_spam)
    stored = _stored_rowids(db_conn, [rowid for rowid, _ in backlog.dated])
    # The dates describe what a run would import, not what it would pass over.
    new_times = [when for rowid, when in backlog.dated if rowid not in stored]
    known = [when for when in new_times if when is not None]
    return {
        "mode": MODE_SINCE_LAST,
        "trusted": reason == "checkpoint",
        "reason": reason,
        "start_rowid": start,
        "high_water_rowid": high_water,
        "high_water_at": high_water_at,
        "chat_db_max_rowid": backlog.chat_db_max_rowid,
        "messages": backlog.messages,
        "spam_skipped": backlog.spam,
        "already_stored": len(stored),
        "to_import": len(new_times),
        "first_at": _iso(min(known)) if known else None,
        "last_at": _iso(max(known)) if known else None,
        "held_to_retry": held_count,
    }


def _confirms(sync_options: Optional[Dict[str, Any]], plan: Dict[str, Any]) -> bool:
    """True when the caller confirmed exactly this plan's starting point."""
    raw = (sync_options or {}).get("confirm_start_rowid")
    if raw is None or isinstance(raw, bool):
        return False
    try:
        return int(raw) == int(plan.get("start_rowid") or 0)
    except (TypeError, ValueError):
        return False


def describe_imessage_checkpoint(conn: Any, dataset_id: str) -> Dict[str, Any]:
    """Where the next since-last sync of this dataset starts, from the checkpoint alone.

    Read-only and cheap (no chat.db): for the settings screen. ``high_water_at`` is
    known only once a sync has saved it.
    """
    empty = {
        "has_checkpoint": False,
        "high_water_rowid": None,
        "high_water_at": None,
        "unbounded_rowid": None,
        "held": 0,
        "updated_at": None,
    }
    if conn is None or not dataset_id:
        return empty
    try:
        found = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingestion_checkpoints'"
        ).fetchone()
        if not found:
            return empty
        row = conn.execute(
            "SELECT last_record_id, metadata_json, updated_at FROM ingestion_checkpoints "
            "WHERE dataset_id = ? AND schema_id = ?",
            (dataset_id, IMESSAGE_SCHEMA_ID),
        ).fetchone()
    except sqlite3.Error as exc:
        logger.debug("checkpoint describe failed: %s", exc)
        return empty
    if not row:
        return empty
    try:
        metadata = json.loads(row[1]) if row[1] else {}
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    held = metadata.get(HELD_ROWIDS_KEY)
    high_water = imessage_high_water(row[0], metadata)
    high_water_at = metadata.get(HIGH_WATER_AT_KEY)
    if high_water is not None and not high_water_at:
        # A checkpoint from before the time was recorded: the stored row at that
        # ROWID, if there is one, carries it (a primary-key lookup).
        try:
            stored = conn.execute(
                "SELECT event_at FROM conversation_messages WHERE message_id = ?",
                (f"imessage:{high_water}",),
            ).fetchone()
            high_water_at = stored[0] if stored and stored[0] else None
        except sqlite3.Error:
            high_water_at = None
    return {
        "has_checkpoint": high_water is not None,
        "high_water_rowid": high_water,
        "high_water_at": high_water_at,
        "unbounded_rowid": _rowid(metadata.get(UNBOUNDED_CURSOR_KEY)),
        "held": len(held) if isinstance(held, dict) else 0,
        "updated_at": row[2],
    }


def run_imessage_sync(
    dataset_id: str,
    *,
    checkpoint_store: Optional[CheckpointStore] = None,
    db_conn: Optional[Any] = None,
    chat_db_path: Optional[Any] = None,
    batch_size: int = 5000,
    sync_options: Optional[Dict[str, Any]] = None,
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """
    Run iMessage sync: load checkpoint → read from chat.db → parse → write to conversation_messages → save checkpoint.
    Returns dict with status, records_processed, records_skipped, last_record_id, error (if any).

    Modes (``sync_options.mode``): ``since_last`` (also ``"all"``, and no options)
    resumes after the dataset's high-water mark and reads nothing older; with no
    checkpoint to trust it returns ``outcome="needs_confirmation"`` and a ``plan``
    (counts and dates, nothing written) until the caller sends that plan's
    ``confirm_start_rowid``. ``dry_run`` returns the plan alone. ``full_history``
    resumes from the last unbounded scan's cursor, so history a bounded sync
    skipped is read too. ``1m``..``5y`` and ``custom`` rescan their window.

    One run per dataset at a time (``code="sync_in_progress"`` otherwise), and
    never into a dataset other than the enrolled one while an iMessage
    enrollment exists (``code="dataset_not_enrolled"``).
    """
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required", "records_processed": 0, "records_skipped": 0}

    if db_conn is None:
        from ..core.state import get_db_connection
        db_conn = get_db_connection()
    if db_conn is None:
        return {"status": "error", "error": "Database connection not available", "records_processed": 0, "records_skipped": 0}

    with exclusive_sync(SOURCE_ID_IMESSAGE, dataset_id) as acquired:
        if not acquired:
            return {
                "status": "error",
                "code": SYNC_IN_PROGRESS,
                "error": "Another sync of this dataset is running.",
                "records_processed": 0,
                "records_skipped": 0,
            }
        refusal = enrolled_dataset_refusal(db_conn, dataset_id, sync_options)
        if refusal is not None:
            return refusal

        store = checkpoint_store if checkpoint_store is not None else SqliteCheckpointStore(db_conn)
        checkpoint = store.get_checkpoint(dataset_id, IMESSAGE_SCHEMA_ID)
        last_record_id = checkpoint.last_record_id if checkpoint else "0"
        checkpoint_metadata = dict(checkpoint.metadata) if checkpoint and isinstance(checkpoint.metadata, dict) else {}

        logger.info(
            "run_imessage_sync starting: dataset_id=%s last_record_id=%s mode=%s",
            dataset_id[:24] + "..." if len(dataset_id) > 24 else dataset_id,
            last_record_id[:20] + "..." if last_record_id and len(last_record_id) > 20 else last_record_id,
            _imessage_mode(sync_options),
        )

        try:
            return _run_imessage_sync_impl(
                dataset_id=dataset_id,
                db_conn=db_conn,
                store=store,
                last_record_id=last_record_id,
                checkpoint_metadata=checkpoint_metadata,
                chat_db_path=chat_db_path,
                batch_size=batch_size,
                sync_options=sync_options,
                progress_cb=progress_cb,
            )
        except Exception as e:
            logger.warning(
                "run_imessage_sync failed (top-level catch): %s",
                e,
                exc_info=True,
            )
            return {"status": "error", "error": str(e), "records_processed": 0, "records_skipped": 0}


def _run_imessage_sync_impl(
    dataset_id: str,
    *,
    db_conn: Any,
    store: CheckpointStore,
    last_record_id: str,
    checkpoint_metadata: Optional[Dict[str, Any]] = None,
    chat_db_path: Optional[Any] = None,
    batch_size: int = 5000,
    sync_options: Optional[Dict[str, Any]] = None,
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """Implementation of run_imessage_sync (called inside try so we never raise).

    Every scanned row ends in exactly one of: written (``records_processed``),
    skipped by the owner's spam policy (``records_skipped``), or held
    (``records_held``). A held row did not validate or had no body the reader can
    build; the cursor still moves past it, so one bad row cannot stall the sync,
    but its ROWID is kept in the checkpoint and retried at the start of every
    later sync until it is written or leaves chat.db.

    Every save also raises the high-water mark, the newest ROWID any sync of
    this dataset has read, which is where the next since-last sync starts.
    """
    start_unix, start_error = _resolve_sync_start_unix(sync_options)
    if start_error:
        return {"status": "error", "error": start_error, "records_processed": 0, "records_skipped": 0}
    mode = _imessage_mode(sync_options)
    dry_run = _as_bool((sync_options or {}).get("dry_run"), default=False)
    if dry_run and mode != MODE_SINCE_LAST:
        # A caller asking for a dry run must never get a real one.
        return {"status": "error", "error": "dry_run is only available for since_last", "records_processed": 0, "records_skipped": 0}
    pause_seconds = _bounded_float(
        (sync_options or {}).get("pause_seconds"), default=0.0, lo=0.0, hi=MAX_PAUSE_SECONDS
    )

    parser_cls = PARSER_REGISTRY.get(IMESSAGE_SCHEMA_ID)
    if not parser_cls:
        return {"status": "error", "error": "No parser for imessage.messages.v1", "records_processed": 0, "records_skipped": 0}
    parser = parser_cls(dataset_id=dataset_id, _schema_id=IMESSAGE_SCHEMA_ID)
    from ..storage.canonical import ConversationsTablesManager
    manager = ConversationsTablesManager(db_conn)
    from .sources.imessage_reader import read_imessage_batch, get_chat_db_path
    path = chat_db_path or get_chat_db_path()
    exclude_spam = _resolve_exclude_spam(sync_options, db_conn=db_conn, dataset_id=dataset_id)

    prior = dict(checkpoint_metadata or {})
    stored_held = prior.get(HELD_ROWIDS_KEY)
    held: Dict[str, str] = {
        str(k): str(v) for k, v in (stored_held if isinstance(stored_held, dict) else {}).items()
        if str(k).isdigit()
    }
    high_water = {
        "rowid": imessage_high_water(last_record_id, prior),
        "at": prior.get(HIGH_WATER_AT_KEY),
    }

    plan: Optional[Dict[str, Any]] = None
    if mode == MODE_SINCE_LAST:
        try:
            plan = plan_imessage_since_last(
                db_conn=db_conn,
                high_water=high_water["rowid"],
                high_water_at=high_water["at"],
                held_count=len(held),
                chat_db_path=path,
                exclude_spam=exclude_spam,
            )
        except (OSError, sqlite3.Error) as e:
            # FileNotFoundError and PermissionError (no Full Disk Access) included.
            return {"status": "error", "error": str(e), "records_processed": 0, "records_skipped": 0}
        if dry_run:
            return {"status": "ok", "outcome": OUTCOME_PREVIEW, "plan": plan, "records_processed": 0, "records_skipped": 0}
        if not plan["trusted"] and not _confirms(sync_options, plan):
            logger.info(
                "run_imessage_sync waiting for the owner: reason=%s start_rowid=%s to_import=%s",
                plan["reason"],
                plan["start_rowid"],
                plan["to_import"],
            )
            return {
                "status": "ok",
                "outcome": OUTCOME_NEEDS_CONFIRMATION,
                "plan": plan,
                "records_processed": 0,
                "records_skipped": 0,
            }
        current_last_record_id = f"imessage:{plan['start_rowid']}" if plan["start_rowid"] else "0"
    else:
        current_last_record_id = _resume_cursor(prior, start_unix=start_unix, exclude_spam=exclude_spam)

    # A scan extends the proven all-history coverage only when it starts inside
    # it -- from ROWID 0, or at or below the unbounded cursor under a spam policy
    # that skipped no more than that cursor's did. A since-last sync starting
    # above the cursor leaves the gap between them unproven, and says so by not
    # moving it.
    start_rowid = _rowid(current_last_record_id) or 0
    unbounded_rowid = _rowid(prior.get(UNBOUNDED_CURSOR_KEY))
    extends_coverage = start_unix is None and (
        start_rowid == 0
        or (
            unbounded_rowid is not None
            and start_rowid <= unbounded_rowid
            and not (exclude_spam is False and _as_bool(prior.get(UNBOUNDED_EXCLUDE_SPAM_KEY), default=True))
        )
    )
    final_last_record_id = last_record_id
    coverage = _unbounded_coverage(prior)
    # Held ROWIDs are retried first, in one pass, from a single chat.db snapshot.
    pending_retry: Optional[List[int]] = sorted(int(k) for k in held) or None
    total_processed = 0
    total_skipped = 0
    batch_num = 0

    def _save(last: str) -> None:
        metadata: Dict[str, Any] = {COVERAGE_RECORDED_KEY: True, "exclude_spam": exclude_spam, **coverage}
        if high_water["rowid"]:
            metadata[HIGH_WATER_KEY] = high_water["rowid"]
            if high_water["at"]:
                metadata[HIGH_WATER_AT_KEY] = high_water["at"]
        if held:
            metadata[HELD_ROWIDS_KEY] = dict(held)
        store.save_checkpoint(IngestionCheckpoint(
            dataset_id=dataset_id,
            schema_id=IMESSAGE_SCHEMA_ID,
            last_record_id=last,
            metadata=metadata,
        ))

    while True:
        batch_num += 1
        retrying, pending_retry = pending_retry, None
        try:
            if retrying is not None:
                batch = read_imessage_batch(
                    rowids=retrying,
                    chat_db_path=path,
                    exclude_spam=exclude_spam,
                )
            else:
                batch = read_imessage_batch(
                    last_rowid=current_last_record_id if current_last_record_id != "0" else None,
                    chat_db_path=path,
                    batch_size=batch_size,
                    start_unix=start_unix,
                    exclude_spam=exclude_spam,
                )
        except FileNotFoundError as e:
            return {"status": "error", "error": str(e), "records_processed": total_processed, "records_skipped": total_skipped}
        except PermissionError as e:
            return {"status": "error", "error": str(e), "records_processed": total_processed, "records_skipped": total_skipped}
        except OSError as e:
            logger.warning(
                "imessage read failed (OSError errno=%s) on batch %d: %s",
                getattr(e, "errno", None),
                batch_num,
                e,
                exc_info=True,
            )
            return {"status": "error", "error": str(e), "records_processed": total_processed, "records_skipped": total_skipped}
        except sqlite3.Error as e:
            logger.warning(
                "imessage read failed (sqlite3.Error) on batch %d: %s",
                batch_num,
                e,
                exc_info=True,
            )
            return {"status": "error", "error": str(e), "records_processed": total_processed, "records_skipped": total_skipped}
        except Exception as e:
            logger.warning("imessage read failed on batch %d: %s", batch_num, e, exc_info=True)
            return {"status": "error", "error": str(e), "records_processed": total_processed, "records_skipped": total_skipped}

        rows = batch.rows
        total_skipped += batch.records_skipped
        if retrying is not None:
            # Settled unless held again below; a ROWID no longer in chat.db is gone.
            for rowid in retrying:
                held.pop(str(rowid), None)
        elif batch.scanned_count == 0:
            break
        held.update({str(rowid): reason for rowid, reason in batch.held.items()})

        # Persist raw iMessage payloads for traceability and debugging (non-fatal on failure).
        try:
            from ..storage.raw.raw_tables_manager import RawTablesManager
            raw_tables_manager = RawTablesManager(db_conn)
            for row in rows:
                raw_tables_manager.write_raw_record(
                    source_id=SOURCE_ID_IMESSAGE,
                    source_record_id=str(row.get("id") or ""),
                    payload=row,
                    source_type="chat_messages",
                )
        except Exception as e:
            logger.warning("[PIPELINE:RAW] iMessage raw write failed (non-fatal): %s", e)

        normalized_records: List[Any] = []
        for row in rows:
            raw = RawRecord(record_id=row["id"], payload=row)
            validation = parser.validate(raw)
            if not validation.is_valid:
                logger.debug("Hold invalid row: %s", validation.errors)
                if row.get("ROWID") is not None:
                    held[str(row["ROWID"])] = "invalid_record"
                continue
            norm = parser.parse(raw)
            normalized_records.append(norm)

        if normalized_records:
            mapped_records = _map_normalized_records_with_canonical_mapper(
                normalized_records,
                source_id=SOURCE_ID_IMESSAGE,
            )
            staging_records: List[Dict[str, Any]] = []
            for rec in mapped_records:
                thread_id = rec.get("thread_id") or rec.get("conversation_id") or dataset_id
                # chat.db's is_from_me, never the sender id: a correspondent
                # handle can be spelled 'self'.
                is_self = rec.get("is_from_self") is True
                staging = {
                    "message_id": rec.get("message_id"),
                    "dataset_id": dataset_id,
                    "thread_id": thread_id,
                    "ts": rec.get("ts") or datetime.now(timezone.utc).isoformat(),
                    # The fill above is ingestion time standing in for a missing
                    # native time. Say so, so the canonical writer can record it
                    # as a substitute rather than as an event time.
                    "_event_time_substituted": not rec.get("ts"),
                    "sender_type": rec.get("sender_type", "human"),
                    "sender_id": rec.get("sender_id"),
                    "from_self": is_self,
                    "reply_to_message_id": rec.get("reply_to_message_id"),
                    "message_type": rec.get("message_type"),
                    "event_type": rec.get("event_type"),
                    "content": rec.get("content"),
                    "source_id": SOURCE_ID_IMESSAGE,
                }
                if "_metadata" in rec:
                    staging["_metadata"] = rec["_metadata"]
                staging_records.append(staging)

            try:
                manager.upsert_message_batch(staging_records, dataset_id, SOURCE_ID_IMESSAGE)
            except Exception as e:
                logger.exception("ConversationsTablesManager.upsert_message_batch failed")
                return {
                    "status": "error",
                    "error": str(e),
                    "records_processed": total_processed,
                    "records_skipped": total_skipped,
                }

            canonical_messages = [
                {
                    "message_id": rec.get("message_id"),
                    "conversation_id": rec.get("thread_id") or dataset_id,
                    "sender_type": rec.get("sender_type"),
                    "sender_id": rec.get("sender_id"),
                    "reply_to_message_id": rec.get("reply_to_message_id"),
                    "message_type": rec.get("message_type"),
                    "event_type": rec.get("event_type"),
                    "ts": rec.get("ts"),
                    "content": rec.get("content"),
                    "source_id": SOURCE_ID_IMESSAGE,
                }
                for rec in staging_records
            ]
            _run_local_sync_enrichment_if_enabled(
                db_conn=db_conn,
                source_id=SOURCE_ID_IMESSAGE,
                canonical_messages=canonical_messages,
            )

            total_processed += len(normalized_records)

        if retrying is not None:
            # The cursor does not move on a retry; persist the shrunken hold list.
            _save(final_last_record_id)
            continue

        if batch.max_scanned_rowid is None:
            # Defensive: avoid infinite loops if no valid rowid in batch.
            break

        final_last_record_id = f"imessage:{batch.max_scanned_rowid}"
        if extends_coverage:
            coverage = {
                UNBOUNDED_CURSOR_KEY: final_last_record_id,
                UNBOUNDED_EXCLUDE_SPAM_KEY: exclude_spam,
            }
        if batch.max_scanned_rowid > (high_water["rowid"] or 0):
            high_water["rowid"] = int(batch.max_scanned_rowid)
            high_water["at"] = _iso(batch.max_scanned_at)
        _save(final_last_record_id)
        current_last_record_id = final_last_record_id
        _emit_sync_progress(
            progress_cb,
            batch_num=batch_num,
            records_processed=total_processed,
            records_skipped=total_skipped,
            last_record_id=final_last_record_id,
        )

        if batch.scanned_count < batch_size:
            break
        if pause_seconds:
            # Between batches, never inside one: the batch's rows and its
            # checkpoint are already committed, and the write gate is free for
            # whoever has been waiting on it.
            time.sleep(pause_seconds)

    held_reasons: Dict[str, int] = {}
    for reason in held.values():
        held_reasons[reason] = held_reasons.get(reason, 0) + 1
    result: Dict[str, Any] = {
        "status": "ok",
        "outcome": OUTCOME_IMPORTED if total_processed else OUTCOME_UP_TO_DATE,
        "records_processed": total_processed,
        "records_skipped": total_skipped,
        "records_held": len(held),
        "held_reasons": held_reasons,
        "exclude_spam": exclude_spam,
        "last_record_id": final_last_record_id,
        "start_rowid": start_rowid,
        "high_water_rowid": high_water["rowid"],
    }
    if plan is not None:
        result["plan"] = plan
    return result


SIGNAL_SCHEMA_ID = "signal.messages.v1"
SOURCE_ID_SIGNAL = "signal"


def run_signal_upload(
    dataset_id: str,
    file_bytes: bytes,
    *,
    my_phone_number: Optional[str] = None,
    owner_user_id: Optional[str] = None,
    db_conn: Optional[Any] = None,
    writer_class: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Parse Signal export file (JSON) and write to conversation_messages.
    Uses stored Signal identity for dataset_id if my_phone_number not provided.

    ``writer_class`` is the door that started the upload
    (features/provenance/writer_class.py). The export decides ``is_from_self``
    (an ``outgoing`` message, or the caller's ``my_phone_number``), so a row an
    upload wrote is the owner's only when an owner door made the upload.
    """
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required", "records_processed": 0}
    if not file_bytes:
        return {"status": "error", "error": "file_bytes required", "records_processed": 0}

    if db_conn is None:
        from ..core.state import get_db_connection
        db_conn = get_db_connection()
    if db_conn is None:
        return {"status": "error", "error": "Database connection not available", "records_processed": 0}

    if my_phone_number is None and owner_user_id is None:
        from ..storage.signal_identity import get_signal_identity
        identity = get_signal_identity(db_conn, dataset_id)
        if identity:
            my_phone_number = my_phone_number or identity.get("my_phone_number")
        owner_user_id = owner_user_id or dataset_id

    try:
        from .sources.signal_export_parser import parse_signal_export_json
        records = parse_signal_export_json(
            file_bytes,
            my_phone_number=my_phone_number,
            owner_user_id=owner_user_id,
        )
    except ValueError as e:
        return {"status": "error", "error": str(e), "records_processed": 0}

    if not records:
        return {"status": "ok", "records_processed": 0}

    # Persist raw Signal payloads for traceability and debugging (non-fatal on failure).
    try:
        from ..storage.raw.raw_tables_manager import RawTablesManager
        raw_tables_manager = RawTablesManager(db_conn)
        for rec in records:
            raw_tables_manager.write_raw_record(
                source_id=SOURCE_ID_SIGNAL,
                source_record_id=str(rec.get("message_id") or rec.get("id") or ""),
                payload=rec,
                source_type="chat_messages",
            )
    except Exception as e:
        logger.warning("[PIPELINE:RAW] Signal upload raw write failed (non-fatal): %s", e)

    for rec in records:
        rec["dataset_id"] = dataset_id
    _resolve_signal_reply_links(db_conn=db_conn, dataset_id=dataset_id, staging_records=records)
    try:
        from ..storage.canonical import ConversationsTablesManager
        manager = ConversationsTablesManager(db_conn)
        refused: Dict[str, str] = {}
        manager.upsert_message_batch(
            records, dataset_id, SOURCE_ID_SIGNAL, writer_class=writer_class, refused=refused
        )
        _backfill_signal_reply_links_in_db(db_conn=db_conn, dataset_id=dataset_id)
    except Exception as e:
        logger.exception("Signal upload: upsert_message_batch failed")
        return {"status": "error", "error": str(e), "records_processed": 0}

    canonical_messages = [
        {
            "message_id": rec.get("message_id"),
            "conversation_id": rec.get("thread_id") or rec.get("conversation_id") or dataset_id,
            "sender_type": rec.get("sender_type"),
            "sender_id": rec.get("sender_id"),
            "reply_to_message_id": rec.get("reply_to_message_id"),
            "message_type": rec.get("message_type"),
            "event_type": rec.get("event_type"),
            "ts": rec.get("ts"),
            "content": rec.get("content"),
            "source_id": SOURCE_ID_SIGNAL,
            "writer_class": writer_class,
        }
        for rec in records
        # A declined write changed nothing; its text is not derived under that row.
        if str(rec.get("message_id") or "") not in refused
    ]
    _run_local_sync_enrichment_if_enabled(
        db_conn=db_conn,
        source_id=SOURCE_ID_SIGNAL,
        canonical_messages=canonical_messages,
    )

    result: Dict[str, Any] = {"status": "ok", "records_processed": len(records) - len(refused)}
    if refused:
        result["records_refused"] = len(refused)
    return result


def run_signal_sync(
    dataset_id: str,
    *,
    checkpoint_store: Optional[CheckpointStore] = None,
    db_conn: Optional[Any] = None,
    my_phone_number: Optional[str] = None,
    owner_user_id: Optional[str] = None,
    batch_size: int = 5000,
    sync_options: Optional[Dict[str, Any]] = None,
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> Dict[str, Any]:
    """
    Run Signal sync: load checkpoint → read from SQLCipher DB → parse → write to conversation_messages → save checkpoint.
    Requires pysqlcipher3. Uses stored Signal identity if my_phone_number/owner_user_id not provided.
    """
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required", "records_processed": 0}
    if _as_bool((sync_options or {}).get("dry_run"), default=False):
        # Signal has no plan to preview; a caller asking for a dry run must never get a real one.
        return {"status": "error", "error": "dry_run is not available for Signal", "records_processed": 0}

    with exclusive_sync(SOURCE_ID_SIGNAL, dataset_id) as acquired:
        if not acquired:
            return {
                "status": "error",
                "code": SYNC_IN_PROGRESS,
                "error": "Another sync of this dataset is running.",
                "records_processed": 0,
            }
        return _run_signal_sync_locked(
            dataset_id,
            checkpoint_store=checkpoint_store,
            db_conn=db_conn,
            my_phone_number=my_phone_number,
            owner_user_id=owner_user_id,
            batch_size=batch_size,
            sync_options=sync_options,
            progress_cb=progress_cb,
        )


def _run_signal_sync_locked(
    dataset_id: str,
    *,
    checkpoint_store: Optional[CheckpointStore],
    db_conn: Optional[Any],
    my_phone_number: Optional[str],
    owner_user_id: Optional[str],
    batch_size: int,
    sync_options: Optional[Dict[str, Any]],
    progress_cb: Optional[Callable[[Dict[str, Any]], None]],
) -> Dict[str, Any]:
    """``run_signal_sync``'s body, run while this dataset's sync is held."""
    if db_conn is None:
        from ..core.state import get_db_connection
        db_conn = get_db_connection()
    if db_conn is None:
        return {"status": "error", "error": "Database connection not available", "records_processed": 0}

    identity = None
    if my_phone_number is None or owner_user_id is None:
        from ..storage.signal_identity import get_signal_identity
        identity = get_signal_identity(db_conn, dataset_id)
        my_phone_number = my_phone_number or (identity.get("my_phone_number") if identity else None)
        owner_user_id = owner_user_id or dataset_id

    store = checkpoint_store if checkpoint_store is not None else SqliteCheckpointStore(db_conn)
    checkpoint = store.get_checkpoint(dataset_id, SIGNAL_SCHEMA_ID)
    last_record_id = checkpoint.last_record_id if checkpoint else "0"
    prior = dict(checkpoint.metadata) if checkpoint and isinstance(checkpoint.metadata, dict) else {}
    if checkpoint:
        prior = _adopt_legacy_signal_cursor(prior, last_record_id)
    start_unix, start_error = _resolve_sync_start_unix(sync_options)
    if start_error:
        return {"status": "error", "error": start_error, "records_processed": 0}
    signal_key_hex = None
    if isinstance(sync_options, dict):
        candidate = sync_options.get("signal_hex_key")
        if isinstance(candidate, str) and candidate.strip():
            signal_key_hex = candidate.strip()

    parser_cls = PARSER_REGISTRY.get(SIGNAL_SCHEMA_ID)
    if not parser_cls:
        return {"status": "error", "error": "No parser for signal.messages.v1", "records_processed": 0}
    parser = parser_cls(dataset_id=dataset_id, _schema_id=SIGNAL_SCHEMA_ID)
    from .sources.signal_reader import read_signal_rows, signal_cursor_for_row
    from ..storage.canonical import ConversationsTablesManager
    manager = ConversationsTablesManager(db_conn)

    current_last_record_id = _resume_cursor(prior, start_unix=start_unix)
    final_last_record_id = last_record_id
    coverage = {k: v for k, v in _unbounded_coverage(prior).items() if k != UNBOUNDED_EXCLUDE_SPAM_KEY}
    total_processed = 0
    batch_num = 0

    def _save(last: str) -> None:
        store.save_checkpoint(IngestionCheckpoint(
            dataset_id=dataset_id,
            schema_id=SIGNAL_SCHEMA_ID,
            last_record_id=last,
            metadata={COVERAGE_RECORDED_KEY: True, **coverage},
        ))

    while True:
        batch_num += 1
        try:
            rows = read_signal_rows(
                last_record_id=current_last_record_id if current_last_record_id != "0" else None,
                my_phone_number=my_phone_number,
                batch_size=batch_size,
                start_unix=start_unix,
                signal_key_hex=signal_key_hex,
            )
        except ImportError as e:
            return {"status": "error", "error": str(e), "records_processed": total_processed}
        except FileNotFoundError as e:
            return {"status": "error", "error": str(e), "records_processed": total_processed}
        except ValueError as e:
            return {"status": "error", "error": str(e), "records_processed": total_processed}
        except Exception as e:
            return {"status": "error", "error": str(e), "records_processed": total_processed}

        if not rows:
            break

        # Persist raw Signal payloads for traceability and debugging (non-fatal on failure).
        try:
            from ..storage.raw.raw_tables_manager import RawTablesManager
            raw_tables_manager = RawTablesManager(db_conn)
            for row in rows:
                raw_tables_manager.write_raw_record(
                    source_id=SOURCE_ID_SIGNAL,
                    source_record_id=str(row.get("id") or ""),
                    payload=row,
                    source_type="chat_messages",
                )
        except Exception as e:
            logger.warning("[PIPELINE:RAW] Signal sync raw write failed (non-fatal): %s", e)

        # Rows arrive in (sent_at, id) order, so the last one is the batch's cursor.
        batch_cursor = signal_cursor_for_row(rows[-1])
        row_norm_pairs: List[tuple[Dict[str, Any], Any]] = []
        for row in rows:
            raw = RawRecord(record_id=row["id"], payload=row)
            validation = parser.validate(raw)
            if not validation.is_valid:
                logger.debug("Skip invalid row: %s", validation.errors)
                continue
            norm = parser.parse(raw)
            row_norm_pairs.append((row, norm))

        if not row_norm_pairs:
            if len(rows) < batch_size:
                break
            current_last_record_id = final_last_record_id = batch_cursor
            if start_unix is None:
                coverage = {**coverage, UNBOUNDED_CURSOR_KEY: batch_cursor}
            _save(final_last_record_id)
            continue

        normalized_records = [norm for _, norm in row_norm_pairs]
        mapped_records = _map_normalized_records_with_canonical_mapper(
            normalized_records,
            source_id=SOURCE_ID_SIGNAL,
        )
        mapped_by_message_id = {
            str(rec.get("message_id")): rec
            for rec in mapped_records
            if rec.get("message_id") is not None
        }

        staging_records: List[Dict[str, Any]] = []
        for row, norm in row_norm_pairs:
            p = norm.payload
            mapped = mapped_by_message_id.get(str(p.get("message_id")), {})
            from_self = (row.get("role") == "user")
            sender_id = mapped.get("sender_id") or p.get("sender_id") or row.get("sender_id")
            if not sender_id:
                sender_id = "self" if from_self else f"unknown:{p.get('thread_id') or p.get('message_id') or 'signal'}"
            staging_records.append({
                "message_id": mapped.get("message_id") or p.get("message_id"),
                "dataset_id": dataset_id,
                "thread_id": mapped.get("thread_id") or mapped.get("conversation_id") or p.get("thread_id") or p.get("conversation_id") or dataset_id,
                "ts": mapped.get("ts") or p.get("ts") or datetime.now(timezone.utc).isoformat(),
                "_event_time_substituted": not (mapped.get("ts") or p.get("ts")),
                "sender_type": "self" if from_self else "contact",
                "sender_id": str(sender_id),
                "reply_to_message_id": mapped.get("reply_to_message_id") or p.get("reply_to_message_id"),
                "message_type": mapped.get("message_type") or p.get("message_type"),
                "event_type": mapped.get("event_type") or p.get("event_type"),
                "content": mapped.get("content") if mapped.get("content") is not None else p.get("content"),
                "source_id": SOURCE_ID_SIGNAL,
                "from_self": from_self,
                "owner_user_id": owner_user_id,
            })
            if "_metadata" in mapped:
                staging_records[-1]["_metadata"] = mapped["_metadata"]
            elif "_metadata" in p:
                staging_records[-1]["_metadata"] = p["_metadata"]

        _resolve_signal_reply_links(
            db_conn=db_conn,
            dataset_id=dataset_id,
            staging_records=staging_records,
        )

        try:
            manager.upsert_message_batch(staging_records, dataset_id, SOURCE_ID_SIGNAL)
            _backfill_signal_reply_links_in_db(db_conn=db_conn, dataset_id=dataset_id)
        except Exception as e:
            logger.exception("Signal sync: upsert_message_batch failed")
            return {"status": "error", "error": str(e), "records_processed": total_processed}

        canonical_messages = [
            {
                "message_id": rec.get("message_id"),
                "conversation_id": rec.get("thread_id") or dataset_id,
                "sender_type": rec.get("sender_type"),
                "sender_id": rec.get("sender_id"),
                "reply_to_message_id": rec.get("reply_to_message_id"),
                "message_type": rec.get("message_type"),
                "event_type": rec.get("event_type"),
                "ts": rec.get("ts"),
                "content": rec.get("content"),
                "source_id": SOURCE_ID_SIGNAL,
            }
            for rec in staging_records
        ]
        _run_local_sync_enrichment_if_enabled(
            db_conn=db_conn,
            source_id=SOURCE_ID_SIGNAL,
            canonical_messages=canonical_messages,
        )

        total_processed += len(row_norm_pairs)
        current_last_record_id = final_last_record_id = batch_cursor
        if start_unix is None:
            coverage = {**coverage, UNBOUNDED_CURSOR_KEY: batch_cursor}
        _save(final_last_record_id)
        _emit_sync_progress(
            progress_cb,
            batch_num=batch_num,
            records_processed=total_processed,
            records_skipped=0,
            last_record_id=final_last_record_id,
        )

        if len(rows) < batch_size:
            break

    return {
        "status": "ok",
        "records_processed": total_processed,
        "last_record_id": final_last_record_id,
    }
