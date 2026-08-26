"""Fit composition evaluators over typed signal objects."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..signal.signal_object_store import SignalObjectStore


@lru_cache(maxsize=1)
def load_opportunity_registry() -> Dict[str, Any]:
    path = Path(__file__).resolve().parent / "opportunity_types.json"
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def evaluate_opportunity(
    conn,
    opportunity_type: str,
    *,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    registry = load_opportunity_registry()
    opp = (registry.get("opportunity_types") or {}).get(opportunity_type)
    if not opp:
        raise ValueError(f"Unknown opportunity_type: {opportunity_type!r}")

    store = SignalObjectStore(conn)
    ctx = context or {}
    facet_results: List[Dict[str, Any]] = []
    confidences: List[float] = []

    for facet in opp.get("facets") or []:
        facet_id = str(facet.get("id") or "")
        result = _evaluate_facet(store, facet_id, facet.get("method"), ctx)
        weight = float(facet.get("weight") or 0)
        result["weight"] = weight
        facet_results.append(result)
        confidences.append(float(result.get("confidence") or 0))

    composite = sum(r["score"] * r["weight"] for r in facet_results)
    min_conf = min(confidences) if confidences else 0.0
    threshold = float(opp.get("pass_threshold") or 0.5)

    return {
        "opportunity_type": opportunity_type,
        "facet_results": facet_results,
        "composite_score": round(composite, 4),
        "confidence_band": _confidence_band(min_conf),
        "pass": composite >= threshold,
        "pass_threshold": threshold,
    }


def _evaluate_facet(
    store: SignalObjectStore,
    facet_id: str,
    method: Optional[str],
    ctx: Dict[str, Any],
) -> Dict[str, Any]:
    if facet_id == "timing_feasibility":
        scores = store.list_objects("time", object_type="availability_window_scores", limit=1)
        payload = (scores[0].get("payload") if scores else {}) or {}
        windows = payload.get("windows") or []
        target_date = str(ctx.get("target_window_start") or "")[:10]
        if not target_date:
            # No target requested: feasibility = any known free window.
            hit = bool(windows)
        else:
            hit = any(
                str(w.get("start") or "").startswith(target_date)
                for w in windows
                if isinstance(w, dict)
            )
        if hit:
            return {
                "facet_id": facet_id,
                "score": 0.85,
                "confidence": float(scores[0]["confidence"]) if scores else 0.3,
                "public_band": "overlap_found",
            }
        # No free window — a negotiable busy block (flex halo) on the target
        # date still allows a soft yes: "possible, requires rescheduling".
        flex_hit = False
        if target_date:
            flex = store.list_objects("time", object_type="flex_windows", limit=1)
            flex_payload = (flex[0].get("payload") if flex else {}) or {}
            flex_hit = any(
                str(w.get("start") or "").startswith(target_date)
                or str((w.get("flex_before") or {}).get("start") or "").startswith(target_date)
                or str((w.get("flex_after") or {}).get("end") or "").startswith(target_date)
                for w in (flex_payload.get("windows") or [])
                if isinstance(w, dict)
            )
        return {
            "facet_id": facet_id,
            "score": 0.6 if flex_hit else 0.2,
            "confidence": float(scores[0]["confidence"]) if scores else 0.3,
            "public_band": "negotiable_overlap" if flex_hit else "no_overlap",
        }
    if facet_id == "commitment_conflict":
        load = store.list_objects("time", object_type="meeting_load_band", limit=1)
        load_payload = (load[0].get("payload") if load else {}) or {}
        load_band = str(load_payload.get("band") or "")
        if load_band:
            score = {"light": 0.9, "moderate": 0.65, "heavy": 0.35}.get(load_band, 0.5)
            return {
                "facet_id": facet_id,
                "score": score,
                "confidence": float(load[0]["confidence"]),
                "public_band": f"{load_band}_load",
            }
        blocks = store.list_objects("time", object_type="commitment_hard_blocks", limit=1)
        payload = (blocks[0].get("payload") if blocks else {}) or {}
        count = int(payload.get("busy_window_count") or 0)
        score = 0.9 if count <= 3 else 0.4
        return {
            "facet_id": facet_id,
            "score": score,
            "confidence": float(blocks[0]["confidence"]) if blocks else 0.3,
            "public_band": "light_load" if score >= 0.7 else "heavy_load",
        }
    if facet_id == "relationship_warmth":
        warmth = store.list_objects("relationships", object_type="warmth_score", limit=1)
        payload = (warmth[0].get("payload") if warmth else {}) or {}
        # An "unknown" band is the absence of a measurement, not a warm one. This
        # read "any band present -> warm_network", so a constant stamped by the
        # extractor was the whole evidence base for calling the owner's network warm.
        bands = [b for b in (payload.get("warmth_bands") or [])
                 if str(b).strip().lower() not in ("", "unknown")]
        score = 0.75 if bands else 0.2
        return {
            "facet_id": facet_id,
            "score": score,
            "confidence": float(warmth[0]["confidence"]) if (warmth and bands) else 0.3,
            "public_band": "warm_network" if score >= 0.6 else "cold_network",
        }
    if facet_id in ("domain_overlap", "seeking_alignment"):
        tags = [t.lower() for t in (ctx.get("domain_tags") or ["edtech", "personal ai"])]
        edges = store.list_objects("relationships", object_type="RelationshipEdge", limit=20)
        edge_tags: List[str] = []
        for edge in edges:
            edge_tags.extend((edge.get("payload") or {}).get("context_tags") or [])
        overlap = len(set(tags) & set(t.lower() for t in edge_tags))
        score = min(1.0, overlap / max(1, len(tags)))
        if facet_id == "seeking_alignment" and "intro" in edge_tags:
            score = max(score, 0.7)
        return {
            "facet_id": facet_id,
            "score": round(score, 4),
            "confidence": 0.7 if edges else 0.35,
            "public_band": "aligned" if score >= 0.5 else "weak_alignment",
        }
    if facet_id == "willingness":
        seeking = store.list_objects(
            "intentions", object_type="opportunity_seeking_score", limit=1
        )
        payload = (seeking[0].get("payload") if seeking else {}) or {}
        band = str(payload.get("band") or "")
        score = {"actively_seeking": 0.85, "receptive": 0.55, "dormant": 0.25}.get(
            band, 0.3
        )
        return {
            "facet_id": facet_id,
            "score": score,
            "confidence": float(seeking[0]["confidence"]) if seeking else 0.3,
            "public_band": band or "unknown_seeking",
        }
    if facet_id == "capacity":
        return {
            "facet_id": facet_id,
            "score": 0.65,
            "confidence": 0.5,
            "public_band": "moderate_capacity",
        }
    return {"facet_id": facet_id, "score": 0.0, "confidence": 0.0, "public_band": "unknown"}


def _confidence_band(confidence: float) -> str:
    if confidence >= 0.8:
        return "high"
    if confidence >= 0.5:
        return "medium"
    return "low"


def compute_fit_readiness(conn) -> Dict[str, float]:
    store = SignalObjectStore(conn)
    readiness: Dict[str, float] = {}
    time_ok = bool(store.list_objects("time", object_type="availability_window_scores", limit=1))
    rel_ok = bool(store.list_objects("relationships", object_type="warmth_score", limit=1))
    readiness["schedule_meeting"] = 0.85 if time_ok else 0.2
    readiness["evaluate_introduction"] = 0.82 if (time_ok and rel_ok) else 0.35 if rel_ok else 0.15
    return readiness
