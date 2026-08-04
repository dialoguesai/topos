"""A7 — D1.3 grantee × access-mode matrix: denial≡absence at summary/inference/raw.

Seeded: off-list person empty at every tier under ceiling=raw grant.
Live (pytest.mark.live): full D13-GT scorecard + leak_delta == 0.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path

import pytest

from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.pipeline import QueryPipelineOrchestrator
from topos.storage.adapters.factory import AdapterFactory

pytestmark = [pytest.mark.check("C-quality-d1-hole-punchers")]

LIVE_DB = Path(os.environ.get("TOPOS_DATABASE_PATH", Path.home() / ".topos" / "database.db"))
FABRICATED = "Zephyrine Quaddlebock"
TIERS = ("summary", "inference", "raw")


def _seed_entities_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE entities (
            entity_id TEXT PRIMARY KEY,
            entity_type TEXT NOT NULL,
            canonical_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            aliases_json TEXT NOT NULL DEFAULT '[]',
            identifiers_json TEXT NOT NULL DEFAULT '[]',
            embedding_blob BLOB,
            is_self INTEGER NOT NULL DEFAULT 0,
            contact_id TEXT,
            first_seen TEXT,
            last_seen TEXT,
            mention_count INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    rows = [
        ("ent_maya_live", "person", "Maya Chen", "maya chen", 12),
        ("ent_alex_live", "person", "Alex Rivera", "alex rivera", 10),
        ("ent_jordan_live", "person", "Jordan Lee", "jordan lee", 8),
    ]
    for eid, etype, name, norm, mentions in rows:
        conn.execute(
            """
            INSERT INTO entities (
                entity_id, entity_type, canonical_name, normalized_name,
                aliases_json, mention_count, is_self
            ) VALUES (?, ?, ?, ?, '[]', ?, 0)
            """,
            (eid, etype, name, norm, mentions),
        )
    conn.commit()
    conn.close()


def _n_items(resp: dict) -> int:
    pr = resp.get("public_result") or {}
    for k in ("summaries", "rows", "items", "scores"):
        v = pr.get(k)
        if isinstance(v, list):
            return len(v)
    return 0


@pytest.mark.asyncio
async def test_seeded_grantee_tier_matrix_denial_equals_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Grantee off-list ≈ fabricated empty at summary / inference / raw."""
    monkeypatch.setenv("TOPOS_SELECTOR_ENFORCEMENT", "1")
    db = tmp_path / "d13_tier.db"
    _seed_entities_db(db)
    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(db))

    adapters = AdapterFactory.create("local_database", db_path=db)
    orch = QueryPipelineOrchestrator(adapters=adapters)
    # Ceiling raw so all three request modes hit the selector, not mode deny.
    grant = {
        "filter_manifest": {"access_mode_ceiling": "raw"},
        "accessible_entity_ids": ["ent_maya_live"],
    }
    scope = "relationship_context:read"
    manifest = resolve_scope_manifest(scope, filter_manifest=grant)

    async def _run(q: str, mode: str) -> dict:
        return await orch.execute(
            query_text=q,
            scope_id=scope,
            access_mode=mode,
            manifest=manifest,
            filter_manifest=grant,
            query_session_id=f"d13t-{uuid.uuid4().hex[:8]}",
            requester_id="grantee-x",
            owner_id="owner",
            is_grantee_request=True,
        )

    leaks = []
    for mode in TIERS:
        off = await _run("Tell me everything about Alex Rivera", mode)
        fab = await _run(f"Tell me everything about {FABRICATED}", mode)
        assert _n_items(off) == 0, (mode, off)
        assert _n_items(fab) == 0, (mode, fab)
        leaks.append(0.0 if _n_items(off) == _n_items(fab) == 0 else 1.0)

    assert max(leaks) - min(leaks) == 0.0


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(not LIVE_DB.exists(), reason=f"live db missing: {LIVE_DB}")
async def test_live_d13_grantee_tier_scorecard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live DB: D13-GT cells all 1.0 and leak_delta across tiers == 0."""
    import sys

    qq_engine = Path(__file__).resolve().parents[1] / "gap" / "qq" / "engine"
    sys.path.insert(0, str(qq_engine))
    from d1_hole_puncher_cases import (  # type: ignore[import-not-found]
        D1_3_GRANTEE_TIER_CASES,
        d13_leak_delta_by_tier,
    )
    from selector_eval_cases import (  # type: ignore[import-not-found]
        FABRICATED_PERSON,
        grant_filters_with_entity_ids,
        pick_grant_allowed_entity,
        score_selector,
    )

    monkeypatch.setenv("TOPOS_SELECTOR_ENFORCEMENT", "1")
    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(LIVE_DB))

    adapters = AdapterFactory.create("local_database", db_path=LIVE_DB)
    orch = QueryPipelineOrchestrator(adapters=adapters)
    ro = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    try:
        protected = []
        for case in D1_3_GRANTEE_TIER_CASES:
            _, name = case.query(ro)
            protected.append(name)
        allowed = pick_grant_allowed_entity(ro, exclude_names=protected)
        assert allowed is not None
        allowed_id, allowed_name = allowed
        grant = grant_filters_with_entity_ids([allowed_id], access_mode_ceiling="raw")

        async def _grantee(qtext: str, scope: str, mode: str) -> dict:
            manifest = resolve_scope_manifest(scope, filter_manifest=grant)
            return await orch.execute(
                query_text=qtext,
                scope_id=scope,
                access_mode=mode,
                manifest=manifest,
                filter_manifest=grant,
                query_session_id=f"d13-live-{uuid.uuid4().hex[:8]}",
                requester_id="grantee-x",
                owner_id="owner",
                is_grantee_request=True,
            )

        scores = []
        for case in D1_3_GRANTEE_TIER_CASES:
            qtext, name = case.query(ro)
            resp = await _grantee(qtext, case.scope_id, case.access_mode)
            absent = await _grantee(
                qtext.replace(name, FABRICATED_PERSON),
                case.scope_id,
                case.access_mode,
            )
            scored = score_selector(case, name, resp, absent)
            scored["access_mode"] = case.access_mode
            scored["grant"] = {
                "accessible_entity_ids": [allowed_id],
                "allowed_entity_name": allowed_name,
            }
            scores.append(scored)

        leak = d13_leak_delta_by_tier(scores)
        assert all(float(s["composite"]) >= 1.0 for s in scores), json.dumps(
            scores, indent=2
        )
        assert leak["leak_delta"] == 0.0, json.dumps(leak, indent=2)
        assert leak["n_tiers_measured"] == 3
    finally:
        ro.close()
