"""Recompute gate-ready aggregate signal objects from typed heads."""

from __future__ import annotations

from typing import Any, Dict, List

from ..signal_object_store import SignalObjectStore


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
        store.upsert_object(
            "time",
            "commitment_hard_blocks",
            "aggregate",
            {
                "busy_window_count": len(busy_windows),
                "windows": [
                    {
                        "start": (w.get("payload") or {}).get("start"),
                        "end": (w.get("payload") or {}).get("end"),
                        "hard_or_soft": (w.get("payload") or {}).get("hard_or_soft"),
                    }
                    for w in busy_windows[:12]
                ],
            },
            source_refs=_refs_from_objects(busy_windows),
            confidence=0.9,
        )
        created += 1
    return created


def recompute_relationship_aggregates(store: SignalObjectStore) -> int:
    edges = store.list_objects("relationships", object_type="RelationshipEdge", limit=200)
    if not edges:
        return 0
    warmth = {str((e.get("payload") or {}).get("warmth_band") or "medium") for e in edges}
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


def recompute_all_gate_aggregates(store: SignalObjectStore) -> Dict[str, int]:
    return {
        "time": recompute_time_aggregates(store),
        "relationships": recompute_relationship_aggregates(store),
    }


def _refs_from_objects(objects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []
    for obj in objects:
        for ref in obj.get("source_refs") or []:
            if isinstance(ref, dict) and ref not in refs:
                refs.append(ref)
    return refs[:50]
