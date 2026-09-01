"""The stage before enrichment must report too, and report the real work.

Measured on a live import of 726 conversations: the job read
`processing · parsing · 0.0%` for its entire duration while the node sat at
195% CPU running torch on the privacy layer. Nothing was stuck — the stage that
takes the longest simply had no way to say so, and the screen kept whatever
label the ingest set before it.

Two separate defects meet here:

1. ``run_privacy_disclosure_layer`` already ACCEPTED a progress callback, and
   fired it only on the branches that SKIP a message. A run with nothing to
   skip reported nothing at all — a hook that measures the cheap path and goes
   silent through the expensive one.
2. The orchestrator pinned ``processed_count=0`` mid-job, so any caller
   rendering "N of M" read zero for a whole job, and its completion callback
   passed a CUMULATIVE count against a per-job denominator.
"""

from __future__ import annotations

import inspect

from topos.disclosure.privacy_layer import (
    PRIVACY_STAGE_NSFW,
    PRIVACY_STAGE_REDACT,
    run_privacy_disclosure_layer,
)
from topos.enrichment.orchestrator import EnrichmentOrchestrator
from topos.ingestion import canonical_pipeline, manager


def test_the_pipeline_hands_the_privacy_layer_a_reporter():
    """The gap this file exists for: the call site passed no callback, so the
    longest stage of an import was structurally unable to report."""
    src = inspect.getsource(canonical_pipeline.run_post_canonical_pipeline)
    assert "progress_callback=_privacy_progress" in src


def test_the_expensive_batches_report_not_just_the_skips():
    """Both model passes must report from the loop that does the work."""
    src = inspect.getsource(run_privacy_disclosure_layer)
    redact = src.split("PRIVACY_DISCLOSE_MAX_BATCH)", 1)[1].split("NSFW_CLASSIFY_MAX_BATCH", 1)[0]
    assert f"progress_callback(_redact_done, _redact_total, {'PRIVACY_STAGE_REDACT'})" in redact
    nsfw = src.split("NSFW_CLASSIFY_MAX_BATCH)", 1)[1]
    assert f"progress_callback(_nsfw_done, _nsfw_total, {'PRIVACY_STAGE_NSFW'})" in nsfw


def test_the_two_passes_are_named_apart():
    """They are different checks and take different time; one label for both
    would hide a whole pass behind the other's progress."""
    assert PRIVACY_STAGE_REDACT != PRIVACY_STAGE_NSFW
    for name in (PRIVACY_STAGE_REDACT, PRIVACY_STAGE_NSFW):
        # These surface verbatim in the UI, so they are written for a person.
        assert name == name.lower()
        assert "_" not in name


def test_the_orchestrator_reports_the_real_within_job_position():
    """It passed a literal 0 with a note that it was unused. It is used now."""
    src = inspect.getsource(EnrichmentOrchestrator.run_canonical)
    assert "processed_count=current_count" in src
    assert "processed_count=0" not in src


def test_a_completed_job_never_reports_more_than_its_own_total():
    """The completion callback passed a cumulative count against a per-job
    denominator, so the fourth job of ten rendered "2,904 of 726"."""
    src = inspect.getsource(EnrichmentOrchestrator.run_canonical)
    assert "progress_callback(messages_processed_so_far, total_messages" not in src
    assert "progress_callback(total_messages, total_messages" in src


def test_the_privacy_stage_owns_the_slot_ahead_of_the_jobs():
    """It runs before them and is a stage in its own right, so the bar has
    jobs + 1 slots. Left in the job band it reported a flat zero."""
    src = inspect.getsource(manager.IngestionManager.process_job)
    assert "slots = jobs_total + 1" in src
    assert "_PRE_ENRICHMENT_STAGES" in src


def test_the_final_job_slot_is_not_double_counted():
    """At completion a job counts as done AND reports 100%, so the two terms
    both claim the last slot unless the fraction is clamped first."""
    src = inspect.getsource(manager.IngestionManager.process_job)
    assert "jobs_frac = min(" in src


def test_the_privacy_layer_signature_carries_the_stage():
    params = inspect.signature(run_privacy_disclosure_layer).parameters
    assert "progress_callback" in params
