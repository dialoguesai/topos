"""Schema for a source's declared canonical field map (§5a capabilities 2–3).

The VOCABULARY and its validation live here, next to the source-definition
schema, because both the engine and the control-plane bundled mirror
(CONNECTOR_SPEC.md §4, regenerate_bundled_mirror.sh) must be able to check a
declaration without importing the engine's canonicalization stack. The runtime
that executes a declaration — path resolution, transforms, mappers — lives in
``topos/canonicalization/declared_field_map.py`` and imports these names so the
two can never drift.

Pure stdlib on purpose: no engine imports, no I/O.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# Identity column per canonical table (mirrors SQLiteCanonicalStore._dispatch_upsert).
# Doubles as the set of tables a declaration may target.
ID_COLUMNS: Dict[str, str] = {
    "activity_events": "event_id",
    "ai_chat_messages": "message_id",
    "calendar_events": "event_id",
    "conversation_messages": "message_id",
    "documents": "doc_id",
    "financial_transactions": "transaction_id",
    "journal_entries": "entry_id",
    "location_events": "event_id",
    "profile_records": "record_id",
}

# Columns a declaration may never write: JSON/provenance columns whose contract
# belongs to the pipeline, not to the registerer.
RESERVED_COLUMNS = frozenset({"metadata_json", "source_id", "sync_batch_id", "ingested_at"})

# Named transforms a rule may apply. Implementations: declared_field_map.TRANSFORMS.
TRANSFORM_IDS = frozenset(
    {
        "lower",
        "upper",
        "strip",
        "first_line",
        "org_prefix",
        "basename",
        "strip_event_suffix",
        "hostname",
    }
)

BLOCK_KEYS = frozenset({"fan_out", "where", "fields"})
RULE_KEYS = frozenset(
    {"path", "first_of", "template", "const", "map", "default", "transform", "join", "when", "scope"}
)


def table_block(declaration: Any) -> Dict[str, Any]:
    """Both declaration forms → the block form. A flat ``{column: rule}`` map is
    the no-fan-out case; ``{fan_out, where, fields}`` is the per-item case."""
    if not isinstance(declaration, dict):
        return {"fields": {}}
    if any(key in declaration for key in BLOCK_KEYS):
        fields = declaration.get("fields")
        return {
            "fan_out": declaration.get("fan_out"),
            "where": declaration.get("where"),
            "fields": dict(fields) if isinstance(fields, dict) else {},
        }
    return {"fields": dict(declaration)}


def validate_canonical_field_map(value: Optional[Dict[str, Any]]) -> None:
    """Fail loudly at definition time — a typo'd path silently ingesting nothing
    is exactly the failure mode this capability exists to remove."""
    if value is None:
        return
    if not isinstance(value, dict) or not value:
        raise ValueError("canonical_field_map must be a non-empty object keyed by canonical table")
    for table, declaration in value.items():
        if not isinstance(table, str) or not table.strip():
            raise ValueError("canonical_field_map keys must be canonical table names")
        if table not in ID_COLUMNS:
            raise ValueError(
                f"canonical_field_map[{table!r}]: unknown canonical table; one of "
                + ", ".join(sorted(ID_COLUMNS))
            )
        block = table_block(declaration)
        if not isinstance(declaration, dict) or not block["fields"]:
            raise ValueError(f"canonical_field_map[{table!r}] must declare at least one field")
        if block.get("fan_out") is not None and not str(block.get("fan_out") or "").strip():
            raise ValueError(f"canonical_field_map[{table!r}].fan_out must be a non-empty path")
        for column, rule in block["fields"].items():
            if column in RESERVED_COLUMNS:
                raise ValueError(
                    f"canonical_field_map[{table!r}].{column} is reserved and cannot be declared"
                )
            if isinstance(rule, str):
                if not rule.strip():
                    raise ValueError(f"canonical_field_map[{table!r}].{column} path must be non-empty")
                continue
            if not isinstance(rule, dict) or not rule:
                raise ValueError(
                    f"canonical_field_map[{table!r}].{column} must be a path string or a rule object"
                )
            unknown = sorted(set(rule) - RULE_KEYS)
            if unknown:
                raise ValueError(
                    f"canonical_field_map[{table!r}].{column}: unknown rule keys {unknown}"
                )
            transform = str(rule.get("transform") or "").strip()
            if transform and transform not in TRANSFORM_IDS:
                raise ValueError(
                    f"canonical_field_map[{table!r}].{column}: unknown transform {transform!r}; "
                    "one of " + ", ".join(sorted(TRANSFORM_IDS))
                )
