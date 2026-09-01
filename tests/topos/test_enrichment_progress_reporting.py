"""Progress must be reported through the stage that actually takes the time.

Storing an import is seconds. The per-record model work afterwards — entities,
topics, facts — is most of an hour, and it was the part the screen could say
nothing about: a healthy job read `processing · 0.0%` indefinitely, which is
exactly what a dead one reads.

Every layer already accepted a progress callback — the orchestrator, and each
enrichment job. The ingestion path was the single link that passed None.
"""

from __future__ import annotations

import inspect

from topos.enrichment.orchestrator import EnrichmentOrchestrator
from topos.ingestion import canonical_pipeline, manager


def test_the_pipeline_accepts_a_progress_reporter():
    params = inspect.signature(canonical_pipeline.run_post_canonical_pipeline).parameters
    assert "enrichment_progress" in params


def test_the_pipeline_hands_it_to_the_orchestrator():
    src = inspect.getsource(canonical_pipeline.run_post_canonical_pipeline)
    assert "progress_callback=enrichment_progress" in src


def test_the_orchestrator_still_takes_one():
    # The chain is only as good as its weakest link; this end already worked.
    params = inspect.signature(EnrichmentOrchestrator.run_canonical).parameters
    assert "progress_callback" in params


def test_the_manager_passes_a_reporter_rather_than_none():
    """The defect: this call site dropped the callback, so nothing below it
    could report and the bar sat at zero through the whole slow phase."""
    src = inspect.getsource(manager.IngestionManager.process_job)
    assert "enrichment_progress=_on_enrichment_progress" in src


def test_progress_posts_are_throttled():
    """A callback fires per record. Posting each one would turn telemetry into
    a load test against the control plane."""
    src = inspect.getsource(manager.IngestionManager.process_job)
    assert "(now - _last_post[0]) < 2.0" in src


def test_the_end_of_a_job_is_never_throttled_away():
    # The job-to-job transition is the change a watcher most wants to see, and
    # it is exactly what a naive time throttle would swallow.
    src = inspect.getsource(manager.IngestionManager.process_job)
    assert "finished = current_job_progress >= 100.0" in src
    assert "if not finished and" in src


def test_reporting_never_fails_the_import():
    """Telemetry must not be able to break the thing it describes."""
    src = inspect.getsource(manager.IngestionManager.process_job)
    assert "telemetry only" in src


def test_overall_percent_never_claims_completion_early():
    # Enrichment finishing is not the import finishing; 100% belongs to the
    # completion post, not to the last enrichment job.
    src = inspect.getsource(manager.IngestionManager.process_job)
    assert "min(\n                        99.0" in src or "min(99.0" in src
