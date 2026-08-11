"""P12 tests: consolidation sweep, review approve/dismiss, curation API."""

from __future__ import annotations

import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from topos.features.entities.consolidation import (
    count_review,
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
    entity_id = _mk_entity(conn, "Longform Learning", "topic", mentions=59)
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
        EntityResolver(conn).resolve("Longform Learning", entity_type="topic")


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


class TestOneRowPerDecision:
    """The queue asks a question once.

    ``_queue_review`` runs per mention, so a surface seen every week used to
    stack up hundreds of identical rows: the live queue held 8,122 rows for
    ~99 real decisions, the same four pairs repeating down the page, with the
    genuine merge candidates buried underneath.
    """

    def _duplicate_sightings(self, conn, surface, candidate_id, times):
        """What the pre-fix resolver left behind: one row per sighting."""
        for i in range(times):
            conn.execute(
                "INSERT INTO entity_review (review_id, surface_text, candidate_entity_id,"
                " score, status, kind, created_at) VALUES (?, ?, ?, 0.85, 'pending',"
                " 'resolution', ?)",
                (f"rev_dup_{i}", surface, candidate_id, f"2026-08-0{i + 1} 00:00:00"),
            )
        conn.commit()

    def test_resolver_queues_one_row_per_decision(self, conn) -> None:
        brooklyn = _mk_entity(conn, "Brooklyn", "place", mentions=18)
        resolver = EntityResolver(conn)
        for _ in range(50):
            resolver._queue_review("Williamsburg- Brooklyn", brooklyn, 0.85, "rec-1")
        conn.commit()

        rows = conn.execute("SELECT COUNT(*) FROM entity_review").fetchone()[0]
        assert rows == 1, "one question, one row"

    def test_answered_decision_is_never_asked_again(self, conn) -> None:
        """The owner's answer lives in their Topos and binds the resolver."""
        person = _mk_entity(conn, "Jonny Johnson", mentions=40)
        resolver = EntityResolver(conn)
        resolver._queue_review("Jonny", person, 0.85, "rec-1")
        conn.commit()

        review_id = list_review(conn)[0]["review_id"]
        resolve_review(conn, review_id, action="dismiss")

        # ingest keeps meeting the surface long after the owner ruled on it
        for _ in range(10):
            resolver._queue_review("Jonny", person, 0.85, "rec-2")
        conn.commit()
        assert list_review(conn) == [], "settled decision came back"

    def test_backlog_of_duplicates_lists_once(self, conn) -> None:
        brooklyn = _mk_entity(conn, "Brooklyn", "place", mentions=18)
        self._duplicate_sightings(conn, "Williamsburg- Brooklyn", brooklyn, 5)

        items = list_review(conn)
        assert len(items) == 1
        assert items[0]["occurrences"] == 5
        assert items[0]["review_id"] == "rev_dup_0", "oldest sighting represents the decision"
        assert count_review(conn) == 1

    def test_one_answer_settles_every_duplicate(self, conn) -> None:
        brooklyn = _mk_entity(conn, "Brooklyn", "place", mentions=18)
        self._duplicate_sightings(conn, "Williamsburg- Brooklyn", brooklyn, 5)

        result = resolve_review(conn, list_review(conn)[0]["review_id"], action="dismiss")
        assert result["also_settled"] == 4
        assert list_review(conn) == [], "duplicates of an answered question survived"
        pending = conn.execute(
            "SELECT COUNT(*) FROM entity_review WHERE status='pending'"
        ).fetchone()[0]
        assert pending == 0

    def test_approving_one_settles_every_duplicate(self, conn) -> None:
        samer = _mk_entity(conn, "Samer Salem", mentions=8)
        self._duplicate_sightings(conn, "Same", samer, 3)

        result = resolve_review(conn, list_review(conn)[0]["review_id"], action="approve")
        assert result["also_settled"] == 2
        assert list_review(conn) == []

    def test_unanswerable_rows_are_hidden_and_uncounted(self, conn) -> None:
        """Entities merge and vanish; reviews pointing at them can never be
        acted on, and used to consume the page before being filtered out."""
        conn.execute(
            "INSERT INTO entity_review (review_id, surface_text, candidate_entity_id,"
            " score, status, kind) VALUES ('rev_dead', 'Ghost', 'ent_gone', 1.0,"
            " 'pending', 'resolution')"
        )
        conn.commit()
        assert list_review(conn) == []
        assert count_review(conn) == 0

    def test_merge_row_with_vanished_subject_is_hidden(self, conn) -> None:
        keep = _mk_entity(conn, "Jonathan", mentions=10)
        conn.execute(
            "INSERT INTO entity_review (review_id, surface_text, candidate_entity_id,"
            " subject_entity_id, score, status, kind) VALUES ('rev_orphan', 'Jon', ?,"
            " 'ent_gone', 0.85, 'pending', 'merge')",
            (keep,),
        )
        conn.commit()
        assert list_review(conn) == [], "approving this would fail at merge time"

    def test_duplicate_people_outrank_surface_confirmations(self, conn) -> None:
        """A resolution row scores 1.0 when the resolver found several equally
        good matches — high score, low value. Ordering on score alone pushed
        the duplicate people this panel exists to catch off the visible page."""
        _mk_entity(conn, "Jon", mentions=65)
        _mk_entity(conn, "Jonathan", mentions=54)
        propose_merges(conn, use_embeddings=False)
        for i in range(30):
            entity_id = _mk_entity(conn, f"Surface Target{i}", mentions=10)
            conn.execute(
                "INSERT INTO entity_review (review_id, surface_text, candidate_entity_id,"
                " score, status, kind) VALUES (?, ?, ?, 1.0, 'pending', 'resolution')",
                (f"rev_s{i}", f"Ambiguous {i}", entity_id),
            )
        conn.commit()

        items = list_review(conn)
        assert items[0]["kind"] == "merge"
        assert items[0]["subject"]["canonical_name"] == "Jon"

    def test_total_reports_the_queue_not_the_page(self, conn) -> None:
        for i in range(5):
            entity_id = _mk_entity(conn, f"Person Number{i}", mentions=10)
            conn.execute(
                "INSERT INTO entity_review (review_id, surface_text, candidate_entity_id,"
                " score, status, kind) VALUES (?, ?, ?, 0.85, 'pending', 'resolution')",
                (f"rev_{i}", f"Surface {i}", entity_id),
            )
        conn.commit()

        assert len(list_review(conn, limit=2)) == 2
        assert count_review(conn) == 5


class TestReviewGates:
    """A question needs something real on both sides of it.

    Dedup fixed how often the queue asked; these fix what it asked about, and
    they are independent. Provenance covers the surface: ``resolve()`` serves
    ingest, which passes the ``record_id`` a surface came from, and derivation,
    which passes nothing because there is no record — the string came out of a
    cluster tag. On the live node 2,886 pending rows with no record_id carried
    18 low-value decisions, against 88 rows with one carrying 76. The mention
    bar covers the candidate: of those 76, only 24 proposed an entity that had
    ever been seen in the owner's data.

    The derivation call sites also declare themselves with
    ``resolve(..., queue_review=False)`` (see test_p3_entity_spine); these cover
    the row-level enforcement that makes the declaration unnecessary to trust.
    """

    def test_derived_surface_is_never_queued(self, conn) -> None:
        # Candidate well above the mention bar, so only provenance can block.
        topic = _mk_entity(conn, "Personal", "topic", mentions=40)
        EntityResolver(conn)._queue_review(
            "Personal Intelligence Infrastructure Quadrant", topic, 1.0, None
        )
        conn.commit()
        assert count_review(conn) == 0, "asked about a string the owner never wrote"

    def test_blank_record_id_counts_as_no_provenance(self, conn) -> None:
        topic = _mk_entity(conn, "Personal", "topic", mentions=40)
        EntityResolver(conn)._queue_review("Personal Intelligence", topic, 1.0, "   ")
        conn.commit()
        assert count_review(conn) == 0

    def test_mention_backed_surface_is_queued(self, conn) -> None:
        brooklyn = _mk_entity(conn, "Brooklyn", "place", mentions=18)
        EntityResolver(conn)._queue_review("Williamsburg- Brooklyn", brooklyn, 0.85, "rec-1")
        conn.commit()
        assert count_review(conn) == 1

    def test_never_mentioned_contact_is_not_asked_about(self, conn) -> None:
        """An address book is not a list of important people.

        Most imported contacts are someone met in passing years ago. A contact
        row says the owner once had a phone number, not that a name in today's
        data is that person — so this is "Alex Karp" (the Palantir CEO) offered
        as their contact "Alex", which is a first-name collision, not an
        identity match. 35 of the live queue's 39 contact questions were this.
        """
        alex = _mk_entity(conn, "Alex", mentions=0, contact="c-1")
        EntityResolver(conn)._queue_review("Alex Karp", alex, 0.85, "rec-1")
        conn.commit()
        assert count_review(conn) == 0, "address-book presence treated as evidence"

    def test_contact_earns_the_question_once_it_is_mentioned(self, conn) -> None:
        """The bar defers the question, it does not cancel it."""
        alex = _mk_entity(conn, "Alex", mentions=0, contact="c-1")
        resolver = EntityResolver(conn)
        for i in (1, 2):
            resolver.record_mention(alex, record_id=f"m{i}", surface_text="Alex")
        conn.commit()

        resolver._queue_review("Alexis", alex, 0.85, "rec-1")
        conn.commit()
        items = list_review(conn)
        assert len(items) == 1
        assert items[0]["candidate"]["canonical_name"] == "Alex"

    def test_single_mention_candidate_is_not_asked_about(self, conn) -> None:
        """One sighting is thin evidence for a near-irreversible merge."""
        code = _mk_entity(conn, "Code", "topic", mentions=1)
        EntityResolver(conn)._queue_review("Codex", code, 0.9, "rec-1")
        conn.commit()
        assert count_review(conn) == 0

    def test_derivation_lane_resolves_without_asking(self, conn) -> None:
        """End-to-end through the two callers.

        ``fact_materializer`` resolves each of a cluster's ``related_entities``
        with no record_id; ingest resolves a similar surface with one. Both get
        an entity id; only ingest gets a question.
        """
        # Two candidates, because a lone one would auto-merge: a token subset
        # scores 1.0, and the resolver only refuses to merge at that score when
        # more than one ties. The live "Personal" hub sat in exactly this
        # ambiguity, which is why it collected review rows instead of aliases.
        # Both are well-mentioned so the mention bar cannot be what decides.
        _mk_entity(conn, "Personal", "topic", mentions=40)
        _mk_entity(conn, "Personal Intelligence", "topic", mentions=12)
        conn.commit()
        resolver = EntityResolver(conn)

        derived, tier = resolver.resolve("Personal Intelligence Infrastructure")
        conn.commit()
        assert tier == "created" and derived
        assert count_review(conn) == 0, "derivation raised an owner question"

        resolver.resolve("Personal Intelligence Quadrant", record_id="msg-42")
        conn.commit()
        assert count_review(conn) == 1
        assert conn.execute(
            "SELECT record_id FROM entity_review WHERE status='pending'"
        ).fetchone()[0] == "msg-42"

    def test_derived_entity_churn_stops_asking(self, conn) -> None:
        """The 478-row loop: mint -> scrub -> mint again, one question per run.

        ``derived_scrub`` deletes a mention-less, contact-less entity, so the
        next derivation run cannot find it at the alias tier and mints a fresh
        one. Under the old rule each cycle left another review row; the gate
        makes the cycle silent even though it still churns entity ids.
        """
        from topos.features.lifecycle.derived_scrub import _delete_orphan_entities

        _mk_entity(conn, "Personal", "topic", mentions=40)
        _mk_entity(conn, "Personal Intelligence", "topic", mentions=12)
        resolver = EntityResolver(conn)
        minted = set()
        for _ in range(5):
            entity_id, _tier = resolver.resolve("Personal Intelligence Quadrant")
            conn.commit()
            minted.add(entity_id)
            _delete_orphan_entities(conn)
            conn.commit()

        assert len(minted) == 5, "expected the scrub/re-mint churn this guards against"
        assert count_review(conn) == 0
        assert conn.execute("SELECT COUNT(*) FROM entity_review").fetchone()[0] == 0


class TestNoCrossTypeMerge:
    """A merge across entity types is always a bad merge.

    The resolver scores candidates within one ``entity_type`` and gives the
    entity it mints that same type, so no proposal is ever cross-type at the
    point it is written. Approval reached past that: it re-found the surface's
    entity by normalized name alone. On the live node that meant "is
    'Unemployment Benefit Services' the topic 'unemployment'?" — 478 rows whose
    approval would have folded a 21-mention org into a mention-less topic hub.
    """

    def test_resolver_only_proposes_a_same_type_candidate(self, conn) -> None:
        hub = _mk_entity(conn, "unemployment", "topic", mentions=50)
        resolver = EntityResolver(conn)
        entity_id, tier = resolver.resolve("Unemployment Benefit Services", entity_type="org")
        conn.commit()

        assert tier == "created"
        assert conn.execute(
            "SELECT entity_type FROM entities WHERE entity_id=?", (entity_id,)
        ).fetchone()[0] == "org"
        assert conn.execute(
            "SELECT COUNT(*) FROM entity_review WHERE candidate_entity_id=?", (hub,)
        ).fetchone()[0] == 0, "org surface proposed against a topic"

    def test_approving_a_resolution_never_absorbs_another_type(self, conn) -> None:
        hub = _mk_entity(conn, "unemployment", "topic", mentions=6)
        org = _mk_entity(conn, "Unemployment Benefit Services", "org", mentions=21)
        conn.execute(
            "INSERT INTO entity_review (review_id, surface_text, candidate_entity_id,"
            " score, status, kind) VALUES ('rev_ubs', 'Unemployment Benefit Services', ?,"
            " 1.0, 'pending', 'resolution')",
            (hub,),
        )
        conn.commit()

        result = resolve_review(conn, "rev_ubs", action="approve")

        assert result["alias_added"] == "Unemployment Benefit Services"
        assert "absorbed" not in result
        row = conn.execute(
            "SELECT entity_type, mention_count FROM entities WHERE entity_id=?", (org,)
        ).fetchone()
        assert row == ("org", 21), "a real org was absorbed into a topic hub"

    def test_approving_a_resolution_still_merges_its_own_type(self, conn) -> None:
        """The type filter must not cost the case approval exists for."""
        matteo = _mk_entity(conn, "Matteo Iraggi", mentions=10)
        _mk_entity(conn, "Matt", "topic", mentions=4)  # unrelated same-name topic
        matt = _mk_entity(conn, "Matt", "person", mentions=3)
        conn.execute(
            "INSERT INTO entity_review (review_id, surface_text, candidate_entity_id,"
            " score, status, kind) VALUES ('rev_matt', 'Matt', ?, 0.85, 'pending',"
            " 'resolution')",
            (matteo,),
        )
        conn.commit()

        result = resolve_review(conn, "rev_matt", action="approve")
        assert result["absorbed"] == matt, "picked the wrong same-named entity"
        assert conn.execute(
            "SELECT COUNT(*) FROM entities WHERE entity_type='topic'"
        ).fetchone()[0] == 1, "the same-named topic was absorbed instead"

    def test_merge_entities_refuses_across_types(self, conn) -> None:
        """The backstop under every proposal path — this write cannot be undone."""
        hub = _mk_entity(conn, "unemployment", "topic", mentions=6)
        org = _mk_entity(conn, "Unemployment Benefit Services", "org", mentions=21)
        with pytest.raises(ValueError, match="across entity types"):
            EntityResolver(conn).merge_entities(hub, org)
        assert conn.execute(
            "SELECT COUNT(*) FROM entities WHERE entity_id=?", (org,)
        ).fetchone()[0] == 1


class TestProvenanceMigration:
    """One-time retirement of questions the owner never raised."""

    def _status(self, conn, review_id):
        return conn.execute(
            "SELECT status FROM entity_review WHERE review_id=?", (review_id,)
        ).fetchone()[0]

    def _row(self, conn, review_id, candidate, *, record_id=None, status="pending",
             kind="resolution"):
        conn.execute(
            "INSERT INTO entity_review (review_id, surface_text, candidate_entity_id,"
            " score, status, kind, record_id) VALUES (?, 'Surface', ?, 0.85, ?, ?, ?)",
            (review_id, candidate, status, kind, record_id),
        )

    def test_retires_derived_questions_only(self, conn) -> None:
        from topos.storage.db.migrations.entity_review_provenance_v1 import (
            apply_entity_review_provenance_v1_up,
        )

        hub = _mk_entity(conn, "Personal", "topic", mentions=0)
        thin = _mk_entity(conn, "Code", "topic", mentions=1)
        contact = _mk_entity(conn, "Alex", mentions=0, contact="c-1")
        attested = _mk_entity(conn, "Brooklyn", "place", mentions=18)

        self._row(conn, "rev_derived", hub, record_id=None)
        self._row(conn, "rev_blank", hub, record_id="  ")
        self._row(conn, "rev_thin", thin, record_id="msg-1")
        self._row(conn, "rev_contact", contact, record_id="msg-2")
        self._row(conn, "rev_attested", attested, record_id="msg-3")
        # a sweep proposal and an owner guard, neither of which this touches
        conn.execute(
            "INSERT INTO entity_review (review_id, surface_text, candidate_entity_id,"
            " subject_entity_id, score, status, kind) VALUES ('rev_merge', 'Personal', ?,"
            " ?, 0.85, 'pending', 'merge')",
            (hub, attested),
        )
        self._row(conn, "rev_guard", hub, status="approved", kind="no_bind")
        conn.commit()

        apply_entity_review_provenance_v1_up(conn)

        assert self._status(conn, "rev_derived") == "stale"
        assert self._status(conn, "rev_blank") == "stale"
        assert self._status(conn, "rev_thin") == "pending", "a thin candidate is not noise"
        assert self._status(conn, "rev_contact") == "pending", "contact question retired"
        assert self._status(conn, "rev_attested") == "pending"
        assert self._status(conn, "rev_merge") == "pending", "sweep proposal retired"
        assert self._status(conn, "rev_guard") == "approved", "owner guard touched"

    def test_restores_what_the_withdrawn_mention_bar_retired(self, conn) -> None:
        """``entity_review_quality_bar_v1`` gated on mention_count and reached a
        live node before the profile disproved it. Its casualties come back."""
        from topos.storage.db.migrations.entity_review_provenance_v1 import (
            apply_entity_review_provenance_v1_up,
        )

        thin = _mk_entity(conn, "Code", "topic", mentions=1)
        contact = _mk_entity(conn, "Alex", mentions=0, contact="c-1")
        self._row(conn, "rev_thin", thin, record_id="msg-1", status="stale")
        self._row(conn, "rev_contact", contact, record_id="msg-2", status="stale")
        # genuinely stale: the candidate was absorbed and no longer exists
        self._row(conn, "rev_dead", "ent_gone", record_id="msg-3", status="stale")
        conn.execute(
            "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id)"
            " VALUES ('entity_review_quality_bar_v1')"
        )
        conn.commit()

        apply_entity_review_provenance_v1_up(conn)

        assert self._status(conn, "rev_thin") == "pending"
        assert self._status(conn, "rev_contact") == "pending"
        assert self._status(conn, "rev_dead") == "stale", "unanswerable row reopened"
        assert count_review(conn) == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM wiki_schema_migrations WHERE migration_id="
            "'entity_review_quality_bar_v1'"
        ).fetchone()[0] == 0, "withdrawn migration left recorded"

    def test_repair_only_fires_on_a_node_that_ran_the_withdrawn_bar(self, conn) -> None:
        """Otherwise it would reopen what a later migration legitimately
        retired — 58 runs before 59 on a fresh node."""
        from topos.storage.db.migrations.entity_review_provenance_v1 import (
            apply_entity_review_provenance_v1_up,
        )

        thin = _mk_entity(conn, "Code", "topic", mentions=1)
        self._row(conn, "rev_thin", thin, record_id="msg-1", status="stale")
        conn.commit()

        apply_entity_review_provenance_v1_up(conn)
        assert self._status(conn, "rev_thin") == "stale"

    def test_retired_rows_leave_the_queue_without_being_deleted(self, conn) -> None:
        from topos.storage.db.migrations.entity_review_provenance_v1 import (
            apply_entity_review_provenance_v1_up,
        )

        hub = _mk_entity(conn, "Personal", "topic", mentions=0)
        for i in range(20):
            conn.execute(
                "INSERT INTO entity_review (review_id, surface_text, candidate_entity_id,"
                " score, status, kind) VALUES (?, ?, ?, 1.0, 'pending', 'resolution')",
                (f"rev_p{i}", f"Personal Surface {i}", hub),
            )
        conn.commit()
        assert count_review(conn) == 20

        apply_entity_review_provenance_v1_up(conn)

        assert count_review(conn) == 0
        assert list_review(conn) == []
        assert conn.execute("SELECT COUNT(*) FROM entity_review").fetchone()[0] == 20


class TestCandidateBarMigration:
    """One-time retirement of questions with nothing on the candidate side."""

    def _status(self, conn, review_id):
        return conn.execute(
            "SELECT status FROM entity_review WHERE review_id=?", (review_id,)
        ).fetchone()[0]

    def _row(self, conn, review_id, candidate, *, record_id="msg-1", kind="resolution",
             status="pending"):
        conn.execute(
            "INSERT INTO entity_review (review_id, surface_text, candidate_entity_id,"
            " score, status, kind, record_id) VALUES (?, 'Surface', ?, 0.85, ?, ?, ?)",
            (review_id, candidate, status, kind, record_id),
        )

    def test_retires_unseen_candidates_including_contacts(self, conn) -> None:
        from topos.storage.db.migrations.entity_review_candidate_bar_v1 import (
            apply_entity_review_candidate_bar_v1_up,
        )

        contact = _mk_entity(conn, "Alex", mentions=0, contact="c-1")
        thin = _mk_entity(conn, "Code", "topic", mentions=1)
        seen = _mk_entity(conn, "Brooklyn", "place", mentions=18)
        just_enough = _mk_entity(conn, "Domino park", "place", mentions=2)

        self._row(conn, "rev_contact", contact)
        self._row(conn, "rev_thin", thin)
        self._row(conn, "rev_seen", seen)
        self._row(conn, "rev_edge", just_enough)
        conn.execute(
            "INSERT INTO entity_review (review_id, surface_text, candidate_entity_id,"
            " subject_entity_id, score, status, kind) VALUES ('rev_merge', 'Alexis', ?,"
            " ?, 0.85, 'pending', 'merge')",
            (contact, seen),
        )
        self._row(conn, "rev_guard", contact, kind="no_bind", status="approved")
        conn.commit()

        apply_entity_review_candidate_bar_v1_up(conn)

        assert self._status(conn, "rev_contact") == "stale", "address book treated as evidence"
        assert self._status(conn, "rev_thin") == "stale"
        assert self._status(conn, "rev_seen") == "pending"
        assert self._status(conn, "rev_edge") == "pending", "the bar is >= 2, not > 2"
        assert self._status(conn, "rev_merge") == "pending", "sweep proposal retired"
        assert self._status(conn, "rev_guard") == "approved", "owner guard touched"

    def test_runs_after_the_provenance_repair_without_being_undone(self, conn) -> None:
        """The live sequence on a node that ran the withdrawn mention bar: 58
        restores its casualties, 59 re-retires the ones that deserve it."""
        from topos.storage.db.migrations.entity_review_candidate_bar_v1 import (
            apply_entity_review_candidate_bar_v1_up,
        )
        from topos.storage.db.migrations.entity_review_provenance_v1 import (
            apply_entity_review_provenance_v1_up,
        )

        contact = _mk_entity(conn, "Alex", mentions=0, contact="c-1")
        seen = _mk_entity(conn, "Brooklyn", "place", mentions=18)
        self._row(conn, "rev_contact", contact, status="stale")
        self._row(conn, "rev_seen", seen, status="stale")
        self._row(conn, "rev_derived", seen, record_id=None)
        conn.execute(
            "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id)"
            " VALUES ('entity_review_quality_bar_v1')"
        )
        conn.commit()

        apply_entity_review_provenance_v1_up(conn)
        apply_entity_review_candidate_bar_v1_up(conn)

        assert self._status(conn, "rev_seen") == "pending", "restored then lost again"
        assert self._status(conn, "rev_contact") == "stale"
        assert self._status(conn, "rev_derived") == "stale"
        assert count_review(conn) == 1


class TestDedupMigration:
    """One-time cleanup of the backlog the per-mention queueing left behind."""

    def test_collapses_backlog_to_decisions(self, conn) -> None:
        from topos.storage.db.migrations.entity_review_dedup_v1 import (
            apply_entity_review_dedup_v1_up,
        )

        brooklyn = _mk_entity(conn, "Brooklyn", "place", mentions=18)
        codex = _mk_entity(conn, "Code", "topic", mentions=1)
        for i in range(20):
            conn.execute(
                "INSERT INTO entity_review (review_id, surface_text, candidate_entity_id,"
                " score, status, kind, created_at) VALUES (?, ?, ?, 0.85, 'pending',"
                " 'resolution', ?)",
                (f"rev_w{i}", "Williamsburg- Brooklyn", brooklyn, f"2026-08-11 00:00:{i:02d}"),
            )
        for i in range(8):
            conn.execute(
                "INSERT INTO entity_review (review_id, surface_text, candidate_entity_id,"
                " score, status, kind) VALUES (?, 'Codex', ?, 0.9, 'pending', 'resolution')",
                (f"rev_c{i}", codex),
            )
        # rows aimed at an entity that no longer exists
        for i in range(30):
            conn.execute(
                "INSERT INTO entity_review (review_id, surface_text, candidate_entity_id,"
                " score, status, kind) VALUES (?, 'Ghost', 'ent_gone', 1.0, 'pending',"
                " 'resolution')",
                (f"rev_g{i}",),
            )
        # a decision the owner already answered, still being re-asked
        conn.execute(
            "INSERT INTO entity_review (review_id, surface_text, candidate_entity_id,"
            " score, status, kind) VALUES ('rev_done', 'Jonny', ?, 0.85, 'dismissed',"
            " 'resolution')",
            (brooklyn,),
        )
        conn.execute(
            "INSERT INTO entity_review (review_id, surface_text, candidate_entity_id,"
            " score, status, kind) VALUES ('rev_again', 'Jonny', ?, 0.85, 'pending',"
            " 'resolution')",
            (brooklyn,),
        )
        # an owner guard, which is not a question and must survive untouched
        conn.execute(
            "INSERT INTO entity_review (review_id, surface_text, candidate_entity_id,"
            " score, status, kind) VALUES ('rev_guard', 'jonny', 'ent_gone', 1.0,"
            " 'approved', 'no_bind')"
        )
        conn.commit()

        apply_entity_review_dedup_v1_up(conn)

        pending = conn.execute(
            "SELECT review_id FROM entity_review WHERE status='pending' ORDER BY review_id"
        ).fetchall()
        assert [r[0] for r in pending] == ["rev_c0", "rev_w0"], "kept one per open decision"
        assert conn.execute(
            "SELECT COUNT(*) FROM entity_review WHERE kind='no_bind'"
        ).fetchone()[0] == 1, "owner guard deleted"
        assert conn.execute(
            "SELECT COUNT(*) FROM entity_review WHERE review_id='rev_done'"
        ).fetchone()[0] == 1, "answer lost"
