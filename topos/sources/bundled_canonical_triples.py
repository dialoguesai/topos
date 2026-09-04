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
    "notion.page.v1": ("documents", "documents"),
    "gdrive.file.v1": ("documents", "documents"),
    "gcal.events.v1": ("google_calendar", "schedule"),
    "transcript.session.v1": ("transcript", "transcripts"),
}

# Known drift variants of bundled schema ids, seen in the wild: a 2026-07
# operator install template invented "gdrive.files.v1" (the bundled id is
# "gdrive.file.v1"). Deliberately NOT keys of BUNDLED_CANONICAL_TRIPLES: an
# exact key makes normalize_canonical_source_payload raise and rehydrate
# demote, and the live drive_files install carries this schema with no bundled
# replacement to fall back to — enforcement would orphan a working connector.
# Callers use bundled_schema_drift() and decide per source_id.
BUNDLED_SCHEMA_ALIASES: Dict[str, str] = {
    "gdrive.files.v1": "gdrive.file.v1",
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
        "documents",
        "transcripts",
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


def bundled_lane_conflict(payload: Dict[str, Any]) -> Optional[str]:
    """Reason string when a payload's lane contradicts its bundled triple, else None.

    A conflict is permanent: the bundled triple is authoritative, so no retry can
    make the payload installable. Callers that replay persisted payloads use this
    to demote the record instead of failing on every pass.
    """
    schema_id = str(payload.get("schema_id") or "").strip()
    parser_id = str(payload.get("parser_id") or "").strip()
    group_id = str(payload.get("canonical_group_id") or "").strip()
    if not group_id:
        return None
    inferred = infer_bundled_canonical_triple(schema_id=schema_id, parser_id=parser_id)
    if not inferred or group_id == inferred[1]:
        return None
    return (
        f"canonical_group_id {group_id!r} does not match bundled lane "
        f"{inferred[1]!r} for schema {schema_id or parser_id!r}"
    )


def bundled_schema_drift(payload: Dict[str, Any]) -> Optional[str]:
    """Reason when the payload's schema is a drift variant of a bundled id and
    its declared lane contradicts the bundled triple, else None.

    Advisory, unlike bundled_lane_conflict: the caller decides whether the row
    can be retired (its source_id has a bundled replacement) or only reported.
    """
    group_id = str(payload.get("canonical_group_id") or "").strip()
    if not group_id:
        return None
    for key in (
        str(payload.get("schema_id") or "").strip(),
        str(payload.get("parser_id") or "").strip(),
    ):
        bundled_key = BUNDLED_SCHEMA_ALIASES.get(key)
        if not bundled_key:
            continue
        _, bundled_lane = BUNDLED_CANONICAL_TRIPLES[bundled_key]
        if group_id != bundled_lane:
            return (
                f"schema {key!r} is a drift variant of bundled {bundled_key!r} and "
                f"canonical_group_id {group_id!r} contradicts bundled lane {bundled_lane!r}"
            )
    return None


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
        conflict = bundled_lane_conflict(merged)
        if conflict:
            raise ValueError(conflict)
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
