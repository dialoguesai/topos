"""Pinned intents (PLAN_INTENT_STEERING.md F1/F2).

A declared_intent is pure expression: owner-authored, confidence 1.0,
`extractor_version="owner_declared"` — the temporal graph's first
future-facing objects, and the forward attractor the backward-looking
interest model lacks. `intent_match` is the semantic term: stored-embedding
cosine when the item has a vector, deterministic keyword proximity otherwise
(so new-category items without embeddings are still steerable — the exact
items the graph term is blind to).
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from ..signal.signal_object_store import SignalObjectStore

_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "with",
         "my", "our", "their", "this", "that", "is", "are", "be", "see",
         "more", "new", "build", "collect", "evidence", "people", "things"}


def _keywords(text: str) -> List[str]:
    words = re.findall(r"[a-z][a-z0-9\-']{2,}", str(text or "").lower())
    return [w for w in words if w not in _STOP]


def pin_intent(conn: sqlite3.Connection, intent_text: str, *,
               horizon: str = "quarter", links: Optional[List[str]] = None,
               origin_timeframe: Optional[str] = None) -> Dict[str, Any]:
    """F1: create (or refresh) a declared_intent signal object."""
    key = "intent:" + re.sub(r"[^a-z0-9]+", "-", intent_text.lower())[:60].strip("-")
    payload = {
        "intent_text": intent_text,
        "horizon": horizon,
        "origin_timeframe": origin_timeframe,
        "status": "active",
        "links": links or [],
        "keywords": _keywords(intent_text),
        "disclosure": "owner_only",
    }
    store = SignalObjectStore(conn)
    return store.upsert_object(
        "intentions", "declared_intent", key, payload,
        source_refs=[{"declared_by": "owner"}],
        confidence=1.0, extractor_version="owner_declared", created_by="owner")


def active_intents(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT object_key, payload_json FROM signal_objects "
            "WHERE object_type='declared_intent' AND valid_to IS NULL").fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for key, pj in rows:
        try:
            p = json.loads(pj)
        except (TypeError, ValueError):
            continue
        if p.get("status", "active") == "active":
            p["_key"] = key
            out.append(p)
    return out


def intent_match(text: str, intents: List[Dict[str, Any]],
                 item_vec=None, intent_vecs=None) -> Tuple[float, Optional[str]]:
    """F2 semantic term: max proximity of an item to any active intent.

    Deterministic keyword proximity (matches/3, capped at 1.0) always runs;
    embedding cosine supersedes it when both vectors exist. Returns
    (score, intent_text-of-best) — grounds must name the pin that fired.
    """
    best, best_intent = 0.0, None
    words = set(_keywords(text))
    for i, intent in enumerate(intents):
        kw = set(intent.get("keywords") or _keywords(intent.get("intent_text", "")))
        score = min(1.0, len(words & kw) / 3.0) if kw else 0.0
        if item_vec is not None and intent_vecs is not None and intent_vecs.get(intent["_key"]) is not None:
            import numpy as np
            v = intent_vecs[intent["_key"]]
            score = max(score, float(item_vec @ v))
        if score > best:
            best, best_intent = score, intent.get("intent_text")
    return best, best_intent


ASK_PATTERNS = re.compile(
    r"\?|\b(can|could|would|will) you\b|\blet me know\b|\bplease (send|review|confirm|check)\b"
    r"|\bdid you (get|see|review)\b|\bwere you able\b|\bdo you (want|need|have)\b"
    r"|\bconfirm\b|\brsvp\b|\bby (monday|tuesday|wednesday|thursday|friday|tomorrow|eod)\b",
    re.IGNORECASE)


def response_expected(text: str) -> bool:
    """F3: does this message actually ask for anything? PS-grade warmth doesn't."""
    return bool(ASK_PATTERNS.search(str(text or "")))
