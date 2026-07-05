"""P6 tests: query planner, timeline projection, stat routing, end-to-end fact answer."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone

import pytest

from topos.query.planner import QueryPlan, build_query_plan
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "planner.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _seed_entity(conn, name="Maya Chen", mentions=1):
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, mention_count)"
        " VALUES ('ent_maya', 'person', ?, ?, ?)",
        (name, name.lower(), mentions),
    )
    conn.commit()


_NOW = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)  # a Sunday


class TestQueryPlanner:
    def test_entity_linking(self, conn) -> None:
        _seed_entity(conn)
        plan = build_query_plan(conn, "when did I last talk to Maya Chen?", now=_NOW)
        assert [e["canonical_name"] for e in plan.entities] == ["Maya Chen"]
        assert "maya" not in plan.semantic_residual.lower()

    def test_explicit_date_range(self, conn) -> None:
        plan = build_query_plan(conn, "what happened between March 13 and March 16 2026", now=_NOW)
        assert plan.time_range == ("2026-03-13T00:00:00+00:00", "2026-03-16T23:59:59+00:00")

    def test_relative_last_week(self, conn) -> None:
        plan = build_query_plan(conn, "what did I do last week", now=_NOW)
        start, end = plan.time_range
        assert start.startswith("2026-06-2")  # week before the week of Jul 5
        assert end < _NOW.isoformat()

    def test_last_n_days(self, conn) -> None:
        plan = build_query_plan(conn, "show my runs from the last 30 days", now=_NOW)
        assert plan.time_range[0].startswith("2026-06-05")

    def test_aggregate_intent(self, conn) -> None:
        assert build_query_plan(conn, "how often do I go running?", now=_NOW).aggregate_intent
        assert build_query_plan(conn, "what is my average session length", now=_NOW).aggregate_intent
        assert not build_query_plan(conn, "what did Maya say about glazes", now=_NOW).aggregate_intent

    def test_past_shift(self, conn) -> None:
        plan = build_query_plan(conn, "where did I work before Heliograph?", now=_NOW)
        assert plan.temporal_shift == "past"
        assert "work" in plan.dimensions

    def test_bare_day_number_yields_no_range(self, conn) -> None:
        plan = build_query_plan(conn, "can we meet on the 5th?", now=_NOW)
        assert plan.time_range is None  # never fabricate a month

    def test_empty_query(self, conn) -> None:
        plan = build_query_plan(conn, "", now=_NOW)
        assert plan == QueryPlan(query_text="")


class TestTimelineJob:
    def test_batch_writes_timeline_rows(self, conn, monkeypatch) -> None:
        from topos.enrichment.jobs.canonical.timeline_job import TimelineJob

        import topos.enrichment.jobs.canonical.timeline_job as tj

        monkeypatch.setattr(tj, "get_db_connection", lambda: conn)
        rows = [
            {"record_id": "j1", "_table": "journal_entries", "entry_at": "2026-05-04T06:40:00Z"},
            {"record_id": "c1", "_table": "calendar_events", "title": "PT", "starts_at": "2026-06-02T17:00:00Z"},
            {"record_id": "skip", "_table": "journal_entries"},  # no timestamp
        ]
        asyncio.get_event_loop().run_until_complete(TimelineJob().enrich(rows))
        stored = conn.execute(
            "SELECT record_id, canonical_table FROM timeline ORDER BY event_at"
        ).fetchall()
        assert [r[0] for r in stored] == ["j1", "c1"]
        # idempotent re-run
        asyncio.get_event_loop().run_until_complete(TimelineJob().enrich(rows))
        assert conn.execute("SELECT COUNT(*) FROM timeline").fetchone()[0] == 2


class TestStatRouting:
    def test_aggregate_query_surfaces_stat_insights(self, conn, monkeypatch) -> None:
        from topos.features.stats.engine import StatsEngine
        from topos.query.retrieval import _load_stat_insight_items
        from topos.query.manifest_validation import resolve_scope_manifest
        from topos.storage.adapters.factory import AdapterFactory

        engine = StatsEngine(conn)
        engine.fold_batch(
            [
                {"record_id": f"j{i}", "_table": "journal_entries",
                 "entry_at": f"2026-05-{4 + i:02d}T06:40:00Z", "category": "exercise",
                 "duration_minutes": 50 + i}
                for i in range(4)
            ]
        )
        bundle = AdapterFactory.create("local_database", conn=conn)
        engine.promote_insights(bundle)

        manifest = resolve_scope_manifest("health:read")
        items = _load_stat_insight_items(
            conn, "how long are my exercise sessions on average",
            dimensions=["wellbeing"], disclosure_tier="owner_raw", manifest=manifest,
        )
        assert items, "aggregate query found no stat insights"
        assert any("exercise" in i["summary_text"] for i in items)
        assert all(i["retrieval_source"] == "stat_insight" for i in items)

        # and the disclosure gate still binds for non-owner tiers
        blocked = _load_stat_insight_items(
            conn, "how long are my exercise sessions on average",
            dimensions=["wellbeing"], disclosure_tier="default_disclosure", manifest=manifest,
        )
        assert blocked == []


class TestEndToEndPriorEmployer:
    """The audit's canonical miss: answerable via facts + planner, no keyword hacks."""

    def test_prior_employer_answered_from_fact_store(self, conn, monkeypatch) -> None:
        from topos.features.facts.extract import extract_facts_from_batch
        from topos.query.manifest_validation import resolve_scope_manifest
        from topos.query.retrieval import DefaultSignalRetrievalAdapter
        from topos.query.types import RetrievalRequest
        from topos.storage.adapters.factory import AdapterFactory

        conn.execute(
            "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self)"
            " VALUES ('ent_self', 'person', 'Ada Voss', 'ada voss', 1)"
        )
        conn.commit()
        extract_facts_from_batch(
            conn,
            [
                {"_table": "profile_records", "record_id": "prof-001",
                 "record_type": "experience", "title": "Senior ML Engineer",
                 "organization": "Heliograph Labs", "description": "2024–present."},
                {"_table": "profile_records", "record_id": "prof-002",
                 "record_type": "experience", "title": "Data Engineer",
                 "organization": "Lumon Industries", "description": "ETL platform. 2021–2024."},
            ],
        )

        import topos.core.state as state_mod

        monkeypatch.setattr(state_mod, "get_db_connection", lambda: conn)
        # retrieval.py imports get_db_connection from ..core.state lazily inside
        # functions, so patching the module attribute is sufficient.

        bundle = AdapterFactory.create("local_database", conn=conn)
        adapter = DefaultSignalRetrievalAdapter(bundle)
        manifest = resolve_scope_manifest("work_context:read")
        request = RetrievalRequest(
            manifest=manifest,
            access_mode="summary",
            query_text="Where did I work before Heliograph Labs?",
            disclosure_tier="owner_raw",
        )
        result = adapter.retrieve(request)
        summaries = result.context_packet.get("summaries") or []
        assert summaries, "no summaries returned"
        joined = " ".join(str(s.get("summary_text") or "") for s in summaries[:10])
        assert "Lumon Industries" in joined, (
            "prior-employer fact missing from top summaries: "
            + joined[:400]
        )
        fact_hits = [s for s in summaries if s.get("retrieval_source") == "fact"]
        assert fact_hits and "2021–2024" in " ".join(
            str(s.get("summary_text")) for s in fact_hits
        )
