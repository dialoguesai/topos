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
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..signal.signal_object_store import SignalObjectStore

# A pin is a bet with a shelf life. The horizon is coarse on purpose — owners
# think in "weeks / months / this quarter", not in target dates — and it is the
# only thing that decides when the pin stops steering.
HORIZON_DAYS = {"weeks": 21, "months": 60, "quarter": 90}
DEFAULT_HORIZON = "quarter"

_STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "for", "on", "with",
         "my", "our", "their", "this", "that", "is", "are", "be", "see",
         "more", "new", "build", "collect", "evidence", "people", "things"}


def _keywords(text: str) -> List[str]:
    words = re.findall(r"[a-z][a-z0-9\-']{2,}", str(text or "").lower())
    return [w for w in words if w not in _STOP]


def _parse_day(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def intent_faded(intent: Dict[str, Any], *, today: Optional[date] = None) -> bool:
    """Has this pin's horizon elapsed since it was declared?

    Read-time only — nothing is ever rewritten. An intent with no parseable
    origin never fades (we cannot date it, so we keep steering by it).
    """
    origin = _parse_day(intent.get("origin_timeframe"))
    if origin is None:
        return False
    days = HORIZON_DAYS.get(str(intent.get("horizon") or DEFAULT_HORIZON),
                            HORIZON_DAYS[DEFAULT_HORIZON])
    return origin + timedelta(days=days) < (today or date.today())


def intent_status(intent: Dict[str, Any], *, today: Optional[date] = None) -> str:
    """Reported status: an explicit retire wins, otherwise the horizon decides."""
    stored = str(intent.get("status") or "active")
    if stored != "active":
        return stored
    return "faded" if intent_faded(intent, today=today) else "active"


def pin_intent(conn: sqlite3.Connection, intent_text: str, *,
               horizon: str = DEFAULT_HORIZON, links: Optional[List[str]] = None,
               origin_timeframe: Optional[str] = None) -> Dict[str, Any]:
    """F1: create (or refresh) a declared_intent signal object."""
    key = "intent:" + re.sub(r"[^a-z0-9]+", "-", intent_text.lower())[:60].strip("-")
    payload = {
        "intent_text": intent_text,
        "horizon": horizon,
        # The fade clock starts the moment the pin is made unless the owner
        # backdates it — an undated pin would otherwise steer forever.
        "origin_timeframe": origin_timeframe or date.today().isoformat(),
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


def _all_intents(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
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
        p["_key"] = key
        out.append(p)
    return out


def active_intents(conn: sqlite3.Connection,
                   *, today: Optional[date] = None) -> List[Dict[str, Any]]:
    """The live pins: neither retired by hand nor faded by their horizon."""
    return [p for p in _all_intents(conn) if intent_status(p, today=today) == "active"]


def faded_intents(conn: sqlite3.Connection, *, limit: int = 5,
                  today: Optional[date] = None) -> List[Dict[str, Any]]:
    """Pins whose horizon has elapsed, most recently declared first."""
    out = []
    for p in _all_intents(conn):
        if intent_status(p, today=today) != "faded":
            continue
        p["status"] = "faded"
        out.append(p)
    out.sort(key=lambda p: str(p.get("origin_timeframe") or ""), reverse=True)
    return out[:limit]


def retire_intent(conn: sqlite3.Connection, object_key: str) -> Optional[Dict[str, Any]]:
    """Manual retire — the owner calling it before the horizon does."""
    key = str(object_key or "").strip()
    row = conn.execute(
        "SELECT payload_json FROM signal_objects WHERE object_type='declared_intent' "
        "AND object_key=? AND valid_to IS NULL", (key,)).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    payload["status"] = "retired"
    payload.pop("_key", None)
    store = SignalObjectStore(conn)
    return store.upsert_object(
        "intentions", "declared_intent", key, payload,
        source_refs=[{"declared_by": "owner"}],
        confidence=1.0, extractor_version="owner_declared", created_by="owner")


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
