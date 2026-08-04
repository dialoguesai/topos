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
    # Selector-aware disclosure (plan A2 / D-002): entity_ids a GRANTEE is authorized to
    # select by name. Populated from grant filters siblings `accessible_entity_ids` ∪
    # resolve(`accessible_entity_cohorts`) (D-002: both; v1 enums-first).
    # Semantics (A2.1 finish):
    #   entity_selector_policy_active=False → legacy / unrestricted (keys missing on grant)
    #   entity_selector_policy_active=True + empty ids → deny any named person
    #   entity_selector_policy_active=True + non-empty ids → allow-list
    # Owner tier ignores this entirely.
    accessible_entity_ids: List[str] = field(default_factory=list)
    # Cohort rule ids from the grant (audit / A2.3 / C1). Membership tokens
    # (`contacts`, `message_peers`, `calendar_attendees`) widen
    # `accessible_entity_ids` via cohort_resolvers; `stats_aggregate` / `none`
    # are aggregate-permit only. See `_cohort_aggregate_permitted`.
    accessible_entity_cohorts: List[str] = field(default_factory=list)
    entity_selector_policy_active: bool = False

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
            accessible_entity_cohorts=list(data.get("accessible_entity_cohorts") or []),
            entity_selector_policy_active=bool(data.get("entity_selector_policy_active")),
        )
