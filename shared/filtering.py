from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Iterable, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class FilterCategory(str, Enum):
    RETRIEVAL = "retrieval"
    AGGREGATION = "aggregation"
    FIELD_LEVEL = "field_level"
    SANITIZATION = "sanitization"
    INFERABILITY_REDUCTION = "inferability_reduction"


class FilterRuntimeStatus(str, Enum):
    SUPPORTED_NOW = "supported_now"
    STAGE_8_TARGET = "stage_8_target"
    FUTURE_ONLY = "future_only"


# Relative compute / latency band for UI grouping (curated per filter; not measured SLA).
FilterComputeTier = Literal["low", "medium", "high"]
VALID_FILTER_COMPUTE_TIERS = frozenset({"low", "medium", "high"})


@dataclass(frozen=True)
class FilterParamSpec:
    name: str
    value_type: str
    required: bool = False
    min_value: Optional[float] = None
    allowed_values: Optional[List[str]] = None
    item_type: Optional[str] = None


@dataclass(frozen=True)
class FilterDefinition:
    filter_id: str
    version: int
    category: FilterCategory
    display_name: str
    description: str
    parameter_schema: List[FilterParamSpec] = field(default_factory=list)
    compatibility_rules: Dict[str, Any] = field(default_factory=dict)
    ui_metadata: Dict[str, Any] = field(default_factory=dict)
    third_party_default_allowed: bool = True
    runtime_status: FilterRuntimeStatus = FilterRuntimeStatus.FUTURE_ONLY
    handler_id: Optional[str] = None


def _catalog_entry(
    filter_id: str,
    category: FilterCategory,
    description: str,
    *,
    parameter_schema: Optional[List[FilterParamSpec]] = None,
    runtime_status: FilterRuntimeStatus = FilterRuntimeStatus.FUTURE_ONLY,
    handler_id: Optional[str] = None,
    ui_group: str = "Advanced",
    compute_tier: FilterComputeTier = "low",
) -> FilterDefinition:
    return FilterDefinition(
        filter_id=filter_id,
        version=1,
        category=category,
        display_name=filter_id.replace("_", " ").title(),
        description=description,
        parameter_schema=parameter_schema or [],
        compatibility_rules={},
        ui_metadata={"group": ui_group, "compute_tier": compute_tier},
        third_party_default_allowed=True,
        runtime_status=runtime_status,
        handler_id=handler_id,
    )


FILTER_CATALOG: Dict[str, FilterDefinition] = {
    "rolling_window_days": _catalog_entry(
        "rolling_window_days",
        FilterCategory.RETRIEVAL,
        "Restrict records to the last N days.",
        parameter_schema=[
            FilterParamSpec("days", "int", required=True, min_value=0),
            FilterParamSpec("table_id", "str", required=False),
        ],
        runtime_status=FilterRuntimeStatus.SUPPORTED_NOW,
        handler_id="retrieval.rolling_window_days",
        ui_group="Time",
    ),
    "date_range": _catalog_entry(
        "date_range",
        FilterCategory.RETRIEVAL,
        "Restrict records to an explicit start/end range.",
        parameter_schema=[
            FilterParamSpec("start", "iso_datetime", required=True),
            FilterParamSpec("end", "iso_datetime", required=True),
        ],
        runtime_status=FilterRuntimeStatus.SUPPORTED_NOW,
        handler_id="retrieval.date_range",
        ui_group="Time",
    ),
    "max_rows": _catalog_entry(
        "max_rows",
        FilterCategory.RETRIEVAL,
        "Cap the number of returned rows.",
        parameter_schema=[
            FilterParamSpec("count", "int", required=True, min_value=0),
            FilterParamSpec("table_id", "str", required=False),
        ],
        runtime_status=FilterRuntimeStatus.SUPPORTED_NOW,
        handler_id="retrieval.max_rows",
        ui_group="Detail",
    ),
    "most_recent_n": _catalog_entry(
        "most_recent_n",
        FilterCategory.RETRIEVAL,
        "Return only the N most recent records.",
        parameter_schema=[
            FilterParamSpec("count", "int", required=True, min_value=0),
            FilterParamSpec("table_id", "str", required=False),
        ],
        runtime_status=FilterRuntimeStatus.SUPPORTED_NOW,
        handler_id="retrieval.most_recent_n",
        ui_group="Detail",
    ),
    "source_filter": _catalog_entry(
        "source_filter",
        FilterCategory.RETRIEVAL,
        "Limit results to a set of source_ids.",
        parameter_schema=[FilterParamSpec("source_ids", "list", required=True, item_type="str")],
        runtime_status=FilterRuntimeStatus.SUPPORTED_NOW,
        handler_id="retrieval.source_filter",
        ui_group="Sources",
    ),
    # Enrichment-backed retrieval filters: match against derived enrichment
    # tables (topics/entities/emotions) via SQL pushdown on UMA table reads.
    # Fail closed when enrichment output is unavailable.
    "topic_filter": _catalog_entry(
        "topic_filter",
        FilterCategory.RETRIEVAL,
        "Limit results to records tagged with any of the given topics (topics enrichment).",
        parameter_schema=[FilterParamSpec("topics", "list", required=True, item_type="str")],
        runtime_status=FilterRuntimeStatus.SUPPORTED_NOW,
        handler_id="retrieval.topic_filter",
        ui_group="Signals",
    ),
    "entity_filter": _catalog_entry(
        "entity_filter",
        FilterCategory.RETRIEVAL,
        "Limit results to records mentioning any of the given entities (entities enrichment).",
        parameter_schema=[FilterParamSpec("entities", "list", required=True, item_type="str")],
        runtime_status=FilterRuntimeStatus.SUPPORTED_NOW,
        handler_id="retrieval.entity_filter",
        ui_group="Signals",
    ),
    "emotion_filter": _catalog_entry(
        "emotion_filter",
        FilterCategory.RETRIEVAL,
        "Limit results to records classified with any of the given emotions (emotion enrichment).",
        parameter_schema=[FilterParamSpec("emotions", "list", required=True, item_type="str")],
        runtime_status=FilterRuntimeStatus.SUPPORTED_NOW,
        handler_id="retrieval.emotion_filter",
        ui_group="Signals",
    ),
    "column_allowlist": _catalog_entry(
        "column_allowlist",
        FilterCategory.FIELD_LEVEL,
        "Only include the specified fields.",
        parameter_schema=[FilterParamSpec("fields", "list", required=True, item_type="str")],
        runtime_status=FilterRuntimeStatus.SUPPORTED_NOW,
        handler_id="field_level.column_allowlist",
        ui_group="Detail",
    ),
    "column_blocklist": _catalog_entry(
        "column_blocklist",
        FilterCategory.FIELD_LEVEL,
        "Exclude the specified fields.",
        parameter_schema=[FilterParamSpec("fields", "list", required=True, item_type="str")],
        runtime_status=FilterRuntimeStatus.SUPPORTED_NOW,
        handler_id="field_level.column_blocklist",
        ui_group="Detail",
    ),
    "daily_rollup": _catalog_entry(
        "daily_rollup",
        FilterCategory.AGGREGATION,
        "Aggregate records by day.",
        runtime_status=FilterRuntimeStatus.STAGE_8_TARGET,
        handler_id="aggregation.daily_rollup",
        ui_group="Summaries",
    ),
    "weekly_rollup": _catalog_entry(
        "weekly_rollup",
        FilterCategory.AGGREGATION,
        "Aggregate records by week.",
        runtime_status=FilterRuntimeStatus.STAGE_8_TARGET,
        handler_id="aggregation.weekly_rollup",
        ui_group="Summaries",
    ),
    "count_only": _catalog_entry(
        "count_only",
        FilterCategory.AGGREGATION,
        "Return only record counts.",
        runtime_status=FilterRuntimeStatus.STAGE_8_TARGET,
        handler_id="aggregation.count_only",
        ui_group="Summaries",
    ),
    "timestamp_to_date": _catalog_entry(
        "timestamp_to_date",
        FilterCategory.SANITIZATION,
        "Reduce timestamps to date precision.",
        runtime_status=FilterRuntimeStatus.SUPPORTED_NOW,
        handler_id="sanitization.timestamp_to_date",
        ui_group="Redaction",
        compute_tier="low",
    ),
    "raw_to_summary": _catalog_entry(
        "raw_to_summary",
        FilterCategory.SANITIZATION,
        "Transform raw text into a summary.",
        parameter_schema=[
            FilterParamSpec("style", "str", required=False),
            FilterParamSpec("max_length", "int", required=False, min_value=1),
        ],
        runtime_status=FilterRuntimeStatus.FUTURE_ONLY,
        handler_id="sanitization.raw_to_summary",
        ui_group="Redaction",
        compute_tier="high",
    ),
    "raw_to_sentiment": _catalog_entry(
        "raw_to_sentiment",
        FilterCategory.SANITIZATION,
        "Transform raw text into sentiment output.",
        parameter_schema=[
            FilterParamSpec("scale", "str", required=False),
            FilterParamSpec("labels", "list", required=False, item_type="str"),
        ],
        runtime_status=FilterRuntimeStatus.FUTURE_ONLY,
        handler_id="sanitization.raw_to_sentiment",
        ui_group="Redaction",
        compute_tier="medium",
    ),
    "third_party_anonymization": _catalog_entry(
        "third_party_anonymization",
        FilterCategory.SANITIZATION,
        "Redact third-party identities from content.",
        parameter_schema=[FilterParamSpec("mode", "str", required=False)],
        runtime_status=FilterRuntimeStatus.FUTURE_ONLY,
        handler_id="sanitization.third_party_anonymization",
        ui_group="Redaction",
        compute_tier="medium",
    ),
    "name_removal": _catalog_entry(
        "name_removal",
        FilterCategory.SANITIZATION,
        "Remove names from content.",
        runtime_status=FilterRuntimeStatus.FUTURE_ONLY,
        handler_id="sanitization.name_removal",
        ui_group="Redaction",
        compute_tier="medium",
    ),
    "contact_removal": _catalog_entry(
        "contact_removal",
        FilterCategory.SANITIZATION,
        "Remove contact details from content.",
        runtime_status=FilterRuntimeStatus.FUTURE_ONLY,
        handler_id="sanitization.contact_removal",
        ui_group="Redaction",
        compute_tier="medium",
    ),
    "pii_redaction": _catalog_entry(
        "pii_redaction",
        FilterCategory.SANITIZATION,
        "Redact PII (names, contact details, etc.) from content using NER and replacement.",
        runtime_status=FilterRuntimeStatus.FUTURE_ONLY,
        handler_id="sanitization.pii_redaction",
        ui_group="Redaction",
        compute_tier="medium",
    ),
    "nsfw_sanitization": _catalog_entry(
        "nsfw_sanitization",
        FilterCategory.SANITIZATION,
        "Mask or redact NSFW content in text.",
        runtime_status=FilterRuntimeStatus.FUTURE_ONLY,
        handler_id="sanitization.nsfw_sanitization",
        ui_group="Redaction",
        compute_tier="high",
    ),
    "coords_to_city": _catalog_entry(
        "coords_to_city",
        FilterCategory.INFERABILITY_REDUCTION,
        "Reduce coordinate precision to city-level.",
        runtime_status=FilterRuntimeStatus.FUTURE_ONLY,
        handler_id="inferability.coords_to_city",
        ui_group="Redaction",
        compute_tier="medium",
    ),
    "amount_to_range": _catalog_entry(
        "amount_to_range",
        FilterCategory.INFERABILITY_REDUCTION,
        "Reduce numeric amounts to bands.",
        parameter_schema=[FilterParamSpec("bands", "list", required=False, item_type="str")],
        runtime_status=FilterRuntimeStatus.FUTURE_ONLY,
        handler_id="inferability.amount_to_range",
        ui_group="Redaction",
        compute_tier="low",
    ),
    "behavior_anonymization": _catalog_entry(
        "behavior_anonymization",
        FilterCategory.INFERABILITY_REDUCTION,
        "Break direct identity-behavior linkage.",
        parameter_schema=[FilterParamSpec("mode", "str", required=False)],
        runtime_status=FilterRuntimeStatus.FUTURE_ONLY,
        handler_id="inferability.behavior_anonymization",
        ui_group="Redaction",
        compute_tier="medium",
    ),
    "precision_level": _catalog_entry(
        "precision_level",
        FilterCategory.INFERABILITY_REDUCTION,
        "Coarsen exposed precision level.",
        parameter_schema=[FilterParamSpec("level", "enum", required=True, allowed_values=["exact", "city", "region", "band"])],
        runtime_status=FilterRuntimeStatus.FUTURE_ONLY,
        handler_id="inferability.precision_level",
        ui_group="Redaction",
        compute_tier="high",
    ),
    # --- Stage 11: contact resolution & participation (see sprints_roles_scopes_stage_11) ---
    "contact_display_names": _catalog_entry(
        "contact_display_names",
        FilterCategory.FIELD_LEVEL,
        "Resolve sender display names from the contact book when contacts:resolve is granted.",
        parameter_schema=[FilterParamSpec("enabled", "bool", required=True)],
        runtime_status=FilterRuntimeStatus.SUPPORTED_NOW,
        handler_id="field_level.contact_display_names",
        ui_group="Contacts",
    ),
    "message_contact_participation": _catalog_entry(
        "message_contact_participation",
        FilterCategory.RETRIEVAL,
        "Include or exclude messages by resolved contact_id for the sender.",
        parameter_schema=[
            FilterParamSpec("mode", "enum", required=True, allowed_values=["all", "allowlist", "blocklist"]),
            FilterParamSpec("contact_ids", "list", required=True, item_type="str"),
            FilterParamSpec("match", "enum", required=True, allowed_values=["sender_only", "thread_participants"]),
        ],
        runtime_status=FilterRuntimeStatus.SUPPORTED_NOW,
        handler_id="retrieval.message_contact_participation",
        ui_group="Contacts",
    ),
    "event_contact_participation": _catalog_entry(
        "event_contact_participation",
        FilterCategory.RETRIEVAL,
        "Phase 2: include or exclude events by linked contacts (organizer/attendee).",
        parameter_schema=[
            FilterParamSpec("mode", "enum", required=True, allowed_values=["all", "allowlist", "blocklist"]),
            FilterParamSpec("contact_ids", "list", required=True, item_type="str"),
            FilterParamSpec("match", "enum", required=True, allowed_values=["organizer", "attendee", "any_linked"]),
        ],
        runtime_status=FilterRuntimeStatus.FUTURE_ONLY,
        handler_id="retrieval.event_contact_participation",
        ui_group="Contacts",
    ),
}


def list_filter_definitions() -> List[FilterDefinition]:
    return list(FILTER_CATALOG.values())


def get_filter_definition(filter_id: str) -> Optional[FilterDefinition]:
    return FILTER_CATALOG.get((filter_id or "").strip())


def _validate_iso_datetime(value: str) -> None:
    datetime.fromisoformat(value.replace("Z", "+00:00"))


def _validate_param(spec: FilterParamSpec, value: Any) -> None:
    if spec.value_type == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"Parameter {spec.name!r} must be a bool")
        return
    if spec.value_type == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(f"Parameter {spec.name!r} must be an int")
        if spec.min_value is not None and value < spec.min_value:
            raise ValueError(f"Parameter {spec.name!r} must be >= {spec.min_value}")
        return
    if spec.value_type == "str":
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Parameter {spec.name!r} must be a non-empty string")
        return
    if spec.value_type == "iso_datetime":
        if not isinstance(value, str):
            raise ValueError(f"Parameter {spec.name!r} must be an ISO datetime string")
        _validate_iso_datetime(value)
        return
    if spec.value_type == "enum":
        if value not in (spec.allowed_values or []):
            raise ValueError(f"Parameter {spec.name!r} must be one of {spec.allowed_values}")
        return
    if spec.value_type == "list":
        if not isinstance(value, list):
            raise ValueError(f"Parameter {spec.name!r} must be a list")
        if spec.item_type == "str":
            if any(not isinstance(item, str) or not item.strip() for item in value):
                raise ValueError(f"Parameter {spec.name!r} must contain only non-empty strings")
        return
    raise ValueError(f"Unsupported parameter type {spec.value_type!r} for {spec.name!r}")


def validate_filter_params(filter_id: str, params: Dict[str, Any], *, allow_future: bool = False) -> None:
    definition = get_filter_definition(filter_id)
    if not definition:
        raise ValueError(f"Unknown filter_id: {filter_id}")
    if definition.runtime_status == FilterRuntimeStatus.FUTURE_ONLY and not allow_future:
        raise ValueError(f"Filter {filter_id!r} is not supported for runtime manifests yet")
    expected = {spec.name: spec for spec in definition.parameter_schema}
    for spec in definition.parameter_schema:
        if spec.required and spec.name not in params:
            raise ValueError(f"Missing required parameter {spec.name!r} for filter {filter_id!r}")
    for key, value in params.items():
        if key not in expected:
            raise ValueError(f"Unexpected parameter {key!r} for filter {filter_id!r}")
        _validate_param(expected[key], value)


class FilterInstance(BaseModel):
    filter_id: str = Field(..., min_length=1)
    category: Optional[FilterCategory] = Field(None)
    params: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("filter_id")
    @classmethod
    def known_filter_id(cls, value: str) -> str:
        filter_id = (value or "").strip()
        if not get_filter_definition(filter_id):
            raise ValueError(f"Unknown filter_id: {filter_id}")
        return filter_id

    @model_validator(mode="after")
    def validate_against_catalog(self) -> "FilterInstance":
        definition = get_filter_definition(self.filter_id)
        if definition is None:
            raise ValueError(f"Unknown filter_id: {self.filter_id}")
        if self.category is None:
            self.category = definition.category
        elif self.category != definition.category:
            raise ValueError(
                f"Filter {self.filter_id!r} must use category {definition.category.value!r}, "
                f"got {self.category.value!r}"
            )
        validate_filter_params(self.filter_id, self.params)
        return self

    def to_storage_dict(self) -> Dict[str, Any]:
        return self.model_dump(exclude_none=True, mode="json")


class FieldTransform(BaseModel):
    """
    Field-level transform: apply a single transform (by transform_id) to a specific table/field.
    Used in permission payload as field_transforms list; registerer field_transform_defaults
    use the same transform_ids from FILTER_CATALOG.
    """

    table_id: Optional[str] = Field(None, description="Canonical table; optional if scope implies one table")
    field: str = Field(..., min_length=1, description="Column/field name to transform")
    transform_id: str = Field(..., min_length=1, description="Filter/transform ID from FILTER_CATALOG")
    params: Dict[str, Any] = Field(default_factory=dict, description="Transform parameters")

    @field_validator("transform_id")
    @classmethod
    def known_transform_id(cls, value: str) -> str:
        transform_id = (value or "").strip()
        if not get_filter_definition(transform_id):
            raise ValueError(f"Unknown transform_id: {transform_id}")
        return transform_id

    @model_validator(mode="after")
    def validate_params_against_catalog(self) -> "FieldTransform":
        definition = get_filter_definition(self.transform_id)
        if definition is not None:
            validate_filter_params(self.transform_id, self.params, allow_future=True)
        return self

    def to_storage_dict(self) -> Dict[str, Any]:
        return self.model_dump(exclude_none=True, mode="json")


def validate_field_transforms(field_transforms: List[Any]) -> None:
    """
    Validate a list of field_transforms: each item must have field and transform_id,
    and transform_id must be in FILTER_CATALOG. Raises ValueError on first invalid entry.
    """
    if not isinstance(field_transforms, list):
        raise ValueError("field_transforms must be a list")
    for i, item in enumerate(field_transforms):
        if isinstance(item, FieldTransform):
            continue
        if isinstance(item, dict):
            FieldTransform.model_validate(item)
            continue
        raise ValueError(f"field_transforms[{i}] must be a FieldTransform or dict, got {type(item).__name__}")


def field_transforms_from_storage(value: Any) -> Optional[List[FieldTransform]]:
    """Parse field_transforms from storage (list of dicts). Returns None for None or missing; empty list for []."""
    if value is None:
        return None
    if isinstance(value, list):
        if not value:
            return []
        return [FieldTransform.model_validate(item) for item in value]
    raise ValueError("field_transforms must be a list or None")


class FilterManifestProvenance(BaseModel):
    resource_defaults_applied: bool = False
    role_defaults_applied: List[str] = Field(default_factory=list)
    source_defaults_applied: List[str] = Field(default_factory=list)
    owner_overrides: List[str] = Field(default_factory=list)


class FilterManifest(BaseModel):
    manifest_version: int = Field(1, ge=1)
    filters: List[FilterInstance] = Field(default_factory=list)
    provenance: Optional[FilterManifestProvenance] = None

    def to_storage_dict(self) -> Dict[str, Any]:
        return self.model_dump(exclude_none=True, mode="json")

    def get_filter(self, filter_id: str) -> Optional[FilterInstance]:
        for item in self.filters:
            if item.filter_id == filter_id:
                return item
        return None

    def iter_filters(self, categories: Optional[Iterable[FilterCategory]] = None) -> List[FilterInstance]:
        if categories is None:
            return list(self.filters)
        allowed = set(categories)
        return [item for item in self.filters if item.category in allowed]


def filter_manifest_from_storage(value: Any) -> Optional[FilterManifest]:
    if value is None:
        return None
    if isinstance(value, FilterManifest):
        return value
    if not isinstance(value, dict):
        raise ValueError("filter_manifest must be a dict")
    return FilterManifest.model_validate(value)


def build_filter_manifest(*filters: FilterInstance, provenance: Optional[FilterManifestProvenance] = None) -> FilterManifest:
    return FilterManifest(filters=list(filters), provenance=provenance)


def _merge_param_values(filter_id: str, existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(existing)
    if filter_id == "rolling_window_days":
        current = merged.get("days")
        incoming_days = incoming.get("days")
        if current is None:
            merged["days"] = incoming_days
        elif incoming_days is not None:
            merged["days"] = min(int(current), int(incoming_days))
        return merged
    if filter_id in {"max_rows", "most_recent_n"}:
        current = merged.get("count")
        incoming_count = incoming.get("count")
        if current is None:
            merged["count"] = incoming_count
        elif incoming_count is not None:
            merged["count"] = min(int(current), int(incoming_count))
        return merged
    if filter_id == "date_range":
        start = merged.get("start")
        end = merged.get("end")
        incoming_start = incoming.get("start")
        incoming_end = incoming.get("end")
        if incoming_start is not None:
            if start is None or str(incoming_start) > str(start):
                merged["start"] = incoming_start
        if incoming_end is not None:
            if end is None or str(incoming_end) < str(end):
                merged["end"] = incoming_end
        return merged
    if filter_id == "source_filter":
        current = set(str(item) for item in merged.get("source_ids", []))
        incoming_values = set(str(item) for item in incoming.get("source_ids", []))
        if current and incoming_values:
            merged["source_ids"] = sorted(current & incoming_values)
        elif incoming_values:
            merged["source_ids"] = sorted(incoming_values)
        return merged
    if filter_id == "column_allowlist":
        current = set(str(item) for item in merged.get("fields", []))
        incoming_values = set(str(item) for item in incoming.get("fields", []))
        if current and incoming_values:
            merged["fields"] = sorted(current & incoming_values)
        elif incoming_values:
            merged["fields"] = sorted(incoming_values)
        return merged
    if filter_id == "column_blocklist":
        current = set(str(item) for item in merged.get("fields", []))
        incoming_values = set(str(item) for item in incoming.get("fields", []))
        merged["fields"] = sorted(current | incoming_values)
        return merged
    if filter_id == "contact_display_names":
        # Stricter: both must allow names.
        merged["enabled"] = bool(merged.get("enabled", True)) and bool(incoming.get("enabled", True))
        return merged
    if filter_id == "message_contact_participation":
        em = str(merged.get("mode") or "all")
        im = str(incoming.get("mode") or "all")
        ec = {str(x) for x in (merged.get("contact_ids") or [])}
        ic = {str(x) for x in (incoming.get("contact_ids") or [])}
        match = str(incoming.get("match") or merged.get("match") or "sender_only")
        if "blocklist" in (em, im):
            return {"mode": "blocklist", "contact_ids": sorted(ec | ic), "match": match}
        if em == "allowlist" and im == "allowlist":
            inter = ec & ic if ec and ic else ec | ic
            return {"mode": "allowlist", "contact_ids": sorted(inter), "match": match}
        if im != "all":
            return {"mode": im, "contact_ids": sorted(ic), "match": match}
        if em != "all":
            return {"mode": em, "contact_ids": sorted(ec), "match": match}
        return {"mode": "all", "contact_ids": [], "match": match}
    return incoming if incoming else merged


def _manifest_merge_key(item: FilterInstance) -> str:
    """Merge key: retrieval caps may repeat per logical table via params.table_id."""
    fid = item.filter_id
    if fid in {"rolling_window_days", "max_rows", "most_recent_n"}:
        tid = str(item.params.get("table_id") or "").strip()
        return f"{fid}\x00{tid}"
    return fid


def merge_filter_manifests(
    manifests: Iterable[Optional[FilterManifest]],
    *,
    provenance: Optional[FilterManifestProvenance] = None,
) -> FilterManifest:
    merged_instances: Dict[str, FilterInstance] = {}
    for manifest in manifests:
        if manifest is None:
            continue
        for item in manifest.filters:
            key = _manifest_merge_key(item)
            existing = merged_instances.get(key)
            if existing is None:
                merged_instances[key] = item
                continue
            merged_instances[key] = FilterInstance(
                filter_id=item.filter_id,
                category=item.category,
                params=_merge_param_values(item.filter_id, existing.params, item.params),
            )
    return FilterManifest(
        filters=sorted(merged_instances.values(), key=lambda item: (_manifest_merge_key(item), item.filter_id)),
        provenance=provenance,
    )
