"""Route canonical rows to extraction artifacts and signal objects."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ..signal_object_store import SignalObjectStore
from .artifact_store import ExtractionArtifactStore
from .rule_extractors import extract_artifacts


def _conversation_context_tag(conn, record: Dict[str, Any]) -> Optional[str]:
    """Owner-set work/personal tag on the message's parent conversation."""
    conversation_id = str(record.get("conversation_id") or "").strip()
    if not conversation_id:
        return None
    try:
        row = conn.execute(
            "SELECT context_tag FROM conversations "
            "WHERE conversation_id=? AND context_tag IS NOT NULL LIMIT 1",
            (conversation_id,),
        ).fetchone()
    except Exception:
        return None
    return str(row[0]) if row and row[0] else None


def route_canonical_record(
    conn,
    *,
    canonical_table: str,
    record: Dict[str, Any],
) -> Dict[str, int]:
    artifact_store = ExtractionArtifactStore(conn)
    object_store = SignalObjectStore(conn)
    counts = {"artifacts": 0, "objects": 0}

    if canonical_table in ("conversation_messages", "conversation_message"):
        tag = _conversation_context_tag(conn, record)
        if tag:
            record = {**record, "_conversation_context": tag}

    for artifact_type, payload, affinity, source_refs, confidence in extract_artifacts(
        canonical_table, record
    ):
        artifact_store.upsert_artifact(
            artifact_type,
            payload,
            dimension_affinity=affinity,
            source_refs=source_refs,
            confidence=confidence,
        )
        counts["artifacts"] += 1
        counts["objects"] += _materialize_signal_objects(
            object_store,
            artifact_type=artifact_type,
            payload=payload,
            dimension_affinity=affinity,
            source_refs=source_refs,
            confidence=confidence,
        )

    return counts


def _materialize_signal_objects(
    object_store: SignalObjectStore,
    *,
    artifact_type: str,
    payload: Dict[str, Any],
    dimension_affinity: List[str],
    source_refs: List[Dict[str, Any]],
    confidence: float,
) -> int:
    created = 0
    if artifact_type == "Interval" and "time" in dimension_affinity:
        start = str(payload.get("start") or "")
        end = str(payload.get("end") or "")
        if not start:
            return 0
        object_key = f"{start}|{end}|{payload.get('availability_kind', 'busy')}"
        object_store.upsert_object(
            "time",
            "AvailabilityWindow",
            object_key,
            payload,
            source_refs=source_refs,
            confidence=confidence,
        )
        created += 1
    elif artifact_type == "Edge" and "relationships" in dimension_affinity:
        entity_key = str(payload.get("target_entity_key") or "unknown")
        # Keyed on the person AND what they were doing, because those are two
        # different facts about that person and this store keeps one row per key.
        #
        # On the person alone, every journal edge for someone collapses to one
        # row and the last write decides what it says. Measured on the live node
        # 2026-08-28: 16 artifacts said `mitch × Topos` and 2 said
        # `mitch × Chill`; the surviving row said Chill, because a Chill entry
        # happened to be written last. Six rows node-wide said Topos against 38
        # saying Chill — a write-order artifact reading as a finding.
        #
        # The branches either side of this one already compose their keys
        # (`start|end|kind`, `kind:label`); this was the only one that dropped
        # the dimension that makes its rows distinct.
        coactivity = str(payload.get("coactivity_band") or "").strip()
        object_key = f"{entity_key}|{coactivity}" if coactivity else entity_key
        object_store.upsert_object(
            "relationships",
            "RelationshipEdge",
            object_key,
            payload,
            source_refs=source_refs,
            confidence=confidence,
        )
        created += 1
    elif artifact_type == "Claim" and "profile" in dimension_affinity:
        claim_kind = str(payload.get("claim_kind") or "claim")
        label = str(payload.get("label") or payload.get("title_band") or claim_kind)
        object_key = f"{claim_kind}:{label[:48]}"
        object_type = "SkillNode" if claim_kind == "skill" else "ExperienceNode"
        object_store.upsert_object(
            "profile",
            object_type,
            object_key,
            payload,
            source_refs=source_refs,
            confidence=confidence,
        )
        created += 1
    elif artifact_type == "EntityRef" and "places" in dimension_affinity:
        entity_key = str(payload.get("entity_key") or "unknown")
        object_store.upsert_object(
            "places",
            "PlaceContext",
            entity_key,
            payload,
            source_refs=source_refs,
            confidence=confidence,
        )
        created += 1
    elif artifact_type == "Goal" and "intentions" in dimension_affinity:
        goal_text = str(payload.get("goal_text") or "").strip()
        if not goal_text:
            return 0
        object_key = _goal_object_key(goal_text)
        object_store.upsert_object(
            "intentions",
            "Goal",
            object_key,
            payload,
            source_refs=source_refs,
            confidence=confidence,
        )
        created += 1
    return created


def _goal_object_key(goal_text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(goal_text or "").strip().lower()).strip("-")
    return slug[:64] or "goal"


def route_canonical_batch(
    conn,
    records: List[Dict[str, Any]],
    *,
    default_table: Optional[str] = None,
) -> Dict[str, int]:
    totals = {"artifacts": 0, "objects": 0}
    for record in records:
        table = str(record.get("canonical_table") or default_table or "").strip()
        if not table:
            continue
        result = route_canonical_record(conn, canonical_table=table, record=record)
        totals["artifacts"] += result["artifacts"]
        totals["objects"] += result["objects"]
    return totals
