"""§G.1 (stats) — stat-insight rollups get sparser for a grantee.

A stat rollup ("most active 02:00–04:00 weekdays") can fingerprint a person even with no raw
rows. This drives the REAL dense retrieval path (`_load_stat_insight_items`, which applies the
owner-only gate) over a seeded signal_facts table, then composes it with the B.3 grantee scrub:

  owner              → sees the rollup (non-vacuous)
  grantee, no grant  → does NOT see it (gate blocks — owner-only by default)
  grantee, granted   → sees it, but PII in it is redacted (defense in depth)
"""

from __future__ import annotations

import json

import pytest

from topos.query.disclosure import DisclosureFilterPipeline
from topos.query.manifest import ScopeResolutionManifest
from topos.query.retrieval import _load_stat_insight_items
from topos.query.types import RetrievalBundle

from tests.gap.remediation.remediation_helpers import sqlite_conn

pytestmark = [pytest.mark.private]

RHYTHM_CANARY = "dense-rhythm-canary-0231"
PII_CANARY = "owner-canary@example-priv.net"
QUERY = "when am I most active late nights"


def _seed(conn):
    fact = {
        "fact_id": "stat:activity.hour_of_week.rhythm",
        "object_type": "stat_insight",
        "disclosure": "owner_only",
        "dimension": "Memory",
        "group_key": "hour_of_week",
        "record_id": "stat-rhythm",
        "tag": f"most active {RHYTHM_CANARY} late nights weekdays; reach owner at {PII_CANARY}",
    }
    conn.execute(
        "INSERT INTO signal_facts (fact_id, dimension, source_id, record_id, model, provider, payload_json, created_at) "
        "VALUES (?, 'Memory', 'stats', 'stat-rhythm', 'm', 'p', ?, '2026-06-01T00:00:00Z')",
        (fact["fact_id"], json.dumps(fact)),
    )
    conn.commit()


def _manifest(signal_objects=None):
    return ScopeResolutionManifest(
        scope_id="activity:read", primary_dimensions=["Memory"],
        canonical_tables=[], signal_objects=list(signal_objects or []),
    )


def _load(conn, *, tier, signal_objects):
    return _load_stat_insight_items(conn, QUERY, dimensions=["Memory"], disclosure_tier=tier, manifest=_manifest(signal_objects))


def test_owner_sees_the_rollup():
    conn = sqlite_conn()
    _seed(conn)
    items = _load(conn, tier="owner_raw", signal_objects=[])
    assert items, "owner must see the stat rollup (non-vacuous)"
    assert RHYTHM_CANARY in json.dumps(items)


def test_grantee_without_grant_is_blocked():
    conn = sqlite_conn()
    _seed(conn)
    items = _load(conn, tier="default_disclosure", signal_objects=[])
    assert items == [], "an owner-only stat rollup must not reach a grantee without the grant"


def test_grantee_with_grant_sees_it():
    conn = sqlite_conn()
    _seed(conn)
    items = _load(conn, tier="default_disclosure", signal_objects=["stat_insights"])
    assert items, "granting stat_insights must expose the rollup"
    assert RHYTHM_CANARY in json.dumps(items)


def test_granted_rollup_has_pii_scrubbed():
    """Defense in depth: even when the owner grants stat_insights, PII inside the rollup text is
    redacted by the B.3 grantee scrub."""
    conn = sqlite_conn()
    _seed(conn)
    items = _load(conn, tier="default_disclosure", signal_objects=["stat_insights"])
    # Feed the surfaced items through the disclosure pipeline as a grantee (as scores).
    filtered = DisclosureFilterPipeline().apply(
        RetrievalBundle(context_packet={"scores": items}),
        access_mode="inference", disclosure_tier="default_disclosure",
    )
    blob = json.dumps(filtered.context_packet)
    assert PII_CANARY not in blob, "PII inside a granted stat rollup must be redacted for a grantee"
    assert "[REDACTED_EMAIL]" in blob
