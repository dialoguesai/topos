"""P8 tests: derived-intelligence scrub propagation and owner exclusions.

Scenario corpus: two sources (source_a, source_b) sharing an entity, with
stats, facts, timeline, and embeddings derived from both. Scrubbing source_a
must leave a database indistinguishable (in the derived layers) from one where
source_a never existed — except where evidence from source_b independently
attests the same artifact.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from topos.features.entities.resolver import EntityResolver
from topos.features.facts.extract import extract_facts_from_batch
from topos.features.facts.store import FactStore
from topos.features.lifecycle.derived_scrub import (
    purge_derived_for_records,
    purge_derived_for_source,
)
from topos.features.lifecycle.exclusions import ExclusionStore
from topos.features.stats.engine import StatsEngine
from topos.features.stats.fold import summarize
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "lifecycle.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _journal_rows(source_id: str, n: int, *, people: str = "", start_day: int = 1):
    return [
        {
            "record_id": f"{source_id}-j{i}",
            "_table": "journal_entries",
            "source_id": source_id,
            "entry_at": f"2026-06-{start_day + i:02d}T08:00:00Z",
            "category": "exercise",
            "duration_minutes": 30 + i,
            "content": f"run number {i}",
            "people": people,
        }
        for i in range(n)
    ]


def _populate_two_sources(conn) -> dict:
    """Stats + timeline + mentions + facts across source_a and source_b."""
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self)"
        " VALUES ('ent_self', 'person', 'Jordan Lee', 'jordan lee', 1)"
    )
    conn.commit()
    rows_a = _journal_rows("source_a", 4)
    rows_b = _journal_rows("source_b", 3, start_day=10)

    engine = StatsEngine(conn)
    engine.fold_batch(rows_a + rows_b)
    bundle = AdapterFactory.create("local_database", conn=conn)
    engine.promote_insights(bundle)

    for row in rows_a + rows_b:
        conn.execute(
            "INSERT OR REPLACE INTO timeline (event_at, record_id, source_id, canonical_table)"
            " VALUES (?, ?, ?, 'journal_entries')",
            (row["entry_at"], row["record_id"], row["source_id"]),
        )

    resolver = EntityResolver(conn)
    shared, _ = resolver.resolve("Maya Chen", entity_type="person")
    only_a, _ = resolver.resolve("Alpha Corp", entity_type="org")
    for i in range(3):
        resolver.record_mention(
            shared, record_id=f"source_a-j{i}", surface_text="Maya Chen",
            source_id="source_a", event_at=f"2026-06-0{i + 1}T08:00:00Z",
        )
        resolver.record_mention(
            only_a, record_id=f"source_a-j{i}", surface_text="Alpha Corp",
            source_id="source_a", event_at=f"2026-06-0{i + 1}T08:00:00Z",
        )
    resolver.record_mention(
        shared, record_id="source_b-j0", surface_text="Maya Chen",
        source_id="source_b", event_at="2026-06-10T08:00:00Z",
    )
    conn.commit()

    # facts: one evidenced only by source_a, one by both
    extract_facts_from_batch(conn, [
        {"_table": "profile_records", "record_id": "source_a-p1", "source_id": "source_a",
         "record_type": "certification", "title": "Scrum Master", "description": ""},
        {"_table": "profile_records", "record_id": "source_a-p2", "source_id": "source_a",
         "record_type": "experience", "title": "Engineer", "organization": "Acme",
         "description": "Lead platform work."},
        {"_table": "profile_records", "record_id": "source_b-p1", "source_id": "source_b",
         "record_type": "experience", "title": "Engineer", "organization": "Acme",
         "description": "Lead platform work."},
    ])
    return {"shared_entity": shared, "only_a_entity": only_a}


class TestSourceScrubPropagation:
    def test_stats_refold_removes_source_contribution(self, conn) -> None:
        _populate_two_sources(conn)
        engine = StatsEngine(conn)
        before = engine.read_state("journal.duration.by_category", group_key="exercise")
        assert before["n"] == 7  # 4 from a + 3 from b

        # attribution sweep analogue: delete source_a rows, then propagate
        conn.execute("DELETE FROM entity_mentions WHERE source_id='source_a'")
        conn.execute("DELETE FROM timeline WHERE source_id='source_a'")
        conn.commit()
        # (journal rows live only in the fold; simulate canonical deletion by
        # having no journal_entries table rows — refold sees nothing from a)
        purge_derived_for_source(conn, "source_a")

        after = engine.read_state("journal.duration.by_category", group_key="exercise")
        assert after["n"] == 0  # no canonical journal table in this fixture
        # insight facts pruned along with the vanished groups
        stale = conn.execute(
            "SELECT COUNT(*) FROM signal_facts WHERE fact_id LIKE 'stat:journal%'"
        ).fetchone()[0]
        assert stale == 0

    def test_stats_refold_keeps_remaining_source(self, conn, tmp_path) -> None:
        """With a real canonical table, refold keeps source_b's contribution."""
        _populate_two_sources(conn)
        # journal_entries is the real migrated canonical table; only source_b
        # rows exist there (source_a's were "already deleted" by the sweep).
        cols = {
            str(r[1]) for r in conn.execute("PRAGMA table_info(journal_entries)").fetchall()
        }
        duration_col = "duration_minutes" if "duration_minutes" in cols else None
        for row in _journal_rows("source_b", 3, start_day=10):
            fields = {
                "record_id": row["record_id"],
                "source_id": row["source_id"],
                "entry_at": row["entry_at"],
                "category": row["category"],
                "content": row["content"],
            }
            if duration_col:
                fields[duration_col] = row["duration_minutes"]
            else:
                fields["metadata_json"] = json.dumps(
                    {"duration_minutes": row["duration_minutes"]}
                )
            usable = {k: v for k, v in fields.items() if k in cols}
            placeholders = ",".join("?" for _ in usable)
            conn.execute(
                f"INSERT OR REPLACE INTO journal_entries ({','.join(usable)}) VALUES ({placeholders})",
                tuple(usable.values()),
            )
        conn.commit()

        purge_derived_for_source(conn, "source_a")
        after = StatsEngine(conn).read_state("journal.duration.by_category", group_key="exercise")
        assert after["n"] == 3, "source_b contribution lost in refold"
        out = summarize("mean_var", after)
        assert out["mean"] == pytest.approx((30 + 31 + 32) / 3)

    def test_entities_orphaned_by_scrub_are_removed_shared_survive(self, conn) -> None:
        ids = _populate_two_sources(conn)
        conn.execute("DELETE FROM entity_mentions WHERE source_id='source_a'")
        conn.commit()
        purge_derived_for_source(conn, "source_a")

        remaining = {
            r[0] for r in conn.execute("SELECT entity_id FROM entities").fetchall()
        }
        assert ids["only_a_entity"] not in remaining, "entity evidenced only by source_a survived scrub"
        assert ids["shared_entity"] in remaining, "shared entity wrongly removed"
        # shared entity mention count reflects only source_b evidence
        count = conn.execute(
            "SELECT mention_count FROM entities WHERE entity_id=?", (ids["shared_entity"],)
        ).fetchone()[0]
        assert count == 1

    def test_facts_sole_provenance_deleted_mixed_trimmed(self, conn) -> None:
        _populate_two_sources(conn)
        store = FactStore(conn)
        before = {f["payload"]["predicate"]: f for f in store.facts_for_subject("ent_self")}
        assert "certified_in" in before and "works_at" in before

        purge_derived_for_source(conn, "source_a")

        after = store.facts_for_subject("ent_self")
        by_pred = {f["payload"]["predicate"]: f for f in after}
        assert "certified_in" not in by_pred, "fact evidenced only by source_a survived"
        assert "works_at" in by_pred, "fact with surviving source_b evidence was deleted"
        refs = by_pred["works_at"]["source_refs"]
        assert all(r.get("source_id") != "source_a" for r in refs), "scrubbed ref not trimmed"

    def test_scrub_service_wiring_includes_derived_purge(self) -> None:
        from topos.sources.scrub_service import (
            REMOVE_SOURCE_OPTIONS,
            SCRUB_SOURCE_OPTIONS,
            normalize_scrub_payload,
        )

        assert SCRUB_SOURCE_OPTIONS.purge_derived_intelligence is True
        assert REMOVE_SOURCE_OPTIONS.purge_derived_intelligence is False
        _sid, opts = normalize_scrub_payload({"source_id": "x", "preset": "remove"})
        assert opts.purge_derived_intelligence is False
        _sid, opts = normalize_scrub_payload({"source_id": "x", "preset": "scrub"})
        assert opts.purge_derived_intelligence is True


class TestRecordPurge:
    def test_purge_single_record(self, conn) -> None:
        ids = _populate_two_sources(conn)
        report = purge_derived_for_records(conn, ["source_a-j0"])
        assert report["entity_mentions"] == 2  # Maya + Alpha mention on j0
        assert report["timeline"] == 1
        remaining_mentions = conn.execute(
            "SELECT COUNT(*) FROM entity_mentions WHERE record_id='source_a-j0'"
        ).fetchone()[0]
        assert remaining_mentions == 0


class TestOwnerExclusions:
    def test_fact_exclusion_soft_closes_and_blocks_reassertion(self, conn) -> None:
        _populate_two_sources(conn)
        store = ExclusionStore(conn)
        result = store.exclude_fact(
            subject_entity_id="ent_self", predicate="certified_in",
            object_value="Scrum Master", note="not relevant",
        )
        assert result["facts_closed"] == 1

        fact_store = FactStore(conn)
        active = fact_store.facts_for_subject("ent_self")
        assert not any(f["payload"]["predicate"] == "certified_in" for f in active)
        # provenance preserved: row soft-closed, not deleted
        closed = fact_store.facts_for_subject("ent_self", include_closed=True)
        cert = [f for f in closed if f["payload"]["predicate"] == "certified_in"]
        assert cert and cert[0]["valid_to"] is not None
        assert cert[0]["payload"]["excluded_by_owner"] is True

        # idempotent re-extraction must NOT resurrect it
        extract_facts_from_batch(conn, [
            {"_table": "profile_records", "record_id": "source_a-p1", "source_id": "source_a",
             "record_type": "certification", "title": "Scrum Master", "description": ""},
        ])
        active = fact_store.facts_for_subject("ent_self")
        assert not any(f["payload"]["predicate"] == "certified_in" for f in active), (
            "excluded fact resurrected by re-extraction"
        )

    def test_entity_exclusion_blocks_retracking(self, conn) -> None:
        ids = _populate_two_sources(conn)
        store = ExclusionStore(conn)
        result = store.exclude_entity(entity_ref="Maya Chen", note="private")
        assert result["entity_found"] and result["mentions_removed"] == 4

        resolver = EntityResolver(conn)
        with pytest.raises(ValueError, match="excluded by owner"):
            resolver.resolve("Maya Chen", entity_type="person")
        with pytest.raises(ValueError, match="excluded by owner"):
            resolver.resolve("maya  chen", entity_type="person")  # normalization holds

    def test_stat_insight_exclusion_blocks_repromotion(self, conn) -> None:
        _populate_two_sources(conn)
        row = conn.execute(
            "SELECT payload_json FROM signal_facts WHERE fact_id LIKE 'stat:%' LIMIT 1"
        ).fetchone()
        payload = json.loads(row[0])
        stat_id = payload["record_id"]
        group_key = payload.get("group_key") or ""

        store = ExclusionStore(conn)
        result = store.exclude_stat_insight(stat_id=stat_id, group_key=group_key)
        assert result["insights_removed"] == 1

        engine = StatsEngine(conn)
        bundle = AdapterFactory.create("local_database", conn=conn)
        engine.promote_insights(bundle)
        resurrected = conn.execute(
            "SELECT COUNT(*) FROM signal_facts WHERE fact_id=?",
            (f"stat:{stat_id}:{group_key or 'all'}",),
        ).fetchone()[0]
        assert resurrected == 0, "excluded insight re-promoted"

    def test_record_exclusion_blocks_refold(self, conn) -> None:
        conn.execute(
            "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self)"
            " VALUES ('ent_self', 'person', 'Jordan Lee', 'jordan lee', 1)"
        )
        conn.commit()
        rows = _journal_rows("source_a", 2)
        engine = StatsEngine(conn)
        engine.fold_batch(rows)
        assert engine.read_state("journal.duration.by_category", group_key="exercise")["n"] == 2

        ExclusionStore(conn).exclude_record(record_id="source_a-j0")

        # refold (fresh engine) must skip the excluded record
        conn.execute("DELETE FROM stat_state")
        conn.execute("DELETE FROM stat_seen")
        conn.commit()
        engine = StatsEngine(conn)
        engine.fold_batch(rows)
        assert engine.read_state("journal.duration.by_category", group_key="exercise")["n"] == 1

    def test_exclusion_lift_allows_reinclusion(self, conn) -> None:
        _populate_two_sources(conn)
        store = ExclusionStore(conn)
        store.exclude_fact(subject_entity_id="ent_self", predicate="certified_in")
        assert store.is_excluded("fact", "ent_self:certified_in")
        assert store.remove_exclusion("fact", "ent_self:certified_in")
        extract_facts_from_batch(conn, [
            {"_table": "profile_records", "record_id": "source_a-p1", "source_id": "source_a",
             "record_type": "certification", "title": "Scrum Master", "description": ""},
        ])
        active = FactStore(conn).facts_for_subject("ent_self")
        assert any(f["payload"]["predicate"] == "certified_in" for f in active)
