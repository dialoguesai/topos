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
    redact, nsfw = src.split("_nsfw_chunk =", 1)
    assert "progress_callback(_redact_done, _redact_total, PRIVACY_STAGE_REDACT)" in redact
    assert "progress_callback(_nsfw_done, _nsfw_total, PRIVACY_STAGE_NSFW)" in nsfw


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


# The two tests that pinned the slot model are gone rather than patched: the
# model itself was the defect. It gave both privacy passes one leading slot, so
# a correct implementation of it still produced a bar that ran backwards.
# test_the_bar_is_built_from_one_ordered_stage_list below covers what replaced it.


def test_the_privacy_layer_signature_carries_the_stage():
    params = inspect.signature(run_privacy_disclosure_layer).parameters
    assert "progress_callback" in params


def test_progress_groups_are_sized_for_the_bar_not_for_throughput():
    """Both engine backends loop over `items` one at a time, so a group of 32
    bought no batched inference -- only a bar that moved once every two
    minutes and read as frozen. Measured: ~3.75s per record on CPU."""
    from topos.disclosure.privacy_layer import PRIVACY_PROGRESS_CHUNK
    from topos.sanitization.privacy_filter import PRIVACY_DISCLOSE_MAX_BATCH

    assert PRIVACY_PROGRESS_CHUNK < PRIVACY_DISCLOSE_MAX_BATCH


def test_the_backends_really_do_loop_per_item():
    """The premise of the group size above. If either ever batches for real,
    shrinking the group starts costing throughput and this should be revisited."""
    import inspect

    from topos.sanitization.nsfw_classifier import classify_nsfw_batch
    from topos.sanitization.privacy_filter import redact_privacy_batch

    for fn in (redact_privacy_batch, classify_nsfw_batch):
        assert "for item in items:" in inspect.getsource(fn), fn.__name__


def test_the_bar_is_built_from_one_ordered_stage_list():
    """The backwards jump, at its source.

    The percentage was built from a slot model that gave BOTH privacy passes the
    same leading slot, so it ran 0 -> 9% through redaction and then started
    again from 0 through the NSFW pass. Observed live as a bar going backwards
    while the work only went forwards.
    """
    src = inspect.getsource(manager.IngestionManager.process_job)
    assert "_stage_order" in src
    assert "_stage_order.index(job_name)" in src
    # The old two-scale arithmetic must be gone, not merely bypassed.
    assert "slots = jobs_total + 1" not in src
    assert "jobs_frac" not in src


def test_the_percentage_never_goes_down():
    """Even a late post from a finished stage must not drag the bar back."""
    src = inspect.getsource(manager.IngestionManager.process_job)
    assert "_high_water" in src
    assert "if overall < _high_water[0]:" in src


def test_job_percent_is_no_longer_mixed_into_the_bar():
    """It is the orchestrator's view of the JOB list, which does not know the
    privacy passes exist. Mixing the two scales is what broke this."""
    src = inspect.getsource(manager.IngestionManager.process_job)
    calc = src.split("stage_frac =", 1)[1].split("body = {", 1)[0]
    assert "job_percent" not in calc


def test_the_step_count_uses_the_jobs_this_source_actually_runs():
    """A ChatGPT export runs five enrichment jobs, not the full ten. Counting
    the catalogue would tell the user "step 3 of 12" for a 7-step import, and
    name stages that never run."""
    src = inspect.getsource(manager.IngestionManager.process_job)
    assert "effective_canonical_enrichment_jobs(source_def)" in src
    assert '"pipeline_stage"' in src


def test_the_stage_names_match_what_the_orchestrator_reports():
    """The order list is keyed by job name; if these drifted, every stage would
    fall into the unknown branch and the bar would freeze."""
    from topos.enrichment.jobs import CANONICAL_JOBS
    from topos.enrichment.source_overrides import effective_canonical_enrichment_jobs
    from topos.sources.registry import REGISTRY

    catalogue = {j.get_job_name() for j in CANONICAL_JOBS}
    source = REGISTRY.get("chatgpt_file_ingestion")
    effective = effective_canonical_enrichment_jobs(source) or []
    assert effective, "no enrichment jobs resolved for the file source"
    assert set(effective) <= catalogue, set(effective) - catalogue


def test_signal_derivation_reports_too():
    """The third place in this chain that ACCEPTED a progress callback and was
    handed none — and the most expensive. A live import sat on
    "emo 27, 275 of 275" for 37 minutes while this phase ran topics over 726
    records underneath it, with the node at work the whole time."""
    src = inspect.getsource(canonical_pipeline.run_post_canonical_pipeline)
    assert "progress_callback=_signal_progress" in src


def test_the_stage_list_covers_signal_derivation():
    """It is the larger half: 5 canonical jobs against 13 signal ones on a file
    import. Leaving it out made the bar reach "7 of 7" and stop."""
    src = inspect.getsource(manager.IngestionManager.process_job)
    assert "resolved_signal_derivation_jobs(source_def)" in src
    assert 'f"deriving_{name}"' in src


def test_signal_stages_are_named_apart_from_the_canonical_ones():
    """Four job names run in BOTH phases over different populations. Sharing a
    name means a position lookup finds the canonical entry, and the bar walks
    backwards into a stage that already finished."""
    from topos.enrichment.source_overrides import effective_canonical_enrichment_jobs
    from topos.sources.canonical_signal_defaults import resolved_signal_derivation_jobs
    from topos.sources.registry import REGISTRY

    source = REGISTRY.get("chatgpt_file_ingestion")
    canon = list(effective_canonical_enrichment_jobs(source) or [])
    signal = list(resolved_signal_derivation_jobs(source) or [])
    assert set(canon) & set(signal), "no overlap left — this guard is now moot, re-check the naming"

    order = ["a", "b"] + canon + [f"deriving_{n}" for n in signal]
    assert len(order) == len(set(order)), "duplicate stage names would break position lookup"
