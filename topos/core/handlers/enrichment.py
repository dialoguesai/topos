"""Source enrichment message handlers."""
from __future__ import annotations

import topos.core.handlers as hub

from .common import (
    Any,
    Dict,
    Optional,
    REGISTRY,
    asyncio,
    logger,
    total_messages,
    uuid,
)
from .registry import handles


@handles("enrichment_process_source")
async def handle_enrichment_process_source(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    import sys
    import uuid
    
    print(f"\033[93m[CRITICAL TOPOS HANDLER] enrichment_process_source ENTERED: req_id={req_id}\033[0m", file=sys.stderr, flush=True)
    
    payload = message.get("payload") or {}
    source_id = payload.get("source_id")
    dataset_id = payload.get("dataset_id")
    job_names = payload.get("job_names")
    force_reprocess = payload.get("force_reprocess", False)
    
    print(f"\033[93m[CRITICAL TOPOS HANDLER] Params: source_id={source_id}, dataset_id={dataset_id}\033[0m", file=sys.stderr, flush=True)
    
    logger.debug(
        "[PIPELINE:ENRICHMENT] enrichment_process_source received: source_id=%s, dataset_id=%s, job_names=%s, force_reprocess=%s",
        source_id,
        dataset_id,
        job_names,
        force_reprocess,
    )
    
    if not source_id:
        print(f"\033[93m[CRITICAL TOPOS HANDLER] ERROR: Missing source_id\033[0m", file=sys.stderr, flush=True)
        return {"id": req_id, "status": "error", "error": "source_id required"}
    
    try:
        print(f"\033[93m[CRITICAL TOPOS HANDLER] Importing modules...\033[0m", file=sys.stderr, flush=True)
        from ...api.enrichment import _process_enrichment_core
        # REGISTRY is already imported at module level (line 20), don't re-import here
        
        # Import progress module from engine (it's in engine/enrichment/progress.py)
        # We need to import it via sys.path or use a different approach
        # Since we're in topos but need engine module, we'll create a simple progress tracker
        import time
        # Create module-level progress store if it doesn't exist
        import sys as sys_module
        module = sys_module.modules[__name__]
        if not hasattr(module, '_progress_store'):
            module._progress_store = {}
        _progress_store = module._progress_store
        
        class SimpleProgress:
            """Simple progress tracker compatible with engine's EnrichmentProgress interface."""
            def __init__(self, job_id: str, total_messages: int):
                self.job_id = job_id
                self.total_messages = total_messages
                self.messages_processed = 0
                self.messages_skipped = 0
                self.status = "processing"
                self.start_time = time.time()
                self.last_update_time = self.start_time
                self.errors = []
                self.records_created = {}
                self.jobs_complete = 0
                self.jobs_total = 0
                self.jobs_progress_percent = 0.0
                self.current_job_name = None
                self.current_job_progress_percent = 0.0
                _progress_store[job_id] = self
            
            def update(self, **kwargs):
                """Update progress fields."""
                for key, value in kwargs.items():
                    setattr(self, key, value)
                self.last_update_time = time.time()
            
            def complete(self, result):
                """Mark as complete."""
                self.status = "completed"
                self.update(**result)
            
            def fail(self, error):
                """Mark as failed."""
                self.status = "failed"
                self.errors.append({"error": error})
                self.last_update_time = time.time()
            
            def get_progress_percent(self):
                """Get overall progress percentage."""
                if not self.jobs_total or self.jobs_total == 0:
                    if not self.total_messages or self.total_messages == 0:
                        return 0.0
                    total_handled = self.messages_processed + self.messages_skipped
                    return min(100.0, (total_handled / self.total_messages) * 100.0)
                jobs_base_progress = (self.jobs_complete / self.jobs_total) * 100.0
                current_job_contribution = (self.current_job_progress_percent / 100.0) * (100.0 / self.jobs_total)
                return min(100.0, jobs_base_progress + current_job_contribution)
            
            def to_dict(self):
                """Convert to dict for API response."""
                return {
                    "job_id": self.job_id,
                    "status": self.status,
                    "progress_percent": self.get_progress_percent(),
                    "messages_processed": self.messages_processed,
                    "messages_skipped": self.messages_skipped,
                    "messages_total": self.total_messages,
                    "records_created": self.records_created,
                    "errors": self.errors,
                    "jobs_complete": self.jobs_complete,
                    "jobs_total": self.jobs_total,
                    "jobs_progress_percent": (self.jobs_complete / self.jobs_total * 100) if self.jobs_total > 0 else 0.0,
                    "current_job_name": self.current_job_name,
                    "current_job_progress_percent": self.current_job_progress_percent,
                }
        
        def create_progress(job_id: str, total_messages: int):
            """Create a progress tracker."""
            return SimpleProgress(job_id, total_messages)
        
        def get_progress(job_id: str):
            """Get progress tracker by job_id."""
            return _progress_store.get(job_id)
        
        # Make get_progress available at module level for enrichment_progress handler
        import sys as sys_module
        module = sys_module.modules[__name__]
        module._get_progress = get_progress
        
        print(f"\033[93m[CRITICAL TOPOS HANDLER] Getting source definition...\033[0m", file=sys.stderr, flush=True)
        source_def = REGISTRY.get(source_id)
        if not source_def:
            print(f"\033[93m[CRITICAL TOPOS HANDLER] ERROR: Source {source_id} not found\033[0m", file=sys.stderr, flush=True)
            return {"id": req_id, "status": "error", "error": f"Source {source_id} not found"}
        
        from ...enrichment.source_overrides import effective_canonical_enrichment_jobs

        jobs_to_run = job_names or effective_canonical_enrichment_jobs(source_def)
        if not jobs_to_run:
            print(f"\033[93m[CRITICAL TOPOS HANDLER] No jobs configured, returning early\033[0m", file=sys.stderr, flush=True)
            return {
                "id": req_id,
                "status": "ok",
                "payload": {
                    "status": "ok",
                    "message": "No enrichment jobs configured for this source",
                    "messages_processed": 0,
                    "records_created": {},
                },
            }
        
        print(f"\033[93m[CRITICAL TOPOS HANDLER] Creating job_id...\033[0m", file=sys.stderr, flush=True)
        job_id = str(uuid.uuid4())
        progress = create_progress(job_id, 0)
        
        print(f"\033[93m[CRITICAL TOPOS HANDLER] Starting background task...\033[0m", file=sys.stderr, flush=True)
        # Background processing function
        async def _process_in_background():
            try:
                print(f"\033[93m[CRITICAL TOPOS BACKGROUND] Background task started\033[0m", file=sys.stderr, flush=True)
                from ...core.state import get_db_connection
                from ...enrichment.derived_tables import DerivedTablesManager
                from ...enrichment.orchestrator import EnrichmentOrchestrator
                from ...api.enrichment import _find_unprocessed_messages
                
                db_conn = hub.get_db_connection()
                if not db_conn:
                    raise RuntimeError("Database connection not available")
                
                # Find unprocessed messages
                print(f"\033[93m[CRITICAL TOPOS BACKGROUND] Finding unprocessed messages...\033[0m", file=sys.stderr, flush=True)
                if force_reprocess:
                    # Load all messages for this source
                    cursor = db_conn.execute("""
                            SELECT message_id, conversation_id, sender_type, sender_id,
                                   event_at, content, content_rendered, metadata_json, sequence, source_id
                            FROM ai_chat_messages
                            WHERE source_id = ?
                            ORDER BY event_at ASC
                        """, (source_id,))
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
                else:
                    unprocessed_messages = await _find_unprocessed_messages(source_id, dataset_id, jobs_to_run)
                
                if not unprocessed_messages:
                    print(f"\033[93m[CRITICAL TOPOS BACKGROUND] No unprocessed messages\033[0m", file=sys.stderr, flush=True)
                    progress.complete({
                        "messages_processed": 0,
                        "messages_skipped": 0,
                        "records_created": {},
                        "errors": [],
                    })
                    return
                
                # Update progress with total messages
                print(f"\033[93m[CRITICAL TOPOS BACKGROUND] Found {len(unprocessed_messages)} messages, updating progress\033[0m", file=sys.stderr, flush=True)
                progress.total_messages = len(unprocessed_messages)
                progress.update(
                    messages_processed=0,
                    messages_skipped=0,
                    jobs_total=len(jobs_to_run),
                    jobs_complete=0,
                )
                
                # Run enrichment with progress callback
                print(f"\033[93m[CRITICAL TOPOS BACKGROUND] Initializing orchestrator...\033[0m", file=sys.stderr, flush=True)
                tables_manager = DerivedTablesManager(conn=db_conn)
                orchestrator = EnrichmentOrchestrator(tables_manager=tables_manager)
                
                # Define progress callback
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
                    progress.update(
                        messages_processed=estimated_messages_processed,
                        messages_skipped=0,
                        current_job_name=job_name,
                        current_job_progress_percent=current_job_progress,
                        jobs_complete=jobs_complete,
                        jobs_total=len(jobs_to_run),
                    )
                
                print(f"\033[93m[CRITICAL TOPOS BACKGROUND] Running enrichment...\033[0m", file=sys.stderr, flush=True)
                enrichment_result = await orchestrator.run_canonical(
                    unprocessed_messages,
                    job_names=jobs_to_run,
                    progress_callback=progress_callback,
                )
                
                print(f"\033[93m[CRITICAL TOPOS BACKGROUND] Enrichment complete\033[0m", file=sys.stderr, flush=True)
                result = {
                    "status": "ok",
                    "source_id": source_id,
                    "messages_processed": len(unprocessed_messages),
                    "jobs_run": enrichment_result.get("jobs_run", 0),
                    "records_created": enrichment_result.get("records_created", {}),
                    "errors": enrichment_result.get("errors", []),
                }
                progress.complete(result)
            except Exception as e:
                print(f"\033[91m[CRITICAL TOPOS BACKGROUND] Error: {e}\033[0m", file=sys.stderr, flush=True)
                import traceback
                print(f"\033[91m[CRITICAL TOPOS BACKGROUND] Traceback:\n{traceback.format_exc()}\033[0m", file=sys.stderr, flush=True)
                progress.fail(str(e))
        
        # Start background task (non-blocking)
        asyncio.create_task(_process_in_background())
        
        print(f"\033[93m[CRITICAL TOPOS HANDLER] Returning immediately with job_id={job_id}\033[0m", file=sys.stderr, flush=True)
        logger.debug(
            "[PIPELINE:ENRICHMENT] enrichment_process_source: source_id=%s, job_id=%s",
            source_id,
            job_id,
        )
        return {
            "id": req_id,
            "status": "ok",
            "payload": {
                "job_id": job_id,
                "status": "processing",
                "source_id": source_id,
                "messages_total": 0,  # Will be updated when background task finds messages
                "message": "Processing started. Use /v1/enrichment/progress/{job_id} to track progress.",
            },
        }
    except Exception as exc:  # noqa: BLE001
        print(f"\033[91m[CRITICAL TOPOS HANDLER] Exception: {exc}\033[0m", file=sys.stderr, flush=True)
        import traceback
        print(f"\033[91m[CRITICAL TOPOS HANDLER] Traceback:\n{traceback.format_exc()}\033[0m", file=sys.stderr, flush=True)
        logger.error("[PIPELINE:ENRICHMENT] enrichment_process_source error: %s", exc, exc_info=True)
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("enrichment_progress")
async def handle_enrichment_progress(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    job_id = payload.get("job_id")
    
    if not job_id:
        return {"id": req_id, "status": "error", "error": "Missing job_id"}
    
    # Get progress from the store (created in enrichment_process_source handler)
    try:
        import sys as sys_module
        module = sys_module.modules[__name__]
        if hasattr(module, '_get_progress'):
            get_progress_func = getattr(module, '_get_progress')
            progress = get_progress_func(job_id)
        else:
            # Fallback: try to access _progress_store directly
            if hasattr(module, '_progress_store'):
                progress = module._progress_store.get(job_id)
            else:
                progress = None
        
        if not progress:
            return {"id": req_id, "status": "error", "error": f"Job {job_id} not found"}
        
        progress_dict = progress.to_dict() if hasattr(progress, 'to_dict') else {
            "job_id": job_id,
            "status": getattr(progress, 'status', 'unknown'),
            "progress_percent": getattr(progress, 'get_progress_percent', lambda: 0.0)(),
        }
        return {"id": req_id, "status": "ok", "payload": progress_dict}
    except Exception as e:
        print(f"\033[91m[CRITICAL TOPOS HANDLER] Error getting progress: {e}\033[0m", file=sys.stderr, flush=True)
        import traceback
        print(f"\033[91m[CRITICAL TOPOS HANDLER] Traceback:\n{traceback.format_exc()}\033[0m", file=sys.stderr, flush=True)
        return {"id": req_id, "status": "error", "error": str(e)}

@handles("enrichment_status_source")
async def handle_enrichment_status_source(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    source_id = payload.get("source_id")
    dataset_id = payload.get("dataset_id")
    
    if not source_id:
        return {"id": req_id, "status": "error", "error": "source_id required"}
    
    try:
        from ...api.enrichment import _get_enrichment_status_core
        
        # Call the core enrichment status logic
        result = await _get_enrichment_status_core(
            source_id=source_id,
            dataset_id=dataset_id,
        )
        logger.debug(
            "[PIPELINE:ENRICHMENT] enrichment_status_source: source_id=%s, total=%s, processed=%s",
            source_id,
            result.get("total_messages"),
            result.get("processed_messages"),
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("[PIPELINE:ENRICHMENT] enrichment_status_source error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("source_enrichments_list")
async def handle_source_enrichments_list(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    source_id = payload.get("source_id")
    if not source_id:
        return {"id": req_id, "status": "error", "error": "source_id required"}
    try:
        from ...api.enrichment import list_source_enrichments

        result = await list_source_enrichments(source_id=source_id)
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("[PIPELINE:ENRICHMENT] source_enrichments_list error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("source_enrichment_backfill")
async def handle_source_enrichment_backfill(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    source_id = payload.get("source_id")
    enrichment_name = payload.get("enrichment_name")
    only_missing = payload.get("only_missing", True)
    limit = payload.get("limit")
    if not source_id:
        return {"id": req_id, "status": "error", "error": "source_id required"}
    if not enrichment_name:
        return {"id": req_id, "status": "error", "error": "enrichment_name required"}
    try:
        logger.debug(
            "[PIPELINE:ENRICHMENT] source_enrichment_backfill received: source_id=%s enrichment=%s only_missing=%s limit=%s",
            source_id,
            enrichment_name,
            only_missing,
            limit,
        )
        from ...api.enrichment import backfill_source_enrichment

        result = await backfill_source_enrichment(
            source_id=source_id,
            enrichment_name=enrichment_name,
            only_missing=bool(only_missing),
            limit=limit,
        )
        logger.debug(
            "[PIPELINE:ENRICHMENT] source_enrichment_backfill complete: source_id=%s enrichment=%s rows_scanned=%s rows_processed=%s rows_failed=%s",
            source_id,
            enrichment_name,
            result.get("rows_scanned"),
            result.get("rows_processed"),
            result.get("rows_failed"),
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("[PIPELINE:ENRICHMENT] source_enrichment_backfill error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("enrichment_catalog")
async def handle_enrichment_catalog(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...api.enrichment import _enrichment_catalog_core

        result = _enrichment_catalog_core(source_id=payload.get("source_id"))
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("[PIPELINE:ENRICHMENT] enrichment_catalog error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("enrichment_preview")
async def handle_enrichment_preview(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    source_id = payload.get("source_id")
    if not source_id:
        return {"id": req_id, "status": "error", "error": "source_id required"}
    try:
        from ...api.enrichment import _enrichment_preview_core

        result = _enrichment_preview_core(
            source_id=source_id, limit=int(payload.get("limit") or 20)
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("[PIPELINE:ENRICHMENT] enrichment_preview error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("enrichment_coverage")
async def handle_enrichment_coverage(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    source_id = payload.get("source_id")
    if not source_id:
        return {"id": req_id, "status": "error", "error": "source_id required"}
    try:
        from ...api.enrichment import _enrichment_coverage_core

        result = _enrichment_coverage_core(source_id=source_id)
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("[PIPELINE:ENRICHMENT] enrichment_coverage error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("source_enrichment_delete")
async def handle_source_enrichment_delete(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    source_id = payload.get("source_id")
    enrichment_name = payload.get("enrichment_name")
    if not source_id:
        return {"id": req_id, "status": "error", "error": "source_id required"}
    if not enrichment_name:
        return {"id": req_id, "status": "error", "error": "enrichment_name required"}
    try:
        from ...api.enrichment import _delete_enrichment_data_core

        result = _delete_enrichment_data_core(source_id=source_id, job_name=enrichment_name)
        logger.debug(
            "[PIPELINE:ENRICHMENT] source_enrichment_delete complete: source_id=%s enrichment=%s deleted=%s",
            source_id,
            enrichment_name,
            result.get("deleted_total"),
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("[PIPELINE:ENRICHMENT] source_enrichment_delete error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("source_enrichment_toggle")
async def handle_source_enrichment_toggle(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    source_id = payload.get("source_id")
    enrichment_name = payload.get("enrichment_name")
    if not source_id:
        return {"id": req_id, "status": "error", "error": "source_id required"}
    if not enrichment_name:
        return {"id": req_id, "status": "error", "error": "enrichment_name required"}
    enabled = payload.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        return {"id": req_id, "status": "error", "error": "'enabled' must be true, false, or null"}
    try:
        from ...api.enrichment import _toggle_source_enrichment_core

        result = _toggle_source_enrichment_core(source_id, enrichment_name, enabled)
        logger.debug(
            "[PIPELINE:ENRICHMENT] source_enrichment_toggle complete: source_id=%s enrichment=%s enabled=%s",
            source_id,
            enrichment_name,
            result.get("enabled"),
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("[PIPELINE:ENRICHMENT] source_enrichment_toggle error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("source_enrichment_test")
async def handle_source_enrichment_test(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    source_id = payload.get("source_id")
    enrichment_name = payload.get("enrichment_name")
    data_packet = payload.get("data_packet") or {}
    if not source_id:
        return {"id": req_id, "status": "error", "error": "source_id required"}
    if not enrichment_name:
        return {"id": req_id, "status": "error", "error": "enrichment_name required"}
    try:
        logger.debug(
            "[PIPELINE:ENRICHMENT] source_enrichment_test received: source_id=%s enrichment=%s",
            source_id,
            enrichment_name,
        )
        from ...api.enrichment import test_source_enrichment

        result = await test_source_enrichment(
            source_id=source_id,
            enrichment_name=enrichment_name,
            data_packet=data_packet,
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("[PIPELINE:ENRICHMENT] source_enrichment_test error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}
