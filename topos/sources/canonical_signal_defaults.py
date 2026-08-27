"""Default signal derivation for sources mapped to Topos canonical tables."""

from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple, Union

# Out-of-box signal jobs for any source that writes to a Topos canonical table.
CANONICAL_BASELINE_SIGNAL_JOBS: Tuple[str, ...] = (
    "embeddings",
    "dimension_summary",
    "topic_clusters",
    "entities",
    "relationship_edges",
    "statistics",
    "facts",
    "timeline",
    "attention_triage",
    # Runs after the producers above and indexes what they wrote, so the
    # derivation layer's own objects are reachable by meaning rather than only
    # through a hand-written alias per question.
    "derived_object_embeddings",
)


def maps_to_canonical_table(source_def: Any) -> bool:
    """True when the source maps to a Topos canonical table."""
    from .bundled_canonical_triples import maps_to_canonical_table as _maps

    return _maps(source_def)


def resolved_signal_derivation_jobs(
    source_def: Any,
    *,
    explicit_jobs: Optional[Sequence[str]] = None,
) -> List[str]:
    """
    Resolve signal derivation jobs for a source.

    Canonical-mapped sources always receive CANONICAL_BASELINE_SIGNAL_JOBS
    (embeddings, briefs, topic clusters, graph). Source definitions may list
    additional lane-specific jobs; repeating baseline jobs is optional.
    Runtime per-source overrides (user attach/detach) are applied last unless
    the caller passed explicit jobs.
    """
    if explicit_jobs is not None:
        return list(explicit_jobs)

    extra_jobs = list(getattr(source_def, "signal_derivation_jobs", None) or [])
    if not maps_to_canonical_table(source_def):
        merged = list(extra_jobs)
    else:
        merged = []
        for job in (*CANONICAL_BASELINE_SIGNAL_JOBS, *extra_jobs):
            if job and job not in merged:
                merged.append(job)

    source_id = str(getattr(source_def, "source_id", "") or "")
    if source_id:
        from ..enrichment.source_overrides import apply_overrides_to_jobs

        merged = apply_overrides_to_jobs(source_id, "signal", merged)
    return merged


def topic_cluster_eligible_source_ids(registry_values: Sequence[Any]) -> Tuple[str, ...]:
    """Source ids whose embeddings participate in cross-source topic clustering."""
    return tuple(
        sorted(
            str(defn.source_id)
            for defn in registry_values
            if str(getattr(defn, "source_id", "") or "").strip() and maps_to_canonical_table(defn)
        )
    )
