"""P1.3 (PLAN_PROVENANCE_SPLIT): role-gated stat families.

- messages.volume.sent.* counts ONLY owner-authored rows (IMB6: "how many
  messages have I sent" must answer 23, never the 2000 thread volume) and
  renders "You sent N messages…".
- ai_chat.hour_of_week folds only owner-typed rows (assistant replies land
  seconds later and doubled every rhythm band).
- activity.visits.by_title carries the {"ledger": "exposure"} payload marker
  (contract 5) through to the promoted fact's stat_summary — the query-side
  selection key for exposure-vs-expression.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.stats import definitions as defs
from topos.features.stats.engine import StatsEngine
from topos.features.stats.insights import render_insights
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "stats.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _conv_row(i: int, *, mine: bool, conversation_id: str = "harbor-1") -> dict:
    return {
        "record_id": f"m{i}",
        "_table": "conversation_messages",
        "conversation_id": conversation_id,
        "sender_type": "human",  # mapper default for EVERYONE — must not decide
        "sender_id": "self" if mine else "+31612345678",
        "is_from_self": 1 if mine else 0,
        "event_at": f"2026-06-{(i % 27) + 1:02d}T10:00:00+00:00",
    }


def _ai_row(i: int, sender_type: str) -> dict:
    return {
        "message_id": f"a{i}",
        "_table": "ai_chat_messages",
        "conversation_id": "chatgpt:conv-1",
        "sender_type": sender_type,
        "sender_id": None,
        "content": "hello",
        "event_at": "2026-06-01T10:00:00+00:00",
    }


class TestSentFamilyFold:
    def test_sent_counts_only_authored_rows(self, conn):
        engine = StatsEngine(conn)
        rows = [_conv_row(i, mine=(i < 3)) for i in range(10)]  # 3 mine, 7 others
        engine.fold_batch(rows)

        total = engine.read_state("messages.volume.sent.total", group_key="self")
        assert int(total.get("n") or 0) == 3

        by_thread = engine.read_state(
            "messages.volume.sent.by_thread", group_key="harbor-1"
        )
        assert int(by_thread.get("n") or 0) == 3

        # thread/contact volume keeps counting everyone — the split is a new
        # family, not surgery on the exposure denominator.
        by_contact = engine.read_state(
            "messages.volume.by_contact", group_key="+31612345678"
        )
        assert int(by_contact.get("n") or 0) == 7

    def test_sender_type_alone_never_counts_as_sent(self, conn):
        # messenger mapper writes 'human' for other people: without self
        # markers, nothing lands in the sent family.
        engine = StatsEngine(conn)
        engine.fold_batch([_conv_row(i, mine=False) for i in range(5)])
        total = engine.read_state("messages.volume.sent.total", group_key="self")
        assert int(total.get("n") or 0) == 0

    def test_sent_insight_renders_you_sent_shape(self, conn):
        # IMB6's oracle greps "you sent N" / "N messages": live stats must
        # produce that shape (the corpus plants the same family id).
        engine = StatsEngine(conn)
        engine.fold_batch([_conv_row(i, mine=True) for i in range(4)])
        insights = render_insights(engine)
        by_id = {}
        for insight in insights:
            by_id.setdefault(insight["stat_id"], insight)
        total = by_id.get("messages.volume.sent.total")
        assert total is not None
        assert total["text"] == "You sent 4 messages overall."
        thread = by_id.get("messages.volume.sent.by_thread")
        assert thread is not None
        assert thread["text"] == "You sent 4 messages in harbor-1."
        assert "you sent 4" in thread["text"].lower()
        assert "4 messages" in thread["text"]


class TestAiChatHourOfWeekGate:
    def test_only_owner_rows_fold(self, conn):
        engine = StatsEngine(conn)
        engine.fold_batch(
            [
                _ai_row(1, "human"),
                _ai_row(2, "human"),
                _ai_row(3, "assistant"),
                _ai_row(4, "system"),
            ]
        )
        state = engine.read_state("ai_chat.hour_of_week")
        assert int(state.get("n") or 0) == 2

    def test_legacy_user_rows_still_fold(self, conn):
        engine = StatsEngine(conn)
        engine.fold_batch([_ai_row(1, "user"), _ai_row(2, "user")])
        state = engine.read_state("ai_chat.hour_of_week")
        assert int(state.get("n") or 0) == 2


class TestExposureLedgerMarker:
    def _visit(self, i: int, title: str) -> dict:
        return {
            "event_id": f"e{i}",
            "_table": "activity_events",
            "activity_type": "visit",
            "url": f"https://x.test/{i}",
            "title": title,
            "occurred_at": "2026-06-01T10:00:00+00:00",
        }

    def test_definition_carries_exposure_payload(self, conn):
        defs.seed_definitions(conn)
        by_id = {d["stat_id"]: d for d in defs.load_enabled_definitions(conn)}
        assert by_id["activity.visits.by_title"]["payload"] == {"ledger": "exposure"}
        # expression-side family carries NO exposure marker
        assert "payload" not in by_id["messages.volume.sent.total"]

    def test_promoted_fact_stat_summary_carries_ledger(self, conn):
        from topos.storage.adapters.factory import AdapterFactory

        engine = StatsEngine(conn)
        engine.fold_batch([self._visit(i, "Kombucha Basics") for i in range(4)])
        bundle = AdapterFactory.create("local_database", conn=conn)
        assert engine.promote_insights(bundle) > 0

        rows = conn.execute(
            "SELECT payload_json FROM signal_facts WHERE fact_id LIKE 'stat:activity.visits.by_title%'"
        ).fetchall()
        assert rows
        for (payload_json,) in rows:
            payload = json.loads(payload_json)
            assert (payload.get("stat_summary") or {}).get("ledger") == "exposure"

    def test_sent_family_fact_has_no_exposure_ledger(self, conn):
        from topos.storage.adapters.factory import AdapterFactory

        engine = StatsEngine(conn)
        engine.fold_batch([_conv_row(i, mine=True) for i in range(4)])
        bundle = AdapterFactory.create("local_database", conn=conn)
        engine.promote_insights(bundle)
        rows = conn.execute(
            "SELECT payload_json FROM signal_facts WHERE fact_id LIKE 'stat:messages.volume.sent%'"
        ).fetchall()
        assert rows
        for (payload_json,) in rows:
            payload = json.loads(payload_json)
            assert (payload.get("stat_summary") or {}).get("ledger") is None
            assert payload["record_id"].startswith("messages.volume.sent")


def test_sent_family_ids_start_with_contract_prefix():
    # Contract 5: authored-gated family ids start with 'messages.volume.sent'.
    sent = [d for d in defs.SEED_DEFINITIONS if d["stat_id"].startswith("messages.volume.sent")]
    assert {d["stat_id"] for d in sent} == {
        "messages.volume.sent.total",
        "messages.volume.sent.by_thread",
    }
    for d in sent:
        assert d["canonical_table"] == "conversation_messages"
        assert d["group_by"] in ("authored_total", "authored_conversation")
