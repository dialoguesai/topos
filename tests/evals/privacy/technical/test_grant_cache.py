"""§E/§F.8 — grant-scoped cache: isolation, revocation, stale-while-revalidate.

Isolation and revocation are the hard gates (a cross-request cache is only safe if it can
never serve across grants/tiers and a revoked grant is dropped instantly). SWR behaviour is
the performance feature: serve stale within a per-scope budget while revalidating.
"""

from __future__ import annotations

import pytest

from topos.query.fingerprint import compute_retrieval_fingerprint
from topos.query.grant_cache import GrantScopedCache, staleness_budget_for

pytestmark = [pytest.mark.private]


def _fp(*, grant, tier, scope="messages:read", mode="raw"):
    return compute_retrieval_fingerprint(
        scope_id=scope, access_mode=mode, data_health_version="v1",
        disclosure_tier=tier, grant_id=grant,
    )


def _put(cache, *, grant, tier, scope="messages:read", dhv="v1", value="X", now=1000.0):
    cache.put(fingerprint=_fp(grant=grant, tier=tier, scope=scope), value=value,
              grant_id=grant, disclosure_tier=tier, scope_id=scope, data_health_version=dhv, now=now)


# --- isolation (hard gate) -----------------------------------------------------------------

def test_two_grants_never_share_an_entry():
    cache = GrantScopedCache()
    _put(cache, grant="req-A", tier="default_disclosure", value="A-data")
    # req-B queries with an identical scope/mode but a different grant → different fingerprint → miss.
    r = cache.get_swr(fingerprint=_fp(grant="req-B", tier="default_disclosure"),
                      current_data_health_version="v1", scope_id="messages:read")
    assert r.status == "miss"


def test_two_tiers_never_share_an_entry():
    cache = GrantScopedCache()
    _put(cache, grant="req-A", tier="owner_raw", value="raw-data")
    r = cache.get_swr(fingerprint=_fp(grant="req-A", tier="default_disclosure"),
                      current_data_health_version="v1", scope_id="messages:read")
    assert r.status == "miss", "a lower tier must not read a higher tier's cached entry"


def test_same_grant_same_tier_hits():
    cache = GrantScopedCache()
    _put(cache, grant="req-A", tier="default_disclosure", value="A-data")
    r = cache.get_swr(fingerprint=_fp(grant="req-A", tier="default_disclosure"),
                      current_data_health_version="v1", scope_id="messages:read")
    assert r.status == "hit" and r.value == "A-data"


# --- revocation (hard gate) ----------------------------------------------------------------

def test_revocation_drops_entries_instantly():
    cache = GrantScopedCache()
    _put(cache, grant="req-A", tier="default_disclosure", value="A-data")
    assert cache.invalidate_grant("req-A") == 1
    r = cache.get_swr(fingerprint=_fp(grant="req-A", tier="default_disclosure"),
                      current_data_health_version="v1", scope_id="messages:read")
    assert r.status == "miss", "a revoked grant must never be served, stale or otherwise"


def test_scope_invalidation_drops_only_that_scope():
    cache = GrantScopedCache()
    _put(cache, grant="req-A", tier="default_disclosure", scope="messages:read", value="m")
    _put(cache, grant="req-A", tier="default_disclosure", scope="availability:read", value="a")
    assert cache.invalidate_scope("messages:read") == 1
    assert cache.get_swr(fingerprint=_fp(grant="req-A", tier="default_disclosure", scope="messages:read"),
                         current_data_health_version="v1").status == "miss"
    assert cache.get_swr(fingerprint=_fp(grant="req-A", tier="default_disclosure", scope="availability:read"),
                         current_data_health_version="v1", scope_id="availability:read").status == "hit"


# --- stale-while-revalidate ----------------------------------------------------------------

def test_fresh_data_health_is_a_hit():
    cache = GrantScopedCache()
    _put(cache, grant="g", tier="default_disclosure", dhv="v1", now=1000.0)
    r = cache.get_swr(fingerprint=_fp(grant="g", tier="default_disclosure"),
                      current_data_health_version="v1", scope_id="messages:read", now=1001.0)
    assert r.status == "hit" and r.needs_revalidate is False


def test_data_moved_within_budget_serves_stale_and_flags_revalidate():
    cache = GrantScopedCache()
    _put(cache, grant="g", tier="default_disclosure", scope="messages:read", dhv="v1", now=1000.0)
    # data_health bumped to v2; only 60s elapsed, well within the messages budget (3600s).
    r = cache.get_swr(fingerprint=_fp(grant="g", tier="default_disclosure"),
                      current_data_health_version="v2", scope_id="messages:read", now=1060.0)
    assert r.status == "stale"
    assert r.needs_revalidate is True
    assert r.value is not None  # served the old value


def test_data_moved_beyond_budget_is_a_miss():
    cache = GrantScopedCache()
    _put(cache, grant="g", tier="default_disclosure", scope="availability:read", dhv="v1", now=1000.0)
    # availability budget is 300s; 400s elapsed with moved data → recompute (miss).
    r = cache.get_swr(fingerprint=_fp(grant="g", tier="default_disclosure", scope="availability:read"),
                      current_data_health_version="v2", scope_id="availability:read", now=1400.0)
    assert r.status == "miss"


def test_per_scope_budgets():
    assert staleness_budget_for("availability:read") < staleness_budget_for("relationship_context:read")


def test_stats_report():
    cache = GrantScopedCache()
    _put(cache, grant="g", tier="default_disclosure", scope="messages:read", dhv="v1", now=1000.0)
    cache.get_swr(fingerprint=_fp(grant="g", tier="default_disclosure"), current_data_health_version="v1",
                  scope_id="messages:read", now=1001.0)  # hit
    cache.get_swr(fingerprint=_fp(grant="g", tier="default_disclosure"), current_data_health_version="v2",
                  scope_id="messages:read", now=1060.0)  # stale
    cache.get_swr(fingerprint=_fp(grant="missing", tier="default_disclosure"),
                  current_data_health_version="v1", scope_id="messages:read")  # miss
    stats = cache.stats.to_dict()
    assert stats["hits"] == 1 and stats["stale_serves"] == 1 and stats["misses"] == 1
    assert 0.0 < stats["hit_rate"] <= 1.0
