"""Local sync ingestion: iMessage, Signal (read from local DB, write to conversation_messages)."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .checkpoints.checkpoint_store import CheckpointStore, IngestionCheckpoint
from .checkpoints.sqlite_checkpoint_store import SqliteCheckpointStore
from .parsers import PARSER_REGISTRY
from .sources.base import RawRecord
from ..storage.db.write_gate import commit_connection, with_db_write

logger = logging.getLogger("topos.ingestion.local_sync")

IMESSAGE_SCHEMA_ID = "imessage.messages.v1"
SOURCE_ID_IMESSAGE = "imessage"


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

    timeline_rows = []
    for message in canonical_messages:
        row = dict(message)
        row.setdefault("_table", "conversation_messages")
        timeline_rows.append(row)
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
    if mode in {"", "all"}:
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


def run_imessage_sync(
    dataset_id: str,
    *,
    checkpoint_store: Optional[CheckpointStore] = None,
    db_conn: Optional[Any] = None,
    chat_db_path: Optional[Any] = None,
    batch_size: int = 5000,
    sync_options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Run iMessage sync: load checkpoint → read from chat.db → parse → write to conversation_messages → save checkpoint.
    Returns dict with status, records_processed, records_skipped, last_record_id, error (if any).
    """
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required", "records_processed": 0, "records_skipped": 0}

    if db_conn is None:
        from ..core.state import get_db_connection
        db_conn = get_db_connection()
    if db_conn is None:
        return {"status": "error", "error": "Database connection not available", "records_processed": 0, "records_skipped": 0}

    store = checkpoint_store if checkpoint_store is not None else SqliteCheckpointStore(db_conn)
    checkpoint = store.get_checkpoint(dataset_id, IMESSAGE_SCHEMA_ID)
    last_record_id = checkpoint.last_record_id if checkpoint else "0"

    logger.info(
        "run_imessage_sync starting: dataset_id=%s last_record_id=%s",
        dataset_id[:24] + "..." if len(dataset_id) > 24 else dataset_id,
        last_record_id[:20] + "..." if last_record_id and len(last_record_id) > 20 else last_record_id,
    )

    try:
        return _run_imessage_sync_impl(
            dataset_id=dataset_id,
            db_conn=db_conn,
            store=store,
            last_record_id=last_record_id,
            chat_db_path=chat_db_path,
            batch_size=batch_size,
            sync_options=sync_options,
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
    chat_db_path: Optional[Any] = None,
    batch_size: int = 5000,
    sync_options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Implementation of run_imessage_sync (called inside try so we never raise)."""
    start_unix, start_error = _resolve_sync_start_unix(sync_options)
    if start_error:
        return {"status": "error", "error": start_error, "records_processed": 0, "records_skipped": 0}

    parser_cls = PARSER_REGISTRY.get(IMESSAGE_SCHEMA_ID)
    if not parser_cls:
        return {"status": "error", "error": "No parser for imessage.messages.v1", "records_processed": 0, "records_skipped": 0}
    parser = parser_cls(dataset_id=dataset_id, _schema_id=IMESSAGE_SCHEMA_ID)
    from ..storage.canonical import ConversationsTablesManager
    manager = ConversationsTablesManager(db_conn)
    from .sources.imessage_reader import read_imessage_batch, get_chat_db_path
    path = chat_db_path or get_chat_db_path()
    exclude_spam = _resolve_exclude_spam(sync_options, db_conn=db_conn, dataset_id=dataset_id)

    # For bounded history sync, restart from row 0 and apply time filter.
    current_last_record_id = "0" if start_unix is not None else last_record_id
    final_last_record_id = last_record_id
    total_processed = 0
    total_skipped = 0
    batch_num = 0

    while True:
        batch_num += 1
        try:
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
        if batch.scanned_count == 0:
            break

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
                logger.debug("Skip invalid row: %s", validation.errors)
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
                is_self = str(rec.get("sender_id") or "").strip().lower() == "self"
                staging = {
                    "message_id": rec.get("message_id"),
                    "dataset_id": dataset_id,
                    "thread_id": thread_id,
                    "ts": rec.get("ts") or datetime.now(timezone.utc).isoformat(),
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

        if batch.max_scanned_rowid is None:
            # Defensive: avoid infinite loops if no valid rowid in batch.
            break

        final_last_record_id = f"imessage:{batch.max_scanned_rowid}"
        store.save_checkpoint(IngestionCheckpoint(
            dataset_id=dataset_id,
            schema_id=IMESSAGE_SCHEMA_ID,
            last_record_id=final_last_record_id,
            metadata={"exclude_spam": exclude_spam},
        ))
        current_last_record_id = final_last_record_id

        if batch.scanned_count < batch_size:
            break

    return {
        "status": "ok",
        "records_processed": total_processed,
        "records_skipped": total_skipped,
        "exclude_spam": exclude_spam,
        "last_record_id": final_last_record_id,
    }


SIGNAL_SCHEMA_ID = "signal.messages.v1"
SOURCE_ID_SIGNAL = "signal"


def run_signal_upload(
    dataset_id: str,
    file_bytes: bytes,
    *,
    my_phone_number: Optional[str] = None,
    owner_user_id: Optional[str] = None,
    db_conn: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Parse Signal export file (JSON) and write to conversation_messages.
    Uses stored Signal identity for dataset_id if my_phone_number not provided.
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
        manager.upsert_message_batch(records, dataset_id, SOURCE_ID_SIGNAL)
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
        }
        for rec in records
    ]
    _run_local_sync_enrichment_if_enabled(
        db_conn=db_conn,
        source_id=SOURCE_ID_SIGNAL,
        canonical_messages=canonical_messages,
    )

    return {"status": "ok", "records_processed": len(records)}


def run_signal_sync(
    dataset_id: str,
    *,
    checkpoint_store: Optional[CheckpointStore] = None,
    db_conn: Optional[Any] = None,
    my_phone_number: Optional[str] = None,
    owner_user_id: Optional[str] = None,
    batch_size: int = 5000,
    sync_options: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Run Signal sync: load checkpoint → read from SQLCipher DB → parse → write to conversation_messages → save checkpoint.
    Requires pysqlcipher3. Uses stored Signal identity if my_phone_number/owner_user_id not provided.
    """
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required", "records_processed": 0}

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
    from .sources.signal_reader import read_signal_rows
    from ..storage.canonical import ConversationsTablesManager
    manager = ConversationsTablesManager(db_conn)

    current_last_record_id = "0" if start_unix is not None else last_record_id
    final_last_record_id = last_record_id
    total_processed = 0

    while True:
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

        row_norm_pairs: List[tuple[Dict[str, Any], Any]] = []
        max_sent_at: Optional[float] = None
        for row in rows:
            raw = RawRecord(record_id=row["id"], payload=row)
            validation = parser.validate(raw)
            if not validation.is_valid:
                logger.debug("Skip invalid row: %s", validation.errors)
                continue
            norm = parser.parse(raw)
            row_norm_pairs.append((row, norm))
            sat = row.get("sent_at")
            if sat is not None and (max_sent_at is None or sat > max_sent_at):
                max_sent_at = sat

        if not row_norm_pairs:
            if len(rows) < batch_size:
                break
            if max_sent_at is not None:
                current_last_record_id = f"signal:0:{max_sent_at:.6f}"
                final_last_record_id = current_last_record_id
                store.save_checkpoint(IngestionCheckpoint(
                    dataset_id=dataset_id,
                    schema_id=SIGNAL_SCHEMA_ID,
                    last_record_id=final_last_record_id,
                    metadata={},
                ))
            else:
                break
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
        if max_sent_at is not None:
            final_last_record_id = f"signal:0:{max_sent_at:.6f}"
            store.save_checkpoint(IngestionCheckpoint(
                dataset_id=dataset_id,
                schema_id=SIGNAL_SCHEMA_ID,
                last_record_id=final_last_record_id,
                metadata={},
            ))
            current_last_record_id = final_last_record_id

        if len(rows) < batch_size:
            break

    return {
        "status": "ok",
        "records_processed": total_processed,
        "last_record_id": final_last_record_id,
    }
