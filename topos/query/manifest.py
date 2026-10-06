"""
Query scope resolution manifest (PRD §8.9).

Audit extension fields (§8.8): turn_outcome, scope_id, access_mode, session_id,
game_layer_strategy, stores_touched[], filters_applied[], cache_keys[], deny_reason.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ScopeResolutionManifest:
    scope_id: str
    primary_dimensions: List[str]
    signal_objects: List[str] = field(default_factory=list)
    canonical_tables: List[str] = field(default_factory=list)
    summary_objects: List[str] = field(default_factory=list)
    inference_objects: List[str] = field(default_factory=list)
    access_mode_ceiling: str = "summary"
    default_source_id: Optional[str] = None
    default_source_ids: List[str] = field(default_factory=list)
    filter_manifest: Optional[Dict[str, Any]] = None
    must_not_retrieve: List[str] = field(default_factory=list)
    # G6: the derived-fact classes this scope declares (`standard` / `special`
    # in the registry). Two scopes exist solely to split them —- `facts:read`
    # declares `standard` and `facts_sensitive:read` declares `special`, and
    # every OTHER field of those two registry entries is identical. Without
    # this field the two compiled to the same manifest and released the same
    # facts, so the split was a description of an intention, not a boundary.
    #
    # Empty means "this scope says nothing about fact classes" and is
    # unrestricted: only the two fact scopes declare classes, and every other
    # scope carried facts before this field existed. An explicitly empty
    # ALLOWLIST is a different thing and no scope has one.
    fact_classes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScopeResolutionManifest":
        return cls(
            scope_id=str(data["scope_id"]),
            primary_dimensions=list(data.get("primary_dimensions") or []),
            signal_objects=list(data.get("signal_objects") or []),
            canonical_tables=list(data.get("canonical_tables") or data.get("raw_tables") or []),
            summary_objects=list(data.get("summary_objects") or []),
            inference_objects=list(data.get("inference_objects") or []),
            access_mode_ceiling=str(data.get("access_mode_ceiling") or data.get("default_mode_ceiling") or "summary"),
            default_source_id=data.get("default_source_id"),
            default_source_ids=list(data.get("default_source_ids") or []),
            filter_manifest=data.get("filter_manifest"),
            must_not_retrieve=list(data.get("must_not_retrieve") or []),
            fact_classes=list(data.get("fact_classes") or []),
        )
