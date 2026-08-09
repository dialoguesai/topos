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
    # TimelineJob projects its rows on a worker thread (asyncio.to_thread), so
    # the injected connection must allow cross-thread use, matching how
    # core.state opens every real connection.
    c = sqlite3.connect(str(tmp_path / "planner.db"), check_same_thread=False)
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

    def test_injected_now_drives_month_arithmetic(self, conn) -> None:
        """now= threading (B1.1): the same bare-month query resolves against
        the injected instant, not the wall clock."""
        july = build_query_plan(conn, "what was my setup in January?", now=_NOW)
        assert july.as_of == "2026-01-31"
        jan = build_query_plan(
            conn, "what was my setup in January?",
            now=datetime(2026, 1, 15, tzinfo=timezone.utc),
        )
        assert jan.as_of == "2025-01-31"


class TestAsOfDerivation:
    """B1.1: 'in <Month> [year]' → last day of that (past) month, ISO date."""

    def test_explicit_month_and_year(self, conn) -> None:
        plan = build_query_plan(conn, "What was my studio space in May 2026?", now=_NOW)
        assert plan.as_of == "2026-05-31"
        assert plan.to_meta()["as_of"] == "2026-05-31"

    def test_bare_past_month_resolves_this_year(self, conn) -> None:
        plan = build_query_plan(conn, "What was my studio space in March?", now=_NOW)
        assert plan.as_of == "2026-03-31"

    def test_bare_future_month_resolves_last_year(self, conn) -> None:
        # now is July 2026; November hasn't happened yet → November 2025.
        plan = build_query_plan(conn, "where did I stay in November?", now=_NOW)
        assert plan.as_of == "2025-11-30"

    def test_february_leap_year(self, conn) -> None:
        plan = build_query_plan(conn, "what happened in February 2024?", now=_NOW)
        assert plan.as_of == "2024-02-29"

    def test_bare_current_month_is_last_years(self, conn) -> None:
        # "in July" asked during July: the most recent PAST July is last year's.
        assert build_query_plan(conn, "what happened in July?", now=_NOW).as_of == "2025-07-31"

    def test_explicit_current_month_is_present_tense(self, conn) -> None:
        assert build_query_plan(conn, "what happened in July 2026?", now=_NOW).as_of is None

    def test_explicit_future_month_never_anchors(self, conn) -> None:
        assert build_query_plan(conn, "my calendar in March 2027", now=_NOW).as_of is None

    def test_month_with_day_is_a_date_not_an_as_of(self, conn) -> None:
        plan = build_query_plan(conn, "what happened in March 13?", now=_NOW)
        assert plan.as_of is None  # explicit-date path owns this phrase

    def test_no_month_no_as_of(self, conn) -> None:
        assert build_query_plan(conn, "what is my studio space?", now=_NOW).as_of is None


class TestFirstPersonIntent:
    """P3.3 conservative v1: identity/belief phrasings trip; artifact
    possessives must not."""

    @pytest.mark.parametrize(
        "query",
        [
            "What do I actually think about cryptocurrency?",
            "How do I feel about remote work?",
            "What is my opinion on urban beekeeping?",
            "What are my hobbies and interests?",
            "Am I interested in cold plunges?",
            "What's my style preference?",
            "Have I said anything about the merger?",
            "How many messages have I sent in the Harbor Collective group?",
            "Who are the people I talk to and interact with?",
            "What are my current goals and what am I working toward?",
        ],
    )
    def test_positive(self, conn, query) -> None:
        assert build_query_plan(conn, query, now=_NOW).first_person_intent

    @pytest.mark.parametrize(
        "query",
        [
            "my meeting notes from Tuesday",
            "Show me my most recent messages",
            "What is on my calendar, what meetings do I have?",
            "How many messages have I exchanged with my most frequent contact?",
            "What do I know about my contact Alex?",
            "What moods do I record most often in my journal?",
            "How often do I message people — what is my messaging cadence?",
            "What was I doing last week?",
            "What is my current studio space?",
            "Where was my studio space before?",
            "when am I free next week",
            "Tell me about Luc and how often we talk",
        ],
    )
    def test_negative(self, conn, query) -> None:
        assert not build_query_plan(conn, query, now=_NOW).first_person_intent

    def test_belief_subclass(self, conn) -> None:
        plan = build_query_plan(conn, "What do I think about glazes?", now=_NOW)
        assert plan.first_person_belief
        # goals possessive is first-person but NOT belief (goal store answers it;
        # no message-row hard filter).
        goals = build_query_plan(conn, "What are my current goals?", now=_NOW)
        assert goals.first_person_intent and not goals.first_person_belief

    def test_interaction_browse(self, conn) -> None:
        plan = build_query_plan(conn, "Who do I talk to the most?", now=_NOW)
        assert plan.interaction_browse and plan.first_person_intent
        assert not build_query_plan(
            conn, "Tell me about Luc and how often we talk", now=_NOW
        ).interaction_browse


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
        asyncio.run(TimelineJob().enrich(rows))
        stored = conn.execute(
            "SELECT record_id, canonical_table FROM timeline ORDER BY event_at"
        ).fetchall()
        assert [r[0] for r in stored] == ["j1", "c1"]
        # idempotent re-run
        asyncio.run(TimelineJob().enrich(rows))
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
