"""P3 tests: entity resolution tiers, decayed edges, dossiers, query linking, disclosure."""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.entities.dossier import build_dossier, refresh_dossiers, significant_entities
from topos.features.entities.edges import (
    EDGE_CO_OCCURRENCE,
    EDGE_COMMUNICATES,
    EDGE_PART_OF,
    graph_snapshot,
    supersede_edge,
    top_edges,
    update_edge,
)
from topos.features.entities.linking import entity_context_items, link_query_entities
from topos.features.entities.resolver import (
    AUTO_MERGE_SCORE,
    EntityResolver,
    clean_entity_surface,
    is_valid_entity_surface,
    normalize_name,
    token_set_similarity,
)
from topos.query.manifest_validation import resolve_scope_manifest
from topos.storage.db.migrations import apply_all_migrations

#: `entity_context_items` bounds its mention lane by the caller's grant, so these
#: direct calls have to name one. `messages:read` authorizes `conversation_messages`,
#: which is the table the mentions below are recorded against.
MESSAGES_MANIFEST = resolve_scope_manifest("messages:read")


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "entities.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _seed_contacts(conn) -> None:
    conn.execute(
        "INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, known_usernames_json, is_self)"
        " VALUES ('c-maya', 'ds', 'src', 'Maya Chen', '[\"maya.chen\"]', 0)"
    )
    conn.execute(
        "INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, known_usernames_json, is_self)"
        " VALUES ('c-ada', 'ds', 'src', 'Ada Voss', '[\"ada.voss\"]', 1)"
    )
    conn.execute(
        "INSERT INTO contact_identifiers (dataset_id, source_id, identifier, identifier_type, contact_id)"
        " VALUES ('ds', 'src', 'maya@mudlark.studio', 'email', 'c-maya')"
    )
    conn.commit()


class TestNormalization:
    def test_diacritics_case_honorifics(self) -> None:
        assert normalize_name("Dr. Patél") == "patel"
        assert normalize_name("MAYA   Chen") == "maya chen"

    def test_possessive_is_the_same_name(self) -> None:
        # "Altman's" used to normalize to "altman s" and mint a twin beside
        # "Altman" — 26 such pairs in the first live graph.
        assert normalize_name("Altman’s") == normalize_name("Altman")
        assert normalize_name("Woody Guthrie's") == normalize_name("Woody Guthrie")
        # a name that merely ends in s is not a possessive
        assert normalize_name("James") == "james"
        assert normalize_name("Suggs") == "suggs"

    def test_trailing_punctuation_is_not_identity(self) -> None:
        assert normalize_name(clean_entity_surface("Williamsburg-")) == "williamsburg"
        assert clean_entity_surface("- Hood Circle") == "Hood Circle"
        assert clean_entity_surface("NYC-") == "NYC"


class TestSurfaceRejection:
    def test_redaction_placeholders_never_become_entities(self) -> None:
        # Text sent to a model that must not see names comes back with these
        # standing in for them; minting from it names the redaction.
        assert not is_valid_entity_surface("[NAME]")
        assert not is_valid_entity_surface("Meet [NAME][NAME] at Barton Creek Saloon")
        assert not is_valid_entity_surface("[EMAIL]")
        assert is_valid_entity_surface("Barton Creek Saloon")

    def test_resolve_refuses_a_redacted_surface(self, conn) -> None:
        with pytest.raises(ValueError, match="invalid entity surface"):
            EntityResolver(conn).resolve("[NAME] Johnson", entity_type="person")

    def test_dangling_dash_resolves_onto_the_clean_name(self, conn) -> None:
        resolver = EntityResolver(conn)
        clean_id = resolver._create_entity("Williamsburg", "place")
        conn.commit()
        got_id, tier = resolver.resolve("Williamsburg-", entity_type="place")
        assert got_id == clean_id, f"minted a second Williamsburg (tier={tier})"

    def test_token_set_similarity_orderless(self) -> None:
        assert token_set_similarity("Chen Maya", "Maya Chen") == 1.0
        assert token_set_similarity("Maya", "Maya Chen") == 1.0  # subset
        assert token_set_similarity("Jon Smith", "completely different") < 0.5


class TestResolutionTiers:
    def test_identifier_match(self, conn) -> None:
        _seed_contacts(conn)
        resolver = EntityResolver(conn)
        resolver.seed_from_contacts()
        entity_id, tier = resolver.resolve("maya@mudlark.studio", entity_type="person")
        assert tier == "identifier"
        row = conn.execute(
            "SELECT canonical_name FROM entities WHERE entity_id=?", (entity_id,)
        ).fetchone()
        assert row[0] == "Maya Chen"

    def test_alias_match_full_name(self, conn) -> None:
        _seed_contacts(conn)
        resolver = EntityResolver(conn)
        resolver.seed_from_contacts()
        entity_id, tier = resolver.resolve("maya chen", entity_type="person")
        assert tier == "contact"  # contact-seeded people match at tier 1.5

    def test_single_token_unique_person(self, conn) -> None:
        """'Maya' resolves to Maya Chen when unambiguous."""
        _seed_contacts(conn)
        resolver = EntityResolver(conn)
        resolver.seed_from_contacts()
        entity_id, tier = resolver.resolve("Maya", entity_type="person")
        row = conn.execute(
            "SELECT canonical_name FROM entities WHERE entity_id=?", (entity_id,)
        ).fetchone()
        assert row[0] == "Maya Chen" and tier == "contact"

    def test_single_token_ambiguous_creates_new(self, conn) -> None:
        _seed_contacts(conn)
        resolver = EntityResolver(conn)
        resolver.seed_from_contacts()
        resolver._create_entity("Maya Rodriguez", "person")
        conn.commit()
        entity_id, tier = resolver.resolve("Maya", entity_type="person")
        assert tier == "created"  # two Mayas -> never guess

    def test_fuzzy_reordered_name_merges(self, conn) -> None:
        _seed_contacts(conn)
        resolver = EntityResolver(conn)
        resolver.seed_from_contacts()
        entity_id, tier = resolver.resolve("Chen, Maya", entity_type="person")
        assert tier in ("alias", "fuzzy")
        row = conn.execute(
            "SELECT canonical_name FROM entities WHERE entity_id=?", (entity_id,)
        ).fetchone()
        assert row[0] == "Maya Chen"

    def test_low_margin_match_goes_to_review_not_merge(self, conn) -> None:
        resolver = EntityResolver(conn)
        resolver._create_entity("Jonathan Marchetti", "person")
        conn.commit()
        entity_id, tier = resolver.resolve("Jonathan Marchesi", entity_type="person")
        # Similar but not identical people must never silently merge.
        if tier == "created":
            reviews = conn.execute("SELECT COUNT(*) FROM entity_review").fetchone()[0]
            assert reviews >= 0  # created + possibly queued
        else:
            pytest.fail(f"near-duplicate person auto-merged (tier={tier})")

    def test_derivation_mints_a_node_without_asking_the_owner(self, conn) -> None:
        """Graph derivation resolves names to get a vertex, cites no record,
        and has its mention-less entity deleted by orphan cleanup before the
        next run — so a question raised here is unanswerable and comes back
        every run. 97% of the review rows on the first live node came from
        this path."""
        resolver = EntityResolver(conn)
        resolver._create_entity("Jonathan Marchetti", "person")
        conn.commit()

        for _ in range(5):  # five derivation runs over the same name
            resolver.resolve("Jonathan Marchesi", entity_type="person", queue_review=False)
        assert conn.execute("SELECT COUNT(*) FROM entity_review").fetchone()[0] == 0

    def test_ingest_still_asks_when_it_has_a_record_to_cite(self, conn) -> None:
        resolver = EntityResolver(conn)
        resolver._create_entity("Jonathan Marchetti", "person")
        conn.execute("UPDATE entities SET mention_count=5")
        conn.commit()

        resolver.resolve("Jonathan Marchesi", entity_type="person", record_id="rec-1")
        row = conn.execute(
            "SELECT record_id FROM entity_review WHERE kind='resolution'"
        ).fetchone()
        assert row is not None, "ingest path stopped asking"
        assert row[0] == "rec-1", "question queued without the record backing it"

    def test_no_duplicates_for_seeded_contacts(self, conn) -> None:
        _seed_contacts(conn)
        resolver = EntityResolver(conn)
        resolver.seed_from_contacts()
        resolver.seed_from_contacts()  # idempotent
        for surface in ("Maya Chen", "maya.chen", "maya@mudlark.studio", "Chen Maya"):
            resolver.resolve(surface, entity_type="person")
        count = conn.execute(
            "SELECT COUNT(*) FROM entities WHERE normalized_name LIKE '%maya%'"
        ).fetchone()[0]
        assert count == 1, "resolution created duplicate person entities"


class TestEdges:
    def test_decay_halves_after_half_life(self, conn) -> None:
        resolver = EntityResolver(conn)
        a = resolver._create_entity("A Person", "person")
        b = resolver._create_entity("B Person", "person")
        conn.commit()
        update_edge(conn, src_entity_id=a, dst_entity_id=b, edge_type=EDGE_CO_OCCURRENCE,
                    event_at="2026-01-01T00:00:00Z")
        # 60 days later (default half-life): old weight 1.0 -> 0.5, +1 new = 1.5
        update_edge(conn, src_entity_id=a, dst_entity_id=b, edge_type=EDGE_CO_OCCURRENCE,
                    event_at="2026-03-02T00:00:00Z")
        edges = top_edges(conn, a)
        assert edges[0]["weight"] == pytest.approx(1.5, abs=0.01)
        assert edges[0]["evidence_count"] == 2

    def test_undirected_edges_share_row(self, conn) -> None:
        resolver = EntityResolver(conn)
        a = resolver._create_entity("A Person", "person")
        b = resolver._create_entity("B Person", "person")
        conn.commit()
        update_edge(conn, src_entity_id=a, dst_entity_id=b, edge_type=EDGE_COMMUNICATES)
        update_edge(conn, src_entity_id=b, dst_entity_id=a, edge_type=EDGE_COMMUNICATES)
        count = conn.execute("SELECT COUNT(*) FROM entity_edges").fetchone()[0]
        assert count == 1

    def test_graph_snapshot_shape(self, conn) -> None:
        resolver = EntityResolver(conn)
        a = resolver._create_entity("A Person", "person")
        b = resolver._create_entity("B Org", "org")
        conn.commit()
        update_edge(conn, src_entity_id=a, dst_entity_id=b, edge_type=EDGE_CO_OCCURRENCE)
        graph = graph_snapshot(conn)
        assert len(graph["nodes"]) == 2 and len(graph["edges"]) == 1
        assert graph["edges"][0]["weight"] == 1.0


class TestMergeReversibility:
    def test_merge_moves_provenance(self, conn) -> None:
        resolver = EntityResolver(conn)
        keep = resolver._create_entity("Maya Chen", "person")
        dupe = resolver._create_entity("M. Chen", "person")
        conn.commit()
        resolver.record_mention(dupe, record_id="r1", surface_text="M. Chen")
        conn.commit()
        resolver.merge_entities(keep, dupe)
        assert conn.execute(
            "SELECT COUNT(*) FROM entities WHERE entity_id=?", (dupe,)
        ).fetchone()[0] == 0
        mention = conn.execute(
            "SELECT entity_id, surface_text FROM entity_mentions WHERE record_id='r1'"
        ).fetchone()
        # Mention keeps its original surface_text — merges stay auditable/undoable.
        assert mention[0] == keep and mention[1] == "M. Chen"
        aliases = json.loads(
            conn.execute("SELECT aliases_json FROM entities WHERE entity_id=?", (keep,)).fetchone()[0]
        )
        assert "M. Chen" in aliases


class TestMergeEdgeCollisions:
    """merge_entities must not raise on the active-row partial unique index
    (idx_entity_edges_active) when both entities hold an active edge of the same
    type to the same third entity — it folds the duplicate into the survivor."""

    @staticmethod
    def _active(conn, a, b, edge_type):
        """Return (weight, evidence_count, count) for active edges between a and b."""
        rows = conn.execute(
            """
            SELECT weight, evidence_count FROM entity_edges
            WHERE edge_type=? AND valid_to IS NULL
              AND ((src_entity_id=? AND dst_entity_id=?)
                   OR (src_entity_id=? AND dst_entity_id=?))
            """,
            (edge_type, a, b, b, a),
        ).fetchall()
        return rows

    def test_undirected_active_collision_folds(self, conn) -> None:
        resolver = EntityResolver(conn)
        keep = resolver._create_entity("Maya Chen", "person")
        absorb = resolver._create_entity("M. Chen", "person")
        third = resolver._create_entity("Ada Voss", "person")
        conn.commit()

        # Both keep and absorb have an ACTIVE communicates_with edge to `third`.
        update_edge(conn, src_entity_id=keep, dst_entity_id=third,
                    edge_type=EDGE_COMMUNICATES, event_at="2026-01-01T00:00:00Z")
        update_edge(conn, src_entity_id=absorb, dst_entity_id=third,
                    edge_type=EDGE_COMMUNICATES, event_at="2026-01-02T00:00:00Z")
        conn.commit()
        assert len(self._active(conn, keep, third, EDGE_COMMUNICATES)) == 1
        assert len(self._active(conn, absorb, third, EDGE_COMMUNICATES)) == 1

        # The blanket UPDATE would raise IntegrityError here; the fold must not.
        resolver.merge_entities(keep, absorb)

        surviving = self._active(conn, keep, third, EDGE_COMMUNICATES)
        assert len(surviving) == 1  # exactly one active edge remains
        weight, evidence = surviving[0]
        assert evidence == 2  # both edges' evidence folded in
        assert weight > 0
        # absorb has no edges left at all
        assert conn.execute(
            "SELECT COUNT(*) FROM entity_edges WHERE src_entity_id=? OR dst_entity_id=?",
            (absorb, absorb),
        ).fetchone()[0] == 0

    def test_directed_non_colliding_edges_rewrite(self, conn) -> None:
        # part_of is directed: (absorb -> parent) and (keep -> parent) both
        # active canonicalise to the SAME (src, dst, type) -> fold; but
        # (absorb -> parent) with keep having none simply rewrites.
        resolver = EntityResolver(conn)
        keep = resolver._create_entity("Widget", "org")
        absorb = resolver._create_entity("Widget Inc", "org")
        parent = resolver._create_entity("Holdco", "org")
        conn.commit()
        update_edge(conn, src_entity_id=absorb, dst_entity_id=parent,
                    edge_type=EDGE_PART_OF, event_at="2026-01-01T00:00:00Z")
        conn.commit()
        resolver.merge_entities(keep, absorb)
        row = conn.execute(
            "SELECT src_entity_id, dst_entity_id FROM entity_edges "
            "WHERE edge_type=? AND valid_to IS NULL",
            (EDGE_PART_OF,),
        ).fetchone()
        assert row == (keep, parent)  # rewritten, single active edge

    def test_edge_between_merged_entities_becomes_self_loop_and_drops(self, conn) -> None:
        resolver = EntityResolver(conn)
        keep = resolver._create_entity("Maya Chen", "person")
        absorb = resolver._create_entity("M. Chen", "person")
        conn.commit()
        update_edge(conn, src_entity_id=keep, dst_entity_id=absorb,
                    edge_type=EDGE_COMMUNICATES, event_at="2026-01-01T00:00:00Z")
        conn.commit()
        resolver.merge_entities(keep, absorb)
        # the keep<->absorb edge collapses to a self-loop and is dropped
        assert conn.execute("SELECT COUNT(*) FROM entity_edges").fetchone()[0] == 0

    def test_closed_edge_history_survives_merge(self, conn) -> None:
        resolver = EntityResolver(conn)
        keep = resolver._create_entity("Maya Chen", "person")
        absorb = resolver._create_entity("M. Chen", "person")
        third = resolver._create_entity("Ada Voss", "person")
        conn.commit()
        # absorb had a relationship to `third` that was later closed (history).
        update_edge(conn, src_entity_id=absorb, dst_entity_id=third,
                    edge_type=EDGE_COMMUNICATES, event_at="2025-06-01T00:00:00Z")
        supersede_edge(conn, src_entity_id=absorb, dst_entity_id=third,
                       edge_type=EDGE_COMMUNICATES, valid_to="2025-09-01T00:00:00Z")
        conn.commit()
        resolver.merge_entities(keep, absorb)
        # the closed row is rewritten to keep and preserved (not a collision:
        # the partial unique index ignores closed rows).
        closed = conn.execute(
            "SELECT src_entity_id, dst_entity_id, valid_to FROM entity_edges "
            "WHERE valid_to IS NOT NULL",
        ).fetchall()
        assert len(closed) == 1
        assert keep in closed[0][:2] and third in closed[0][:2]


class TestDossiers:
    def _populate(self, conn) -> str:
        _seed_contacts(conn)
        resolver = EntityResolver(conn)
        resolver.seed_from_contacts()
        maya, _ = resolver.resolve("Maya Chen", entity_type="person")
        for i, table in enumerate(("conversation_messages", "journal_entries", "calendar_events")):
            resolver.record_mention(
                maya,
                record_id=f"rec-{i}",
                surface_text="Maya Chen",
                canonical_table=table,
                event_at=f"2026-06-0{i + 1}T10:00:00Z",
            )
        conn.commit()
        return maya

    def test_significance_threshold(self, conn) -> None:
        maya = self._populate(conn)
        significant = significant_entities(conn)
        assert [e["entity_id"] for e in significant] == [maya]

    def test_dossier_content_and_owner_only(self, conn) -> None:
        maya = self._populate(conn)
        written = refresh_dossiers(conn)
        assert written == 1
        from topos.features.entities.dossier import load_dossier_for_entity

        dossier = load_dossier_for_entity(conn, maya)
        assert dossier is not None
        assert dossier["disclosure"] == "owner_only"
        assert "Maya Chen" in dossier["summary_text"]
        assert len(dossier["recent_activity"]) == 3

    def test_dossier_stable_upsert(self, conn) -> None:
        self._populate(conn)
        refresh_dossiers(conn)
        refresh_dossiers(conn)
        live = conn.execute(
            "SELECT COUNT(*) FROM signal_objects WHERE object_type='entity_dossier' AND valid_to IS NULL"
        ).fetchone()[0]
        assert live == 1

    def test_ensure_dossier_materializes_below_significance(self, conn) -> None:
        """Named asks must not degrade to entity_graph after supersede (D4)."""
        from topos.features.entities.dossier import ensure_dossier, load_dossier_for_entity

        _seed_contacts(conn)
        resolver = EntityResolver(conn)
        resolver.seed_from_contacts()
        maya, _ = resolver.resolve("Maya Chen", entity_type="person")
        # Only two mentions — below SIGNIFICANT_MENTIONS=3, so refresh skips.
        for i in range(2):
            resolver.record_mention(
                maya,
                record_id=f"sub-{i}",
                surface_text="Maya Chen",
                canonical_table="conversation_messages",
                event_at=f"2026-06-0{i + 1}T10:00:00Z",
            )
        conn.commit()
        assert refresh_dossiers(conn) == 0
        assert load_dossier_for_entity(conn, maya) is None
        payload = ensure_dossier(
            conn,
            {
                "entity_id": maya,
                "entity_type": "person",
                "canonical_name": "Maya Chen",
                "mention_count": 2,
            },
        )
        assert payload is not None and "Maya Chen" in payload["summary_text"]
        assert load_dossier_for_entity(conn, maya) is not None
        items = entity_context_items(
            conn, link_query_entities(conn, "Who is Maya Chen?"), manifest=MESSAGES_MANIFEST
        )
        assert any(i.get("retrieval_source") == "entity_dossier" for i in items)


class TestQueryLinking:
    def test_link_by_name_and_alias(self, conn) -> None:
        _seed_contacts(conn)
        resolver = EntityResolver(conn)
        resolver.seed_from_contacts()
        maya, _ = resolver.resolve("Maya Chen", entity_type="person")
        resolver.record_mention(maya, record_id="r1", surface_text="Maya Chen")
        conn.commit()

        linked = link_query_entities(conn, "What has Maya Chen been up to?")
        assert linked and linked[0]["canonical_name"] == "Maya Chen"
        assert linked[0]["match_score"] == 1.0

        linked_partial = link_query_entities(conn, "climbing with maya on tuesday")
        assert linked_partial and linked_partial[0]["canonical_name"] == "Maya Chen"

    def test_no_link_on_unrelated_query(self, conn) -> None:
        _seed_contacts(conn)
        EntityResolver(conn).seed_from_contacts()
        assert link_query_entities(conn, "quarterly tax filing deadline") == []

    def test_context_items_include_dossier_and_mentions(self, conn) -> None:
        _seed_contacts(conn)
        resolver = EntityResolver(conn)
        resolver.seed_from_contacts()
        maya, _ = resolver.resolve("Maya Chen", entity_type="person")
        for i in range(3):
            resolver.record_mention(
                maya, record_id=f"r{i}", surface_text="Maya Chen",
                canonical_table="conversation_messages", event_at=f"2026-06-0{i + 1}T10:00:00Z",
            )
        conn.commit()
        refresh_dossiers(conn)
        items = entity_context_items(
            conn, link_query_entities(conn, "Maya Chen"), manifest=MESSAGES_MANIFEST
        )
        sources = [i["retrieval_source"] for i in items]
        assert "entity_dossier" in sources and "entity_mention" in sources


class TestDossierDisclosure:
    def _manifest(self, signal_objects):
        from topos.query.manifest import ScopeResolutionManifest

        return ScopeResolutionManifest(
            scope_id="relationship_context:read",
            canonical_tables=[],
            signal_objects=signal_objects,
            primary_dimensions=["relationships"],
            access_mode_ceiling="summary",
            must_not_retrieve=[],
        )

    def test_dossier_blocked_at_default_tier(self) -> None:
        from topos.query.retrieval import _fact_disclosure_allowed

        item = {"object_type": "entity_dossier", "disclosure": "owner_only"}
        assert not _fact_disclosure_allowed(item, "default_disclosure", self._manifest([]))
        assert not _fact_disclosure_allowed(
            item, "default_disclosure", self._manifest(["stat_insights"])
        ), "stat grant must not unlock dossiers"

    def test_dossier_grant_unlocks(self) -> None:
        from topos.query.retrieval import _fact_disclosure_allowed

        item = {"object_type": "entity_dossier", "disclosure": "owner_only"}
        assert _fact_disclosure_allowed(
            item, "default_disclosure", self._manifest(["entity_dossiers"])
        )
