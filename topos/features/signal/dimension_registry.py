"""Wiki signal dimension registry — MVP live subset + full 10-dimension taxonomy."""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from .dimension_definition_loader import (
    gate_objects_for_dimension,
    get_definition_or_none,
    view_types_for_dimension,
)

# Full wiki taxonomy (Layer 3): all brief shells and future canonical routing.
SIGNAL_DIMENSIONS: List[Dict[str, str]] = [
    {"id": "profile", "label": "Profile"},
    {"id": "time", "label": "Time"},
    {"id": "interests", "label": "Interests"},
    {"id": "relationships", "label": "Relationships"},
    {"id": "work", "label": "Work"},
    {"id": "memory", "label": "Memory"},
    {"id": "wellbeing", "label": "Wellbeing"},
    {"id": "resources", "label": "Resources"},
    {"id": "places", "label": "Places"},
    {"id": "intentions", "label": "Intentions"},
]

SIGNAL_DIMENSION_IDS: List[str] = [d["id"] for d in SIGNAL_DIMENSIONS]
SIGNAL_DIMENSION_IDS_SET = frozenset(SIGNAL_DIMENSION_IDS)

# MVP subset with live signal derivation today (data health + legacy APIs).
MVP_DIMENSION_IDS: frozenset[str] = frozenset(
    {"time", "relationships", "memory", "profile", "interests"}
)

MVP_DIMENSIONS: List[Dict[str, str]] = [d for d in SIGNAL_DIMENSIONS if d["id"] in MVP_DIMENSION_IDS]

DIMENSION_SIGNAL_OBJECTS: Dict[str, List[str]] = {
    "profile": ["goal_extraction", "activity_summary", "profile_living_brief"],
    "time": ["availability_scores", "signal_scores", "time_living_brief"],
    "interests": ["url_classification", "topics", "interests_living_brief"],
    "relationships": ["relationship_edges", "message_entities", "relationships_living_brief"],
    "work": ["user_goals", "work_context_summary", "work_living_brief"],
    "memory": ["embeddings", "topics", "dimension_summary", "message_emotions", "memory_living_brief"],
    "wellbeing": ["wellness_summary", "wellbeing_living_brief"],
    "resources": ["financial_summary", "resources_living_brief"],
    "places": ["location_context", "places_living_brief"],
    "intentions": ["user_goals", "goal_inference", "intentions_living_brief"],
}

# Signal-row targets for data health volume scoring: the count at which a
# dimension's volume component reaches 50% (see data_health.volume_score;
# saturating curve, never a hard 100% cliff). High-traffic dimensions need
# more rows to look healthy than sparse ones (e.g. places, wellbeing).
DIMENSION_VOLUME_TARGETS: Dict[str, int] = {
    "memory": 200,
    "relationships": 200,
    "interests": 100,
    "work": 75,
    "time": 50,
    "profile": 40,
    "intentions": 40,
    "wellbeing": 25,
    "resources": 25,
    "places": 25,
}
DEFAULT_VOLUME_TARGET = 50

# Target canonical tables per dimension (wiki Layer 2); ingestion fills these over time.
DIMENSION_CANONICAL_TABLES: Dict[str, List[str]] = {
    "profile": ["public_bio", "personal_profile", "skill", "experience", "activity_events", "profile_records", "journal_entries"],
    "time": ["calendar_events", "journal_entries", "availability_window", "commitment", "routine"],
    "interests": ["activity_events", "topic", "content_item", "reading_activity"],
    "relationships": [
        "conversation_messages",
        "contacts",
        "contact_identifiers",
        "relationship_edges",
        "journal_entries",
    ],
    "work": ["ai_chat_messages", "project", "task", "document", "user_goals", "profile_records"],
    "memory": ["ai_chat_messages", "conversation_messages", "journal_entries", "note"],
    "wellbeing": ["journal_entries", "sleep_session", "mood_entry", "activity_log"],
    "resources": ["transaction", "receipt", "subscription", "financial_summary", "financial_transactions"],
    "places": ["calendar_events", "location_events", "location_event", "trip", "place", "journal_entries"],
    "intentions": ["user_goals", "ai_chat_messages", "journal_entries"],
}

# Canonical ingest lanes → dimensions that should receive brief LLM updates from that lane.
CANONICAL_GROUP_BRIEF_DIMENSIONS: Dict[str, Tuple[str, ...]] = {
    "ai_messages": ("memory", "work", "intentions"),
    "conversations": ("relationships", "memory", "interests"),
    "activity": ("interests", "profile"),
    "schedule": ("time", "places", "relationships", "work"),
    "contacts": ("relationships", "profile"),
    "journal": ("wellbeing", "memory", "profile"),
    "profile": ("profile", "work", "intentions"),
    "financial": ("resources",),
    "places": ("places", "interests", "relationships"),
}

# Per-source overrides when a source spans dimensions beyond its canonical lane.
# Prefer brief_update_dimensions on DataSourceDefinition (see sources/registry.py).
SOURCE_BRIEF_DIMENSIONS: Dict[str, Tuple[str, ...]] = {}

# Default when source lane is unknown (message-heavy ingest paths).
DEFAULT_BRIEF_UPDATE_DIMENSIONS: Tuple[str, ...] = tuple(MVP_DIMENSION_IDS)


def is_signal_dimension(dimension: str) -> bool:
    return str(dimension or "").strip().lower() in SIGNAL_DIMENSION_IDS_SET


def view_types_by_dimension(dimension: str) -> List[str]:
    return view_types_for_dimension(str(dimension or "").strip().lower())


def gate_objects_by_dimension(dimension: str) -> List[str]:
    explicit = gate_objects_for_dimension(str(dimension or "").strip().lower())
    if explicit:
        return explicit
    dim = str(dimension or "").strip().lower()
    return list(DIMENSION_SIGNAL_OBJECTS.get(dim, []))


VIEW_TYPES_BY_DIMENSION: Dict[str, List[str]] = {
    d["id"]: view_types_by_dimension(d["id"]) for d in SIGNAL_DIMENSIONS
}

GATE_OBJECTS_BY_DIMENSION: Dict[str, List[str]] = {
    d["id"]: gate_objects_by_dimension(d["id"]) for d in SIGNAL_DIMENSIONS
}


def dimension_has_explicit_definition(dimension: str) -> bool:
    return get_definition_or_none(str(dimension or "").strip().lower()) is not None


def dimensions_for_brief_update(
    *,
    source_id: Optional[str] = None,
    canonical_group_id: Optional[str] = None,
    only_dimension: Optional[str] = None,
) -> Tuple[str, ...]:
    """
    Resolve which dimension briefs to LLM-update for an ingest batch.
    Empty brief shells always exist for all 10 dimensions regardless of this filter.
    """
    if only_dimension:
        dim = str(only_dimension).strip().lower()
        return (dim,) if dim in SIGNAL_DIMENSION_IDS_SET else ()

    sid = str(source_id or "").strip()
    group = str(canonical_group_id or "").strip()
    if sid or group:
        try:
            from ...sources.registry import REGISTRY

            defn = REGISTRY.get(sid) if sid else None
            if defn is not None:
                explicit_dims = getattr(defn, "brief_update_dimensions", None) or []
                normalized = tuple(
                    str(d).strip().lower()
                    for d in explicit_dims
                    if str(d).strip().lower() in SIGNAL_DIMENSION_IDS_SET
                )
                if normalized:
                    return normalized
                if not group:
                    group = str(defn.canonical_group_id or "").strip()
        except Exception:
            pass

    if sid and sid in SOURCE_BRIEF_DIMENSIONS:
        return SOURCE_BRIEF_DIMENSIONS[sid]

    if group and group in CANONICAL_GROUP_BRIEF_DIMENSIONS:
        return CANONICAL_GROUP_BRIEF_DIMENSIONS[group]

    return DEFAULT_BRIEF_UPDATE_DIMENSIONS
