"""Bundled parser/schema → canonical mapper + group inference."""

from __future__ import annotations

from dataclasses import fields
from typing import Any, Dict, Optional, Tuple

# schema_id (or parser_id) → (canonical_mapper_id, canonical_group_id)
BUNDLED_CANONICAL_TRIPLES: Dict[str, Tuple[str, str]] = {
    "journal.time_log.v1": ("journal_time_log", "journal"),
    "chatgpt.conversation.v2": ("chatgpt", "ai_messages"),
    "chatgpt.conversation.v1": ("chatgpt", "ai_messages"),
    "grok.conversation.v1": ("grok", "ai_messages"),
    "browser.visits.v1": ("browser_activity", "activity"),
    "managed.file.browser_history_dem.v1": ("browser_activity", "activity"),
    "browser.events.v1": ("browser_activity", "activity"),
    "github.activity.v1": ("github_activity", "activity"),
    "imessage.messages.v1": ("imessage", "conversations"),
    "signal.messages.v1": ("signal", "conversations"),
    "voxterm.transcript.v1": ("voxterm_transcript", "conversations"),
    "demo.messenger.v1": ("demo_messenger", "conversations"),
    "demo.calendar.v1": ("demo_calendar", "schedule"),
    "demo.journal.v1": ("demo_journal", "journal"),
    "demo.profile.v1": ("demo_profile", "profile"),
    "demo.financial.v1": ("demo_financial", "financial"),
    "demo.browser.v1": ("browser_activity", "activity"),
    "demo.places.v1": ("demo_places", "places"),
    "demo.contacts.v1": ("demo_contacts", "contacts"),
    "grow.time_log.v1": ("journal_time_log", "journal"),
}

VALID_CANONICAL_GROUP_IDS = frozenset(
    {
        "ai_messages",
        "conversations",
        "activity",
        "schedule",
        "journal",
        "profile",
        "financial",
        "places",
        "contacts",
    }
)


def infer_bundled_canonical_triple(
    *,
    schema_id: Optional[str] = None,
    parser_id: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    for key in (str(schema_id or "").strip(), str(parser_id or "").strip()):
        if key and key in BUNDLED_CANONICAL_TRIPLES:
            return BUNDLED_CANONICAL_TRIPLES[key]
    return None


def maps_to_canonical_table(source_def: Any) -> bool:
    """True when the source maps to a Topos canonical table (group + mapper resolved)."""
    if source_def is None:
        return False
    group_id = str(getattr(source_def, "canonical_group_id", "") or "").strip()
    if not group_id:
        return False
    mapper_id = str(getattr(source_def, "canonical_mapper_id", "") or "").strip()
    if mapper_id:
        return True
    inferred = infer_bundled_canonical_triple(
        schema_id=str(getattr(source_def, "schema_id", "") or ""),
        parser_id=str(getattr(source_def, "parser_id", "") or ""),
    )
    return inferred is not None


def normalize_canonical_source_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fill bundled canonical_mapper_id from schema/parser; drop deprecated connected flag.

    Authors declare canonical_group_id (and schema/parser). Mapper is inferred for
    bundled triples; custom sources must still supply canonical_mapper_id explicitly.
    """
    merged = dict(payload)
    merged.pop("canonical_mapping_connected", None)

    schema_id = str(merged.get("schema_id") or "").strip()
    parser_id = str(merged.get("parser_id") or "").strip()
    group_id = str(merged.get("canonical_group_id") or "").strip()
    mapper_id = str(merged.get("canonical_mapper_id") or "").strip()

    inferred = infer_bundled_canonical_triple(schema_id=schema_id, parser_id=parser_id)
    if inferred:
        inferred_mapper, inferred_group = inferred
        if group_id and group_id != inferred_group:
            raise ValueError(
                f"canonical_group_id {group_id!r} does not match bundled lane "
                f"{inferred_group!r} for schema {schema_id or parser_id!r}"
            )
        if not group_id:
            merged["canonical_group_id"] = inferred_group
        if not mapper_id:
            merged["canonical_mapper_id"] = inferred_mapper
    elif group_id and not mapper_id:
        # Lane declared without bundled parser — custom mapper required at install.
        pass

    return merged


def apply_bundled_canonical_defaults(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize payload and keep only DataSourceDefinition fields."""
    from .definitions import DataSourceDefinition

    normalized = normalize_canonical_source_payload(payload)
    allowed = {field.name for field in fields(DataSourceDefinition)}
    return {key: value for key, value in normalized.items() if key in allowed}


def requires_canonical_contract(payload: Dict[str, Any]) -> bool:
    group_id = str(payload.get("canonical_group_id") or "").strip()
    mapper_id = str(payload.get("canonical_mapper_id") or "").strip()
    if group_id or mapper_id:
        return True
    inferred = infer_bundled_canonical_triple(
        schema_id=str(payload.get("schema_id") or ""),
        parser_id=str(payload.get("parser_id") or ""),
    )
    return inferred is not None
