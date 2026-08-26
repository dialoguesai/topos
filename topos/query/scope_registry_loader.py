"""Load wiki MVP scope registry (engine-side mirror of CP catalog)."""

from __future__ import annotations

import difflib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

_REGISTRY_PATH = Path(__file__).resolve().parent / "scope_registry.json"

LEGACY_SCOPE_IDS = frozenset(
    {
        "aiMessages:read",
        "aiChat:read",
        "events:read",
        "activitySummary:read",
        "wellnessSummary:read",
        "publicBio:read",
        "journal:read",
    }
)


@lru_cache(maxsize=1)
def load_scope_registry() -> Dict[str, Any]:
    with _REGISTRY_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def list_scopes() -> List[Dict[str, Any]]:
    return list(load_scope_registry().get("scopes") or [])


def get_scope_entry(scope_id: str) -> Dict[str, Any] | None:
    sid = (scope_id or "").strip()
    for entry in list_scopes():
        if str(entry.get("scope_id")) == sid:
            return entry
    return None


def get_must_not_retrieve(scope_id: str) -> List[str]:
    entry = get_scope_entry(scope_id)
    if not entry:
        return []
    return list(entry.get("must_not_retrieve") or [])


#: Concepts a caller reaches for that are NOT scope ids. The home chat picks its
#: own scope_id as free text and a small local model invents plausible ones: live
#: 2026-08-26 "Who are my friends?" was sent as scope_id "social_graph", which does
#: not exist, and the turn was denied before any retrieval ran. The owner saw "I
#: don't have access to your contacts" while 1,386 contacts sat in the store.
#:
#: Enrichment-only scopes are deliberately absent — `contacts:resolve` is not an
#: answer surface and must never be a snap target.
_CONCEPT_HINTS: Dict[str, tuple] = {
    "relationship_context:read": ("social", "graph", "friend", "friends", "relationship",
                                  "relationships", "people", "person", "contact", "contacts",
                                  "circle", "network", "family", "close"),
    "health:read": ("health", "wellbeing", "wellness", "mood", "medication", "medications",
                    "medical", "sleep", "symptom"),
    "schedule:read": ("calendar", "schedule", "event", "events", "meeting", "meetings"),
    "availability:read": ("availability", "available", "free", "busy"),
    "messages:read": ("message", "messages", "text", "texts", "imessage", "sms",
                      "conversation", "conversations", "thread"),
    "ai_conversations:read": ("chatgpt", "assistant", "ai"),
    "activity:read": ("activity", "browsing", "browser", "web", "website", "visits", "history"),
    "work_context:read": ("work", "job", "career", "employer", "employment", "project",
                          "projects", "role", "skills"),
    "resources:read": ("finance", "financial", "money", "spending", "transaction",
                       "transactions", "budget"),
    "places:read": ("place", "places", "location", "locations", "travel", "city", "cities"),
    "public_bio:read": ("bio", "profile", "resume", "credential", "credentials"),
}


def _tokens(text: str) -> set:
    return {t for t in re.split(r"[^a-z0-9]+", str(text or "").lower()) if t}


def suggest_scope_id(scope_id: str, query_text: str = "") -> str | None:
    """The real scope a caller most likely meant, or None.

    Deliberately NOT part of `resolve_scope_manifest`: that function is the
    authoritative boundary and stays strict. This is client forgiveness, applied
    one layer out and only where no grant constrains the scope, so a grantee still
    has to name what they were granted exactly.
    """
    sid = (scope_id or "").strip()
    if not sid or get_scope_entry(sid) is not None:
        return None

    # `enrichment_only` scopes (contacts:resolve) resolve identifiers for other
    # lanes and are not answer surfaces, so they are never a snap target — without
    # this the bare id "contacts" snapped straight onto contacts:resolve.
    valid = {
        str(e.get("scope_id")) for e in list_scopes()
        if e.get("scope_id") and not e.get("enrichment_only")
    }
    # 1) a bare id missing its action ("relationship_context" -> ":read")
    for candidate in sorted(valid):
        if candidate.split(":", 1)[0] == sid.split(":", 1)[0]:
            return candidate
    # 2) a near-miss spelling of a real id
    close = difflib.get_close_matches(sid, sorted(valid), n=1, cutoff=0.8)
    if close:
        return close[0]
    # 3) an invented id whose WORDS name a real subject ("social_graph")
    words = _tokens(sid) | _tokens(query_text)
    best, best_hits = None, 0
    for candidate in sorted(valid):
        hits = len(words & set(_CONCEPT_HINTS.get(candidate, ())))
        if hits > best_hits:
            best, best_hits = candidate, hits
    return best if best_hits else None
