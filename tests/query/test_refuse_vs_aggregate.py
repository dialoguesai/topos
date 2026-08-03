"""A8 / A2.3 — refuse-vs-aggregate (early).

Cohort-permitted / aggregate-only asks under an active entity selector →
non-entity-specific aggregate. Named-person asks still denial≡absence.

C1 membership resolvers widen named allow-list separately; these cases keep
cohort tokens on grants without contact-linked seed rows so named asks stay
denied (aggregate utility + denial≡absence regression).
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.pipeline import (
    QueryPipelineOrchestrator,
    _cohort_aggregate_permitted,
    _is_aggregate_only_ask,
    _looks_like_aggregate_ask,
    _selector_cohort_aggregate_allowed,
    _selector_unauthorized,
)
from topos.storage.adapters.factory import AdapterFactory

pytestmark = [pytest.mark.check("C-quality-selector-entity-grant")]

FABRICATED = "Zephyrine Quaddlebock"


def _manifest(*, ids=None, cohorts=None, active=True):
    return SimpleNamespace(
        accessible_entity_ids=list(ids or []),
        accessible_entity_cohorts=list(cohorts or []),
        entity_selector_policy_active=active,
    )


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


def test_aggregate_ask_detection() -> None:
    assert _looks_like_aggregate_ask("How many people do I message each week?") is True
    assert _looks_like_aggregate_ask("how often do I message across my contacts?") is True
    assert _looks_like_aggregate_ask("Tell me everything about Maya Chen") is False


def test_cohort_token_permits_aggregate() -> None:
    m = _manifest(ids=[], cohorts=["contacts"], active=True)
    assert _cohort_aggregate_permitted(m) is True
    assert _cohort_aggregate_permitted(_manifest(cohorts=["none"], active=True)) is False
    assert _cohort_aggregate_permitted(_manifest(cohorts=["not_a_real_cohort"], active=True)) is False


def test_aggregate_only_rejects_named_person_shape() -> None:
    with patch("topos.features.entities.linking.link_query_entities", return_value=[]):
        assert (
            _is_aggregate_only_ask(object(), "How many people do I message each week?")
            is True
        )
        assert (
            _is_aggregate_only_ask(
                object(), "How often does Maya Chen message me?"
            )
            is False
        )


def test_named_person_still_unauthorized_under_empty_cohort_ids() -> None:
    """A7 regression: cohort token alone without resolved membership must not false-permit."""
    manifest = _manifest(ids=[], cohorts=["contacts"], active=True)
    with patch(
        "topos.features.entities.linking.link_query_entities",
        return_value=[{"entity_id": "ent_maya", "entity_type": "person"}],
    ):
        assert _selector_unauthorized(object(), "Tell me about Maya Chen", manifest) is True
        # Aggregate path must not fire when unauthorized (caller order), and
        # person-shaped/linked asks are not aggregate-only.
        assert (
            _selector_cohort_aggregate_allowed(
                object(), "Tell me about Maya Chen", manifest
            )
            is False
        )


def test_aggregate_allowed_under_cohort_or_active_selector() -> None:
    cohort = _manifest(ids=[], cohorts=["contacts"], active=True)
    enums = _manifest(ids=["ent_maya"], cohorts=[], active=True)
    inactive = _manifest(ids=[], cohorts=["contacts"], active=False)
    with patch("topos.features.entities.linking.link_query_entities", return_value=[]):
        q = "How many people do I message each week?"
        assert _selector_cohort_aggregate_allowed(object(), q, cohort) is True
        assert _selector_cohort_aggregate_allowed(object(), q, enums) is True
        assert _selector_cohort_aggregate_allowed(object(), q, inactive) is False


def _n_items(resp: dict) -> int:
    pr = resp.get("public_result") or {}
    for k in ("summaries", "rows", "items", "scores"):
        v = pr.get(k)
        if isinstance(v, list):
            return len(v)
    return 0


def _summary_blob(resp: dict) -> str:
    pr = resp.get("public_result") or {}
    parts = []
    for k in ("summaries", "rows", "items", "scores"):
        v = pr.get(k)
        if isinstance(v, list):
            parts.append(str(v))
    return " ".join(parts).lower()


@pytest.mark.asyncio
async def test_seeded_cohort_aggregate_vs_named_person_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AFTER path: aggregate → non-entity rollup; named/fab → empty twin."""
    monkeypatch.setenv("TOPOS_SELECTOR_ENFORCEMENT", "1")
    db = tmp_path / "a8_agg.db"
    _seed_entities_db(db)
    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(db))

    adapters = AdapterFactory.create("local_database", db_path=db)
    orch = QueryPipelineOrchestrator(adapters=adapters)
    grant = {
        "filter_manifest": {"access_mode_ceiling": "summary"},
        "accessible_entity_ids": [],
        "accessible_entity_cohorts": ["contacts"],
    }
    manifest = resolve_scope_manifest("messages:read", filter_manifest=grant)
    assert manifest.entity_selector_policy_active is True
    # Seed has no contact_id rows → contacts cohort resolves empty; named stay denied.
    assert manifest.accessible_entity_ids == []
    assert "contacts" in manifest.accessible_entity_cohorts

    async def _run(q: str) -> dict:
        return await orch.execute(
            query_text=q,
            scope_id="messages:read",
            access_mode="summary",
            manifest=manifest,
            filter_manifest=grant,
            query_session_id=f"a8-{uuid.uuid4().hex[:8]}",
            requester_id="grantee-x",
            owner_id="owner",
            is_grantee_request=True,
        )

    agg = await _run("How many people do I message each week?")
    person = await _run("Tell me everything about Maya Chen")
    fab = await _run(f"Tell me everything about {FABRICATED}")
    named_agg = await _run("How often does Maya Chen message me?")

    # Aggregate utility path
    assert _n_items(agg) >= 1, agg
    blob = _summary_blob(agg)
    assert "cohort aggregate" in blob or "non-entity-specific" in blob
    assert "maya" not in blob
    assert "alex" not in blob
    assert "jordan" not in blob

    # Individual denial≡absence (A7 must not regress)
    assert _n_items(person) == 0, person
    assert _n_items(fab) == 0, fab
    assert (person.get("turn_outcome") or "") == (fab.get("turn_outcome") or "") or (
        _n_items(person) == _n_items(fab) == 0
    )

    # Aggregate cue + named person → still refuse (never rewrite to aggregate)
    assert _n_items(named_agg) == 0, named_agg


@pytest.mark.asyncio
async def test_seeded_aggregate_under_enums_without_cohort_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enums-first path: active allow-list + aggregate-only → aggregate without cohort token."""
    monkeypatch.setenv("TOPOS_SELECTOR_ENFORCEMENT", "1")
    db = tmp_path / "a8_enums.db"
    _seed_entities_db(db)
    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(db))

    adapters = AdapterFactory.create("local_database", db_path=db)
    orch = QueryPipelineOrchestrator(adapters=adapters)
    grant = {
        "filter_manifest": {"access_mode_ceiling": "summary"},
        "accessible_entity_ids": ["ent_maya_live"],
    }
    manifest = resolve_scope_manifest("messages:read", filter_manifest=grant)

    async def _run(q: str) -> dict:
        return await orch.execute(
            query_text=q,
            scope_id="messages:read",
            access_mode="summary",
            manifest=manifest,
            filter_manifest=grant,
            query_session_id=f"a8e-{uuid.uuid4().hex[:8]}",
            requester_id="grantee-x",
            owner_id="owner",
            is_grantee_request=True,
        )

    agg = await _run("How many people do I message each week?")
    off = await _run("Tell me everything about Alex Rivera")

    assert _n_items(agg) >= 1, agg
    assert "maya" not in _summary_blob(agg)
    assert _n_items(off) == 0, off
