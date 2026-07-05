"""P4 tests: fact assertion, belief revision, as-of queries, extraction, disclosure."""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.facts.extract import extract_facts_from_batch
from topos.features.facts.store import FactStore, normalize_predicate
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "facts.db"))
    apply_all_migrations(c)
    c.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self)"
        " VALUES ('ent_self', 'person', 'Ada Voss', 'ada voss', 1)"
    )
    c.commit()
    yield c
    c.close()


class TestBeliefRevision:
    def test_assert_and_read(self, conn) -> None:
        store = FactStore(conn)
        fact = store.assert_fact(
            subject_entity_id="ent_self",
            predicate="works at",
            object_value="Lumon Industries",
            confidence=0.9,
        )
        assert fact["payload"]["predicate"] == "works_at"
        assert fact["valid_to"] is None
        facts = store.facts_for_subject("ent_self")
        assert len(facts) == 1

    def test_same_value_refreshes_not_duplicates(self, conn) -> None:
        store = FactStore(conn)
        store.assert_fact(
            subject_entity_id="ent_self", predicate="works_at",
            object_value="Lumon Industries", confidence=0.7,
            source_refs=[{"table": "profile_records", "record_id": "p1"}],
        )
        store.assert_fact(
            subject_entity_id="ent_self", predicate="works_at",
            object_value="lumon industries", confidence=0.9,
            source_refs=[{"table": "profile_records", "record_id": "p2"}],
        )
        facts = store.facts_for_subject("ent_self", include_closed=True)
        assert len(facts) == 1
        assert facts[0]["payload"]["confidence"] == 0.9
        assert len(facts[0]["source_refs"]) == 2

    def test_contradiction_supersedes_with_history(self, conn) -> None:
        store = FactStore(conn)
        store.assert_fact(
            subject_entity_id="ent_self", predicate="works_at",
            object_value="Lumon Industries", confidence=0.9,
            valid_from="2021-06-01T00:00:00+00:00",
        )
        store.assert_fact(
            subject_entity_id="ent_self", predicate="works_at",
            object_value="Heliograph Labs", confidence=0.9,
            valid_from="2024-03-01T00:00:00+00:00",
        )
        active = store.facts_for_subject("ent_self")
        assert len(active) == 1
        assert active[0]["payload"]["object_value"] == "Heliograph Labs"

        history = store.history("ent_self", "works_at")
        assert len(history) == 2
        assert history[0]["valid_to"] == "2024-03-01T00:00:00+00:00"

    def test_as_of_query(self, conn) -> None:
        store = FactStore(conn)
        store.assert_fact(
            subject_entity_id="ent_self", predicate="works_at",
            object_value="Lumon Industries", confidence=0.9,
            valid_from="2021-06-01T00:00:00+00:00",
        )
        store.assert_fact(
            subject_entity_id="ent_self", predicate="works_at",
            object_value="Heliograph Labs", confidence=0.9,
            valid_from="2024-03-01T00:00:00+00:00",
        )
        then = store.facts_for_subject("ent_self", as_of="2022-01-01T00:00:00+00:00")
        assert [f["payload"]["object_value"] for f in then] == ["Lumon Industries"]
        now = store.facts_for_subject("ent_self", as_of="2025-01-01T00:00:00+00:00")
        assert [f["payload"]["object_value"] for f in now] == ["Heliograph Labs"]

    def test_weak_challenger_queues_conflict_keeps_incumbent(self, conn) -> None:
        store = FactStore(conn)
        store.assert_fact(
            subject_entity_id="ent_self", predicate="lives_in",
            object_value="Portland", confidence=0.9,
        )
        result = store.assert_fact(
            subject_entity_id="ent_self", predicate="lives_in",
            object_value="Chicago", confidence=0.5,
        )
        assert result["payload"]["object_value"] == "Portland", "weak challenger replaced incumbent"
        conflicts = conn.execute("SELECT challenger_value, status FROM fact_conflicts").fetchall()
        assert conflicts and conflicts[0][0] == "Chicago" and conflicts[0][1] == "pending"

    def test_predicate_normalization(self) -> None:
        assert normalize_predicate("Works  At") == "works_at"


class TestExtraction:
    def test_profile_experience_current_vs_past(self, conn) -> None:
        rows = [
            {
                "_table": "profile_records", "record_id": "prof-001",
                "record_type": "experience", "title": "Senior ML Engineer",
                "organization": "Heliograph Labs",
                "description": "Time-series anomaly detection. 2024–present.",
            },
            {
                "_table": "profile_records", "record_id": "prof-002",
                "record_type": "experience", "title": "Data Engineer",
                "organization": "Lumon Industries",
                "description": "Built the macrodata refinement ETL platform. 2021–2024.",
            },
        ]
        written = extract_facts_from_batch(conn, rows)
        assert written >= 3  # works_at + role_is + worked_at
        store = FactStore(conn)
        owner_facts = store.facts_for_subject("ent_self")
        by_pred = {f["payload"]["predicate"]: f for f in owner_facts}
        assert by_pred["works_at"]["payload"]["object_value"] == "Heliograph Labs"
        assert by_pred["role_is"]["payload"]["object_value"] == "Senior ML Engineer"
        # worked_at Lumon stays an *active* claim about a past period —
        # the employment period lives in the payload, not in belief validity.
        lumon = by_pred["worked_at"]
        assert lumon["payload"]["object_value"] == "Lumon Industries"
        assert lumon["valid_to"] is None
        assert lumon["payload"]["period_end"] == "2024"
        assert "2021–2024" in FactStore.render(lumon)
        # The audit's canonical failing question is now answerable structurally:
        as_of_2022 = store.facts_for_subject("ent_self", as_of="2022-06-01T00:00:00+00:00")
        assert any(f["payload"]["object_value"] == "Lumon Industries" for f in as_of_2022)

    def test_certification_fact(self, conn) -> None:
        rows = [{
            "_table": "profile_records", "record_id": "prof-003",
            "record_type": "certification", "title": "Wilderness First Responder",
            "organization": "NOLS", "description": "Certified 2025.",
        }]
        extract_facts_from_batch(conn, rows)
        facts = FactStore(conn).facts_for_subject("ent_self")
        assert any(
            f["payload"]["predicate"] == "certified_in"
            and "Wilderness" in f["payload"]["object_value"]
            for f in facts
        )

    def test_journal_training_fact_is_owner_only(self, conn) -> None:
        rows = [{
            "_table": "journal_entries", "record_id": "jrn-003",
            "entry_at": "2026-05-11T07:05:00Z", "category": "exercise",
            "content": "10k run this morning, longest yet for the half marathon block.",
        }]
        extract_facts_from_batch(conn, rows)
        facts = FactStore(conn).facts_for_subject("ent_self")
        training = [f for f in facts if f["payload"]["predicate"] == "training_for"]
        assert training and training[0]["payload"]["disclosure"] == "owner_only"

    def test_extraction_idempotent(self, conn) -> None:
        rows = [{
            "_table": "profile_records", "record_id": "prof-001",
            "record_type": "experience", "title": "Senior ML Engineer",
            "organization": "Heliograph Labs", "description": "2024–present.",
        }]
        extract_facts_from_batch(conn, rows)
        extract_facts_from_batch(conn, rows)
        count = conn.execute(
            "SELECT COUNT(*) FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL"
        ).fetchone()[0]
        assert count == 2  # works_at + role_is, no duplicates


class TestFactRetrieval:
    def _manifest(self, signal_objects=()):
        from topos.query.manifest import ScopeResolutionManifest

        return ScopeResolutionManifest(
            scope_id="work_context:read",
            canonical_tables=[],
            signal_objects=list(signal_objects),
            primary_dimensions=["work"],
            access_mode_ceiling="summary",
            must_not_retrieve=[],
        )

    def test_fact_items_render_and_rank(self, conn, monkeypatch) -> None:
        from topos.query.retrieval import _load_fact_store_items

        extract_facts_from_batch(
            conn,
            [{
                "_table": "profile_records", "record_id": "prof-002",
                "record_type": "experience", "title": "Data Engineer",
                "organization": "Lumon Industries",
                "description": "ETL platform. 2021–2024.",
            }],
        )
        items = _load_fact_store_items(
            conn, "where did I work before", [], disclosure_tier="owner_raw",
            manifest=self._manifest(),
        )
        assert items, "no fact items returned"
        assert any("Lumon Industries" in i["summary_text"] for i in items)
        assert all(i["retrieval_source"] == "fact" for i in items)

    def test_owner_only_fact_blocked_for_default_tier(self, conn) -> None:
        from topos.query.retrieval import _load_fact_store_items

        extract_facts_from_batch(
            conn,
            [{
                "_table": "journal_entries", "record_id": "jrn-003",
                "entry_at": "2026-05-11T07:05:00Z", "category": "exercise",
                "content": "half marathon training",
            }],
        )
        owner_items = _load_fact_store_items(
            conn, "what am I training for", [], disclosure_tier="owner_raw",
            manifest=self._manifest(),
        )
        assert any(i["predicate"] == "training_for" for i in owner_items)

        shared_items = _load_fact_store_items(
            conn, "what am I training for", [], disclosure_tier="default_disclosure",
            manifest=self._manifest(),
        )
        assert not any(i["predicate"] == "training_for" for i in shared_items), (
            "owner_only fact leaked to default disclosure tier"
        )
        granted_items = _load_fact_store_items(
            conn, "what am I training for", [], disclosure_tier="default_disclosure",
            manifest=self._manifest(["owner_facts"]),
        )
        assert any(i["predicate"] == "training_for" for i in granted_items)
