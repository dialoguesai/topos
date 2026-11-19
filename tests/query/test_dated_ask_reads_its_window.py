"""A dated ask reads the rows inside its window, however busy the days after it were.

Measured 19 Nov 2026 on a snapshot of a store holding 61,000 coding-session messages:

    "What did I talk about with AI last week?"   -> 0 rows inside last week, 17 outside
    "What did I talk about with AI yesterday?"   -> 3 of yesterday's 277 rows

The window was right both times. The canonical lane never used it to FETCH: it asked for
the newest 50 rows containing a content word of the question ("talk"), and only then kept
the ones inside the window. Three busy days put all 50 after last week, so the window
kept nothing, and the soft fallback returned the out-of-window rows instead. Yesterday's
three were the ones that happened to contain the word.

Two more things rode on that fetch. The question's time words were content needles
("week" had to appear in a row), and so was the verb of the question itself ("talk").
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.planner import build_query_plan, is_dated_activity_query
from topos.query.retrieval import (
    DefaultSignalRetrievalAdapter,
    _load_canonical_summary_items,
)
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
from topos.storage.db.migrations import apply_all_migrations

#: Thursday, early afternoon, US Central.
NOW = datetime.fromisoformat("2026-11-19T13:25:00-06:00")
SOURCE = "claude_code_sessions"


def _insert(conn, message_id: str, event_at: str, content: str, sender: str = "human",
            source: str = SOURCE, conversation: str = "conv") -> None:
    conn.execute(
        "INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, source_id,"
        " content, event_at) VALUES (?, ?, ?, ?, ?, ?)",
        (message_id, conversation, sender, source, content, event_at),
    )


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "dated_ask.db"))
    apply_all_migrations(c)
    CanonicalTablesManager(c)
    # Today and the two days before the window "last week" ends: 150 rows, every one of
    # them containing the question's own verb. These are what crowded the window out.
    for i in range(150):
        day = 17 + (i % 3)  # 17, 18, 19 Nov, UTC afternoon
        _insert(c, f"busy-{i}", f"2026-11-{day:02d}T20:{i % 60:02d}:00.000Z",
                f"let us talk through build step {i} this week")
    # Yesterday (18 Nov local): 40 rows that do NOT contain the verb, spread over the day.
    for i in range(40):
        hour = 14 + (i % 10)  # 08:00 to 17:59 local
        _insert(c, f"yday-{i}", f"2026-11-18T{hour:02d}:{i:02d}:00.000Z",
                f"refactor the tide gauge parser, part {i}")
    # Yesterday evening local is already the 19th in UTC.
    _insert(c, "yday-evening", "2026-11-19T03:30:00.000Z", "wire the harbor chart tooltip")
    # Last week (9 to 15 Nov local): 12 rows, none containing "talk" or "week".
    for i in range(12):
        _insert(c, f"lastweek-{i}", f"2026-11-{10 + (i % 4):02d}T16:{i:02d}:00+00:00",
                f"design the lighthouse rota importer, step {i}")
    # A row stamped with a local offset rather than UTC: 23:30 on the 15th in Chicago.
    _insert(c, "lastweek-offset", "2026-11-15T23:30:00-06:00", "sketch the buoy ledger schema")
    # Older history, and one needle that exists only outside every window asked below.
    for i in range(10):
        _insert(c, f"old-{i}", f"2026-09-{10 + i:02d}T12:00:00Z", f"old note about kelp survey {i}")
    _insert(c, "tacos", "2026-09-02T12:00:00Z", "we should talk about the tacos order")
    # The other AI store: its newest rows are two months old, and they share the verb.
    for i in range(6):
        _insert(c, f"chat-{i}", f"2026-09-{20 + i:02d}T12:00:00Z",
                f"can we talk about the regatta poster draft {i}", source="chatgpt_ui_conversation")
    c.commit()
    yield c
    c.close()


def _canonical_items(conn, question: str, scope: str = "ai_coding_sessions:read"):
    adapters = AdapterFactory.create("local_database", conn=conn)
    plan = build_query_plan(conn, question, now=NOW)
    return plan, _load_canonical_summary_items(
        manifest=resolve_scope_manifest(scope),
        adapters=adapters,
        query_text=question,
        source_ids=[SOURCE],
        disclosure_tier="owner_raw",
        plan=plan,
        conn=conn,
    )


def _ids(items):
    return [str(item.get("record_id")) for item in items]


def test_the_list_adapter_takes_a_window(conn):
    store = AdapterFactory.create("local_database", conn=conn).canonical
    page = store.list(
        "ai_chat_messages", source_id=SOURCE, limit=500,
        event_after="2026-11-09T00:00:00-06:00", event_before="2026-11-15T23:59:59-06:00",
    )
    ids = sorted(str(row.get("record_id") or row.get("message_id")) for row in page.items)
    # The UTC rows and the one written with a local offset, by instant rather than by text:
    # "2026-11-15T23:30:00-06:00" is inside, though its UTC reading is the 16th.
    assert ids == sorted([f"lastweek-{i}" for i in range(12)] + ["lastweek-offset"])
    assert page.total == 13


def test_the_in_memory_adapter_takes_the_same_window():
    store = AdapterFactory.create("memory").canonical
    for record_id, event_at in (("in", "2026-11-12T10:00:00Z"), ("out", "2026-11-17T10:00:00Z")):
        store.upsert("ai_chat_messages", {"record_id": record_id, "source_id": SOURCE,
                                          "content": "x", "event_at": event_at})
    page = store.list("ai_chat_messages", event_after="2026-11-09T00:00:00-06:00",
                      event_before="2026-11-15T23:59:59-06:00")
    assert [row["record_id"] for row in page.items] == ["in"]


def test_last_week_is_found_behind_three_busy_days(conn):
    plan, items = _canonical_items(conn, "What did I talk about with AI last week?")
    assert plan.time_range == ("2026-11-09T00:00:00-06:00", "2026-11-15T23:59:59-06:00")
    ids = _ids(items)
    assert ids, "the window holds 13 rows and none came back"
    assert all(i.startswith("lastweek-") for i in ids), ids
    assert "lastweek-offset" in ids
    assert len(ids) == 13


def test_yesterday_is_the_whole_day_not_the_rows_that_share_the_verb(conn):
    plan, items = _canonical_items(conn, "What did I talk about with AI yesterday?")
    assert plan.time_range == ("2026-11-18T00:00:00-06:00", "2026-11-18T23:59:59-06:00")
    ids = _ids(items)
    # Rows of the 18th that say nothing about "talk", and the evening one whose UTC date
    # is the 19th. The busy rows stamped 20:xx UTC on the 18th are inside the day too.
    assert "yday-evening" in ids
    assert sum(1 for i in ids if i.startswith("yday-")) >= 8, ids
    for item in items:
        assert str(item["record_id"]).startswith(("yday-", "busy-")), item["record_id"]


def test_a_browsed_window_is_sampled_across_itself(conn):
    """91 rows sit inside yesterday; the page is 50. Newest-first would be the evening only."""
    _, items = _canonical_items(conn, "What did I talk about with AI yesterday?")
    hours = {str(item.get("event_at"))[11:13] for item in items}
    assert len(hours) >= 6, sorted(hours)


def test_a_specific_ask_still_has_to_match(conn):
    """The window narrows a search. It does not replace the subject of one."""
    _, items = _canonical_items(conn, "What did I say about the tide gauge yesterday?")
    ids = _ids(items)
    assert ids and all(i.startswith("yday-") and i != "yday-evening" for i in ids), ids


def test_a_subject_absent_from_the_window_is_not_answered_with_the_window(conn):
    plan, items = _canonical_items(conn, "What did I say about the tacos order yesterday?")
    assert plan.time_range is not None
    # The only "tacos" row is from September. It may come back flagged out of window (the
    # existing soft fallback); what must not come back is yesterday's unrelated rows
    # presented as the answer.
    assert not any(str(item["record_id"]).startswith("yday-") for item in items), _ids(items)


def test_an_empty_window_keeps_the_existing_soft_fallback(conn):
    plan, items = _canonical_items(conn, "What did I talk about with AI on 2026-10-05?")
    assert plan.time_range == ("2026-10-05T00:00:00-06:00", "2026-10-05T23:59:59-06:00")
    # This lane's own rule is unchanged: with nothing inside the window it still hands
    # back what the words match, and `_prefer_time_window` flags it out of window.
    assert items, "an empty window used to degrade to dated out-of-window rows, and still must"
    assert all(str(item["record_id"]).startswith(("busy-", "tacos")) for item in items), _ids(items)


def test_no_window_no_change(conn):
    _, items = _canonical_items(conn, "What have I said about the kelp survey?")
    assert sorted(_ids(items)) == sorted(f"old-{i}" for i in range(10))


@pytest.mark.parametrize(
    "question",
    [
        "What did I talk about with AI yesterday?",
        "What did I ask Claude Code last week?",
        "What have I been discussing with ChatGPT this week?",
        "What did I chat about with AI on Monday?",
        "What were my AI chats about yesterday?",
        "What did I use AI for yesterday?",
        "Who did I text yesterday?",
        "What did I browse yesterday?",
        "What websites did I visit last Friday?",
        "Where did I go last weekend?",
        "What have I read this week?",
        "Did I talk to anyone yesterday?",
        "What did I ask Claude Code to do yesterday?",
    ],
)
def test_a_dated_conversation_recall_is_a_dated_activity_ask(question):
    assert is_dated_activity_query(question), question


@pytest.mark.parametrize(
    "question",
    [
        "What did Peter say about doing the interview?",
        "What are my long term goals?",
        "Who do I talk to most?",
        "How often do I talk to Sam?",
        "What should I talk about with Sam tomorrow?",
        # A conversation with a particular person is that person's thread, found by
        # identity; these keep the path they had.
        "What did I talk about with my mom last week?",
        "What did I say to Sam yesterday?",
        "What did I ask Sara about the intro?",
        "What did I text my brother yesterday?",
    ],
)
def test_asks_that_are_not_a_recall_of_the_owners_own_activity(question):
    assert not is_dated_activity_query(question), question


def test_the_owner_packet_for_yesterday_holds_only_yesterday(conn, monkeypatch):
    """End to end for the owner: an empty day says so, and a full day is only that day."""
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    adapter = DefaultSignalRetrievalAdapter(AdapterFactory.create("local_database", conn=conn))

    def packet(scope: str, installed):
        return adapter.retrieve(
            RetrievalRequest(
                manifest=resolve_scope_manifest(scope),
                access_mode="summary",
                query_text="What did I talk about with AI yesterday?",
                disclosure_tier="owner_raw",
                owner_mode=True,
                installed_source_ids=installed,
                now=NOW,
            )
        ).context_packet

    coding = packet("ai_coding_sessions:read", [SOURCE])
    rows = coding.get("summaries") or []
    assert len(rows) >= 8, [r.get("retrieval_source") for r in rows]
    for row in rows:
        assert row.get("in_time_window") is True, row
        stamp = datetime.fromisoformat(str(row["event_at"]).replace("Z", "+00:00"))
        assert stamp.astimezone(NOW.tzinfo).date().isoformat() == "2026-11-18", row["event_at"]

    # The chat store holds nothing from that day. Before: its September rows came back,
    # flagged out of window, and a small local model spent minutes reading them.
    chat = packet("ai_conversations:read", ["chatgpt_ui_conversation"])
    assert (chat.get("summaries") or []) == []
