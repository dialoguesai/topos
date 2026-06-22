"""Per-dimension brief section ontologies."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

_ONTOLOGIES_PATH = Path(__file__).resolve().parent / "definitions" / "brief_ontologies.json"

LEGACY_SECTION_KEYS = ("at_a_glance", "stable", "recent_window", "owner_notes")


class BriefOntologyError(ValueError):
    pass


@lru_cache(maxsize=1)
def _catalog() -> Dict[str, Any]:
    if not _ONTOLOGIES_PATH.is_file():
        raise BriefOntologyError(f"Missing brief ontologies: {_ONTOLOGIES_PATH}")
    data = json.loads(_ONTOLOGIES_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "dimensions" not in data:
        raise BriefOntologyError("Invalid brief_ontologies.json shape")
    return data


def ontology_version() -> str:
    return str(_catalog().get("version") or "1.0.0")


def section_defs_for_dimension(dimension: str) -> List[Dict[str, Any]]:
    dim = str(dimension or "").strip().lower()
    dims = _catalog().get("dimensions") or {}
    block = dims.get(dim)
    if not isinstance(block, dict):
        raise BriefOntologyError(f"No brief ontology for dimension {dimension!r}")
    sections = block.get("sections")
    if not isinstance(sections, list) or not sections:
        raise BriefOntologyError(f"Empty brief ontology for dimension {dimension!r}")
    return list(sections)


def section_ids_for_dimension(dimension: str) -> List[str]:
    return [str(s["id"]) for s in section_defs_for_dimension(dimension)]


def llm_merge_section_ids(dimension: str) -> List[str]:
    return [str(s["id"]) for s in section_defs_for_dimension(dimension) if s.get("llm_merge", True)]


def section_def(dimension: str, section_id: str) -> Optional[Dict[str, Any]]:
    for item in section_defs_for_dimension(dimension):
        if str(item.get("id")) == section_id:
            return item
    return None


def default_disclosure(section_def_block: Dict[str, Any]) -> str:
    return str(section_def_block.get("disclosure") or "summary_safe")


def empty_sections(dimension: str) -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for block in section_defs_for_dimension(dimension):
        sid = str(block["id"])
        out[sid] = {"markdown": "", "disclosure": default_disclosure(block)}
    return out


def empty_structured(dimension: str) -> Dict[str, Any]:
    dim = str(dimension or "").strip().lower()
    return {
        "dimension": dim,
        "ontology_version": ontology_version(),
        "sections": empty_sections(dim),
    }


def title_to_section_id(dimension: str, title: str) -> Optional[str]:
    normalized = str(title or "").strip().lower().replace("'", "'")
    for block in section_defs_for_dimension(dimension):
        if str(block.get("title", "")).strip().lower() == normalized:
            return str(block["id"])
    legacy_map = {
        "at a glance": None,
        "stable picture": None,
        "recent window": None,
        "owner notes": "grant_policy",
    }
    if normalized == "owner notes" and section_def(dimension, "grant_policy"):
        return "grant_policy"
    return None


def primary_section_id(dimension: str) -> str:
    for block in section_defs_for_dimension(dimension):
        if block.get("emphasis") == "primary":
            return str(block["id"])
    merge = llm_merge_section_ids(dimension)
    return merge[0] if merge else "grant_policy"


def recent_section_id(dimension: str) -> str:
    merge = llm_merge_section_ids(dimension)
    for block in reversed(section_defs_for_dimension(dimension)):
        sid = str(block["id"])
        if sid in merge and sid != primary_section_id(dimension):
            if "recent" in sid or "momentum" in sid or "cadence" in sid:
                return sid
    return merge[-1] if len(merge) > 1 else primary_section_id(dimension)
