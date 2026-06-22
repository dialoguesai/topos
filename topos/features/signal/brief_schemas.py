"""Section templates and markdown rendering for dimension living briefs."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .brief_ontology import (
    empty_sections,
    empty_structured,
    llm_merge_section_ids,
    section_defs_for_dimension,
    title_to_section_id,
)
from .dimension_definition_loader import extraction_hints_for_dimension
from .dimension_registry import SIGNAL_DIMENSIONS

LEGACY_SECTION_KEYS = ("at_a_glance", "stable", "recent_window", "owner_notes")

SECTION_TITLES = {
    "at_a_glance": "At a glance",
    "stable": "Stable picture",
    "recent_window": "Recent window",
    "owner_notes": "Owner notes",
}


def empty_structured_for_dimension(dimension: str) -> Dict[str, Any]:
    return empty_structured(dimension)


def render_markdown(dimension: str, structured: Dict[str, Any], *, updated_at: Optional[str] = None) -> str:
    dim = str(dimension or "").strip().lower()
    label = next((d["label"] for d in SIGNAL_DIMENSIONS if d["id"] == dim), dim.title())
    sections = structured.get("sections") if isinstance(structured.get("sections"), dict) else empty_sections(dim)
    ts = updated_at or datetime.now(timezone.utc).isoformat()
    lines = [
        f"# {label} brief",
        f"> Updated: {ts}",
        "",
    ]
    for block in section_defs_for_dimension(dim):
        sid = str(block["id"])
        title = str(block["title"])
        body_block = sections.get(sid) if isinstance(sections.get(sid), dict) else {}
        body = str(body_block.get("markdown") or "").strip()
        lines.append(f"## {title}")
        lines.append(body or "_No content yet._")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def parse_markdown_sections(markdown_body: str, dimension: str) -> Dict[str, Any]:
    """Parse ## section headers back into structured sections for this dimension's ontology."""
    dim = str(dimension or "").strip().lower()
    structured = empty_structured(dim)
    sections = structured["sections"]
    current_key: Optional[str] = None
    buffer: List[str] = []

    header_re = re.compile(r"^##\s+(.+?)\s*$")

    for line in (markdown_body or "").splitlines():
        match = header_re.match(line.strip())
        if match:
            if current_key is not None:
                sections[current_key]["markdown"] = "\n".join(buffer).strip()
            title = match.group(1).strip()
            current_key = title_to_section_id(dim, title)
            buffer = []
            continue
        if current_key is not None:
            buffer.append(line)

    if current_key is not None:
        sections[current_key]["markdown"] = "\n".join(buffer).strip()

    return structured


def llm_prompt_for_dimension(dimension: str) -> str:
    dim = str(dimension or "").strip().lower()
    label = next((d["label"] for d in SIGNAL_DIMENSIONS if d["id"] == dim), dim)
    guidance = extraction_hints_for_dimension(dim) or ""
    keys = llm_merge_section_ids(dim)
    key_hints = []
    for block in section_defs_for_dimension(dim):
        if not block.get("llm_merge", True):
            continue
        key_hints.append(f"- {block['id']}: {block.get('description') or block.get('title')}")
    keys_json = ", ".join(f'"{k}": "..."' for k in keys)
    base = (
        f"Update the {label} dimension brief for a personal knowledge node. "
        "Compress noisy source records into high-signal theme summaries only. "
        "Do not copy field labels (Project, Goal, Location, Duration), raw log formatting, "
        "or full sentences from sources. Synthesize patterns, priorities, and momentum bands. "
        "Write honest, summary-safe prose: use theme bands and coarse patterns, "
        "not raw message quotes, exact dates, dollar amounts, or precise locations. "
    )
    if guidance:
        base += f"{guidance} "
    return (
        base
        + "Reply JSON only with these section keys (each a markdown string, 1-4 short paragraphs or bullets):\n"
        + "\n".join(key_hints)
        + f"\n\nJSON shape: {{{keys_json}}}"
    )


def llm_json_template(dimension: str) -> str:
    keys = llm_merge_section_ids(dimension)
    inner = ", ".join(f'"{k}": "..."' for k in keys)
    return "{" + inner + "}"
