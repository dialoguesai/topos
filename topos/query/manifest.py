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
    # Selector-aware disclosure (plan A2): entity_ids a GRANTEE is authorized to select by
    # name. Empty = default-deny (a grantee may not select any named third party — the safe
    # floor until the grant layer populates this). The owner tier ignores it entirely.
    # A2.1 (grant-schema) will populate this per grant; today it stays empty ⇒ grantees who
    # name a real person are answered as if that person is absent (indistinguishability).
    accessible_entity_ids: List[str] = field(default_factory=list)

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
            accessible_entity_ids=list(data.get("accessible_entity_ids") or []),
        )
