"""Registry contract for declared enrichment jobs (PRD_03 R3)."""

from __future__ import annotations

from typing import Dict, List

from .jobs import CANONICAL_JOBS
from .derived_tables import DerivedTablesManager

# Jobs explicitly deferred when provider unavailable.
DEFERRED_JOBS = frozenset({"dimension_summary", "goal_extraction", "topic_clusters"})

# Jobs that must write via DerivedTablesManager or signal adapters.
_REQUIRED_WRITERS = {
    "topics": "message_topics",
    "sentiment": "message_sentiment",
    "embeddings": "message_embeddings",
    "entities": "message_entities",
    "emo_27": "message_emotions",
}
_WRITER_METHOD_NAMES = {
    "emo_27": "_write_emotions_batch",
    "topics": "_write_topics_batch",
    "sentiment": "_write_sentiment_batch",
    "embeddings": "_write_embeddings_batch",
    "entities": "_write_entities_batch",
}


def validate_registry_job_contract() -> List[str]:
    """Return list of contract violations (empty if valid)."""
    violations: List[str] = []
    job_names = {job.get_job_name() for job in CANONICAL_JOBS}
    for job_name in _REQUIRED_WRITERS:
        if job_name not in job_names:
            violations.append(f"missing registry job: {job_name}")
            continue
        method = getattr(
            DerivedTablesManager,
            _WRITER_METHOD_NAMES.get(job_name, f"_write_{job_name}_batch"),
            None,
        )
        if method is None:
            violations.append(f"missing writer method for {job_name}")
            continue
        if "stub" in (method.__doc__ or "").lower():
            violations.append(f"stub writer still documented for {job_name}")
    for deferred in DEFERRED_JOBS:
        if deferred in job_names and deferred not in DEFERRED_JOBS:
            violations.append(f"undeclared deferral for {deferred}")
    return violations
