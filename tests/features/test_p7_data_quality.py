"""Data-quality fixes surfaced by the 2026-07-05 nightly run.

1. NER wordpiece fragments ('##dy') must never become entities
2. Fragmented org entities get part_of hierarchy edges (Google Docs -> Google)
3. Contact-seeded people outrank NER typing ("Austin" the person vs the city)
4. Experience currency inferred from verb tense / since / open ranges
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.entities.resolver import EntityResolver, is_valid_entity_surface
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "dq.db"))
    apply_all_migrations(c)
    yield c
    c.close()


class TestWordpieceFilter:
    @pytest.mark.parametrize(
        "surface", ["##dy", "##ccelerator", "x ##b y", "##2", "42", "7.5", "!!", "", " ", "a"]
    )
    def test_invalid_surfaces_rejected(self, surface) -> None:
        assert not is_valid_entity_surface(surface)

    @pytest.mark.parametrize(
        "surface", ["Maya Chen", "Google", "AWS", "Dr. Patel", "Austin Barbaro", "C3"]
    )
    def test_valid_surfaces_accepted(self, surface) -> None:
        assert is_valid_entity_surface(surface)

    def test_resolver_refuses_invalid_surface(self, conn) -> None:
        with pytest.raises(ValueError):
            EntityResolver(conn).resolve("##dy", entity_type="person")


class TestOrgHierarchy:
    def test_product_links_to_parent(self, conn) -> None:
        resolver = EntityResolver(conn)
        google = resolver._create_entity("Google", "org")
        docs = resolver._create_entity("Google Docs", "org")
        conn.commit()
        edge = conn.execute(
            "SELECT src_entity_id, dst_entity_id FROM entity_edges WHERE edge_type='part_of'"
        ).fetchone()
        assert edge == (docs, google)

    def test_parent_created_after_products_links_upward(self, conn) -> None:
        resolver = EntityResolver(conn)
        docs = resolver._create_entity("Google Docs", "org")
        slides = resolver._create_entity("Google Slide", "org")
        google = resolver._create_entity("Google", "org")
        conn.commit()
        edges = conn.execute(
            "SELECT src_entity_id, dst_entity_id FROM entity_edges WHERE edge_type='part_of'"
        ).fetchall()
        assert set(edges) == {(docs, google), (slides, google)}

    def test_unrelated_orgs_not_linked(self, conn) -> None:
        resolver = EntityResolver(conn)
        resolver._create_entity("Google", "org")
        resolver._create_entity("Anthropic", "org")
        conn.commit()
        assert (
            conn.execute("SELECT COUNT(*) FROM entity_edges WHERE edge_type='part_of'").fetchone()[0]
            == 0
        )


class TestContactFirstTyping:
    def _seed_contact(self, conn, name="Austin Barbaro") -> None:
        conn.execute(
            "INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self)"
            " VALUES ('c-austin', 'ds', 'src', ?, 0)",
            (name,),
        )
        conn.commit()

    def test_place_typed_mention_resolves_to_contact_person(self, conn) -> None:
        """NER says 'Austin' is a place; the contact registry knows better."""
        self._seed_contact(conn)
        resolver = EntityResolver(conn)
        resolver.seed_from_contacts()
        entity_id, tier = resolver.resolve("Austin", entity_type="place")
        assert tier == "contact"
        row = conn.execute(
            "SELECT canonical_name, entity_type FROM entities WHERE entity_id=?",
            (entity_id,),
        ).fetchone()
        assert row == ("Austin Barbaro", "person")

    def test_ambiguous_contact_token_falls_through(self, conn) -> None:
        self._seed_contact(conn, "Austin Barbaro")
        conn.execute(
            "INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self)"
            " VALUES ('c-austin2', 'ds', 'src', 'Austin Reyes', 0)"
        )
        conn.commit()
        resolver = EntityResolver(conn)
        resolver.seed_from_contacts()
        entity_id, tier = resolver.resolve("Austin", entity_type="place")
        assert tier != "contact"  # two Austins -> never guess a person
        etype = conn.execute(
            "SELECT entity_type FROM entities WHERE entity_id=?", (entity_id,)
        ).fetchone()[0]
        assert etype == "place"

    def test_full_name_place_label_still_wins_contact(self, conn) -> None:
        self._seed_contact(conn)
        resolver = EntityResolver(conn)
        resolver.seed_from_contacts()
        entity_id, tier = resolver.resolve("Austin Barbaro", entity_type="place")
        assert tier == "contact"


class TestLinkingTokenGuard:
    def test_single_letter_token_never_links(self, conn) -> None:
        """'…for a contact' must not match half of 'VoxTerm A' (live-node bug)."""
        from topos.features.entities.linking import link_query_entities

        conn.execute(
            "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, mention_count)"
            " VALUES ('ent_vox', 'topic', 'VoxTerm A', 'voxterm a', 30)"
        )
        conn.commit()
        assert link_query_entities(conn, "recall relationship context for a contact") == []
        # the meaningful token still links
        linked = link_query_entities(conn, "notes about voxterm sessions")
        assert linked and linked[0]["canonical_name"] == "VoxTerm A"


class TestExperienceCurrency:
    def _extract(self, conn, title, org, description):
        from topos.features.facts.extract import extract_facts_from_batch
        from topos.features.facts.store import FactStore

        conn.execute(
            "INSERT OR IGNORE INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self)"
            " VALUES ('ent_self', 'person', 'Jordan Lee', 'jordan lee', 1)"
        )
        conn.commit()
        extract_facts_from_batch(
            conn,
            [{
                "_table": "profile_records", "record_id": f"p-{org}",
                "record_type": "experience", "title": title,
                "organization": org, "description": description,
            }],
        )
        return FactStore(conn).facts_for_subject("ent_self")

    def test_present_tense_verb_means_current(self, conn) -> None:
        facts = self._extract(conn, "Staff Engineer", "Topos",
                              "Lead ingestion canonicalization and signal derivation.")
        assert any(f["payload"]["predicate"] == "works_at" and f["payload"]["object_value"] == "Topos"
                   for f in facts)

    def test_past_tense_verb_means_former(self, conn) -> None:
        facts = self._extract(conn, "Senior Software Engineer", "Dialogues",
                              "Built privacy-first messaging and UMA permission flows.")
        assert any(f["payload"]["predicate"] == "worked_at" and f["payload"]["object_value"] == "Dialogues"
                   for f in facts)

    def test_advisor_role_is_concurrent_not_superseding(self, conn) -> None:
        self._extract(conn, "Staff Engineer", "Topos", "Lead ingestion pipelines.")
        facts = self._extract(conn, "Edtech Advisor", "Pilot Schools Network",
                              "Advising edtech pilot schools in Austin.")
        by_pred = {}
        for f in facts:
            by_pred.setdefault(f["payload"]["predicate"], []).append(f["payload"]["object_value"])
        assert by_pred.get("works_at") == ["Topos"], "advisory role superseded the day job"
        assert by_pred.get("advises") == ["Pilot Schools Network"]
        assert by_pred.get("role_is") == ["Staff Engineer"], (
            "advisory title must not supersede the primary role"
        )

    def test_since_year_is_current_with_period(self, conn) -> None:
        facts = self._extract(conn, "Engineer", "Acme", "Platform work since 2023.")
        acme = next(f for f in facts if f["payload"]["object_value"] == "Acme")
        assert acme["payload"]["predicate"] == "works_at"
        assert acme["payload"]["period_start"] == "2023"

    def test_open_range_is_current(self, conn) -> None:
        facts = self._extract(conn, "Engineer", "Beta Corp", "Infra team, 2024– ")
        beta = next(f for f in facts if f["payload"]["object_value"] == "Beta Corp")
        assert beta["payload"]["predicate"] == "works_at"
        assert beta["payload"]["period_start"] == "2024"

    def test_closed_range_still_former(self, conn) -> None:
        facts = self._extract(conn, "Engineer", "Gamma", "Data platform. 2019–2022.")
        gamma = next(f for f in facts if f["payload"]["object_value"] == "Gamma")
        assert gamma["payload"]["predicate"] == "worked_at"
        assert gamma["payload"]["period_end"] == "2022"

    def test_undated_unsignaled_defaults_current_low_confidence(self, conn) -> None:
        facts = self._extract(conn, "Engineer", "Delta", "Various platform responsibilities.")
        delta = next(f for f in facts if f["payload"]["object_value"] == "Delta")
        assert delta["payload"]["predicate"] == "works_at"
        assert delta["payload"]["confidence"] <= 0.6
