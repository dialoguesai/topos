from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from ..utils.base_object import BaseObject
from .derived_tables import DerivedTablesManager
from .jobs import CANONICAL_JOBS, RAW_JOBS
from .jobs.base import BaseEnrichmentJob

logger = logging.getLogger("topos.enrichment.orchestrator")


class EnrichmentOrchestrator(BaseObject):
    def __init__(self, tables_manager: Optional[DerivedTablesManager] = None, *, name: Optional[str] = None):
        super().__init__(name=name)
        self.raw_jobs = list(RAW_JOBS)
        self.canonical_jobs = list(CANONICAL_JOBS)
        self.tables_manager = tables_manager or DerivedTablesManager()

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
        
        total_messages = len(canonical_messages)
        total_jobs = len(jobs_to_run)
        logger.info(
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
                logger.info(
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
                
                # After job completes, calculate how many messages were effectively processed
                # For jobs that create records, assume all messages were processed
                # For jobs that return 0 records, they still "processed" the messages (just didn't create output)
                messages_processed_this_job = total_messages if records else 0
                messages_processed_so_far += messages_processed_this_job
                
                if records:
                    # Write to derived table
                    table_name = job.get_derived_table()
                    records_written = self.tables_manager.write_enrichment_batch(
                        records, table_name
                    )
                    results["records_created"][table_name] = records_written
                    logger.info(
                        "[PIPELINE:ENRICHMENT] %s → %s: %d records written to %s (job %d/%d, %.1f%% complete)",
                        self,
                        job,
                        records_written,
                        table_name,
                        job_idx,
                        total_jobs,
                        (job_idx / total_jobs * 100) if total_jobs > 0 else 100,
                    )
                else:
                    results["records_created"][job.get_derived_table()] = 0
                    logger.info(
                        "[PIPELINE:ENRICHMENT] %s → %s: completed with 0 records (job %d/%d, %.1f%% complete)",
                        self,
                        job,
                        job_idx,
                        total_jobs,
                        (job_idx / total_jobs * 100) if total_jobs > 0 else 100,
                    )
                
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
        
        logger.info(
            "[PIPELINE:ENRICHMENT] %s: Enrichment complete: %d jobs run, %d total records created",
            self,
            results["jobs_run"],
            sum(results["records_created"].values()),
        )
        return results
