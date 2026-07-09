"""P2 tests: fold/merge algebra properties, StatsEngine folding, promotion, disclosure."""

from __future__ import annotations

import math
import random
import sqlite3
import statistics as pystats

import pytest

from topos.features.stats.fold import fold, init_state, merge, summarize, window_state
from topos.features.stats.engine import StatsEngine, _table_for_row
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "stats.db"))
    apply_all_migrations(c)
    yield c
    c.close()


class TestFoldAlgebra:
    @pytest.mark.parametrize("kind", ["count", "sum", "mean_var", "minmax", "histogram", "distinct"])
    def test_merge_equals_fold_all(self, kind) -> None:
        """merge(fold(A), fold(B)) == fold(A+B) — the incrementality invariant."""
        rng = random.Random(7)
        values = [round(rng.uniform(0, 100), 3) for _ in range(200)]
        if kind in ("histogram", "distinct"):
            values = [str(int(v) % 10) for v in values]

        whole = init_state(kind)
        for v in values:
            whole = fold(kind, whole, v)

        half_a, half_b = init_state(kind), init_state(kind)
        for v in values[:97]:
            half_a = fold(kind, half_a, v)
        for v in values[97:]:
            half_b = fold(kind, half_b, v)
        merged = merge(kind, half_a, half_b)

        assert merged["n"] == whole["n"]
        if kind == "sum":
            assert merged["total"] == pytest.approx(whole["total"])
        if kind == "mean_var":
            assert merged["mean"] == pytest.approx(whole["mean"])
            assert merged["m2"] == pytest.approx(whole["m2"], rel=1e-9)
        if kind == "minmax":
            assert merged["min"] == whole["min"] and merged["max"] == whole["max"]
        if kind == "histogram":
            assert merged["buckets"] == whole["buckets"]

    def test_merge_associative(self) -> None:
        rng = random.Random(11)
        parts = [[rng.gauss(50, 10) for _ in range(30)] for _ in range(3)]
        states = []
        for part in parts:
            s = init_state("mean_var")
            for v in part:
                s = fold("mean_var", s, v)
            states.append(s)
        left = merge("mean_var", merge("mean_var", states[0], states[1]), states[2])
        right = merge("mean_var", states[0], merge("mean_var", states[1], states[2]))
        assert left["mean"] == pytest.approx(right["mean"])
        assert left["m2"] == pytest.approx(right["m2"], rel=1e-9)

    def test_welford_matches_reference_stddev(self) -> None:
        values = [12.0, 55.5, 31.2, 8.8, 47.0, 22.1, 39.9]
        s = init_state("mean_var")
        for v in values:
            s = fold("mean_var", s, v)
        out = summarize("mean_var", s)
        assert out["mean"] == pytest.approx(pystats.mean(values), abs=1e-3)
        assert out["stddev"] == pytest.approx(pystats.stdev(values), abs=1e-3)

    def test_blue_ball_incrementality(self) -> None:
        """3 blue + 4 red, add 1 blue: share updates without a recount."""
        s = init_state("histogram")
        for color in ["blue"] * 3 + ["red"] * 4:
            s = fold("histogram", s, color)
        s = fold("histogram", s, "blue")
        out = summarize("histogram", s)
        blue = next(t for t in out["top"] if t["bucket"] == "blue")
        assert blue["share"] == pytest.approx(4 / 8)

    def test_window_is_merge_of_daily_buckets_no_subtraction(self) -> None:
        buckets = []
        for day_vals in ([10, 20], [30], [40, 50, 60]):
            s = init_state("sum")
            for v in day_vals:
                s = fold("sum", s, v)
            buckets.append(s)
        # window over last 2 "days" = merge of those buckets only
        window = window_state("sum", buckets[1:])
        assert window["total"] == pytest.approx(180.0)
        assert window["n"] == 4

    def test_histogram_entropy(self) -> None:
        s = init_state("histogram")
        for v in ["a", "a", "b", "b"]:
            s = fold("histogram", s, v)
        assert summarize("histogram", s)["entropy_bits"] == pytest.approx(1.0)


def _ai_chat_loader_row(sender_type: str = "human", **kw) -> dict:
    # Exact shape the enrichment batch loader passes (api/enrichment.py):
    # canonical ai_chat rows never carry is_from_self, and canonical
    # sender_type is 'human'|'assistant'|'system' ('user' only in legacy rows).
    row = {
        "message_id": "a1",
        "conversation_id": "chatgpt:conv-1",
        "sender_type": sender_type,
        "sender_id": None,
        "event_at": "2026-06-01T10:00:00+00:00",
        "content": "hello",
        "content_rendered": None,
        "metadata_json": None,
        "sequence": 3,
        "source_id": "chatgpt_file_ingestion",
    }
    row.update(kw)
    return row


class TestTableForRow:
    """P0.2 (PLAN_PROVENANCE_SPLIT): canonical ai_chat rows say sender_type=
    'human', so the old ('user','assistant') check mis-tabled real ai_chat rows
    as conversation_messages. Conversation rows are recognized by their own
    marker keys (is_from_self / event_type / message_type / reply_to_message_id),
    never by sender_type — the messenger mapper also writes 'human' there."""

    def test_ai_chat_human_row_classifies_as_ai_chat(self) -> None:
        assert _table_for_row(_ai_chat_loader_row("human")) == "ai_chat_messages"

    def test_ai_chat_assistant_row_classifies_as_ai_chat(self) -> None:
        assert _table_for_row(_ai_chat_loader_row("assistant")) == "ai_chat_messages"

    def test_ai_chat_system_row_classifies_as_ai_chat(self) -> None:
        assert _table_for_row(_ai_chat_loader_row("system")) == "ai_chat_messages"

    def test_legacy_user_row_still_classifies_as_ai_chat(self) -> None:
        assert _table_for_row(_ai_chat_loader_row("user")) == "ai_chat_messages"

    def test_messenger_human_row_classifies_as_conversation(self) -> None:
        row = {
            "message_id": "m1",
            "conversation_id": "c1",
            "sender_type": "human",  # other person's message, mapper default
            "sender_id": "+15551234567",
            "is_from_self": 0,
            "content": "hey",
            "ts": "2026-06-01T10:00:00+00:00",
        }
        assert _table_for_row(row) == "conversation_messages"

    def test_messenger_null_is_from_self_still_conversation(self) -> None:
        # dict(sqlite3.Row) keeps NULL columns as None — key presence decides.
        row = {
            "message_id": "m2",
            "conversation_id": "c1",
            "sender_type": "human",
            "sender_id": None,
            "is_from_self": None,
            "content": "x",
            "ts": "2026-06-01T10:00:00+00:00",
        }
        assert _table_for_row(row) == "conversation_messages"

    def test_messenger_mapper_payload_shape_classifies_as_conversation(self) -> None:
        # messenger_mapper payload: no is_from_self yet, but carries
        # event_type/message_type/reply_to_message_id keys.
        row = {
            "message_id": "m3",
            "conversation_id": "c1",
            "sender_type": "human",
            "sender_id": "+15551234567",
            "reply_to_message_id": None,
            "message_type": None,
            "event_type": None,
            "content": "x",
            "ts": "2026-06-01T10:00:00+00:00",
        }
        assert _table_for_row(row) == "conversation_messages"

    def test_explicit_table_always_wins(self) -> None:
        row = {"_table": "conversation_messages", "sender_type": "human",
               "conversation_id": "c1"}
        assert _table_for_row(row) == "conversation_messages"

    def test_ai_chat_human_rows_fold_into_ai_chat_stats(self, conn) -> None:
        engine = StatsEngine(conn)
        result = engine.fold_batch(
            [
                _ai_chat_loader_row("human", message_id="a1"),
                _ai_chat_loader_row("human", message_id="a2",
                                    event_at="2026-06-01T11:00:00+00:00"),
            ]
        )
        assert result["rows_folded"] > 0
        state = engine.read_state("ai_chat.hour_of_week")
        assert state["n"] == 2


def _persona_rows():
    return [
        # journals: exercise sessions with durations
        {"record_id": "j1", "_table": "journal_entries", "entry_at": "2026-05-04T06:40:00Z", "category": "exercise", "duration_minutes": 52},
        {"record_id": "j2", "_table": "journal_entries", "entry_at": "2026-05-11T07:05:00Z", "category": "exercise", "duration_minutes": 57},
        {"record_id": "j3", "_table": "journal_entries", "entry_at": "2026-05-06T21:15:00Z", "category": "hobby", "duration_minutes": 110},
        {"record_id": "j4", "_table": "journal_entries", "entry_at": "2026-06-09T21:40:00Z", "category": "hobby", "duration_minutes": 95},
        {"record_id": "j5", "_table": "journal_entries", "entry_at": "2026-06-02T20:00:00Z", "category": "health", "duration_minutes": 45},
        # messages with a contact
        {"record_id": "m1", "_table": "conversation_messages", "ts": "2026-05-30T16:02:00Z", "sender_id": "maya.chen", "conversation_id": "c1"},
        {"record_id": "m2", "_table": "conversation_messages", "ts": "2026-06-02T10:00:00Z", "sender_id": "maya.chen", "conversation_id": "c1"},
        {"record_id": "m3", "_table": "conversation_messages", "ts": "2026-06-05T11:00:00Z", "sender_id": "maya.chen", "conversation_id": "c1"},
        # spend
        {"record_id": "f1", "_table": "financial_transactions", "occurred_at": "2026-06-01T12:00:00Z", "category": "pottery", "amount": 42.5},
        {"record_id": "f2", "_table": "financial_transactions", "occurred_at": "2026-06-03T12:00:00Z", "category": "pottery", "amount": 17.5},
    ]


class TestStatsEngine:
    def test_fold_batch_and_read(self, conn) -> None:
        engine = StatsEngine(conn)
        result = engine.fold_batch(_persona_rows())
        assert result["rows_folded"] > 0

        exercise = engine.read_state("journal.duration.by_category", group_key="exercise")
        out = summarize("mean_var", exercise)
        assert out["n"] == 2 and out["mean"] == pytest.approx(54.5)

        spend = engine.read_state("financial.spend.by_category", group_key="pottery")
        assert spend["total"] == pytest.approx(60.0)

        cadence = engine.read_state("messages.cadence.by_contact", group_key="maya.chen")
        assert cadence["n"] == 2  # two gaps from three messages

    def test_replay_is_idempotent(self, conn) -> None:
        """Re-ingesting the same batch must not double-count (stat_seen guard)."""
        engine = StatsEngine(conn)
        engine.fold_batch(_persona_rows())
        first = engine.read_state("financial.spend.by_category", group_key="pottery")
        result = engine.fold_batch(_persona_rows())
        assert result["rows_folded"] == 0
        assert result["rows_skipped_seen"] > 0
        second = engine.read_state("financial.spend.by_category", group_key="pottery")
        assert second == first

    def test_incremental_update_on_new_row(self, conn) -> None:
        engine = StatsEngine(conn)
        engine.fold_batch(_persona_rows())
        engine.fold_batch(
            [{"record_id": "f3", "_table": "financial_transactions",
              "occurred_at": "2026-06-04T12:00:00Z", "category": "pottery", "amount": 40.0}]
        )
        spend = engine.read_state("financial.spend.by_category", group_key="pottery")
        assert spend["total"] == pytest.approx(100.0) and spend["n"] == 3

    def test_window_read_from_daily_buckets(self, conn) -> None:
        engine = StatsEngine(conn)
        engine.fold_batch(_persona_rows())
        # d90 window over June 2026 data: nothing in range relative to *now*
        # (2026-07+), depends on runtime date; assert bucket rows exist instead.
        rows = conn.execute(
            "SELECT COUNT(*) FROM stat_state WHERE stat_id='financial.spend.by_category' AND bucket_date != ''"
        ).fetchone()
        assert rows[0] == 2  # two distinct transaction days

    def test_promotion_writes_owner_only_facts(self, conn) -> None:
        from topos.storage.adapters.factory import AdapterFactory

        engine = StatsEngine(conn)
        engine.fold_batch(_persona_rows())
        bundle = AdapterFactory.create("local_database", conn=conn)
        written = engine.promote_insights(bundle)
        assert written > 0

        rows = conn.execute(
            "SELECT payload_json FROM signal_facts WHERE fact_id LIKE 'stat:%'"
        ).fetchall()
        assert rows
        import json

        payloads = [json.loads(r[0]) for r in rows]
        assert all(p.get("disclosure") == "owner_only" for p in payloads)
        assert all(p.get("object_type") == "stat_insight" for p in payloads)
        # stable ids: promoting again must not create new rows
        count_before = len(rows)
        engine.promote_insights(bundle)
        count_after = conn.execute(
            "SELECT COUNT(*) FROM signal_facts WHERE fact_id LIKE 'stat:%'"
        ).fetchone()[0]
        assert count_after == count_before


class TestLowCountSumStatsSurviveRefold:
    """Regression: refold_statistics + repromote_stat_insights must not silently
    drop the financial.spend.by_category family. Each spend category naturally
    holds few rows (n=1-2); the count-based _MIN_N noise floor previously excluded
    them from render_insights, so repromote's prune deleted every promoted spend
    stat (the D2 "dining spend" case broke after re-enrichment). A SUM is a total,
    meaningful from a single transaction, so it is exempt from the count floor.
    """

    def _seed_financial(self, conn) -> None:
        rows = [
            ("t1", "checking", "2025-01-05T12:00:00Z", 42.5, "dining", "demo_financial_file"),
            ("t2", "checking", "2025-01-19T12:00:00Z", 17.5, "dining", "demo_financial_file"),
            ("t3", "checking", "2025-01-10T12:00:00Z", 900.0, "salary", "demo_financial_file"),
            ("t4", "checking", "2025-01-22T12:00:00Z", 60.0, "groceries", "demo_financial_file"),
        ]
        conn.executemany(
            "INSERT INTO financial_transactions "
            "(transaction_id, account_type, posted_at, amount, category, source_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()

    def test_refold_repromote_keeps_low_count_spend_stats(self, conn) -> None:
        from topos.features.lifecycle.derived_scrub import (
            refold_statistics,
            repromote_stat_insights,
        )

        self._seed_financial(conn)
        # The exact reported path: wipe + refold from the table, then repromote
        # (which includes the prune that used to delete the spend stats).
        refold_statistics(conn)
        repromote_stat_insights(conn)

        rows = conn.execute(
            "SELECT fact_id FROM signal_facts WHERE fact_id LIKE 'stat:financial.spend.by_category:%'"
        ).fetchall()
        cats = {str(f).rsplit(":", 1)[-1] for (f,) in rows}
        # every category survives, including the two-row 'dining' family (D2)
        assert {"dining", "salary", "groceries"} <= cats, cats

    def test_sum_kind_emitted_below_floor_but_count_kind_still_suppressed(self, conn) -> None:
        from topos.features.stats.insights import render_insights, _MIN_N

        engine = StatsEngine(conn)
        # one spend row (sum, n=1) + two messages from one contact (count, n<_MIN_N)
        engine.fold_batch(
            [
                {"record_id": "f1", "_table": "financial_transactions",
                 "occurred_at": "2025-02-01T12:00:00Z", "category": "dining", "amount": 30.0},
                {"record_id": "m1", "_table": "conversation_messages",
                 "ts": "2025-02-02T10:00:00Z", "sender_id": "sol", "conversation_id": "c1"},
                {"record_id": "m2", "_table": "conversation_messages",
                 "ts": "2025-02-03T10:00:00Z", "sender_id": "sol", "conversation_id": "c1"},
            ]
        )
        insights = render_insights(engine)
        stat_ids = {i["stat_id"] for i in insights}
        assert "financial.spend.by_category" in stat_ids  # sum emitted at n=1
        # a count-family group with n < _MIN_N stays suppressed (floor intact)
        vol = engine.read_state("messages.volume.by_contact", group_key="sol")
        assert int(vol.get("n") or 0) < _MIN_N
        assert not any(
            i["stat_id"] == "messages.volume.by_contact" and i.get("group_key") == "sol"
            for i in insights
        )


class TestDisclosureGate:
    def _manifest(self, signal_objects):
        from topos.query.manifest import ScopeResolutionManifest

        return ScopeResolutionManifest(
            scope_id="wellbeing:read",
            canonical_tables=[],
            signal_objects=signal_objects,
            primary_dimensions=["wellbeing"],
            access_mode_ceiling="summary",
            must_not_retrieve=[],
        )

    def test_owner_tier_sees_stat_insights(self) -> None:
        from topos.query.retrieval import _fact_disclosure_allowed

        fact = {"object_type": "stat_insight", "disclosure": "owner_only"}
        assert _fact_disclosure_allowed(fact, "owner_raw", self._manifest([]))

    def test_default_disclosure_blocks_stat_insights(self) -> None:
        from topos.query.retrieval import _fact_disclosure_allowed

        fact = {"object_type": "stat_insight", "disclosure": "owner_only"}
        assert not _fact_disclosure_allowed(fact, "default_disclosure", self._manifest([]))

    def test_explicit_grant_unblocks(self) -> None:
        from topos.query.retrieval import _fact_disclosure_allowed

        fact = {"object_type": "stat_insight", "disclosure": "owner_only"}
        assert _fact_disclosure_allowed(
            fact, "default_disclosure", self._manifest(["stat_insights"])
        )

    def test_normal_facts_unaffected(self) -> None:
        from topos.query.retrieval import _fact_disclosure_allowed

        fact = {"object_type": "user_goal"}
        assert _fact_disclosure_allowed(fact, "default_disclosure", self._manifest([]))
