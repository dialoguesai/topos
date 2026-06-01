from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_api_key
from ..enrichment.derived_tables import DerivedTablesManager
from ..enrichment.jobs import CANONICAL_JOBS
from ..enrichment.orchestrator import EnrichmentOrchestrator
from ..sources.registry import REGISTRY
from ..core.state import get_db_connection
# Removed imports: canonicalization.mappers, ingestion.parsers, storage.raw.file_store, analytics.raw_queries
# Enrichment now reads directly from canonical table (ai_chat_messages) per architecture design

logger = logging.getLogger("topos.api.enrichment")

router = APIRouter()


def _url_classification_test_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "required": ["url"],
        "properties": {
            "url": {
                "type": "string",
                "title": "URL",
                "description": "Website URL to classify",
                "example": "https://www.nytimes.com",
            },
            "title": {
                "type": "string",
                "title": "Page Title",
                "description": "Optional page title for better classification context",
                "example": "The New York Times - Breaking News",
            },
        },
    }


async def _test_browser_visits_url_classification(*, data_packet: Dict[str, Any]) -> Dict[str, Any]:
    from ..engine import Engine, build_url_classification_task

    url = data_packet.get("url")
    title = data_packet.get("title")
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(status_code=400, detail="data_packet.url must be a non-empty string")
    if title is not None and not isinstance(title, str):
        raise HTTPException(status_code=400, detail="data_packet.title must be a string when provided")

    task = build_url_classification_task(
        task_id="test_url_cls",
        url=url.strip(),
        title=title,
    )
    engine = Engine()
    result = await asyncio.to_thread(engine.run, task)
    if result.status != "completed":
        raise HTTPException(
            status_code=502,
            detail=result.error or f"Engine returned status {result.status}",
        )
    return {
        "status": "ok",
        "input": {"url": url, "title": title},
        "output": result.output,
    }


_RAW_SOURCE_TEST_HANDLERS = {
    ("browser_visits", "url_classification"): _test_browser_visits_url_classification,
}

_RAW_SOURCE_TEST_SCHEMAS = {
    ("browser_visits", "url_classification"): _url_classification_test_schema(),
}


async def _backfill_browser_visits_url_classification(
    *,
    db_conn,
    only_missing: bool = True,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Backfill URL classification for normalized browser visits raw table rows."""
    from ..engine import Engine, build_url_classification_task
    from ..storage.raw.browser_flat_tables import (
        ensure_browser_url_classification_table,
        write_browser_url_classification,
    )
    from ..enrichment.progress_bar import ProgressBar
    from ..storage.raw.raw_tables_manager import RawTablesManager

    source_table = "raw_chat_messages_browservisits"

    logger.info(
        "[PIPELINE:ENRICHMENT] Source backfill start: source=browser_visits enrichment=url_classification only_missing=%s limit=%s",
        only_missing,
        limit,
    )

    # Ensure/migrate the raw browser visits table to normalized-column schema first.
    RawTablesManager(db_conn).ensure_raw_table(source_table)

    # If source table does not exist yet, return an empty success result.
    source_exists = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (source_table,),
    ).fetchone()
    if not source_exists:
        logger.info(
            "[PIPELINE:ENRICHMENT] Source backfill complete: source table missing (%s)",
            source_table,
        )
        return {
            "rows_scanned": 0,
            "rows_processed": 0,
            "rows_skipped": 0,
            "rows_failed": 0,
            "errors": [],
        }

    ensure_browser_url_classification_table(db_conn)

    params: List[Any] = []
    if only_missing:
        query = """
            SELECT
                (COALESCE(v.url, '') || '_' || COALESCE(v.visited_at, '')) AS derived_record_id,
                v.dataset_id,
                v.url,
                v.title
            FROM raw_chat_messages_browservisits v
            LEFT JOIN browser_url_classification c
              ON c.source_table = 'browser_visits'
             AND c.record_id = (COALESCE(v.url, '') || '_' || COALESCE(v.visited_at, ''))
            WHERE c.record_id IS NULL
            ORDER BY v.visited_at ASC
        """
    else:
        query = """
            SELECT
                (COALESCE(v.url, '') || '_' || COALESCE(v.visited_at, '')) AS derived_record_id,
                v.dataset_id,
                v.url,
                v.title
            FROM raw_chat_messages_browservisits v
            ORDER BY v.visited_at ASC
        """
    if isinstance(limit, int) and limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    rows = db_conn.execute(query, tuple(params)).fetchall()

    processed = 0
    skipped = 0
    failed = 0
    errors: List[Dict[str, Any]] = []

    if rows:
        with ProgressBar(total=len(rows), desc="url_classification backfill") as pbar:
            for row in rows:
                record_id = row[0]
                dataset_id = row[1]
                url = row[2]
                title = row[3]
                if not isinstance(url, str) or not url.strip():
                    skipped += 1
                    pbar.update(1)
                    continue

                try:
                    task = build_url_classification_task(
                        task_id=f"backfill_url_{record_id}",
                        url=url,
                        title=title,
                        source_id="browser_visits",
                        record_ids=[record_id],
                    )
                    engine = Engine()
                    result = await asyncio.to_thread(engine.run, task)
                    if result.status != "completed":
                        failed += 1
                        errors.append({"record_id": record_id, "error": result.error or result.status})
                        continue
                    out = result.output
                    write_browser_url_classification(
                        db_conn,
                        source_table="browser_visits",
                        record_id=record_id,
                        dataset_id=dataset_id,
                        url=url,
                        title=title,
                        category=out.get("category"),
                        confidence=out.get("confidence"),
                        model_name=out.get("model"),
                        ensure_table=False,
                        log_write=False,  # Avoid per-row log spam during bulk backfill
                    )
                    processed += 1
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    errors.append({"record_id": record_id, "error": str(exc)})
                finally:
                    pbar.update(1)

    summary = {
        "rows_scanned": len(rows),
        "rows_processed": processed,
        "rows_skipped": skipped,
        "rows_failed": failed,
        "errors": errors[:100],
    }
    logger.info(
        "[PIPELINE:ENRICHMENT] Source backfill complete: source=browser_visits enrichment=url_classification scanned=%d processed=%d skipped=%d failed=%d",
        summary["rows_scanned"],
        summary["rows_processed"],
        summary["rows_skipped"],
        summary["rows_failed"],
    )
    return summary


_RAW_SOURCE_BACKFILL_HANDLERS = {
    ("browser_visits", "url_classification"): _backfill_browser_visits_url_classification,
}


def _get_enriched_message_ids(table_name: str, conn) -> set[str]:
    """Get set of message_ids that have enrichment records in the given table."""
    if not conn:
        return set()
    try:
        cursor = conn.execute(f"SELECT DISTINCT message_id FROM {table_name}")
        return {row[0] for row in cursor.fetchall()}
    except Exception as e:
        logger.warning("Failed to query enriched message IDs from %s: %s", table_name, e)
        return set()


async def _find_unprocessed_messages(
    source_id: str,
    dataset_id: Optional[str] = None,
    job_names: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Find canonical messages that haven't been enriched yet.
    
    This function reads directly from the ai_chat_messages table (canonical table)
    as the source of truth, per the architecture design.
    
    Args:
        source_id: Source identifier
        dataset_id: Optional dataset ID to filter by (extracts user_id for filtering)
        job_names: List of enrichment job names to check
        
    Returns:
        List of canonical messages that need enrichment
    """
    # Get source definition
    source_def = REGISTRY.get(source_id)
    if not source_def:
        raise ValueError(f"Source {source_id} not found")
    
    if not source_def.canonical_enrichment_jobs:
        return []
    
    # Determine which jobs to check (default to all canonical enrichment jobs)
    jobs_to_check = job_names or source_def.canonical_enrichment_jobs
    
    # Get database connection
    db_conn = get_db_connection()
    if not db_conn:
        logger.warning("No database connection available for enrichment")
        return []
    
    # Read canonical messages directly from ai_chat_messages table
    # This is the source of truth per architecture design
    try:
        # Check if ai_chat_messages table exists
        cursor = db_conn.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='ai_chat_messages'
        """)
        if not cursor.fetchone():
            logger.info(
                "ai_chat_messages table does not exist yet. "
                "Wait for ingestion to complete (job status 'completed') before triggering enrichment."
            )
            return []
        
        # Check if ai_chat_conversations table exists for dataset_id filtering
        cursor = db_conn.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='ai_chat_conversations'
        """)
        has_conversations_table = cursor.fetchone() is not None
        
        # Build query to read from canonical table
        # First, check if we have messages for this source_id at all
        msg_count_cursor = db_conn.execute("""
            SELECT COUNT(*) FROM ai_chat_messages WHERE source_id = ?
        """, (source_id,))
        total_msgs = msg_count_cursor.fetchone()[0]
        logger.debug("Debug: Total messages in ai_chat_messages for source_id=%s: %d", source_id, total_msgs)
        
        # Debug: Check what source_ids actually exist in the messages table
        all_sources_cursor = db_conn.execute("""
            SELECT DISTINCT source_id, COUNT(*) as count FROM ai_chat_messages GROUP BY source_id
        """)
        all_sources = [(row[0], row[1]) for row in all_sources_cursor.fetchall()]
        logger.debug("Debug: All source_ids in ai_chat_messages table: %s", all_sources if all_sources else "none")
        
        # Debug: Check total message count regardless of source_id
        total_all_cursor = db_conn.execute("SELECT COUNT(*) FROM ai_chat_messages")
        total_all = total_all_cursor.fetchone()[0]
        logger.debug("Debug: Total messages in ai_chat_messages (all sources): %d", total_all)
        
        if has_conversations_table and dataset_id and total_msgs > 0:
            # Join with conversations table to filter by owner_user_id
            user_id = dataset_id.split(":")[0] if ":" in dataset_id else dataset_id
            logger.debug(
                "Querying canonical messages: source_id=%s, dataset_id=%s, extracted_user_id=%s",
                source_id,
                dataset_id,
                user_id,
            )
            
            # Check what owner_user_ids actually exist for this source
            debug_cursor = db_conn.execute("""
                SELECT DISTINCT c.owner_user_id, COUNT(*) as msg_count
                FROM ai_chat_messages m
                INNER JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                WHERE m.source_id = ?
                GROUP BY c.owner_user_id
            """, (source_id,))
            debug_rows = debug_cursor.fetchall()
            logger.debug(
                "Debug: Found conversations with owner_user_ids: %s",
                [(row[0], row[1]) for row in debug_rows] if debug_rows else "none",
            )
            
            # Check what conversation_ids exist in messages
            conv_cursor = db_conn.execute("""
                SELECT DISTINCT conversation_id FROM ai_chat_messages WHERE source_id = ?
            """, (source_id,))
            conv_ids = [row[0] for row in conv_cursor.fetchall()]
            logger.debug("Debug: Conversation IDs in messages: %s", conv_ids[:5] if conv_ids else "none")
            
            # Check what conversations exist in conversations table
            all_conv_cursor = db_conn.execute("""
                SELECT conversation_id, owner_user_id FROM ai_chat_conversations
            """)
            all_convs = [(row[0], row[1]) for row in all_conv_cursor.fetchall()]
            logger.debug("Debug: All conversations in table: %s", all_convs[:5] if all_convs else "none")
            
            # Try query with user_id filter first
            query = """
                SELECT m.message_id, m.conversation_id, m.sender_type, m.sender_id,
                       m.event_at, m.content, m.content_rendered, m.metadata_json, m.sequence, m.source_id
                FROM ai_chat_messages m
                INNER JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                WHERE m.source_id = ? AND c.owner_user_id = ?
                ORDER BY m.event_at ASC
            """
            cursor = db_conn.execute(query, (source_id, user_id))
            result_count = len(cursor.fetchall())
            logger.debug("Debug: Query with user_id filter returned %d messages", result_count)
            
            # If no results with user_id filter, fall back to source_id only (for local mode)
            if result_count == 0:
                logger.debug("Debug: No messages found with user_id filter, falling back to source_id only")
                query = """
                    SELECT message_id, conversation_id, sender_type, sender_id,
                           event_at, content, content_rendered, metadata_json, sequence, source_id
                    FROM ai_chat_messages
                    WHERE source_id = ?
                    ORDER BY event_at ASC
                """
                cursor = db_conn.execute(query, (source_id,))
            else:
                # Re-execute the query since we consumed the cursor
                cursor = db_conn.execute(query, (source_id, user_id))
        else:
            # Direct query without user filtering (fallback if conversations table doesn't exist or no dataset_id)
            logger.debug(
                "Querying canonical messages without user filter: source_id=%s, has_conversations_table=%s, dataset_id=%s",
                source_id,
                has_conversations_table,
                dataset_id,
            )
            query = """
                SELECT message_id, conversation_id, sender_type, sender_id,
                       event_at, content, content_rendered, metadata_json, sequence, source_id
                FROM ai_chat_messages
                WHERE source_id = ?
                ORDER BY event_at ASC
            """
            cursor = db_conn.execute(query, (source_id,))
        
        # Convert rows to dictionaries
        canonical_messages: List[Dict[str, Any]] = []
        for row in cursor.fetchall():
            canonical_messages.append({
                "message_id": row[0],
                "conversation_id": row[1],
                "sender_type": row[2],
                "sender_id": row[3],
                "event_at": row[4],
                "content": row[5],
                "content_rendered": row[6],
                "metadata_json": row[7],
                "sequence": row[8],
                "source_id": row[9],
            })
        
        logger.debug(
            "Found %d canonical messages for source_id=%s, dataset_id=%s",
            len(canonical_messages),
            source_id,
            dataset_id,
        )
        
    except Exception as e:
        logger.error("Failed to read canonical messages from ai_chat_messages table: %s", e)
        return []
    
    if not canonical_messages:
        logger.debug("No canonical messages found for source_id=%s, dataset_id=%s", source_id, dataset_id)
        return []
    
    # Check which messages have already been enriched
    # Get enriched message IDs for each job's table
    enriched_ids: set[str] = set()
    # Create a mapping from job name to table name using the job registry
    job_to_table = {job.get_job_name(): job.get_derived_table() for job in CANONICAL_JOBS}
    
    for job_name in jobs_to_check:
        table_name = job_to_table.get(job_name)
        if table_name:
            enriched_ids.update(_get_enriched_message_ids(table_name, db_conn))
        else:
            logger.warning("Unknown enrichment job: %s (skipping check)", job_name)
    
    # Filter to unprocessed messages
    unprocessed = [
        msg for msg in canonical_messages
        if msg.get("message_id") not in enriched_ids
    ]
    
    logger.debug(
        "Found %d unprocessed messages out of %d total canonical messages for source_id=%s",
        len(unprocessed),
        len(canonical_messages),
        source_id,
    )
    
    return unprocessed


async def _process_enrichment_core(
    source_id: str,
    dataset_id: Optional[str] = None,
    job_names: Optional[List[str]] = None,
    force_reprocess: bool = False,
) -> Dict[str, Any]:
    """Core logic for processing enrichment (reusable from HTTP and WebSocket).
    
    Args:
        source_id: Source identifier
        dataset_id: Optional dataset ID to filter by
        job_names: Optional list of specific enrichment jobs to run
        force_reprocess: If True, reprocess even if already enriched
        
    Returns:
        Processing results
    """
    # Get source definition
    source_def = REGISTRY.get(source_id)
    if not source_def:
        raise ValueError(f"Source {source_id} not found")
    
    # Determine which jobs to run
    jobs_to_run = job_names or source_def.canonical_enrichment_jobs
    if not jobs_to_run:
        return {
            "status": "ok",
            "message": "No enrichment jobs configured for this source",
            "messages_processed": 0,
            "records_created": {},
        }
    
    # Get database connection
    db_conn = get_db_connection()
    if not db_conn:
        return {
            "status": "error",
            "message": "Database connection not available",
            "messages_processed": 0,
            "records_created": {},
        }
    
    # Find unprocessed messages
    if force_reprocess:
        # For force reprocess, load all canonical messages regardless of enrichment status
        # Read directly from canonical table (source of truth)
        try:
            # Check if ai_chat_messages table exists
            cursor = db_conn.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name='ai_chat_messages'
            """)
            if not cursor.fetchone():
                return {
                    "status": "ok",
                    "message": "No canonical messages found",
                    "messages_processed": 0,
                    "records_created": {},
                }
            
            # Check if ai_chat_conversations table exists for dataset_id filtering
            cursor = db_conn.execute("""
                SELECT name FROM sqlite_master 
                WHERE type='table' AND name='ai_chat_conversations'
            """)
            has_conversations_table = cursor.fetchone() is not None
            
            # Build query to read all canonical messages
            if has_conversations_table and dataset_id:
                user_id = dataset_id.split(":")[0] if ":" in dataset_id else dataset_id
                # Use INNER JOIN to ensure we only get messages with matching conversations
                query = """
                    SELECT m.message_id, m.conversation_id, m.sender_type, m.sender_id,
                           m.event_at, m.content, m.content_rendered, m.metadata_json, m.sequence, m.source_id
                    FROM ai_chat_messages m
                    INNER JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                    WHERE m.source_id = ? AND c.owner_user_id = ?
                    ORDER BY m.event_at ASC
                """
                cursor = db_conn.execute(query, (source_id, user_id))
            else:
                query = """
                    SELECT message_id, conversation_id, sender_type, sender_id,
                           event_at, content, content_rendered, metadata_json, sequence, source_id
                    FROM ai_chat_messages
                    WHERE source_id = ?
                    ORDER BY event_at ASC
                """
                cursor = db_conn.execute(query, (source_id,))
            
            # Convert rows to dictionaries
            unprocessed_messages = []
            for row in cursor.fetchall():
                unprocessed_messages.append({
                    "message_id": row[0],
                    "conversation_id": row[1],
                    "sender_type": row[2],
                    "sender_id": row[3],
                    "event_at": row[4],
                    "content": row[5],
                    "content_rendered": row[6],
                    "metadata_json": row[7],
                    "sequence": row[8],
                    "source_id": row[9],
                })
        except Exception as e:
            logger.error("Failed to read canonical messages for force_reprocess: %s", e)
            return {
                "status": "error",
                "message": f"Failed to read canonical messages: {e}",
                "messages_processed": 0,
                "records_created": {},
            }
    else:
        unprocessed_messages = await _find_unprocessed_messages(source_id, dataset_id, jobs_to_run)
    
    if not unprocessed_messages:
        return {
            "status": "ok",
            "message": "No unprocessed messages found",
            "messages_processed": 0,
            "records_created": {},
        }
    
    # Run enrichment
    tables_manager = DerivedTablesManager(conn=db_conn)
    orchestrator = EnrichmentOrchestrator(tables_manager=tables_manager)
    
    logger.info(
        "[PIPELINE:ENRICHMENT] %s: Manual enrichment triggered: source_id=%s, messages=%d, jobs=%s",
        orchestrator,
        source_id,
        len(unprocessed_messages),
        jobs_to_run,
    )
    
    # Define progress callback to update progress during execution
    progress_callback = None
    try:
        from ..enrichment.progress import get_progress
        # Try to get progress object if it exists (created by handler)
        progress_obj = get_progress(source_id)  # Use source_id as fallback lookup
        if progress_obj:
            def progress_callback(
                processed_count: int, 
                total_count: int, 
                job_name: str, 
                job_percent: float,
                current_job_progress: float,
            ):
                """Update progress as jobs execute."""
                estimated_messages_processed = int((job_percent / 100) * total_count)
                jobs_complete = int((job_percent / 100) * len(jobs_to_run))
                progress_obj.update(
                    messages_processed=estimated_messages_processed,
                    messages_skipped=0,
                    current_job_name=job_name,
                    current_job_progress_percent=current_job_progress,
                    jobs_complete=jobs_complete,
                    jobs_total=len(jobs_to_run),
                )
    except Exception:
        pass  # Progress callback is optional
    
    enrichment_result = await orchestrator.run_canonical(
        unprocessed_messages,
        job_names=jobs_to_run,
        progress_callback=progress_callback,
    )
    
    return {
        "status": "ok",
        "source_id": source_id,
        "messages_processed": len(unprocessed_messages),
        "jobs_run": enrichment_result.get("jobs_run", 0),
        "records_created": enrichment_result.get("records_created", {}),
        "errors": enrichment_result.get("errors", []),
    }


async def _get_enrichment_status_core(
    source_id: str,
    dataset_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Core logic for getting enrichment status (reusable from HTTP and WebSocket).
    
    This function reads directly from the ai_chat_messages table (canonical table)
    as the source of truth, per the architecture design.
    
    Returns:
        Status information including counts of processed/unprocessed messages
    """
    source_def = REGISTRY.get(source_id)
    if not source_def:
        raise ValueError(f"Source {source_id} not found")
    
    # Get database connection
    db_conn = get_db_connection()
    if not db_conn:
        return {
            "status": "error",
            "source_id": source_id,
            "total_messages": 0,
            "processed_messages": 0,
            "unprocessed_messages": 0,
            "enrichment_jobs": source_def.canonical_enrichment_jobs,
            "enrichment_trigger": getattr(source_def, "enrichment_trigger", "automatic"),
            "message": "Database connection not available",
        }
    
    # Read canonical messages directly from ai_chat_messages table
    try:
        # Check if ai_chat_messages table exists
        cursor = db_conn.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='ai_chat_messages'
        """)
        if not cursor.fetchone():
            return {
                "status": "ok",
                "source_id": source_id,
                "total_messages": 0,
                "processed_messages": 0,
                "unprocessed_messages": 0,
                "enrichment_jobs": source_def.canonical_enrichment_jobs,
                "enrichment_trigger": getattr(source_def, "enrichment_trigger", "automatic"),
                "message": "Canonical table does not exist yet",
            }
        
        # Check if ai_chat_conversations table exists for dataset_id filtering
        cursor = db_conn.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='ai_chat_conversations'
        """)
        has_conversations_table = cursor.fetchone() is not None
        
        # Build query to count messages from canonical table
        if has_conversations_table and dataset_id:
            # Join with conversations table to filter by owner_user_id
            user_id = dataset_id.split(":")[0] if ":" in dataset_id else dataset_id
            query = """
                SELECT COUNT(*)
                FROM ai_chat_messages m
                LEFT JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                WHERE m.source_id = ? AND c.owner_user_id = ?
            """
            cursor = db_conn.execute(query, (source_id, user_id))
        else:
            # Direct query without user filtering
            query = "SELECT COUNT(*) FROM ai_chat_messages WHERE source_id = ?"
            cursor = db_conn.execute(query, (source_id,))
        
        total = cursor.fetchone()[0]
        
    except Exception as e:
        logger.error("Failed to read canonical messages from ai_chat_messages table: %s", e)
        return {
            "status": "error",
            "source_id": source_id,
            "total_messages": 0,
            "processed_messages": 0,
            "unprocessed_messages": 0,
            "enrichment_jobs": source_def.canonical_enrichment_jobs,
            "enrichment_trigger": getattr(source_def, "enrichment_trigger", "automatic"),
            "message": f"Error reading canonical table: {e}",
        }
    
    # Get unprocessed messages count (reuse the logic from _find_unprocessed_messages)
    unprocessed = await _find_unprocessed_messages(source_id, dataset_id)
    unprocessed_count = len(unprocessed)
    processed_count = total - unprocessed_count
    
    return {
        "status": "ok",
        "source_id": source_id,
        "total_messages": total,
        "processed_messages": processed_count,
        "unprocessed_messages": unprocessed_count,
        "enrichment_jobs": source_def.canonical_enrichment_jobs,
        "enrichment_trigger": getattr(source_def, "enrichment_trigger", "automatic"),
    }


@router.post("/enrichment/process", dependencies=[Depends(require_api_key)])
async def process_enrichment(
    source_id: str = Body(...),
    dataset_id: Optional[str] = Body(None),
    job_names: Optional[List[str]] = Body(None),
    force_reprocess: bool = Body(False),
) -> Dict[str, Any]:
    """Manually trigger enrichment for unprocessed messages.
    
    Args:
        source_id: Source identifier
        dataset_id: Optional dataset ID to filter by
        job_names: Optional list of specific enrichment jobs to run
        force_reprocess: If True, reprocess even if already enriched
        
    Returns:
        Processing results
    """
    try:
        return await _process_enrichment_core(
            source_id=source_id,
            dataset_id=dataset_id,
            job_names=job_names,
            force_reprocess=force_reprocess,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("Manual enrichment failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/enrichment/status", dependencies=[Depends(require_api_key)])
async def get_processing_status(
    source_id: str,
    dataset_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Get enrichment status for a source.
    
    Returns:
        Status information including counts of processed/unprocessed messages
    """
    try:
        return await _get_enrichment_status_core(
            source_id=source_id,
            dataset_id=dataset_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("Failed to get enrichment status: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get(
    "/sources/{source_id}/enrichments",
    dependencies=[Depends(require_api_key)],
)
async def list_source_enrichments(source_id: str) -> Dict[str, Any]:
    """List enrichment capabilities for a specific source."""
    source_def = REGISTRY.get(source_id)
    if not source_def:
        raise HTTPException(status_code=404, detail=f"Source {source_id} not found")

    raw_jobs = list(getattr(source_def, "raw_enrichment_jobs", []) or [])
    canonical_jobs = list(getattr(source_def, "canonical_enrichment_jobs", []) or [])
    implemented_backfills = [
        enrichment_name
        for (sid, enrichment_name) in _RAW_SOURCE_BACKFILL_HANDLERS.keys()
        if sid == source_id
    ]
    implemented_backfills.sort()
    capabilities: List[Dict[str, Any]] = []
    for name in raw_jobs:
        key = (source_id, name)
        capabilities.append(
            {
                "name": name,
                "supports_backfill": key in _RAW_SOURCE_BACKFILL_HANDLERS,
                "supports_test": key in _RAW_SOURCE_TEST_HANDLERS,
                "test_input_schema": _RAW_SOURCE_TEST_SCHEMAS.get(key),
            }
        )

    return {
        "status": "ok",
        "source_id": source_id,
        "ingestion_trigger": getattr(source_def, "ingestion_trigger", "automatic"),
        "enrichment_trigger": getattr(source_def, "enrichment_trigger", "automatic"),
        "raw_enrichments": raw_jobs,
        "raw_enrichment_capabilities": capabilities,
        "canonical_enrichments": canonical_jobs,
        "raw_backfill_supported": implemented_backfills,
    }


@router.post(
    "/sources/{source_id}/enrichments/{enrichment_name}/backfill",
    dependencies=[Depends(require_api_key)],
)
async def backfill_source_enrichment(
    source_id: str,
    enrichment_name: str,
    only_missing: bool = Body(True),
    limit: Optional[int] = Body(None),
) -> Dict[str, Any]:
    """Backfill an enrichment for an ingestion source's existing rows.

    This endpoint is source-scoped (raw/source layer), separate from canonical
    message enrichment endpoints.
    """
    source_def = REGISTRY.get(source_id)
    if not source_def:
        raise HTTPException(status_code=404, detail=f"Source {source_id} not found")

    configured_raw_jobs = set(getattr(source_def, "raw_enrichment_jobs", []) or [])
    if enrichment_name not in configured_raw_jobs:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Enrichment '{enrichment_name}' is not configured for source '{source_id}'. "
                f"Configured raw enrichments: {sorted(configured_raw_jobs)}"
            ),
        )

    handler = _RAW_SOURCE_BACKFILL_HANDLERS.get((source_id, enrichment_name))
    if not handler:
        raise HTTPException(
            status_code=501,
            detail=f"Backfill for source='{source_id}' enrichment='{enrichment_name}' is not implemented",
        )

    db_conn = get_db_connection()
    if not db_conn:
        raise HTTPException(status_code=503, detail="Database connection not available")

    try:
        result = await handler(
            db_conn=db_conn,
            only_missing=only_missing,
            limit=limit,
        )
        return {
            "status": "ok",
            "source_id": source_id,
            "enrichment_name": enrichment_name,
            "only_missing": only_missing,
            "limit": limit,
            **result,
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Source enrichment backfill failed: source=%s enrichment=%s error=%s",
            source_id,
            enrichment_name,
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc))


@router.post(
    "/sources/{source_id}/enrichments/{enrichment_name}/test",
    dependencies=[Depends(require_api_key)],
)
async def test_source_enrichment(
    source_id: str,
    enrichment_name: str,
    data_packet: Dict[str, Any] = Body(...),
) -> Dict[str, Any]:
    """Test-run a source enrichment against a provided data packet."""
    source_def = REGISTRY.get(source_id)
    if not source_def:
        raise HTTPException(status_code=404, detail=f"Source {source_id} not found")

    configured_raw_jobs = set(getattr(source_def, "raw_enrichment_jobs", []) or [])
    if enrichment_name not in configured_raw_jobs:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Enrichment '{enrichment_name}' is not configured for source '{source_id}'. "
                f"Configured raw enrichments: {sorted(configured_raw_jobs)}"
            ),
        )

    handler = _RAW_SOURCE_TEST_HANDLERS.get((source_id, enrichment_name))
    if not handler:
        raise HTTPException(
            status_code=501,
            detail=f"Test for source='{source_id}' enrichment='{enrichment_name}' is not implemented",
        )

    try:
        result = await handler(data_packet=data_packet)
        return {
            "status": "ok",
            "source_id": source_id,
            "enrichment_name": enrichment_name,
            **result,
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Source enrichment test failed: source=%s enrichment=%s error=%s",
            source_id,
            enrichment_name,
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc))
