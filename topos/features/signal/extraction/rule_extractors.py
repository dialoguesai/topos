"""Rule-based extractors from canonical rows to typed artifacts."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

ArtifactDraft = Tuple[str, Dict[str, Any], List[str], List[Dict[str, Any]], float]


def _source_ref(table: str, record_id: str) -> Dict[str, str]:
    return {"table": table, "id": str(record_id)}


def _anon_entity_key(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")
    return slug or "unknown"


def _parse_metadata_json(record: Dict[str, Any]) -> Dict[str, Any]:
    meta = record.get("metadata_json")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = {}
    return meta if isinstance(meta, dict) else {}


def _calendar_is_busy(record: Dict[str, Any]) -> bool:
    is_busy = record.get("is_busy")
    if is_busy is not None:
        return bool(is_busy)
    meta = _parse_metadata_json(record)
    if "is_busy" in meta:
        return bool(meta["is_busy"])
    return True


_SOLO_GROUP_LABELS = frozenset({"solo", "group", ""})
_PROFILE_SKILL_CATEGORIES = frozenset(
    {
        "weight-lifting",
        "run",
        "bicycling",
        "walk",
        "practice",
        "music",
        "reading",
    }
)
_GENERIC_JOURNAL_CATEGORIES = frozenset({"grow", ""})


def _journal_goal_text(record: Dict[str, Any], meta: Dict[str, Any]) -> str:
    goal = str(meta.get("goal") or record.get("goal") or "").strip()
    if goal:
        return goal
    content = str(record.get("content") or "")
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("goal:"):
            return stripped.split(":", 1)[1].strip()
    return ""


def _journal_profile_claim(category: str) -> Optional[Tuple[str, Dict[str, Any]]]:
    label = str(category or "").strip()
    if not label or label.lower() in _GENERIC_JOURNAL_CATEGORIES:
        return None
    normalized = label.lower()
    if normalized in _PROFILE_SKILL_CATEGORIES:
        return (
            "skill",
            {
                "claim_kind": "skill",
                "label": label,
                "polarity": "positive",
            },
        )
    return (
        "domain",
        {
            "claim_kind": "domain",
            "label": label,
            "title_band": label[:80],
            "polarity": "positive",
        },
    )


def _parse_journal_group_names(group: Any) -> List[str]:
    raw = str(group or "").strip()
    if not raw or raw.lower() in _SOLO_GROUP_LABELS:
        return []
    names: List[str] = []
    for part in re.split(r",\s*", raw):
        name = part.strip()
        if name and name.lower() not in _SOLO_GROUP_LABELS:
            names.append(name)
    return names


def _journal_place_name(record: Dict[str, Any], meta: Dict[str, Any]) -> str:
    place = str(record.get("place_name") or "").strip()
    if place:
        return place
    return str(meta.get("location") or "").strip()


def extract_from_journal(record: Dict[str, Any]) -> List[ArtifactDraft]:
    """Time-log journal rows (e.g. Grow) → busy intervals and co-activity relationship edges."""
    entry_id = str(record.get("entry_id") or record.get("record_id") or "")
    if not entry_id:
        return []
    meta = _parse_metadata_json(record)
    refs = [_source_ref("journal_entries", entry_id)]
    drafts: List[ArtifactDraft] = []

    start = str(record.get("entry_at") or "")
    end = str(meta.get("ends_at") or record.get("ends_at") or "").strip()
    place_name = _journal_place_name(record, meta)
    if start and end:
        interval = {
            "start": start,
            "end": end,
            "availability_kind": "busy",
            "hard_or_soft": "soft",
            "activity_band": str(record.get("category") or "")[:80] or None,
            "duration_minutes": meta.get("duration_minutes"),
            "location_band": place_name[:80] if place_name else None,
        }
        interval_dims = ["time", "work", "wellbeing"]
        if place_name:
            interval_dims.append("places")
        drafts.append(
            (
                "Interval",
                interval,
                interval_dims,
                refs,
                0.9,
            )
        )

    if place_name:
        place = {
            "entity_key": _anon_entity_key(place_name),
            "entity_kind": "place",
            "display_band": place_name[:80],
            "event_at": start or None,
            "activity_band": str(record.get("category") or "")[:80] or None,
        }
        drafts.append(
            (
                "EntityRef",
                place,
                ["places", "time"],
                refs,
                0.86,
            )
        )

    activity_band = str(record.get("category") or "").strip()
    people_raw = record.get("people")
    if people_raw is None or str(people_raw).strip() == "":
        people_raw = meta.get("group")
    for name in _parse_journal_group_names(people_raw):
        edge = {
            "target_entity_key": _anon_entity_key(name),
            "warmth_band": "medium",
            "tier": "personal",
            "cadence_band": "recent",
            "context_tags": _infer_context_tags(str(record.get("content") or "")),
            "coactivity_band": activity_band[:40] if activity_band else None,
        }
        drafts.append(
            (
                "Edge",
                edge,
                ["relationships"],
                refs,
                0.82,
            )
        )

    goal_text = _journal_goal_text(record, meta)
    if goal_text:
        drafts.append(
            (
                "Goal",
                {
                    "goal_text": goal_text[:240],
                    "horizon": "near_term",
                    "status": "active" if meta.get("completed") else "in_progress",
                    "source_category": activity_band[:80] or None,
                },
                ["intentions", "work"],
                refs,
                0.88 if meta.get("goal") else 0.78,
            )
        )

    profile_claim = _journal_profile_claim(activity_band)
    if profile_claim:
        claim_kind, claim_payload = profile_claim
        drafts.append(
            (
                "Claim",
                claim_payload,
                ["profile", "work"],
                refs,
                0.84 if claim_kind == "domain" else 0.8,
            )
        )

    return drafts


def extract_from_calendar(record: Dict[str, Any]) -> List[ArtifactDraft]:
    event_id = str(record.get("event_id") or record.get("record_id") or "")
    if not event_id:
        return []
    busy = _calendar_is_busy(record)
    refs = [_source_ref("calendar_events", event_id)]
    interval = {
        "start": str(record.get("starts_at") or ""),
        "end": str(record.get("ends_at") or ""),
        "availability_kind": "busy" if busy else "free",
        "hard_or_soft": "hard" if busy else "soft",
        "location_band": str(record.get("location") or "")[:80] or None,
    }
    return [
        (
            "Interval",
            interval,
            ["time", "places"],
            refs,
            0.95 if record.get("starts_at") else 0.6,
        )
    ]


def extract_from_contact(record: Dict[str, Any]) -> List[ArtifactDraft]:
    contact_id = str(record.get("contact_id") or record.get("record_id") or "")
    if not contact_id:
        return []
    display = str(record.get("display_name") or "")
    refs = [_source_ref("contacts", contact_id)]
    entity = {
        "entity_key": _anon_entity_key(display or contact_id),
        "entity_kind": "person",
        "display_band": display.split()[0] if display else "contact",
        "identifier_type": str(record.get("identifier_type") or ""),
    }
    return [
        (
            "EntityRef",
            entity,
            ["relationships", "profile"],
            refs,
            0.9,
        )
    ]


def extract_from_message(record: Dict[str, Any]) -> List[ArtifactDraft]:
    message_id = str(record.get("message_id") or record.get("record_id") or "")
    if not message_id:
        return []
    sender = str(record.get("sender_name") or record.get("sender_id") or "peer")
    is_self = bool(record.get("is_from_self"))
    if is_self:
        return []
    entity_key = _anon_entity_key(sender)
    refs = [_source_ref("conversation_messages", message_id)]
    edge = {
        "target_entity_key": entity_key,
        "warmth_band": "medium",
        "tier": "professional",
        "cadence_band": "recent",
        "context_tags": _infer_context_tags(str(record.get("content") or "")),
    }
    return [
        (
            "Edge",
            edge,
            ["relationships"],
            refs,
            0.75,
        )
    ]


def extract_from_profile(record: Dict[str, Any]) -> List[ArtifactDraft]:
    record_id = str(record.get("record_id") or "")
    if not record_id:
        return []
    refs = [_source_ref("profile_records", record_id)]
    record_type = str(record.get("record_type") or "").lower()
    drafts: List[ArtifactDraft] = []
    if record_type == "skill" or "python" in str(record.get("title") or "").lower():
        drafts.append(
            (
                "Claim",
                {
                    "claim_kind": "skill",
                    "label": str(record.get("title") or record.get("description") or "skill"),
                    "polarity": "positive",
                },
                ["profile", "work"],
                refs,
                0.85,
            )
        )
    else:
        drafts.append(
            (
                "Claim",
                {
                    "claim_kind": "experience",
                    "title_band": str(record.get("title") or ""),
                    "organization_band": str(record.get("organization") or ""),
                },
                ["profile", "work"],
                refs,
                0.85,
            )
        )
    return drafts


def _infer_context_tags(content: str) -> List[str]:
    lower = content.lower()
    tags: List[str] = []
    for token in ("edtech", "austin", "fundraising", "investor", "intro", "roadmap"):
        if token in lower:
            tags.append(token)
    return tags


EXTRACTORS = {
    "calendar_events": extract_from_calendar,
    "calendar_event": extract_from_calendar,
    "contacts": extract_from_contact,
    "contact": extract_from_contact,
    "conversation_messages": extract_from_message,
    "conversation_message": extract_from_message,
    "profile_records": extract_from_profile,
    "profile_record": extract_from_profile,
    "journal_entries": extract_from_journal,
    "journal_entry": extract_from_journal,
}


def extract_artifacts(canonical_table: str, record: Dict[str, Any]) -> List[ArtifactDraft]:
    table = str(canonical_table or record.get("canonical_table") or "").strip()
    fn = EXTRACTORS.get(table)
    if fn is None:
        return []
    return fn(record)
