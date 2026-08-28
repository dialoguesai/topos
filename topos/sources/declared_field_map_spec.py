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


def _rule_scope(rule: Any) -> str:
    """A bare path string is record-scoped, matching apply_field_map's default."""
    if isinstance(rule, dict):
        return str(rule.get("scope") or "record").strip() or "record"
    return "record"


def _validate_fan_out_identity(table: str, block: Dict[str, Any]) -> None:
    """A fan-out must say which item each child IS and which record it CAME FROM.

    Two failures this closes, both found on the live node before any third party
    had declared a fan_out — the built-in Python fan-outs made both mistakes
    first, which is the argument for enforcing it here rather than trusting the
    registerer.

    **No parent link.** ``_mint`` writes only the declared columns, and
    ``metadata_json`` — the channel the built-in location fan-out uses for its
    parent pointer — is reserved. So a declared fan-out produced children with no
    link of any kind, strictly worse than the built-in one. On the owner's node
    the 362 location children ARE linkable only because Python code sets
    ``source_record_id``; the 121 children of a since-retired GitHub fan-out are
    not, because it overwrote that column with a synthetic composite, and 0 of
    121 join back to anything.

    ``source_record_id`` must therefore be declared and must be RECORD-scoped: it
    names the source record all the children came from, so reading it per-item
    reproduces exactly the GitHub mistake.

    **A collapsing identity.** The id column must be ITEM-scoped. ``_mint``
    checks only that the id resolved to a non-empty string, so a record-scoped id
    template gives every item the SAME id: the upserts overwrite each other,
    N-1 records are silently discarded, and N copies of one id go through
    enrichment while the stored content is only the last item's.
    """
    fields = block.get("fields") or {}
    id_column = ID_COLUMNS.get(table)

    if "source_record_id" not in fields:
        raise ValueError(
            f"canonical_field_map[{table!r}]: a fan_out must declare 'source_record_id' "
            "so each child can be traced back to the source record it was split from "
            "(metadata_json is reserved, so there is no other channel)"
        )
    if _rule_scope(fields["source_record_id"]) != "record":
        raise ValueError(
            f"canonical_field_map[{table!r}].source_record_id must be record-scoped "
            "inside a fan_out: it names the record the children came FROM, not the item"
        )
    if id_column and id_column in fields and _rule_scope(fields[id_column]) != "item":
        raise ValueError(
            f"canonical_field_map[{table!r}].{id_column} must be item-scoped inside a "
            "fan_out, or every item mints the same id and all but the last are discarded"
        )

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
        if str(block.get("fan_out") or "").strip():
            _validate_fan_out_identity(table, block)
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
