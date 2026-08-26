"""Recompute gate-ready aggregate signal objects from typed heads."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..signal_object_store import SignalObjectStore

# Flex halo: how far a negotiable block's soft boundary extends on each side —
# capped so an all-day movable block doesn't claim the whole day as flexible.
_FLEX_HALO_CAP_MINUTES = 60
# meeting_load_band cutoffs, busy hours over the most recent 7 days of data.
_LOAD_LIGHT_MAX_HOURS = 10.0
_LOAD_MODERATE_MAX_HOURS = 25.0


def _parse_iso(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _window_span(payload: Dict[str, Any]) -> Optional[Tuple[datetime, datetime]]:
    start = _parse_iso(payload.get("start"))
    end = _parse_iso(payload.get("end"))
    if start is None or end is None or end <= start:
        return None
    # Mixed aware/naive timestamps can't be compared or subtracted.
    if (start.tzinfo is None) != (end.tzinfo is None):
        return None
    return start, end


def _flex_entry(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Airport-fare halo for a negotiable busy block: the block itself is
    conditionally available, and its shoulders (± up to an hour) are soft."""
    band = str(payload.get("movability_band") or "")
    if band not in ("negotiable", "flexible"):
        return None
    span = _window_span(payload)
    if span is None:
        return None
    start, end = span
    halo = min(end - start, timedelta(minutes=_FLEX_HALO_CAP_MINUTES))
    return {
        "start": payload.get("start"),
        "end": payload.get("end"),
        "kind": "conditional_availability",
        "negotiability": band,
        "flex_before": {"start": (start - halo).isoformat(), "end": start.isoformat()},
        "flex_after": {"start": end.isoformat(), "end": (end + halo).isoformat()},
    }


def _meeting_load(busy_payloads: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    spans = [(p, _window_span(p)) for p in busy_payloads]
    spans = [(p, s) for p, s in spans if s is not None]
    if not spans:
        return None
    latest = max(end for _p, (_start, end) in spans)
    week_ago = latest - timedelta(days=7)
    busy_hours = 0.0
    hard = soft = 0
    for payload, (start, end) in spans:
        clipped_start = max(start, week_ago)
        if end <= week_ago:
            continue
        busy_hours += (end - clipped_start).total_seconds() / 3600.0
        if str(payload.get("hard_or_soft") or "hard") == "soft":
            soft += 1
        else:
            hard += 1
    if busy_hours <= _LOAD_LIGHT_MAX_HOURS:
        band = "light"
    elif busy_hours <= _LOAD_MODERATE_MAX_HOURS:
        band = "moderate"
    else:
        band = "heavy"
    return {
        "band": band,
        "busy_hours_7d": round(busy_hours, 2),
        "window_end": latest.isoformat(),
        "hard_count": hard,
        "soft_count": soft,
    }


def recompute_time_aggregates(store: SignalObjectStore) -> int:
    windows = store.list_objects("time", object_type="AvailabilityWindow", limit=200)
    free_windows = [
        w
        for w in windows
        if str((w.get("payload") or {}).get("availability_kind") or "") == "free"
    ]
    busy_windows = [
        w
        for w in windows
        if str((w.get("payload") or {}).get("availability_kind") or "") != "free"
    ]
    busy_payloads = [dict(w.get("payload") or {}) for w in busy_windows]
    created = 0
    if free_windows:
        store.upsert_object(
            "time",
            "availability_window_scores",
            "aggregate",
            {
                "free_window_count": len(free_windows),
                "windows": [
                    {
                        "start": (w.get("payload") or {}).get("start"),
                        "end": (w.get("payload") or {}).get("end"),
                    }
                    for w in free_windows[:12]
                ],
            },
            source_refs=_refs_from_objects(free_windows),
            confidence=0.9,
        )
        created += 1
    if busy_windows:
        hard_count = sum(
            1 for p in busy_payloads if str(p.get("hard_or_soft") or "hard") != "soft"
        )
        store.upsert_object(
            "time",
            "commitment_hard_blocks",
            "aggregate",
            {
                "busy_window_count": len(busy_windows),
                "hard_count": hard_count,
                "soft_count": len(busy_windows) - hard_count,
                "windows": [
                    {
                        "start": p.get("start"),
                        "end": p.get("end"),
                        "hard_or_soft": p.get("hard_or_soft"),
                        "movability_band": p.get("movability_band"),
                    }
                    for p in busy_payloads[:12]
                ],
            },
            source_refs=_refs_from_objects(busy_windows),
            confidence=0.9,
        )
        created += 1
        flex_entries = [e for e in (_flex_entry(p) for p in busy_payloads) if e]
        if flex_entries:
            store.upsert_object(
                "time",
                "flex_windows",
                "aggregate",
                {
                    "flex_window_count": len(flex_entries),
                    "windows": flex_entries[:12],
                },
                source_refs=_refs_from_objects(busy_windows),
                confidence=0.85,
            )
            created += 1
        load = _meeting_load(busy_payloads)
        if load:
            store.upsert_object(
                "time",
                "meeting_load_band",
                "rolling_7d",
                load,
                source_refs=_refs_from_objects(busy_windows),
                confidence=0.85,
            )
            created += 1
    created += _recompute_availability_summary(store)
    return created


def _recompute_availability_summary(store: SignalObjectStore) -> int:
    """Grantee-safe prose+structured rollup of the time dimension. Inputs are
    already title/attendee-free, so the summary is safe by construction."""

    def _head(object_type: str) -> Dict[str, Any]:
        rows = store.list_objects("time", object_type=object_type, limit=1)
        return dict((rows[0].get("payload") or {})) if rows else {}

    free = _head("availability_window_scores")
    busy = _head("commitment_hard_blocks")
    flex = _head("flex_windows")
    load = _head("meeting_load_band")
    routine = _head("routine_confidence")
    if not (free or busy):
        return 0

    parts: List[str] = []
    top_bands = routine.get("top_bands") or []
    if top_bands:
        bands_text = "; ".join(
            f"{b.get('day_of_week')} {b.get('time_band')} ({b.get('dominant_kind')})"
            for b in top_bands[:3]
            if isinstance(b, dict)
        )
        if bands_text:
            parts.append(f"Typically active: {bands_text}")
    if load:
        parts.append(
            f"Load last 7 days: {load.get('band')} ({load.get('busy_hours_7d')} busy hours)"
        )
    busy_count = int(busy.get("busy_window_count") or 0)
    soft_count = int(busy.get("soft_count") or 0)
    if busy_count:
        parts.append(f"{soft_count} of {busy_count} busy blocks negotiable")
    free_count = int(free.get("free_window_count") or 0)
    if free_count:
        parts.append(f"{free_count} open windows known")
    flex_count = int(flex.get("flex_window_count") or 0)
    if flex_count:
        parts.append(
            f"{flex_count} conditionally available (soft boundaries before/after)"
        )

    store.upsert_object(
        "time",
        "availability_summary",
        "rolling",
        {
            "summary_text": ". ".join(parts) + ".",
            "free_window_count": free_count,
            "busy_window_count": busy_count,
            "soft_count": soft_count,
            "flex_window_count": flex_count,
            "load_band": load.get("band"),
            "routine_confidence": routine.get("confidence"),
            "top_bands": top_bands[:3],
        },
        confidence=0.85,
    )
    return 1


def recompute_relationship_aggregates(store: SignalObjectStore) -> int:
    edges = store.list_objects("relationships", object_type="RelationshipEdge", limit=200)
    if not edges:
        return 0
    # Absent means unknown. Defaulting to "medium" invented the same measurement
    # the extractor used to stamp, one layer further from anyone who could check.
    warmth = {str((e.get("payload") or {}).get("warmth_band") or "unknown") for e in edges}
    store.upsert_object(
        "relationships",
        "warmth_score",
        "aggregate",
        {"edge_count": len(edges), "warmth_bands": sorted(warmth)},
        source_refs=_refs_from_objects(edges),
        confidence=0.85,
    )
    store.upsert_object(
        "relationships",
        "relationship_edge_summary",
        "aggregate",
        {
            "entities": [
                (e.get("payload") or {}).get("target_entity_key")
                for e in edges[:20]
                if (e.get("payload") or {}).get("target_entity_key")
            ]
        },
        source_refs=_refs_from_objects(edges),
        confidence=0.85,
    )
    return 2


# Phrases in goal text that signal active opportunity-seeking (willingness).
_SEEKING_PHRASES = (
    "looking for",
    "seeking",
    "open to",
    "hiring",
    "want to meet",
    "interested in",
    "find a",
    "need a",
    "exploring",
)


def recompute_seeking_aggregates(store: SignalObjectStore) -> int:
    """opportunity_seeking_score — willingness half of "good candidate for an
    opportunity": is this person actively seeking, receptive, or dormant?
    Read at fit gates only; never served through availability:read."""
    goals = store.list_objects("intentions", object_type="Goal", limit=100)
    goals += store.list_objects("intentions", object_type="user_goals", limit=100)
    if not goals:
        return 0
    active = 0
    seeking_hits = 0
    seeking_kinds: List[str] = []
    for goal in goals:
        payload = goal.get("payload") or {}
        status = str(payload.get("status") or "").lower()
        if status in ("active", "in_progress", ""):
            active += 1
        text = str(payload.get("goal_text") or "").lower()
        for phrase in _SEEKING_PHRASES:
            if phrase in text:
                seeking_hits += 1
                seeking_kinds.append(phrase)
                break
    score = round(min(1.0, 0.1 * active + 0.3 * seeking_hits), 3)
    if score >= 0.5:
        band = "actively_seeking"
    elif score >= 0.2:
        band = "receptive"
    else:
        band = "dormant"
    store.upsert_object(
        "intentions",
        "opportunity_seeking_score",
        "rolling",
        {
            "score": score,
            "band": band,
            "active_goal_count": active,
            "seeking_goal_count": seeking_hits,
            "seeking_kinds": sorted(set(seeking_kinds)),
        },
        source_refs=_refs_from_objects(goals),
        confidence=round(min(0.9, 0.4 + 0.05 * len(goals)), 3),
    )
    return 1


def recompute_all_gate_aggregates(store: SignalObjectStore) -> Dict[str, int]:
    return {
        "time": recompute_time_aggregates(store),
        "relationships": recompute_relationship_aggregates(store),
        "intentions": recompute_seeking_aggregates(store),
    }


def _refs_from_objects(objects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    for obj in objects:
        for ref in obj.get("source_refs") or []:
            if isinstance(ref, dict) and ref not in refs:
                refs.append(ref)
    return refs[:50]
