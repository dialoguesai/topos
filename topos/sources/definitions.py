from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional

from shared.filtering import filter_manifest_from_storage, get_filter_definition


DELIVERY_OWNER_UPLOAD = "owner_upload"
DELIVERY_OWNER_UI = "owner_ui"
DELIVERY_LOCAL_SYNC = "local_sync"
DELIVERY_CLIENT_PUSH = "client_push"
DELIVERY_VALUES = frozenset(
    {DELIVERY_OWNER_UPLOAD, DELIVERY_OWNER_UI, DELIVERY_LOCAL_SYNC, DELIVERY_CLIENT_PUSH}
)

# Source posture (PLAN_PROVENANCE_SPLIT §3.1, axis 1): source-level default for
# whose words the content is. Per-row record role refines it downstream
# (features.provenance.roles.record_role).
POSTURE_PERSONAL = "personal"  # owner-authored by construction (journal, resume)
POSTURE_MIXED = "mixed"  # role decided per-row (chats: owner + others + assistant)
POSTURE_AMBIENT = "ambient"  # exposure, not expression (browser visits, feeds)
POSTURE_VALUES = frozenset({POSTURE_PERSONAL, POSTURE_MIXED, POSTURE_AMBIENT})

# ui_stream splits by who carries the bytes (CONNECTOR_SPEC.md §3). Every bundled
# ui_stream source today is pushed by external software; owner_ui is reserved for
# future in-UI entry (journal, home chat) and gets sources listed here explicitly.
_OWNER_UI_SOURCE_IDS: frozenset[str] = frozenset()


def derive_delivery(source_type: str, source_id: str = "") -> Optional[str]:
    """Delivery for definitions that predate the field. stub/unknown stay None."""
    st = str(source_type or "").strip()
    if st == "file":
        return DELIVERY_OWNER_UPLOAD
    if st == "local_sync":
        return DELIVERY_LOCAL_SYNC
    if st == "ui_stream":
        if str(source_id or "").strip() in _OWNER_UI_SOURCE_IDS:
            return DELIVERY_OWNER_UI
        return DELIVERY_CLIENT_PUSH
    return None


def source_type_for_delivery(delivery: Optional[str]) -> Optional[str]:
    """Back-derive legacy source_type so pre-delivery readers keep working."""
    d = str(delivery or "").strip()
    if d == DELIVERY_OWNER_UPLOAD:
        return "file"
    if d in (DELIVERY_OWNER_UI, DELIVERY_CLIENT_PUSH):
        return "ui_stream"
    if d == DELIVERY_LOCAL_SYNC:
        return "local_sync"
    return None


def is_file_delivery(defn: "DataSourceDefinition") -> bool:
    """Owner uploads the bytes themselves (legacy source_type == 'file')."""
    return defn.delivery == DELIVERY_OWNER_UPLOAD


def accepts_app_ingest(defn: "DataSourceDefinition") -> bool:
    """Streamed records may target this source (legacy source_type == 'ui_stream')."""
    return defn.delivery in (DELIVERY_CLIENT_PUSH, DELIVERY_OWNER_UI)


def definition_from_payload(payload: Dict[str, Any]) -> "DataSourceDefinition":
    """Build a definition from serialized JSON, ignoring unknown keys so newer
    payloads never break older readers again. Fills source_type from delivery
    (and vice versa) when only one is present."""
    from dataclasses import fields as dc_fields

    data = dict(payload)
    if not str(data.get("source_type") or "").strip():
        back_derived = source_type_for_delivery(data.get("delivery"))
        if back_derived:
            data["source_type"] = back_derived
    known = {f.name for f in dc_fields(DataSourceDefinition)}
    return DataSourceDefinition(**{k: v for k, v in data.items() if k in known})


def _validate_field_transform_defaults(value: Optional[List[Dict[str, Any]]]) -> None:
    """Ensure each transform_id in field_transform_defaults is in FILTER_CATALOG."""
    if value is None or not value:
        return
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ValueError(f"field_transform_defaults[{i}] must be a dict")
        table_id = entry.get("table_id")
        field_name = entry.get("field")
        transform_ids = entry.get("transform_ids")
        if not isinstance(field_name, str) or not field_name.strip():
            raise ValueError(f"field_transform_defaults[{i}] must have non-empty 'field'")
        if not isinstance(transform_ids, list):
            raise ValueError(f"field_transform_defaults[{i}] must have 'transform_ids' list")
        for tid in transform_ids:
            if not isinstance(tid, str) or not tid.strip():
                raise ValueError(f"field_transform_defaults[{i}].transform_ids must contain non-empty strings")
            if get_filter_definition(tid.strip()) is None:
                raise ValueError(f"field_transform_defaults[{i}]: unknown transform_id {tid!r}")


@dataclass(frozen=True)
class DataSourceDefinition:
    source_id: str
    display_name: str
    source_type: str
    schema_id: str
    parser_id: str
    # How data arrives (CONNECTOR_SPEC.md §3); derived from source_type when unset.
    delivery: Optional[str] = None
    # Whose words the content is by default (PLAN_PROVENANCE_SPLIT §3.1):
    # personal | mixed | ambient. 'mixed' is the safe default — role is then
    # decided per-row. Registry sources set this explicitly.
    posture: str = POSTURE_MIXED
    canonical_mapper_id: Optional[str] = None
    canonical_group_id: Optional[str] = None
    raw_enrichment_jobs: List[str] = field(default_factory=list)
    canonical_enrichment_jobs: List[str] = field(default_factory=list)
    # Optional lane-specific extras; canonical-mapped sources always receive baseline
    # signal jobs (embeddings, briefs, topic clusters, graph) at runtime.
    signal_derivation_jobs: List[str] = field(default_factory=list)
    analytics_profile_id: Optional[str] = None
    enrichment_trigger: Literal["automatic", "manual"] = "automatic"
    ingestion_trigger: Literal["automatic", "manual"] = "automatic"  # When to start processing after upload
    # MVP roles/scopes (Sprint 02 Stage 1): scope this source's canonical output maps to
    default_scope_id: str = "unknown"
    allowed_scope_ids: Optional[List[str]] = None
    # Signal dimensions whose briefs/summaries should update when this source ingests.
    # When unset, falls back to canonical_group_id lane defaults.
    brief_update_dimensions: Optional[List[str]] = None
    default_filter_hints: Optional[List[str]] = None
    filter_tier_kind: Optional[Literal["sensitivity", "inferability"]] = None
    default_filter_tiers: Optional[Dict[str, Dict[str, object]]] = None
    # Stage 10: registerer-defined field-level default transforms (which fields need which filters)
    field_transform_defaults: Optional[List[Dict[str, Any]]] = None
    # Managed source install metadata used by runtime ingestion for file sources.
    tables: Optional[List[Dict[str, Any]]] = None
    file_ingest_shape: Optional[Dict[str, Any]] = None
    parser_column_map: Optional[Dict[str, str]] = None
    canonical_mapping_connected: Optional[bool] = None
    pipeline_include_parser: Optional[bool] = None
    pipeline_include_data_table: Optional[bool] = None
    pipeline_data_table_after_parser: Optional[bool] = None
    pipeline_data_table_match_parser_output: Optional[bool] = None

    def __post_init__(self) -> None:
        if self.delivery is not None and self.delivery not in DELIVERY_VALUES:
            raise ValueError(
                f"delivery must be one of {sorted(DELIVERY_VALUES)}, got {self.delivery!r}"
            )
        if self.delivery is None:
            object.__setattr__(self, "delivery", derive_delivery(self.source_type, self.source_id))
        if self.posture not in POSTURE_VALUES:
            raise ValueError(
                f"posture must be one of {sorted(POSTURE_VALUES)}, got {self.posture!r}"
            )
        if self.filter_tier_kind is not None and self.filter_tier_kind not in {"sensitivity", "inferability"}:
            raise ValueError("filter_tier_kind must be 'sensitivity' or 'inferability'")
        if self.default_filter_tiers is not None:
            for tier_name, manifest in self.default_filter_tiers.items():
                if tier_name not in {"low", "medium", "high"}:
                    raise ValueError(f"Unsupported default filter tier {tier_name!r}")
                filter_manifest_from_storage(manifest)
        _validate_field_transform_defaults(self.field_transform_defaults)

    def to_dict(self) -> dict:
        out = {
            "source_id": self.source_id,
            "display_name": self.display_name,
            # Both source_type and delivery are always emitted so serialized
            # definitions stay readable across the delivery rollout (spec §3).
            "source_type": self.source_type,
            "delivery": self.delivery,
            # Always emitted: readers that predate posture ignore unknown keys
            # (definition_from_payload filters to known dataclass fields).
            "posture": self.posture,
            "schema_id": self.schema_id,
            "parser_id": self.parser_id,
            "canonical_mapper_id": self.canonical_mapper_id,
            "canonical_group_id": self.canonical_group_id,
            "raw_enrichment_jobs": list(self.raw_enrichment_jobs),
            "canonical_enrichment_jobs": list(self.canonical_enrichment_jobs),
            "signal_derivation_jobs": list(self.signal_derivation_jobs),
            "analytics_profile_id": self.analytics_profile_id,
            "enrichment_trigger": self.enrichment_trigger,
            "ingestion_trigger": self.ingestion_trigger,
            "default_scope_id": self.default_scope_id,
        }
        if self.allowed_scope_ids is not None:
            out["allowed_scope_ids"] = list(self.allowed_scope_ids)
        if self.brief_update_dimensions is not None:
            out["brief_update_dimensions"] = list(self.brief_update_dimensions)
        if self.default_filter_hints is not None:
            out["default_filter_hints"] = list(self.default_filter_hints)
        if self.filter_tier_kind is not None:
            out["filter_tier_kind"] = self.filter_tier_kind
        if self.default_filter_tiers is not None:
            out["default_filter_tiers"] = dict(self.default_filter_tiers)
        if self.field_transform_defaults is not None:
            out["field_transform_defaults"] = list(self.field_transform_defaults)
        if self.tables is not None:
            out["tables"] = list(self.tables)
        if self.file_ingest_shape is not None:
            out["file_ingest_shape"] = dict(self.file_ingest_shape)
        if self.parser_column_map is not None:
            out["parser_column_map"] = dict(self.parser_column_map)
        if self.canonical_mapping_connected is not None:
            out["canonical_mapping_connected"] = bool(self.canonical_mapping_connected)
        if self.pipeline_include_parser is not None:
            out["pipeline_include_parser"] = bool(self.pipeline_include_parser)
        if self.pipeline_include_data_table is not None:
            out["pipeline_include_data_table"] = bool(self.pipeline_include_data_table)
        if self.pipeline_data_table_after_parser is not None:
            out["pipeline_data_table_after_parser"] = bool(self.pipeline_data_table_after_parser)
        if self.pipeline_data_table_match_parser_output is not None:
            out["pipeline_data_table_match_parser_output"] = bool(self.pipeline_data_table_match_parser_output)
        return out
