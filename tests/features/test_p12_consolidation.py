"""P12 tests: consolidation sweep, review approve/dismiss, curation API."""

from __future__ import annotations

import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from topos.features.entities.consolidation import (
    list_review,
    propose_merges,
    resolve_review,
)
from topos.features.entities.resolver import EntityResolver
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "consolidation.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _mk_entity(conn, name, etype="person", mentions=5, contact=None):
    resolver = EntityResolver(conn)
    entity_id = resolver._create_entity(name, etype, contact_id=contact)
    conn.execute(
        "UPDATE entities SET mention_count=? WHERE entity_id=?", (mentions, entity_id)
    )
    conn.commit()
    return entity_id


class TestSweep:
    def test_nickname_prefix_proposed(self, conn) -> None:
        jon = _mk_entity(conn, "Jon", mentions=65)
        jonathan = _mk_entity(conn, "Jonathan", mentions=54)
        result = propose_merges(conn, use_embeddings=False)
        assert result["prefix"] == 1

        items = list_review(conn)
        assert len(items) == 1
        item = items[0]
        assert item["reason"].startswith("prefix:")
        # longer name is the keep target
        assert item["candidate"]["canonical_name"] == "Jonathan"
        assert item["subject"]["canonical_name"] == "Jon"

    def test_contact_wins_merge_direction(self, conn) -> None:
        _mk_entity(conn, "Jonathan Smith", mentions=3, contact="c-1")
        _mk_entity(conn, "Jon", mentions=80)
        propose_merges(conn, use_embeddings=False)
        items = list_review(conn)
        assert items and items[0]["candidate"]["canonical_name"] == "Jonathan Smith"

    def test_fuzzy_band_proposed_not_automerged(self, conn) -> None:
        _mk_entity(conn, "Jonathan Marchetti", mentions=5)
        _mk_entity(conn, "Jonathan Marchesi", mentions=5)
        result = propose_merges(conn, use_embeddings=False)
        assert result["fuzzy"] == 1

    def test_different_types_never_paired(self, conn) -> None:
        _mk_entity(conn, "Austin Barbaro", "person", mentions=5)
        _mk_entity(conn, "Austin", "place", mentions=40)
        result = propose_merges(conn, use_embeddings=False)
        assert result["total"] == 0

    def test_dismissed_never_reproposed(self, conn) -> None:
        _mk_entity(conn, "Jon", mentions=65)
        _mk_entity(conn, "Jonathan", mentions=54)
        propose_merges(conn, use_embeddings=False)
        review_id = list_review(conn)[0]["review_id"]
        resolve_review(conn, review_id, action="dismiss")

        result = propose_merges(conn, use_embeddings=False)
        assert result["total"] == 0, "dismissed pair re-proposed"
        assert list_review(conn) == []

    def test_sweep_idempotent(self, conn) -> None:
        _mk_entity(conn, "Jon", mentions=65)
        _mk_entity(conn, "Jonathan", mentions=54)
        propose_merges(conn, use_embeddings=False)
        propose_merges(conn, use_embeddings=False)
        assert len(list_review(conn)) == 1


class TestApprove:
    def test_approve_merges_with_provenance(self, conn) -> None:
        resolver = EntityResolver(conn)
        jon = _mk_entity(conn, "Jon", mentions=5)
        jonathan = _mk_entity(conn, "Jonathan", mentions=5)
        resolver.record_mention(jon, record_id="m1", surface_text="Jon")
        resolver.record_mention(jonathan, record_id="m2", surface_text="Jonathan")
        conn.commit()
        propose_merges(conn, use_embeddings=False)
        review_id = list_review(conn)[0]["review_id"]

        result = resolve_review(conn, review_id, action="approve")
        assert result["status"] == "approved"

        assert conn.execute(
            "SELECT COUNT(*) FROM entities WHERE entity_id=?", (jon,)
        ).fetchone()[0] == 0
        mentions = conn.execute(
            "SELECT COUNT(*) FROM entity_mentions WHERE entity_id=?", (jonathan,)
        ).fetchone()[0]
        assert mentions == 2, "absorbed mentions lost"
        # alias preserved for future resolution
        aliases = conn.execute(
            "SELECT aliases_json FROM entities WHERE entity_id=?", (jonathan,)
        ).fetchone()[0]
        assert "Jon" in aliases

    def test_approve_marks_overlapping_reviews_stale(self, conn) -> None:
        _mk_entity(conn, "Jon", mentions=10)
        _mk_entity(conn, "Jonathan", mentions=10)
        _mk_entity(conn, "Jonny", mentions=10)
        propose_merges(conn, use_embeddings=False)
        items = list_review(conn)
        assert len(items) >= 2
        resolve_review(conn, items[0]["review_id"], action="approve")
        # remaining pending reviews must not reference a deleted entity
        for item in list_review(conn):
            assert item["candidate"] is not None

    def test_double_action_rejected(self, conn) -> None:
        _mk_entity(conn, "Jon", mentions=10)
        _mk_entity(conn, "Jonathan", mentions=10)
        propose_merges(conn, use_embeddings=False)
        review_id = list_review(conn)[0]["review_id"]
        resolve_review(conn, review_id, action="dismiss")
        with pytest.raises(ValueError, match="already dismissed"):
            resolve_review(conn, review_id, action="approve")


@pytest.fixture()
def api_app(conn, monkeypatch):
    import topos.core.state as state_mod

    monkeypatch.setattr(state_mod, "get_db_connection", lambda: conn)
    from topos.app import app
    from topos.auth import require_api_key

    async def _fake_key():
        return "test-key"

    app.dependency_overrides[require_api_key] = _fake_key
    yield app, conn
    app.dependency_overrides.pop(require_api_key, None)


async def _req(app, method: str, path: str):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.request(method, path, headers={"Authorization": "Bearer test-key"})


@pytest.mark.asyncio
async def test_review_api_flow(api_app) -> None:
    app, conn = api_app
    _mk_entity(conn, "Jon", mentions=65)
    _mk_entity(conn, "Jonathan", mentions=54)

    resp = await _req(app, "POST", "/v1/signal/entity-review/sweep")
    assert resp.status_code == 200 and resp.json()["total"] == 1

    resp = await _req(app, "GET", "/v1/signal/entity-review")
    items = resp.json()["items"]
    assert len(items) == 1
    review_id = items[0]["review_id"]

    resp = await _req(app, "POST", f"/v1/signal/entity-review/{review_id}/approve")
    assert resp.status_code == 200 and resp.json()["status"] == "approved"

    resp = await _req(app, "GET", "/v1/signal/entity-review")
    assert resp.json()["items"] == []

    resp = await _req(app, "POST", "/v1/signal/entity-review/rev_missing/approve")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_entity_exclude_api(api_app) -> None:
    app, conn = api_app
    entity_id = _mk_entity(conn, "LL", "topic", mentions=59)
    resp = await _req(app, "POST", f"/v1/signal/entities/{entity_id}/exclude")
    assert resp.status_code == 200
    body = resp.json()
    assert body["entity_found"] is True
    assert conn.execute(
        "SELECT COUNT(*) FROM entities WHERE entity_id=?", (entity_id,)
    ).fetchone()[0] == 0
    # tombstoned: resolver refuses to re-track
    from topos.features.entities.resolver import EntityResolver

    with pytest.raises(ValueError, match="excluded by owner"):
        EntityResolver(conn).resolve("LL", entity_type="topic")


class TestResolutionReviewApproval:
    """Resolver-queued reviews (kind='resolution', surface-only, NO subject id)
    filled half the live queue and 400'd on approve — 'Same — merge' silently
    did nothing in the UI."""

    def _queue_resolution(self, conn, surface, candidate_id):
        review_id = f"rev_test_{surface.lower()}"
        conn.execute(
            "INSERT INTO entity_review (review_id, surface_text, candidate_entity_id, score, status, kind) "
            "VALUES (?, ?, ?, 0.85, 'pending', 'resolution')",
            (review_id, surface, candidate_id),
        )
        conn.commit()
        return review_id

    def test_approve_resolution_merges_surface_entity(self, conn) -> None:
        matteo = _mk_entity(conn, "Matteo Iraggi", mentions=10)
        matt = _mk_entity(conn, "Matt", mentions=3)  # the surface got its own entity
        review_id = self._queue_resolution(conn, "Matt", matteo)

        result = resolve_review(conn, review_id, action="approve")
        assert result["status"] == "approved"
        assert result["kept"] == matteo
        # the surface's entity was absorbed
        assert conn.execute(
            "SELECT 1 FROM entities WHERE entity_id=?", (matt,)
        ).fetchone() is None

    def test_approve_resolution_without_surface_entity_adds_alias(self, conn) -> None:
        samer = _mk_entity(conn, "Samer Salem", mentions=8)
        review_id = self._queue_resolution(conn, "Same", samer)

        result = resolve_review(conn, review_id, action="approve")
        assert result["status"] == "approved"
        aliases = conn.execute(
            "SELECT aliases_json FROM entities WHERE entity_id=?", (samer,)
        ).fetchone()[0]
        assert "same" in str(aliases).lower()

    def test_dismiss_resolution_still_works(self, conn) -> None:
        yanan = _mk_entity(conn, "Yanan", mentions=4)
        review_id = self._queue_resolution(conn, "Yan", yanan)
        result = resolve_review(conn, review_id, action="dismiss")
        assert result["status"] == "dismissed"
