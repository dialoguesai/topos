"""Render narrative brief markdown from typed signal objects."""

from __future__ import annotations

from typing import Any, Dict, List

from .brief_ontology import primary_section_id, recent_section_id
from .brief_schemas import empty_structured_for_dimension as empty_structured, render_markdown


def render_brief_from_objects(
    dimension: str,
    objects: List[Dict[str, Any]],
    *,
    updated_at: str | None = None,
) -> str:
    dim = str(dimension or "").strip().lower()
    structured = empty_structured(dim)
    sections = structured["sections"]
    primary = primary_section_id(dim)
    recent = recent_section_id(dim)

    if dim in ("memory", "wellbeing"):
        labels = _collect_labels(objects, limit=6 if dim == "memory" else 4)
        prefix = "Recurring themes" if dim == "memory" else "Wellbeing bands"
        body = f"{prefix}: " + ", ".join(labels) if labels else "_No signal yet._"
        sections[primary]["markdown"] = body
        if recent != primary:
            sections[recent]["markdown"] = body
    else:
        lines = []
        for obj in objects[:8]:
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            label = payload.get("label") or payload.get("goal_text") or payload.get("availability_kind")
            if label:
                lines.append(f"- {label}")
        body = "\n".join(lines) if lines else "_No typed objects yet._"
        sections[primary]["markdown"] = body

    return render_markdown(dim, structured, updated_at=updated_at)


def _collect_labels(objects: List[Dict[str, Any]], *, limit: int) -> List[str]:
    labels: List[str] = []
    for obj in objects[:limit]:
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        for key in ("label", "goal_text", "topic", "mood_band"):
            value = str(payload.get(key) or "").strip()
            if value and value not in labels:
                labels.append(value)
    return labels
