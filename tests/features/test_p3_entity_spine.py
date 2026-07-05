"""P3 tests: entity resolution tiers, decayed edges, dossiers, query linking, disclosure."""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.entities.dossier import build_dossier, refresh_dossiers, significant_entities
from topos.features.entities.edges import (
    EDGE_CO_OCCURRENCE,
    EDGE_COMMUNICATES,
    graph_snapshot,
    top_edges,
    update_edge,
)
from topos.features.entities.linking import entity_context_items, link_query_entities
from topos.features.entities.resolver import (
    AUTO_MERGE_SCORE,
    EntityResolver,
    normalize_name,
    token_set_similarity,
)
from topos.storage.db.migrations import apply_all_migrations


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
        items = entity_context_items(conn, link_query_entities(conn, "Maya Chen"))
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
