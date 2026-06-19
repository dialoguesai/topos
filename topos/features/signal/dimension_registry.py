"""Wiki MVP signal dimension registry (5 dimensions)."""

from __future__ import annotations

from typing import Dict, List

MVP_DIMENSIONS: List[Dict[str, str]] = [
    {"id": "time", "label": "Time"},
    {"id": "relationships", "label": "Relationships"},
    {"id": "memory", "label": "Memory"},
    {"id": "profile", "label": "Profile"},
    {"id": "interests", "label": "Interests"},
]

DIMENSION_SIGNAL_OBJECTS: Dict[str, List[str]] = {
    "time": ["availability_scores", "signal_scores"],
    "relationships": ["relationship_edges", "message_entities"],
    "memory": ["embeddings", "topics", "dimension_summary", "message_emotions"],
    "profile": ["goal_extraction", "activity_summary"],
    "interests": ["url_classification", "topics"],
}
