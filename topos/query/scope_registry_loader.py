"""Load wiki MVP scope registry (engine-side mirror of CP catalog)."""

from __future__ import annotations

import json
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
