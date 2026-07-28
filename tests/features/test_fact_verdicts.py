"""Owner fact verdicts: confirm / reject / edit and their belief-revision effects."""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.facts.store import FactStore
from topos.features.facts.verdicts import apply_fact_verdict
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "verdicts.db"))
    apply_all_migrations(c)
    c.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self)"
        " VALUES ('ent_self', 'person', 'Ada Voss', 'ada voss', 1)"
    )
    c.commit()
    yield c
    c.close()


def _assert_llm_fact(conn, predicate="lives_in", value="Brooklyn", confidence=0.55):
    return FactStore(conn).assert_fact(
        subject_entity_id="ent_self",
        predicate=predicate,
        object_value=value,
        confidence=confidence,
        source_refs=[{"table": "journal_entries", "record_id": "j1"}],
    )


class TestConfirm:
    def test_confirm_raises_confidence_and_stamps(self, conn) -> None:
        fact = _assert_llm_fact(conn)
        out = apply_fact_verdict(conn, object_id=fact["object_id"], action="confirm")
        assert out["status"] == "confirmed"
        stored = FactStore(conn).facts_for_subject("ent_self")[0]
        assert stored["payload"]["confidence"] == 1.0
        assert stored["confidence"] == 1.0
        assert stored["payload"]["verified_by_owner"] is True
        assert stored["payload"]["extracted_confidence"] == 0.55
        assert stored["payload"]["verified_at"]

    def test_confirm_is_idempotent(self, conn) -> None:
        fact = _assert_llm_fact(conn)
        apply_fact_verdict(conn, object_id=fact["object_id"], action="confirm")
        first = FactStore(conn).facts_for_subject("ent_self")[0]["payload"]
        apply_fact_verdict(conn, object_id=fact["object_id"], action="confirm")
        second = FactStore(conn).facts_for_subject("ent_self")[0]["payload"]
        assert second == first  # verified_at not re-stamped, extracted_confidence kept

    def test_confirmed_fact_survives_weak_llm_reassert(self, conn) -> None:
        fact = _assert_llm_fact(conn, value="Brooklyn")
        apply_fact_verdict(conn, object_id=fact["object_id"], action="confirm")
        # A later LLM pass claims something else at extractor confidence.
        challenger = FactStore(conn).assert_fact(
            subject_entity_id="ent_self",
            predicate="lives_in",
            object_value="Queens",
            confidence=0.55,
        )
        # Incumbent kept; challenger queued as a conflict, not a supersession.
        assert challenger["payload"]["object_value"] == "Brooklyn"
        active = [f for f in FactStore(conn).facts_for_subject("ent_self") if f["valid_to"] is None]
        assert len(active) == 1
        n_conflicts = conn.execute("SELECT count(*) FROM fact_conflicts").fetchone()[0]
        assert n_conflicts == 1


class TestReject:
    def test_reject_closes_and_tombstones(self, conn) -> None:
        fact = _assert_llm_fact(conn, value="Brooklyn")
        out = apply_fact_verdict(conn, object_id=fact["object_id"], action="reject")
        assert out["status"] == "rejected"
        assert out["facts_closed"] == 1
        active = [f for f in FactStore(conn).facts_for_subject("ent_self") if f["valid_to"] is None]
        assert active == []
        # Idempotent extraction cannot resurrect it.
        again = _assert_llm_fact(conn, value="Brooklyn")
        assert again is None

    def test_reject_is_value_scoped(self, conn) -> None:
        kept = _assert_llm_fact(conn, predicate="works_on", value="VoxTerm")
        rejected = _assert_llm_fact(conn, predicate="works_on", value="paywall UI")
        apply_fact_verdict(conn, object_id=rejected["object_id"], action="reject")
        active = [f for f in FactStore(conn).facts_for_subject("ent_self") if f["valid_to"] is None]
        assert [f["payload"]["object_value"] for f in active] == ["VoxTerm"]
        assert kept["object_id"] == active[0]["object_id"]
        # Other values of the same predicate still assert fine.
        assert _assert_llm_fact(conn, predicate="works_on", value="Topos node") is not None


class TestEdit:
    def test_edit_value_supersedes_and_blocks_old_value(self, conn) -> None:
        fact = _assert_llm_fact(conn, value="Austin")
        out = apply_fact_verdict(
            conn, object_id=fact["object_id"], action="edit", object_value="Manhattan"
        )
        assert out["status"] == "edited"
        assert out["superseded_object_id"] == fact["object_id"]
        active = [f for f in FactStore(conn).facts_for_subject("ent_self") if f["valid_to"] is None]
        assert len(active) == 1
        payload = active[0]["payload"]
        assert payload["object_value"] == "Manhattan"
        assert payload["confidence"] == 1.0
        assert payload["verified_by_owner"] is True
        assert payload["corrected_from"] == "Austin"
        assert payload["asserted_by"] == "owner"
        # History is preserved, and the old value cannot come back.
        history = FactStore(conn).history("ent_self", "lives_in")
        assert len(history) == 2
        assert _assert_llm_fact(conn, value="Austin") is None

    def test_edit_attribution_only_updates_in_place(self, conn) -> None:
        fact = _assert_llm_fact(conn)
        out = apply_fact_verdict(
            conn, object_id=fact["object_id"], action="edit", asserted_by="contact:ent_amy"
        )
        assert out["object_id"] == fact["object_id"]  # no supersession
        stored = FactStore(conn).facts_for_subject("ent_self")[0]
        assert stored["payload"]["asserted_by"] == "contact:ent_amy"
        assert stored["payload"]["object_value"] == "Brooklyn"
        assert stored["payload"]["confidence"] == 0.55  # attribution repair ≠ verification

    def test_edit_rejects_bad_attribution(self, conn) -> None:
        fact = _assert_llm_fact(conn)
        with pytest.raises(ValueError, match="asserted_by"):
            apply_fact_verdict(
                conn, object_id=fact["object_id"], action="edit", asserted_by="my mom"
            )

    def test_edit_requires_some_change(self, conn) -> None:
        fact = _assert_llm_fact(conn)
        with pytest.raises(ValueError, match="edit requires"):
            apply_fact_verdict(conn, object_id=fact["object_id"], action="edit")


class TestGuards:
    def test_unknown_action_and_missing_fact(self, conn) -> None:
        fact = _assert_llm_fact(conn)
        with pytest.raises(ValueError, match="action"):
            apply_fact_verdict(conn, object_id=fact["object_id"], action="promote")
        with pytest.raises(LookupError):
            apply_fact_verdict(conn, object_id="nope", action="confirm")

    def test_verdict_on_closed_fact_refused(self, conn) -> None:
        fact = _assert_llm_fact(conn, value="Austin")
        apply_fact_verdict(conn, object_id=fact["object_id"], action="edit", object_value="Manhattan")
        with pytest.raises(ValueError, match="closed"):
            apply_fact_verdict(conn, object_id=fact["object_id"], action="confirm")
