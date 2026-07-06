"""Unified enrichment catalog.

Single source of truth describing every enrichment the node can run, across
all three lanes (raw, canonical, signal). Merges the job registries in
``topos.enrichment.jobs`` with model defaults from
``topos.enrichment.models.mvp_defaults`` and adds product metadata
(description, output tables, cost tier, lifecycle support) so the catalog can
be exposed to the UI (`GET /v1/enrichments/catalog`), the Enrichment Lab, and
coverage/backfill tooling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .jobs import CANONICAL_JOBS, RAW_JOBS, SIGNAL_JOB_REGISTRY
from .models.mvp_defaults import MVP_JOB_SPECS

LANE_RAW = "raw"
LANE_CANONICAL = "canonical"
LANE_SIGNAL = "signal"

COST_LOW = "low"
COST_MEDIUM = "medium"
COST_HIGH = "high"


@dataclass(frozen=True)
class EnrichmentCatalogEntry:
    """Product-facing description of one enrichment job."""

    job_id: str
    title: str
    description: str
    lanes: Tuple[str, ...]
    output_tables: Tuple[str, ...]
    cost_tier: str = COST_MEDIUM
    default_provider: Optional[str] = None
    default_model: Optional[str] = None
    # HF task type used for lab model-override compatibility checks.
    hf_task: Optional[str] = None
    supports_backfill: bool = True
    supports_lab: bool = True
    supports_delete: bool = True
    # In CANONICAL_BASELINE_SIGNAL_JOBS (auto-enabled for canonical sources).
    baseline: bool = False
    # Raw-lane stubs that currently produce no output.
    stub: bool = False
    value_props: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "title": self.title,
            "description": self.description,
            "lanes": list(self.lanes),
            "output_tables": list(self.output_tables),
            "cost_tier": self.cost_tier,
            "default_provider": self.default_provider,
            "default_model": self.default_model,
            "hf_task": self.hf_task,
            "supports_backfill": self.supports_backfill,
            "supports_lab": self.supports_lab,
            "supports_delete": self.supports_delete,
            "baseline": self.baseline,
            "stub": self.stub,
            "value_props": list(self.value_props),
        }


_MODEL_DEFAULTS: Dict[str, Tuple[str, str]] = {
    job_id: (provider, model_path)
    for job_id, _task, provider, model_path, _pref in MVP_JOB_SPECS
}

# Output tables per job, including tables written outside get_derived_table()
# (signal adapters, entity spine, promotion targets). Used by coverage and
# per-enrichment delete. Tables listed here all carry a source_id column.
_OUTPUT_TABLES: Dict[str, Tuple[str, ...]] = {
    "emo_27": ("message_emotions",),
    "entities": ("message_entities",),
    "topics": ("message_topics",),
    "sentiment": ("message_sentiment",),
    "embeddings": ("signal_embeddings",),
    "url_classification": ("browser_url_classification", "signal_tags", "signal_facts"),
    "dimension_summary": ("signal_summaries",),
    "goal_extraction": ("user_goals",),
    "relationship_edges": ("relationship_edges",),
    "availability_scores": ("signal_scores",),
    "topic_clusters": ("topic_clusters",),
    "statistics": ("signal_facts",),
    "facts": ("signal_objects",),
    "timeline": ("timeline",),
}

# Jobs whose enrich() is a pure function over records (no direct table writes),
# safe to dry-run in the Enrichment Lab without touching node data.
_LAB_SAFE_JOBS = frozenset(
    {
        "emo_27",
        "entities",
        "sentiment",
        "embeddings",
        "topics",
        "url_classification",
        "goal_extraction",
    }
)

# Tables that are safe targets for per-enrichment coverage counting: they key
# rows by record_id (or message_id) and carry source_id.
_COVERAGE_TABLES: Dict[str, str] = {
    "emo_27": "message_emotions",
    "entities": "message_entities",
    "topics": "message_topics",
    "sentiment": "message_sentiment",
    "embeddings": "signal_embeddings",
    "url_classification": "signal_facts",
    "goal_extraction": "user_goals",
}

_STATIC_METADATA: Dict[str, Dict[str, Any]] = {
    "emo_27": {
        "title": "Emotion detection",
        "description": (
            "Classifies each message into 27 fine-grained emotions "
            "(GoEmotions), attaching the top label and confidence per message."
        ),
        "cost_tier": COST_MEDIUM,
        "hf_task": "text-classification",
        "value_props": ("signal_density", "precision_retrieval"),
    },
    "entities": {
        "title": "Entity extraction",
        "description": (
            "Extracts people, organizations, and places mentioned in each "
            "record and links them into the entity spine that powers the "
            "People view."
        ),
        "cost_tier": COST_MEDIUM,
        "hf_task": "token-classification",
        "value_props": ("signal_density", "precision_retrieval"),
    },
    "topics": {
        "title": "Topic extraction",
        "description": (
            "Uses a local LLM to label each record with the topics it "
            "discusses, enabling topic-scoped retrieval and grants."
        ),
        "cost_tier": COST_HIGH,
        "value_props": ("signal_density", "precision_retrieval"),
    },
    "sentiment": {
        "title": "Sentiment scoring",
        "description": (
            "Scores each record positive/neutral/negative for tone-aware "
            "insights and filters."
        ),
        "cost_tier": COST_LOW,
        "hf_task": "text-classification",
        "value_props": ("signal_density",),
    },
    "embeddings": {
        "title": "Semantic embeddings",
        "description": (
            "Encodes every record into a vector index so queries and grants "
            "can retrieve by meaning instead of scanning chronologically."
        ),
        "cost_tier": COST_MEDIUM,
        "hf_task": "feature-extraction",
        "value_props": ("precision_retrieval", "latency"),
    },
    "url_classification": {
        "title": "URL classification",
        "description": (
            "Classifies visited pages into interest categories (zero-shot), "
            "turning raw browser history into an Interests signal."
        ),
        "cost_tier": COST_HIGH,
        "hf_task": "zero-shot-classification",
        "value_props": ("signal_density",),
    },
    "dimension_summary": {
        "title": "Dimension briefs",
        "description": (
            "Writes short LLM briefs per life dimension (work, health, "
            "relationships, ...) summarizing recent activity."
        ),
        "cost_tier": COST_HIGH,
        "value_props": ("signal_density",),
    },
    "goal_extraction": {
        "title": "Goal extraction",
        "description": "Detects stated goals and intentions across records.",
        "cost_tier": COST_HIGH,
        "value_props": ("signal_density",),
    },
    "relationship_edges": {
        "title": "Relationship graph",
        "description": (
            "Builds who-talks-to-whom edges that power the People view and "
            "relationship context scopes."
        ),
        "cost_tier": COST_LOW,
        "value_props": ("signal_density",),
    },
    "availability_scores": {
        "title": "Availability scores",
        "description": "Derives schedule availability signals from calendar data.",
        "cost_tier": COST_LOW,
        "value_props": ("signal_density",),
    },
    "topic_clusters": {
        "title": "Topic clustering",
        "description": (
            "Clusters embeddings into named themes so Insights can show "
            "patterns across your whole history."
        ),
        "cost_tier": COST_MEDIUM,
        "value_props": ("signal_density", "precision_retrieval"),
    },
    "statistics": {
        "title": "Statistics engine",
        "description": (
            "Folds records into incremental statistics (counts, rhythms, "
            "streaks) promoted into durable facts."
        ),
        "cost_tier": COST_LOW,
        "value_props": ("signal_density", "latency"),
    },
    "facts": {
        "title": "Fact extraction",
        "description": (
            "Distills durable, dated, correctable facts from records into the "
            "fact store behind the Facts view."
        ),
        "cost_tier": COST_LOW,
        "value_props": ("signal_density", "precision_retrieval"),
    },
    "timeline": {
        "title": "Timeline projection",
        "description": "Projects records onto the episodic Timeline view.",
        "cost_tier": COST_LOW,
        "value_props": ("signal_density",),
    },
    "attachments": {
        "title": "Attachment indexing",
        "description": "Indexes attachments referenced by raw records (not yet implemented).",
        "cost_tier": COST_LOW,
    },
    "tool_calls": {
        "title": "Tool call extraction",
        "description": "Extracts tool/function calls from AI chat raw records (not yet implemented).",
        "cost_tier": COST_LOW,
    },
    "language": {
        "title": "Language detection",
        "description": "Detects the language of raw records (not yet implemented).",
        "cost_tier": COST_LOW,
    },
    "time_normalization": {
        "title": "Time normalization",
        "description": "Normalizes raw timestamps to UTC ISO-8601 (not yet implemented).",
        "cost_tier": COST_LOW,
    },
}


def _baseline_job_ids() -> Tuple[str, ...]:
    from ..sources.canonical_signal_defaults import CANONICAL_BASELINE_SIGNAL_JOBS

    return tuple(CANONICAL_BASELINE_SIGNAL_JOBS)


def _build_catalog() -> Dict[str, EnrichmentCatalogEntry]:
    baseline = set(_baseline_job_ids())
    canonical_ids = {job.get_job_name() for job in CANONICAL_JOBS}
    signal_ids = set(SIGNAL_JOB_REGISTRY.keys())
    raw_ids = {job.get_job_name() for job in RAW_JOBS}

    entries: Dict[str, EnrichmentCatalogEntry] = {}
    for job_id in sorted(canonical_ids | signal_ids | raw_ids):
        lanes: List[str] = []
        if job_id in raw_ids:
            lanes.append(LANE_RAW)
        if job_id in canonical_ids:
            lanes.append(LANE_CANONICAL)
        if job_id in signal_ids:
            lanes.append(LANE_SIGNAL)

        meta = _STATIC_METADATA.get(job_id, {})
        provider, model = _MODEL_DEFAULTS.get(job_id, (None, None))
        is_stub = job_id in raw_ids and job_id not in canonical_ids and job_id not in signal_ids
        entries[job_id] = EnrichmentCatalogEntry(
            job_id=job_id,
            title=str(meta.get("title") or job_id.replace("_", " ").title()),
            description=str(meta.get("description") or ""),
            lanes=tuple(lanes),
            output_tables=_OUTPUT_TABLES.get(job_id, ()),
            cost_tier=str(meta.get("cost_tier") or COST_MEDIUM),
            default_provider=provider,
            default_model=model or None,
            hf_task=meta.get("hf_task"),
            supports_backfill=not is_stub,
            supports_lab=job_id in _LAB_SAFE_JOBS,
            supports_delete=bool(_OUTPUT_TABLES.get(job_id)),
            baseline=job_id in baseline,
            stub=is_stub,
            value_props=tuple(meta.get("value_props") or ()),
        )
    return entries


_CATALOG: Optional[Dict[str, EnrichmentCatalogEntry]] = None


def get_enrichment_catalog() -> Dict[str, EnrichmentCatalogEntry]:
    """All enrichments keyed by job_id (built once)."""
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = _build_catalog()
    return _CATALOG


def get_catalog_entry(job_id: str) -> Optional[EnrichmentCatalogEntry]:
    return get_enrichment_catalog().get(str(job_id or "").strip())


def list_catalog_entries() -> List[Dict[str, Any]]:
    return [entry.to_dict() for entry in get_enrichment_catalog().values()]


def output_tables_for_job(job_id: str) -> Tuple[str, ...]:
    entry = get_catalog_entry(job_id)
    return entry.output_tables if entry else ()


def coverage_table_for_job(job_id: str) -> Optional[str]:
    return _COVERAGE_TABLES.get(str(job_id or "").strip())


def jobs_configured_for_source(source_def: Any) -> Dict[str, List[str]]:
    """Effective enrichment jobs per lane for a source definition."""
    from ..sources.canonical_signal_defaults import resolved_signal_derivation_jobs

    return {
        LANE_RAW: list(getattr(source_def, "raw_enrichment_jobs", []) or []),
        LANE_CANONICAL: list(getattr(source_def, "canonical_enrichment_jobs", []) or []),
        LANE_SIGNAL: resolved_signal_derivation_jobs(source_def),
    }


def all_jobs_for_source(source_def: Any) -> List[str]:
    """Deduplicated job ids across all lanes for a source."""
    lanes = jobs_configured_for_source(source_def)
    seen: List[str] = []
    for lane_jobs in lanes.values():
        for job_id in lane_jobs:
            if job_id and job_id not in seen:
                seen.append(job_id)
    return seen
