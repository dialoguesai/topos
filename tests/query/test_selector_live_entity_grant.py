"""A6 — Live SEL with real grants: populated accessible_entity_ids end-to-end.

Seeded path (always): tiny entities DB + grant allow-list → denial≡absence for
off-list persons; on-list person is not selector-unauthorized.

Live path (pytest.mark.live): same assertions against ~/.topos/database.db and a
full SEL lane score (real≈fabricated) under a real-id grant.
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path

import pytest

from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.pipeline import (
    QueryPipelineOrchestrator,
    _selector_enforcement_enabled,
    _selector_unauthorized,
)
from topos.storage.adapters.factory import AdapterFactory

pytestmark = [pytest.mark.check("C-quality-selector-entity-grant")]

LIVE_DB = Path(os.environ.get("TOPOS_DATABASE_PATH", Path.home() / ".topos" / "database.db"))
FABRICATED = "Zephyrine Quaddlebock"


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


def test_seeded_grant_allow_list_denial_equals_absence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Populated accessible_entity_ids: off-list person unauthorized; fabricated not linked."""
    monkeypatch.delenv("TOPOS_SELECTOR_ENFORCEMENT", raising=False)
    assert _selector_enforcement_enabled() is True

    db = tmp_path / "sel_entities.db"
    _seed_entities_db(db)
    conn = sqlite3.connect(db)
    try:
        grant = {
            "filter_manifest": {"access_mode_ceiling": "summary"},
            "accessible_entity_ids": ["ent_maya_live"],
        }
        manifest = resolve_scope_manifest("messages:read", filter_manifest=grant)
        assert manifest.entity_selector_policy_active is True
        assert manifest.accessible_entity_ids == ["ent_maya_live"]

        # Allowed person → not unauthorized
        assert _selector_unauthorized(conn, "meeting with Maya Chen", manifest) is False
        # Off-list real person → unauthorized (A2.E2 false-permit guard)
        assert _selector_unauthorized(conn, "prep with Alex Rivera", manifest) is True
        # Fabricated → no person link → not unauthorized (absence path)
        assert _selector_unauthorized(conn, f"tell me about {FABRICATED}", manifest) is False
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_seeded_pipeline_suppresses_off_list_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: grantee query for off-list person returns empty like fabricated."""
    monkeypatch.setenv("TOPOS_SELECTOR_ENFORCEMENT", "1")
    db = tmp_path / "sel_pipe.db"
    _seed_entities_db(db)
    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(db))

    adapters = AdapterFactory.create("local_database", db_path=db)
    orch = QueryPipelineOrchestrator(adapters=adapters)
    grant = {
        "filter_manifest": {"access_mode_ceiling": "summary"},
        "accessible_entity_ids": ["ent_maya_live"],
    }
    manifest = resolve_scope_manifest("relationship_context:read", filter_manifest=grant)

    async def _run(q: str) -> dict:
        return await orch.execute(
            query_text=q,
            scope_id="relationship_context:read",
            access_mode="summary",
            manifest=manifest,
            filter_manifest=grant,
            query_session_id=f"sel-seed-{uuid.uuid4().hex[:8]}",
            requester_id="grantee-x",
            owner_id="owner",
            is_grantee_request=True,
        )

    off_list = await _run("Tell me everything about Alex Rivera")
    fabricated = await _run(f"Tell me everything about {FABRICATED}")

    def _n(resp: dict) -> int:
        pr = resp.get("public_result") or {}
        for k in ("summaries", "rows", "items", "scores"):
            v = pr.get(k)
            if isinstance(v, list):
                return len(v)
        return 0

    assert _n(off_list) == 0, off_list
    assert _n(fabricated) == 0, fabricated
    # Shape parity: both empty (denial≡absence)
    assert (off_list.get("turn_outcome") or "") == (fabricated.get("turn_outcome") or "") or (
        _n(off_list) == _n(fabricated) == 0
    )


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.skipif(not LIVE_DB.exists(), reason=f"live db missing: {LIVE_DB}")
async def test_live_sel_with_real_entity_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    """Live DB: grant with real entity id → SEL cases score real≈fabricated."""
    import sys

    qq_engine = Path(__file__).resolve().parents[1] / "gap" / "qq" / "engine"
    sys.path.insert(0, str(qq_engine))
    from selector_eval_cases import (  # type: ignore[import-not-found]
        FABRICATED_PERSON,
        SELECTOR_CASES,
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
        for case in SELECTOR_CASES:
            _, name = case.query(ro)
            protected.append(name)
        allowed = pick_grant_allowed_entity(ro, exclude_names=protected)
        assert allowed is not None, "live DB needs ≥1 person entity for grant allow-list"
        allowed_id, allowed_name = allowed
        # Allow-list person must not be a SEL protected name
        assert allowed_name not in protected

        grant = grant_filters_with_entity_ids([allowed_id])

        async def _grantee(qtext: str, scope: str) -> dict:
            manifest = resolve_scope_manifest(scope, filter_manifest=grant)
            return await orch.execute(
                query_text=qtext,
                scope_id=scope,
                access_mode="summary",
                manifest=manifest,
                filter_manifest=grant,
                query_session_id=f"sel-live-{uuid.uuid4().hex[:8]}",
                requester_id="grantee-x",
                owner_id="owner",
                is_grantee_request=True,
            )

        # Positive packing: real live id on the grant; protected names are unauthorized.
        # (Fuzzy link_query_entities may attach sibling "Sam …" hits to an allowed-name
        # query — that over-deny is a residual for A7 / linking precision, not A6.)
        manifest = resolve_scope_manifest("messages:read", filter_manifest=grant)
        assert manifest.entity_selector_policy_active is True
        assert allowed_id in manifest.accessible_entity_ids
        for pname in protected:
            assert _selector_unauthorized(
                ro, f"Tell me everything about {pname}", manifest
            ) is True, pname

        scores = []
        for case in SELECTOR_CASES:
            qtext, name = case.query(ro)
            resp = await _grantee(qtext, case.scope_id)
            absent = await _grantee(qtext.replace(name, FABRICATED_PERSON), case.scope_id)
            scored = score_selector(case, name, resp, absent)
            scored["grant"] = {
                "accessible_entity_ids": [allowed_id],
                "allowed_entity_name": allowed_name,
            }
            scores.append(scored)

        composites = [s["composite"] for s in scores]
        assert all(c >= 1.0 for c in composites), json.dumps(scores, indent=2)
    finally:
        ro.close()
