"""Grant-scoped cache with stale-while-revalidate (plan §E).

A cross-request cache for filtered/minimized disclosures. Two hard invariants make it safe to
share beyond a single session:

  * **Isolation** — the cache key is the retrieval fingerprint (§B.4), which already encodes
    scope, mode, disclosure tier, grant identity, filters, and field transforms. So an entry
    can never be served across grants or tiers. Every entry also records its grant_id and
    scope_id for targeted invalidation.
  * **Revocation** — `invalidate_grant` drops every entry for a grant instantly, so a revoked
    grant can never be served a stale result.

Freshness follows the data_health_version: a matching version is a fresh hit; a changed
version means the underlying data moved, and the entry is served STALE only within a per-scope
staleness budget ("send the old, compute the new"), else it's a miss (recompute synchronously).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

# Per-scope staleness budgets (seconds): how long a data-moved entry may still be served stale.
_STALENESS_BUDGETS = {
    "availability:read": 300,       # minutes — availability goes stale fast
    "messages:read": 3600,
    "ai_conversations:read": 3600,
    "relationship_context:read": 86400,  # a relationship summary is good for a day
    "activity:read": 3600,
}
_DEFAULT_STALENESS_BUDGET_S = 1800


def staleness_budget_for(scope_id: str) -> int:
    return _STALENESS_BUDGETS.get(str(scope_id or "").strip(), _DEFAULT_STALENESS_BUDGET_S)


@dataclass
class CacheEntry:
    value: Any
    grant_id: str
    disclosure_tier: str
    scope_id: str
    data_health_version: str
    created_at: float


@dataclass
class CacheResult:
    status: str  # "hit" | "stale" | "miss"
    value: Any = None
    age_s: float = 0.0
    needs_revalidate: bool = False

    @property
    def served(self) -> bool:
        return self.status in ("hit", "stale")


@dataclass
class CacheStats:
    hits: int = 0
    stale_serves: int = 0
    misses: int = 0
    staleness_age_s_at_serve: list = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        total = self.hits + self.stale_serves + self.misses
        return {
            "hits": self.hits,
            "stale_serves": self.stale_serves,
            "misses": self.misses,
            "hit_rate": (self.hits + self.stale_serves) / total if total else 0.0,
            "stale_serve_rate": self.stale_serves / total if total else 0.0,
            "staleness_age_p95_s": (
                sorted(self.staleness_age_s_at_serve)[int(0.95 * len(self.staleness_age_s_at_serve)) - 1]
                if self.staleness_age_s_at_serve else 0.0
            ),
        }


class GrantScopedCache:
    def __init__(self) -> None:
        self._entries: Dict[str, CacheEntry] = {}
        self._by_grant: Dict[str, Set[str]] = {}
        self._by_scope: Dict[str, Set[str]] = {}
        self.stats = CacheStats()

    def put(
        self,
        *,
        fingerprint: str,
        value: Any,
        grant_id: str,
        disclosure_tier: str,
        scope_id: str,
        data_health_version: str,
        now: Optional[float] = None,
    ) -> None:
        entry = CacheEntry(
            value=value, grant_id=grant_id, disclosure_tier=disclosure_tier, scope_id=scope_id,
            data_health_version=data_health_version, created_at=now if now is not None else time.time(),
        )
        self._entries[fingerprint] = entry
        self._by_grant.setdefault(grant_id, set()).add(fingerprint)
        self._by_scope.setdefault(scope_id, set()).add(fingerprint)

    def get_swr(
        self,
        *,
        fingerprint: str,
        current_data_health_version: str,
        scope_id: str = "",
        now: Optional[float] = None,
    ) -> CacheResult:
        """Serve fresh, serve stale (within the scope's budget) with a revalidation flag, or miss."""
        entry = self._entries.get(fingerprint)
        if entry is None:
            self.stats.misses += 1
            return CacheResult(status="miss")

        clock = now if now is not None else time.time()
        age = max(0.0, clock - entry.created_at)

        if entry.data_health_version == current_data_health_version:
            self.stats.hits += 1
            return CacheResult(status="hit", value=entry.value, age_s=age)

        # Data moved: serve stale only within the per-scope budget, and flag revalidation.
        budget = staleness_budget_for(scope_id or entry.scope_id)
        if age <= budget:
            self.stats.stale_serves += 1
            self.stats.staleness_age_s_at_serve.append(age)
            return CacheResult(status="stale", value=entry.value, age_s=age, needs_revalidate=True)

        self.stats.misses += 1
        return CacheResult(status="miss")

    def invalidate_grant(self, grant_id: str) -> int:
        """Revocation: drop every entry for a grant. Returns the count dropped."""
        keys = self._by_grant.pop(grant_id, set())
        for k in keys:
            entry = self._entries.pop(k, None)
            if entry is not None:
                self._by_scope.get(entry.scope_id, set()).discard(k)
        return len(keys)

    def invalidate_scope(self, scope_id: str) -> int:
        """Drop every entry for a scope (e.g. a data_health bump for that scope)."""
        keys = self._by_scope.pop(scope_id, set())
        for k in keys:
            entry = self._entries.pop(k, None)
            if entry is not None:
                self._by_grant.get(entry.grant_id, set()).discard(k)
        return len(keys)

    def __len__(self) -> int:
        return len(self._entries)
