"""B2 temporal-graph storage tests (plan B2.1-B2.4) + P4.4 asserted_by.

Covers the storage/entities half of the temporal upgrade:
  B2.1 signal_objects period columns (migration idempotency, backfill, writers)
  B2.2 entity_edges validity (table rebuild, supersession lifecycle,
       active-row uniqueness, past-shift context rendering — the T7 mechanism)
  B2.3 stat-insight date stamping (the T8 data side) + dossier/brief history
  B2.4 episode object + pipeline writer hook
  P4.4 FactStore asserted_by attribution
"""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from types import SimpleNamespace

import pytest

from topos.storage.db.migrations import apply_all_migrations
from topos.storage.db.migrations.entity_edges_validity_v1 import (
    apply_entity_edges_validity_v1_up,
)
from topos.storage.db.migrations.signal_objects_period_v1 import (
    apply_signal_objects_period_v1_up,
)


@pytest.fixture()
def conn(tmp_path):
    # run_post_canonical_pipeline records episodes on a worker thread
    # (asyncio.to_thread), so the injected connection must allow
    # cross-thread use.
    c = sqlite3.connect(str(tmp_path / "b2.db"), check_same_thread=False)
    apply_all_migrations(c)
    yield c
    c.close()


def _columns(c: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in c.execute(f"PRAGMA table_info({table})").fetchall()}


def _index_names(c: sqlite3.Connection, table: str) -> set:
    return {
        row[0]
        for row in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
        ).fetchall()
    }


def _seed_entity(c: sqlite3.Connection, entity_id: str, name: str) -> None:
    c.execute(
        """INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,
                                 mention_count, first_seen, last_seen)
           VALUES (?, 'person', ?, ?, 2, '2026-01-01T00:00:00Z', '2026-06-01T00:00:00Z')""",
        (entity_id, name, name.lower()),
    )


# ------------------------------------------------------------------ B2.1


class TestB21PeriodColumns:
    def test_apply_all_twice_is_idempotent(self, tmp_path) -> None:
        c = sqlite3.connect(str(tmp_path / "fresh.db"))
        try:
            apply_all_migrations(c)
            apply_all_migrations(c)  # second run must be a no-op, not an error
            cols = _columns(c, "signal_objects")
            assert {"period_start", "period_end"} <= cols
            idx = _index_names(c, "signal_objects")
            assert "idx_signal_objects_period_start" in idx
            assert "idx_signal_objects_period_end" in idx
        finally:
            c.close()

    def test_backfill_promotes_payload_periods(self, tmp_path) -> None:
        """Legacy DB (pre-period schema) with period keys in payload JSON."""
        from topos.storage.db.migrations.signal_objects import apply_signal_objects_up

        c = sqlite3.connect(str(tmp_path / "legacy.db"))
        try:
            apply_signal_objects_up(c)  # legacy table shape, no period columns
            assert "period_start" not in _columns(c, "signal_objects")
            c.execute(
                """INSERT INTO signal_objects
                   (object_id, signal_dimension, object_type, object_key, payload_json,
                    valid_from, created_at, updated_at)
                   VALUES ('o1', 'work', 'fact', 'fact:e1:worked_at:lumon', ?,
                           '2026-01-01', '2026-01-01', '2026-01-01')""",
                (json.dumps({"object_value": "Lumon", "period_start": "2021", "period_end": "2024"}),),
            )
            c.execute(
                """INSERT INTO signal_objects
                   (object_id, signal_dimension, object_type, object_key, payload_json,
                    valid_from, created_at, updated_at)
                   VALUES ('o2', 'work', 'fact', 'fact:e1:role_is', '{"object_value": "eng"}',
                           '2026-01-01', '2026-01-01', '2026-01-01')"""
            )
            c.commit()

            apply_signal_objects_period_v1_up(c)
            apply_signal_objects_period_v1_up(c)  # idempotent re-run

            rows = dict(
                c.execute(
                    "SELECT object_id, period_start || '|' || COALESCE(period_end, '') FROM signal_objects WHERE period_start IS NOT NULL"
                ).fetchall()
            )
            assert rows == {"o1": "2021|2024"}
            assert (
                c.execute("SELECT period_start, period_end FROM signal_objects WHERE object_id='o2'").fetchone()
                == (None, None)
            )
        finally:
            c.close()

    def test_fact_store_stamps_period_columns_on_insert(self, conn) -> None:
        from topos.features.facts.store import FactStore

        FactStore(conn).assert_fact(
            subject_entity_id="e1",
            predicate="worked_at",
            object_value="Lumon",
            period_start="2021",
            period_end="2024",
        )
        row = conn.execute(
            "SELECT period_start, period_end FROM signal_objects WHERE object_type='fact'"
        ).fetchone()
        assert row == ("2021", "2024")


# ------------------------------------------------------------------ B2.2


class TestB22EdgeValidity:
    LEGACY_DDL = """
        CREATE TABLE entity_edges (
            edge_id TEXT PRIMARY KEY,
            src_entity_id TEXT NOT NULL,
            dst_entity_id TEXT NOT NULL,
            edge_type TEXT NOT NULL,
            weight REAL NOT NULL DEFAULT 0,
            evidence_count INTEGER NOT NULL DEFAULT 0,
            last_event_at TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (src_entity_id, dst_entity_id, edge_type)
        )
    """

    def test_rebuild_from_legacy_table(self, tmp_path) -> None:
        c = sqlite3.connect(str(tmp_path / "legacy_edges.db"))
        try:
            c.execute(self.LEGACY_DDL)
            c.execute(
                """INSERT INTO entity_edges
                   (edge_id, src_entity_id, dst_entity_id, edge_type, weight,
                    evidence_count, last_event_at, created_at)
                   VALUES ('edg_1', 'a', 'b', 'worked_with', 2.5, 3,
                           '2026-04-01T00:00:00Z', '2026-03-01 00:00:00')"""
            )
            c.commit()

            apply_entity_edges_validity_v1_up(c)
            apply_entity_edges_validity_v1_up(c)  # idempotent re-run

            table_sql = c.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='entity_edges'"
            ).fetchone()[0]
            assert "UNIQUE" not in table_sql.upper()
            assert {"valid_from", "valid_to"} <= _columns(c, "entity_edges")
            idx = _index_names(c, "entity_edges")
            assert {"idx_entity_edges_active", "idx_entity_edges_src", "idx_entity_edges_dst"} <= idx

            row = c.execute(
                """SELECT edge_id, weight, evidence_count, valid_from, valid_to, created_at
                   FROM entity_edges"""
            ).fetchall()
            assert len(row) == 1
            edge_id, weight, evidence, valid_from, valid_to, created_at = row[0]
            assert (edge_id, weight, evidence) == ("edg_1", 2.5, 3)
            assert valid_from == created_at  # copy rule: valid_from = created_at
            assert valid_to is None
        finally:
            c.close()

    def test_supersede_lifecycle(self, conn) -> None:
        from topos.features.entities.edges import supersede_edge, top_edges, update_edge

        _seed_entity(conn, "ent-a", "Maren Oxbow")
        _seed_entity(conn, "ent-b", "Brindle Cassavetes")
        update_edge(conn, src_entity_id="ent-a", dst_entity_id="ent-b", edge_type="worked_with",
                    event_at="2026-04-01T00:00:00Z")
        conn.commit()

        # Active edge visible by default (the seeded-corpus contract).
        assert [e["entity_name"] for e in top_edges(conn, "ent-a")] == ["Brindle Cassavetes"]
        assert top_edges(conn, "ent-a")[0]["valid_to"] is None

        closed_id = supersede_edge(
            conn, src_entity_id="ent-a", dst_entity_id="ent-b", edge_type="worked_with",
            valid_to="2026-05-30T00:00:00Z",
        )
        conn.commit()
        assert closed_id

        # Closed revision invisible by default, visible with include_closed.
        assert top_edges(conn, "ent-a") == []
        closed = top_edges(conn, "ent-a", include_closed=True)
        assert len(closed) == 1
        assert closed[0]["valid_to"] == "2026-05-30T00:00:00Z"
        assert closed[0]["valid_from"] is not None

        # Superseding again is a no-op (no active row).
        assert supersede_edge(
            conn, src_entity_id="ent-a", dst_entity_id="ent-b", edge_type="worked_with"
        ) is None

        # The triple can become active again — history accumulates.
        update_edge(conn, src_entity_id="ent-a", dst_entity_id="ent-b", edge_type="worked_with",
                    event_at="2026-06-15T00:00:00Z")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM entity_edges").fetchone()[0] == 2
        active = top_edges(conn, "ent-a")
        assert len(active) == 1 and active[0]["valid_to"] is None

    def test_active_row_uniqueness_enforced(self, conn) -> None:
        from topos.features.entities.edges import update_edge

        update_edge(conn, src_entity_id="x", dst_entity_id="y", edge_type="worked_with")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO entity_edges
                   (edge_id, src_entity_id, dst_entity_id, edge_type, weight, valid_from)
                   VALUES ('edg_dup', 'x', 'y', 'worked_with', 1.0, '2026-01-01')"""
            )

    def test_supersede_with_successor(self, conn) -> None:
        from topos.features.entities.edges import supersede_edge, top_edges, update_edge

        _seed_entity(conn, "ent-a", "Maren Oxbow")
        _seed_entity(conn, "ent-b", "Brindle Cassavetes")
        _seed_entity(conn, "ent-c", "Quillon Marsh")
        update_edge(conn, src_entity_id="ent-a", dst_entity_id="ent-b", edge_type="worked_with")
        supersede_edge(
            conn, src_entity_id="ent-a", dst_entity_id="ent-b", edge_type="worked_with",
            successor={"dst_entity_id": "ent-c", "event_at": "2026-06-01T00:00:00Z"},
        )
        conn.commit()
        active = top_edges(conn, "ent-a")
        assert [e["entity_id"] for e in active] == ["ent-c"]
        closed = [e for e in top_edges(conn, "ent-a", include_closed=True) if e["valid_to"]]
        # Chain semantics: successor.valid_from == closed.valid_to.
        assert active[0]["valid_from"] == closed[0]["valid_to"]

    def test_graph_snapshot_exposes_validity_fields(self, conn) -> None:
        from topos.features.entities.edges import graph_snapshot, supersede_edge, update_edge

        _seed_entity(conn, "ent-a", "Maren Oxbow")
        _seed_entity(conn, "ent-b", "Brindle Cassavetes")
        update_edge(conn, src_entity_id="ent-a", dst_entity_id="ent-b", edge_type="worked_with")
        conn.commit()

        graph = graph_snapshot(conn)
        assert len(graph["edges"]) == 1
        edge = graph["edges"][0]
        assert edge["valid_to"] is None
        assert "valid_from" in edge and "last_event_at" in edge

        supersede_edge(conn, src_entity_id="ent-a", dst_entity_id="ent-b", edge_type="worked_with")
        conn.commit()
        assert graph_snapshot(conn)["edges"] == []  # active-only by default
        closed_graph = graph_snapshot(conn, include_closed=True)
        assert len(closed_graph["edges"]) == 1
        assert closed_graph["edges"][0]["valid_to"] is not None


# ------------------------------------------- contract 3: past-shift rendering


class TestEntityContextPastShift:
    def _linked(self):
        return [{"entity_id": "ent-a", "entity_type": "person",
                 "canonical_name": "Maren Oxbow", "match_score": 1.0, "mention_count": 2}]

    def _seed_closed_edge(self, conn) -> None:
        from topos.features.entities.edges import supersede_edge, update_edge

        _seed_entity(conn, "ent-a", "Maren Oxbow")
        _seed_entity(conn, "ent-b", "Brindle Cassavetes")
        update_edge(conn, src_entity_id="ent-a", dst_entity_id="ent-b", edge_type="worked_with",
                    event_at="2026-04-01T00:00:00Z")
        supersede_edge(
            conn, src_entity_id="ent-a", dst_entity_id="ent-b", edge_type="worked_with",
            valid_to="2026-05-30T00:00:00Z",
        )
        conn.commit()

    def test_past_shift_renders_no_longer_current_marker(self, conn) -> None:
        from topos.features.entities.linking import entity_context_items

        self._seed_closed_edge(conn)
        items = entity_context_items(conn, self._linked(), temporal_shift="past")
        # Query-time ensure_dossier prefers entity_dossier; closed edges still
        # render with the T7 staleness marker on that path.
        spine = [
            i
            for i in items
            if i["retrieval_source"] in ("entity_graph", "entity_dossier")
        ]
        assert spine, "closed edge did not surface for a past-tense shift"
        text = spine[0]["summary_text"]
        assert "Brindle Cassavetes" in text
        # T7's oracle greps the exact literal; the form mirrors the fact lane.
        assert re.search(r"no longer current — superseded \d{4}-\d{2}-\d{2}", text)

    def test_default_call_unchanged(self, conn) -> None:
        from topos.features.entities.linking import entity_context_items

        self._seed_closed_edge(conn)
        items = entity_context_items(conn, self._linked())
        assert all("no longer current" not in (i.get("summary_text") or "") for i in items)
        assert all("Brindle" not in (i.get("summary_text") or "") for i in items)

    def test_past_shift_keeps_active_edges_unmarked(self, conn) -> None:
        from topos.features.entities.edges import update_edge
        from topos.features.entities.linking import entity_context_items

        _seed_entity(conn, "ent-a", "Maren Oxbow")
        _seed_entity(conn, "ent-b", "Brindle Cassavetes")
        update_edge(conn, src_entity_id="ent-a", dst_entity_id="ent-b", edge_type="worked_with")
        conn.commit()
        items = entity_context_items(conn, self._linked(), temporal_shift="past")
        spine = [
            i
            for i in items
            if i["retrieval_source"] in ("entity_graph", "entity_dossier")
        ]
        assert spine and "Brindle Cassavetes" in spine[0]["summary_text"]
        assert "no longer current" not in spine[0]["summary_text"]


# ------------------------------------------------------------------ B2.3


class _CaptureAdapters:
    def __init__(self) -> None:
        self.facts = []
        outer = self

        class _Signal:
            def put_fact(self, fact):
                outer.facts.append(fact)
                return fact.get("fact_id")

        self.signal = _Signal()


class TestB23StatDateStamping:
    def _fold_financial(self, conn) -> None:
        from topos.features.stats.engine import StatsEngine

        engine = StatsEngine(conn)
        engine.fold_batch(
            [
                {"record_id": f"f{i}", "_table": "financial_transactions",
                 "occurred_at": f"2026-06-0{i}T12:00:00Z", "category": "kiln",
                 "amount": 40.0 + i}
                for i in (1, 2, 3)
            ]
        )
        self.engine = engine

    def test_promoted_stats_carry_period_and_as_of(self, conn) -> None:
        self._fold_financial(conn)
        adapters = _CaptureAdapters()
        written = self.engine.promote_insights(adapters)
        assert written > 0
        spend = [f for f in adapters.facts if f["record_id"] == "financial.spend.by_category"
                 and not f.get("window")]
        assert spend, "all-time spend insight missing"
        fact = spend[0]
        # Payload keys (the T8 render side reads these).
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", fact["as_of"])
        assert fact["period_end"] == "2026-06-03"  # watermark: last folded event
        assert fact["period_start"] == "2026-06-01"  # earliest daily bucket
        # summary_text carries the window's end date in ISO form (T8 needle shape).
        assert fact["summary_text"].endswith(f"(as of {fact['period_end']})")
        assert "258" not in fact["summary_text"]  # sanity: not the seeded-corpus text

    def test_windowed_insight_period_matches_window(self, conn) -> None:
        from datetime import datetime, timedelta, timezone

        from topos.features.stats.engine import StatsEngine

        engine = StatsEngine(conn)
        fresh = datetime.now(timezone.utc) - timedelta(days=2)
        engine.fold_batch(
            [{"record_id": f"m{i}", "sender_id": "alice", "event_at": fresh.isoformat()}
             for i in range(4)]
        )
        adapters = _CaptureAdapters()
        engine.promote_insights(adapters)
        windowed = [f for f in adapters.facts if str(f.get("window") or "").startswith("d")]
        assert windowed, "no windowed insight promoted"
        fact = windowed[0]
        days = int(fact["window"][1:])
        today = datetime.now(timezone.utc).date()
        assert fact["period_end"] == today.isoformat()
        assert fact["period_start"] == (today - timedelta(days=days)).isoformat()
        assert fact["summary_text"].endswith(f"(as of {fact['period_end']})")

    def test_stable_fact_ids_on_repromote(self, conn) -> None:
        from topos.storage.adapters.factory import AdapterFactory

        self._fold_financial(conn)
        bundle = AdapterFactory.create("local_database", conn=conn)
        self.engine.promote_insights(bundle)
        count_before = conn.execute(
            "SELECT COUNT(*) FROM signal_facts WHERE fact_id LIKE 'stat:%'"
        ).fetchone()[0]
        self.engine.promote_insights(bundle)
        count_after = conn.execute(
            "SELECT COUNT(*) FROM signal_facts WHERE fact_id LIKE 'stat:%'"
        ).fetchone()[0]
        assert count_after == count_before


class TestB23DossierHistory:
    def _store(self, conn):
        from topos.features.signal.signal_object_store import SignalObjectStore

        return SignalObjectStore(conn)

    def _rows(self, conn):
        return conn.execute(
            """SELECT object_id, valid_from, valid_to, payload_json FROM signal_objects
               WHERE object_type='entity_dossier' ORDER BY valid_from"""
        ).fetchall()

    def test_change_supersedes_instead_of_overwriting(self, conn) -> None:
        store = self._store(conn)
        store.upsert_object(
            "relationships", "entity_dossier", "dossier:e1",
            {"summary_text": "v1", "canonical_name": "E One"}, confidence=0.9,
        )
        # Identical payload: refresh, no new revision.
        store.upsert_object(
            "relationships", "entity_dossier", "dossier:e1",
            {"summary_text": "v1", "canonical_name": "E One"}, confidence=0.9,
        )
        assert len(self._rows(conn)) == 1

        # Changed payload: close-and-reinsert — history accumulates.
        store.upsert_object(
            "relationships", "entity_dossier", "dossier:e1",
            {"summary_text": "v2", "canonical_name": "E One"}, confidence=0.9,
        )
        rows = self._rows(conn)
        assert len(rows) == 2
        closed = [r for r in rows if r[2] is not None]
        active = [r for r in rows if r[2] is None]
        assert len(closed) == 1 and len(active) == 1
        assert json.loads(closed[0][3])["summary_text"] == "v1"
        assert json.loads(active[0][3])["summary_text"] == "v2"
        # Chain: the new revision starts where the old one closed.
        assert active[0][1] == closed[0][2]

    def test_non_dossier_types_still_update_in_place(self, conn) -> None:
        store = self._store(conn)
        first = store.upsert_object(
            "work", "work_context_summary", "wcs:current", {"summary": "v1"}, confidence=0.5,
        )
        second = store.upsert_object(
            "work", "work_context_summary", "wcs:current", {"summary": "v2"}, confidence=0.5,
        )
        assert first["object_id"] == second["object_id"]
        count = conn.execute(
            "SELECT COUNT(*) FROM signal_objects WHERE object_key='wcs:current'"
        ).fetchone()[0]
        assert count == 1


# ------------------------------------------------------------------ B2.4


class TestB24Episodes:
    def test_migration_creates_episodes_table(self, conn) -> None:
        cols = _columns(conn, "episodes")
        assert {
            "episode_id", "source_id", "sync_batch_id", "dataset_id",
            "started_at", "finished_at", "n_records", "posture", "role_mix_json",
        } <= cols

    def test_pipeline_records_one_episode_per_batch(self, conn, monkeypatch) -> None:
        import topos.core.state as core_state
        from topos.ingestion.canonical_pipeline import run_post_canonical_pipeline

        monkeypatch.setattr(core_state, "get_db_connection", lambda: conn)

        source_def = SimpleNamespace(
            source_id="demo_journal_file",
            canonical_group_id=None,
            canonical_mapper_id=None,
            enrichment_trigger="manual",
            signal_derivation_jobs=[],
            canonical_enrichment_jobs=[],
            posture="journal",
        )
        records = [
            {"message_id": "m1", "dataset_id": "ds-1", "_table": "journal_entries"},
            {"message_id": "m2", "dataset_id": "ds-1", "_table": "journal_entries"},
        ]
        outcome = asyncio.run(
            run_post_canonical_pipeline(
                source_def=source_def,
                canonical_records=records,
                sync_batch_id="batch-42",
                run_signal=False,
                run_enrichment=False,
            )
        )
        assert outcome.get("episode_id")
        row = conn.execute(
            """SELECT source_id, sync_batch_id, dataset_id, n_records, posture,
                      started_at, finished_at, role_mix_json
               FROM episodes WHERE episode_id=?""",
            (outcome["episode_id"],),
        ).fetchone()
        assert row is not None
        source_id, sync_batch_id, dataset_id, n_records, posture, started, finished, role_mix = row
        assert (source_id, sync_batch_id, dataset_id, n_records) == (
            "demo_journal_file", "batch-42", "ds-1", 2,
        )
        assert posture == "journal"
        assert started and finished and started <= finished
        # role_mix is best-effort: NULL until the provenance roles module
        # lands (sibling workstream), valid JSON after.
        if role_mix is not None:
            assert isinstance(json.loads(role_mix), dict)

    def test_empty_batch_records_no_episode(self, conn, monkeypatch) -> None:
        import topos.core.state as core_state
        from topos.ingestion.canonical_pipeline import run_post_canonical_pipeline

        monkeypatch.setattr(core_state, "get_db_connection", lambda: conn)
        outcome = asyncio.run(
            run_post_canonical_pipeline(
                source_def=SimpleNamespace(source_id="s"),
                canonical_records=[],
                sync_batch_id="batch-0",
            )
        )
        assert "episode_id" not in outcome
        assert conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0] == 0


# ------------------------------------------------------------------ P4.4


class TestP44AssertedBy:
    def test_default_is_owner_and_renders_without_attribution(self, conn) -> None:
        from topos.features.facts.store import FactStore

        store = FactStore(conn)
        fact = store.assert_fact(
            subject_entity_id="e1", predicate="works_at", object_value="Topos",
        )
        assert fact["payload"]["asserted_by"] == "owner"
        assert FactStore.render(fact) == "owner works at Topos"

    def test_non_owner_assertion_renders_attribution(self, conn) -> None:
        from topos.features.facts.store import FactStore

        store = FactStore(conn)
        fact = store.assert_fact(
            subject_entity_id="e2", predicate="lives_in", object_value="Lisbon",
            asserted_by="contact:ana",
        )
        assert fact["payload"]["asserted_by"] == "contact:ana"
        rendered = FactStore.render(fact)
        assert rendered.endswith(" — per contact:ana")
        assert rendered.startswith("owner lives in Lisbon")
