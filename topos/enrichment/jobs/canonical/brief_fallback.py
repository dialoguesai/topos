"""Deterministic dimension brief sections when LLM (Ollama) is unavailable."""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Set

from topos.features.signal.brief_ontology import llm_merge_section_ids

_GROW_FIELD_PREFIXES = (
    "project:",
    "goal:",
    "accomplished:",
    "location:",
    "with:",
    "duration:",
    "completed:",
)
_TECH_CATEGORY_TOKENS = frozenset(
    {
        "topos",
        "dialogues",
        "signal",
        "engineering",
        "code",
        "cursor",
        "api",
        "ingestion",
        "simulations",
    }
)


def _parse_metadata_json(record: Dict[str, Any]) -> Dict[str, Any]:
    meta = record.get("metadata_json")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = {}
    return meta if isinstance(meta, dict) else {}


def _looks_like_grow_journal_content(content: str) -> bool:
    lower = content.lower()
    return any(prefix in lower for prefix in _GROW_FIELD_PREFIXES)


def _parse_grow_content_fields(content: str) -> Dict[str, str]:
    fields: Dict[str, str] = {}
    current_key: Optional[str] = None
    buffer: List[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        matched = re.match(
            r"^(Project|Goal|Accomplished|Location|With|Duration|Completed)\s*:\s*(.*)$",
            stripped,
            re.IGNORECASE,
        )
        if matched:
            if current_key and buffer:
                fields[current_key] = "\n".join(buffer).strip()
            current_key = matched.group(1).lower()
            first = matched.group(2).strip()
            buffer = [first] if first else []
            continue
        if current_key:
            buffer.append(stripped)
    if current_key and buffer:
        fields[current_key] = "\n".join(buffer).strip()
    return fields


def _first_sentence(text: str, *, max_len: int = 100) -> str:
    chunk = str(text or "").strip()
    if not chunk:
        return ""
    chunk = chunk.split("\n\n")[0].split("\n")[0].strip()
    for sep in (". ", "! ", "? "):
        if sep in chunk:
            chunk = chunk.split(sep)[0].strip()
            break
    if len(chunk) > max_len:
        return chunk[: max_len - 3].rstrip() + "..."
    return chunk


def _grow_place_band(record: Dict[str, Any]) -> str:
    place = str(record.get("place_name") or "").strip()
    if place:
        return place
    content = str(record.get("content") or "")
    if _looks_like_grow_journal_content(content):
        fields = _parse_grow_content_fields(content)
        return str(fields.get("location") or "").strip()
    return ""


def _record_fallback_summary(record: Dict[str, Any]) -> str:
    """Last-resort one-line summary without re-entering brief_input_text."""
    parts: List[str] = []
    for key in (
        "category",
        "mood_tag",
        "title",
        "description",
        "display_name",
        "organization",
        "place_name",
        "city",
        "starts_at",
        "ends_at",
        "account_type",
        "amount",
    ):
        value = record.get(key)
        if value is None or value == "":
            continue
        parts.append(str(value).strip())
    return " — ".join(parts)


def brief_input_text(record: Dict[str, Any]) -> str:
    """Dense, summary-safe digest for brief LLM input and rules fallback."""
    content = str(record.get("content") or "").strip()
    category = str(record.get("category") or "").strip()
    meta = _parse_metadata_json(record)

    if _looks_like_grow_journal_content(content):
        fields = _parse_grow_content_fields(content)
        project = fields.get("project") or category
        goal = fields.get("goal") or str(meta.get("goal") or record.get("goal") or "").strip()
        accomplished = _first_sentence(fields.get("accomplished") or "")
        place = _grow_place_band(record)
        parts: List[str] = []
        if project:
            parts.append(f"[{project}]")
        if goal:
            parts.append(f"aim: {goal}")
        if accomplished:
            parts.append(f"progress: {accomplished}")
        mood = str(record.get("mood_tag") or "").strip()
        if mood:
            parts.append(f"mood: {mood}")
        if place:
            parts.append(f"@ {place[:60]}")
        if parts:
            return " ".join(parts)

    parts = []
    if category and category.lower() not in {"grow", ""}:
        parts.append(f"[{category}]")
    if content:
        excerpt = _first_sentence(content, max_len=140)
        if excerpt:
            parts.append(excerpt)
    elif record.get("title"):
        parts.append(str(record["title"]).strip()[:140])
    elif record.get("description"):
        parts.append(_first_sentence(str(record["description"])))
    place = str(record.get("place_name") or record.get("city") or "").strip()
    if place and not _looks_like_grow_journal_content(content):
        parts.append(f"@ {place[:40]}")
    if not parts:
        return _record_fallback_summary(record)
    return " ".join(parts)


def record_summary_text(record: Dict[str, Any]) -> str:
    """Build a single-line summary from heterogeneous canonical/signal records."""
    digest = brief_input_text(record)
    if digest:
        return digest
    return _record_fallback_summary(record)


def prepare_signal_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize records for enrichment jobs expecting message_id + content."""
    out = dict(record)
    record_id = (
        out.get("message_id")
        or out.get("entry_id")
        or out.get("record_id")
        or out.get("transaction_id")
        or out.get("event_id")
        or out.get("contact_id")
    )
    if record_id and not out.get("message_id"):
        out["message_id"] = str(record_id)
    text = record_summary_text(out)
    if text and not str(out.get("content") or "").strip():
        out["content"] = text
    return out


def prepare_brief_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a canonical row for dimension brief updates (dense digest, not raw paste)."""
    out = prepare_signal_record(record)
    digest = brief_input_text(out)
    if digest:
        out["brief_input"] = digest
    return out


def _dedupe_brief_lines(records: List[Dict[str, Any]], *, limit: int = 12) -> List[str]:
    seen: Set[str] = set()
    lines: List[str] = []
    for record in records:
        line = brief_input_text(record).strip()
        if not line:
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def _memory_section_lines(section_id: str, records: List[Dict[str, Any]]) -> List[str]:
    all_lines = _dedupe_brief_lines(records, limit=20)
    if not all_lines:
        return []

    if section_id == "active_preoccupations":
        return all_lines[:6]

    if section_id == "technical_threads":
        tech: List[str] = []
        for record in records:
            line = brief_input_text(record)
            blob = f"{record.get('category') or ''} {line}".lower()
            if any(token in blob for token in _TECH_CATEGORY_TOKENS):
                if line not in tech:
                    tech.append(line)
            if len(tech) >= 5:
                break
        return tech or all_lines[:3]

    if section_id == "cross_source_themes":
        counts: Dict[str, int] = {}
        for record in records:
            cat = str(record.get("category") or "").strip()
            if cat:
                counts[cat] = counts.get(cat, 0) + 1
        if counts:
            ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
            return [f"{label} ({count} entries)" for label, count in ranked[:6]]
        return all_lines[:4]

    if section_id == "entity_and_topic_graph":
        topics: List[str] = []
        for record in records:
            cat = str(record.get("category") or "").strip()
            mood = str(record.get("mood_tag") or "").strip()
            if cat:
                band = f"theme: {cat}"
                if mood:
                    band = f"{band} · mood: {mood}"
                if band not in topics:
                    topics.append(band)
            if len(topics) >= 6:
                break
        return topics or all_lines[:4]

    return all_lines[:4]


def _places_section_lines(section_id: str, records: List[Dict[str, Any]]) -> List[str]:
    places: List[str] = []
    lines: List[str] = []
    for record in records:
        place = _grow_place_band(record)
        if place and place.lower() not in {p.lower() for p in places}:
            places.append(place)
        line = brief_input_text(record).strip()
        if line:
            lines.append(line)
    if section_id == "home_region":
        if not places:
            return lines[:4]
        ranked = Counter(places).most_common(6)
        return [f"{name} ({count} visits)" for name, count in ranked[:3]]
    if section_id == "travel_patterns":
        if not places:
            return lines[:6]
        recent = places[-8:]
        return [f"{name}" for name in dict.fromkeys(recent)]
    if section_id == "place_types":
        typed: List[str] = []
        for record in records:
            cat = str(record.get("category") or record.get("event_type") or "").strip()
            place = _grow_place_band(record)
            if cat and place:
                band = f"{cat} @ {place[:40]}"
                if band not in typed:
                    typed.append(band)
            if len(typed) >= 6:
                break
        return typed or lines[:4]
    return lines[:4]


def _format_brief_bullets(lines: List[str]) -> str:
    if not lines:
        return ""
    return "\n".join(f"- {line}" for line in lines)


def rules_brief_sections(dimension: str, records: List[Dict[str, Any]]) -> Dict[str, str]:
    """Produce compressed theme bullets from canonical digests (no raw paste)."""
    keys = llm_merge_section_ids(dimension)
    if not keys:
        return {}

    dim = str(dimension or "").strip().lower()
    out: Dict[str, str] = {}

    if dim == "memory":
        for key in keys:
            body = _format_brief_bullets(_memory_section_lines(key, records))
            if body:
                out[key] = body
        return out

    if dim == "places":
        for key in keys:
            body = _format_brief_bullets(_places_section_lines(key, records))
            if body:
                out[key] = body
        return out

    lines = _dedupe_brief_lines(records, limit=8)
    if not lines:
        return {}

    label = dimension.replace("_", " ").title()
    bullets = _format_brief_bullets(lines)
    if len(keys) == 1:
        return {keys[0]: bullets}

    out[keys[0]] = _format_brief_bullets(lines[:4]) or bullets
    out[keys[-1]] = _format_brief_bullets(lines[-4:]) or bullets
    if len(keys) > 2:
        mid = _format_brief_bullets(lines[1:6]) or bullets
        for key in keys[1:-1]:
            out[key] = mid
    return out
