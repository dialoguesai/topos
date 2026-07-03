from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..utils.base_object import BaseObject
from .derived_tables import DerivedTablesManager
from .jobs import CANONICAL_JOBS, RAW_JOBS
from .jobs.base import BaseEnrichmentJob

logger = logging.getLogger("topos.enrichment.orchestrator")


class EnrichmentOrchestrator(BaseObject):
    def __init__(
        self,
        tables_manager: Optional[DerivedTablesManager] = None,
        *,
        name: Optional[str] = None,
        engine: Optional[Any] = None,
    ):
        super().__init__(name=name)
        self.raw_jobs = list(RAW_JOBS)
        self.canonical_jobs = list(CANONICAL_JOBS)
        self.tables_manager = tables_manager or DerivedTablesManager()
        self._engine = engine

    def _bind_shared_engine(self, jobs: List[BaseEnrichmentJob]) -> None:
        if self._engine is None:
            return
        for job in jobs:
            if hasattr(job, "_engine"):
                job._engine = self._engine

    def register_raw_job(self, job) -> None:
        self.raw_jobs.append(job)

    def register_canonical_job(self, job: BaseEnrichmentJob) -> None:
        self.canonical_jobs.append(job)

    async def run_raw(self, raw_records: List[Dict[str, Any]]) -> Dict[str, Any]:
        results = {"jobs_run": 0, "records_created": {}, "errors": []}
        for job in self.raw_jobs:
            try:
                records = await job.run(raw_records)
                results["records_created"][job.get_job_name()] = len(records)
                results["jobs_run"] += 1
            except Exception as exc:
                results["errors"].append({"job": job.get_job_name(), "error": str(exc)})
        return results

    async def run_canonical(
        self,
        canonical_messages: List[Dict[str, Any]],
        job_names: Optional[List[str]] = None,
        progress_callback: Optional[Callable[[int, int, str, float, float], None]] = None,
    ) -> Dict[str, Any]:
        """Run canonical enrichment jobs.
        
        Args:
            canonical_messages: List of canonical message dictionaries
            job_names: Optional list of specific job names to run
            progress_callback: Optional callback function(processed_count, total_count, job_name) called during execution
            
        Returns:
            Results dictionary with jobs_run, records_created, errors
        """
        results = {"jobs_run": 0, "records_created": {}, "errors": []}
        jobs_to_run = self.canonical_jobs
        if job_names:
            jobs_to_run = [job for job in self.canonical_jobs if job.get_job_name() in job_names]
        self._bind_shared_engine(jobs_to_run)
        
        total_messages = len(canonical_messages)
        total_jobs = len(jobs_to_run)
        logger.debug(
            "[PIPELINE:ENRICHMENT] %s: Starting enrichment: %d messages, %d jobs to run",
            self,
            total_messages,
            total_jobs,
        )
        
        # Track messages processed across all jobs
        # For progress calculation: each job processes all messages, so we track cumulative progress
        messages_processed_so_far = 0
        
        for job_idx, job in enumerate(jobs_to_run, 1):
            if not job.should_run(canonical_messages):
                logger.debug("[PIPELINE:ENRICHMENT] %s: Skipping job %s (should_run=False)", self, job.get_job_name())
                continue
            try:
                job_name = job.get_job_name()
                logger.debug(
                    "[PIPELINE:ENRICHMENT] %s: Running job %d/%d: %s (%d messages, %.1f%% of jobs complete)",
                    self,
                    job_idx,
                    total_jobs,
                    job_name,
                    total_messages,
                    ((job_idx - 1) / total_jobs * 100) if total_jobs > 0 else 0,
                )
                
                # Create job-level progress callback
                def job_progress_callback(current_count: int, total_count: int):
                    """Callback for job-level progress updates."""
                    if progress_callback:
                        # Calculate job progress percent
                        job_progress = (current_count / total_count * 100) if total_count > 0 else 0.0
                        # Calculate jobs completion percent: (job_idx - 1) means previous jobs are done
                        jobs_percent = ((job_idx - 1) / total_jobs * 100) if total_jobs > 0 else 0
                        # Call orchestrator progress callback with job-level info
                        progress_callback(
                            processed_count=0,  # Not used for job-level tracking
                            total_count=total_count,
                            job_name=job_name,
                            job_percent=jobs_percent,
                            current_job_progress=job_progress,
                        )
                
                # Call progress callback at start of job
                if progress_callback:
                    jobs_percent = ((job_idx - 1) / total_jobs * 100) if total_jobs > 0 else 0
                    progress_callback(0, total_messages, job_name, jobs_percent, 0.0)
                
                records = await job.enrich(canonical_messages, progress_callback=job_progress_callback)
                
                derived_table = job.get_derived_table()
                if records and derived_table:
                    records_written = self.tables_manager.write_enrichment_batch(
                        records, derived_table
                    )
                    results["records_created"][derived_table] = records_written
                    logger.debug(
                        "[PIPELINE:ENRICHMENT] %s → %s: %d records written to %s (job %d/%d, %.1f%% complete)",
                        self,
                        job,
                        records_written,
                        derived_table,
                        job_idx,
                        total_jobs,
                        (job_idx / total_jobs * 100) if total_jobs > 0 else 100,
                    )
                elif getattr(job, "writes_canonical", False):
                    updated = 0
                    if records and isinstance(records[0], dict):
                        updated = int(records[0].get("records_updated") or 0)
                    results["records_created"]["canonical_disclosure"] = (
                        int(results["records_created"].get("canonical_disclosure") or 0) + updated
                    )
                    logger.debug(
                        "[PIPELINE:ENRICHMENT] %s → %s: %d canonical disclosure rows updated (job %d/%d)",
                        self,
                        job,
                        updated,
                        job_idx,
                        total_jobs,
                    )
                else:
                    table_name = derived_table or job.get_job_name()
                    results["records_created"][table_name] = 0
                    logger.debug(
                        "[PIPELINE:ENRICHMENT] %s → %s: completed with 0 records (job %d/%d, %.1f%% complete)",
                        self,
                        job,
                        job_idx,
                        total_jobs,
                        (job_idx / total_jobs * 100) if total_jobs > 0 else 100,
                    )
                
                # After job completes, calculate how many messages were effectively processed
                messages_processed_this_job = total_messages if records or getattr(job, "writes_canonical", False) else 0
                messages_processed_so_far += messages_processed_this_job
                
                # Call progress callback after job completes
                if progress_callback:
                    job_progress_percent = (job_idx / total_jobs * 100) if total_jobs > 0 else 100
                    # Update with messages processed so far (cumulative across jobs)
                    # Job is 100% complete
                    progress_callback(messages_processed_so_far, total_messages, job_name, job_progress_percent, 100.0)
                
                results["jobs_run"] += 1
            except Exception as exc:
                logger.error(
                    "[PIPELINE:ENRICHMENT] %s → %s: failed: %s (job %d/%d)",
                    self,
                    job,
                    exc,
                    job_idx,
                    total_jobs,
                )
                results["errors"].append({"job": job.get_job_name(), "error": str(exc)})
                # Still count this job's messages as "processed" (even if failed) for progress tracking
                messages_processed_so_far += total_messages
                if progress_callback:
                    job_progress_percent = (job_idx / total_jobs * 100) if total_jobs > 0 else 100
                    # Mark job as 100% complete (even if failed, we've moved past it)
                    progress_callback(messages_processed_so_far, total_messages, job.get_job_name(), job_progress_percent, 100.0)
        
        logger.debug(
            "[PIPELINE:ENRICHMENT] %s: Enrichment complete: %d jobs run, %d total records created",
            self,
            results["jobs_run"],
            sum(results["records_created"].values()),
        )
        from ..engine.pipeline_memory import flush_engine_model_cache_after_pipeline

        flush_engine_model_cache_after_pipeline()
        return results

    def enqueue_signal_derive_stub(
        self,
        *,
        source_id: str,
        sync_batch_id: str,
        dataset_id: Optional[str] = None,
        signal_derivation_jobs: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Accept signal_derive work without running ML (Phase 1 stub)."""
        _ = dataset_id
        logger.info(
            "signal_derive stub — deferred Phase 2 (source_id=%s sync_batch_id=%s jobs=%s)",
            source_id,
            sync_batch_id,
            list(signal_derivation_jobs or []),
        )
        return {
            "status": "accepted",
            "job_name": "signal_derive",
            "sync_batch_id": sync_batch_id,
            "source_id": source_id,
            "signal_derivation_jobs": list(signal_derivation_jobs or []),
        }


class SignalDerivationOrchestrator(EnrichmentOrchestrator):
    """Phase 2 orchestrator: source-driven signal jobs writing via adapter bundle."""

    def __init__(
        self,
        tables_manager: Optional[DerivedTablesManager] = None,
        *,
        adapters: Optional[Any] = None,
        name: Optional[str] = None,
        engine: Optional[Any] = None,
    ):
        super().__init__(tables_manager, name=name, engine=engine)
        self._adapters = adapters
        from .jobs import SIGNAL_JOB_REGISTRY

        self._signal_jobs = dict(SIGNAL_JOB_REGISTRY)

    def _get_adapters(self) -> Any:
        if self._adapters is not None:
            return self._adapters
        from ..storage.adapters.factory import AdapterFactory

        bundle = AdapterFactory.from_runtime()
        self._adapters = bundle
        return bundle

    def _resolve_job_names(self, source_id: str, job_names: Optional[List[str]]) -> List[str]:
        if job_names:
            return list(job_names)
        from ..sources.canonical_signal_defaults import resolved_signal_derivation_jobs
        from ..sources.registry import REGISTRY

        source_def = REGISTRY.get(source_id)
        if source_def:
            return resolved_signal_derivation_jobs(source_def)
        return []

    async def run_signal_derivation(
        self,
        canonical_messages: List[Dict[str, Any]],
        source_id: str,
        job_names: Optional[List[str]] = None,
        sync_batch_id: Optional[str] = None,
        progress_callback: Optional[Callable[[int, int, str, float, float], None]] = None,
    ) -> Dict[str, Any]:
        from ..core.state import get_db_connection
        from ..enrichment.job_writer import write_signal_records
        from ..pipeline.envelope import JobEnvelope
        from ..pipeline.stages import PipelineStage

        resolved = self._resolve_job_names(source_id, job_names)
        adapters = self._get_adapters()
        conn = get_db_connection()
        results: Dict[str, Any] = {
            "jobs_run": 0,
            "records_created": {},
            "errors": [],
            "deferred_jobs": [],
            "envelopes": [],
        }
        jobs_to_run = [self._signal_jobs[name] for name in resolved if name in self._signal_jobs]
        self._bind_shared_engine(jobs_to_run)
        total_jobs = len(jobs_to_run)
        logger.debug(
            "[PIPELINE:SIGNAL_DERIVE] source_id=%s batch_id=%s jobs=%s messages=%d",
            source_id,
            sync_batch_id,
            resolved,
            len(canonical_messages),
        )
        for job_idx, job in enumerate(jobs_to_run, 1):
            if not job.should_run(canonical_messages):
                continue
            job_name = job.get_job_name()
            try:
                records = await job.enrich(canonical_messages)
                if records and records[0].get("_deferred"):
                    results["deferred_jobs"].append(job_name)
                    env = JobEnvelope(
                        stage=PipelineStage.SIGNAL_DERIVE,
                        source_id=source_id,
                        batch_id=sync_batch_id or "unknown",
                        record_ids=[],
                        status="failed",
                        provenance={"job_name": job_name, "status": "deferred"},
                        error=str(records[0].get("error")),
                        idempotency_key=f"{sync_batch_id or source_id}:{job_name}:signal_derive",
                    )
                    results["envelopes"].append(env.to_dict())
                    continue
                records = [r for r in records if not r.get("_deferred")]
                provenance = {"provider": records[0].get("provider") if records else "unknown", "job_id": job_name}
                if records:
                    model = records[0].get("model")
                    if model:
                        provenance["model"] = model
                    count = write_signal_records(
                        job_name,
                        records,
                        adapters=adapters,
                        tables_manager=self.tables_manager,
                        provenance={**provenance, "sync_batch_id": sync_batch_id, "source_id": source_id},
                        conn=conn,
                    )
                    results["records_created"][job_name] = count
                else:
                    results["records_created"][job_name] = 0
                env = JobEnvelope(
                    stage=PipelineStage.SIGNAL_DERIVE,
                    source_id=source_id,
                    batch_id=sync_batch_id or "unknown",
                    record_ids=[str(r.get("message_id") or r.get("record_id")) for r in records[:50]],
                    status="completed",
                    provenance={**provenance, "records_out": results["records_created"].get(job_name, 0)},
                    idempotency_key=f"{sync_batch_id or source_id}:{job_name}:signal_derive",
                )
                results["envelopes"].append(env.to_dict())
                results["jobs_run"] += 1
                if progress_callback:
                    progress_callback(0, len(canonical_messages), job_name, (job_idx / total_jobs) * 100, 100.0)
            except Exception as exc:
                logger.error("[PIPELINE:SIGNAL_DERIVE] job %s failed: %s", job_name, exc)
                results["errors"].append({"job": job_name, "error": str(exc)})

        try:
            from ..features.signal.dimension_profiles import DimensionProfileUpdater

            DimensionProfileUpdater(adapters, conn).upsert_all(deferred_jobs=results["deferred_jobs"])
        except Exception as exc:
            logger.debug("[PIPELINE:SIGNAL_DERIVE] dimension profile update skipped: %s", exc)

        logger.debug(
            "[PIPELINE:SIGNAL_DERIVE] complete source_id=%s batch_id=%s jobs_run=%d deferred=%s",
            source_id,
            sync_batch_id,
            results["jobs_run"],
            results["deferred_jobs"],
        )
        from ..engine.pipeline_memory import flush_engine_model_cache_after_pipeline

        flush_engine_model_cache_after_pipeline()
        return results

    async def run_canonical(
        self,
        canonical_messages: List[Dict[str, Any]],
        job_names: Optional[List[str]] = None,
        progress_callback: Optional[Callable[[int, int, str, float, float], None]] = None,
    ) -> Dict[str, Any]:
        logger.debug("run_canonical delegates to legacy EnrichmentOrchestrator path")
        return await super().run_canonical(canonical_messages, job_names, progress_callback)


# Backward-compatible alias: signal orchestrator is the Phase 2 default export.
EnrichmentOrchestratorSignal = SignalDerivationOrchestrator
