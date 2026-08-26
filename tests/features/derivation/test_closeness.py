"""Close-circle lane — ranked from interaction, not from extracted role facts."""
import sqlite3
from datetime import datetime, timezone

import pytest

from topos.query.closeness import (compute_close_circle, matches_closeness,
                                   try_close_circle)

NOW = datetime(2026, 8, 26, tzinfo=timezone.utc)


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "c.db")
    conn.executescript("""
      CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, display_name TEXT);
      CREATE TABLE contact_identifiers (contact_id TEXT, identifier TEXT, identifier_type TEXT);
      CREATE TABLE conversation_messages (message_id TEXT PRIMARY KEY, sender_id TEXT,
        is_from_self INTEGER DEFAULT 0, event_at TEXT);
    """)
    people = [
        # (contact_id, name, identifier, inbound messages, last contact)
        ("c1", "Mike November", "+1 (512) 740-0415", 206, "2026-08-24T10:00:00+00:00"),
        ("c2", "Alpha Xray", "+15125550164", 164, "2026-08-24T09:00:00+00:00"),
        ("c3", "Alpine Xray", "camille@example.com", 28, "2026-08-19T09:00:00+00:00"),
        ("c4", "Old Colleague", "+15125559999", 12, "2026-01-02T09:00:00+00:00"),
    ]
    n = 0
    for cid, name, ident, count, last in people:
        conn.execute("INSERT INTO contacts VALUES (?,?)", (cid, name))
        conn.execute("INSERT INTO contact_identifiers VALUES (?,?,?)",
                     (cid, ident, "email" if "@" in ident else "phone"))
        for i in range(count):
            n += 1
            # the message carries the RAW handle, never the contact_id
            raw = ident.replace(" ", "").replace("(", "").replace(")", "").replace("-", "")
            # filler must predate every last-contact stamp, or MAX(event_at)
            # would report the filler and cadence would test nothing
            at = last if i == 0 else "2025-11-01T09:00:00+00:00"
            conn.execute("INSERT INTO conversation_messages VALUES (?,?,0,?)",
                         (f"m{n}", raw, at))
    # noise that must never be reported as a person
    for j, junk in enumerate(["Speaker 1", "Speaker 2", "sys", "rec"]):
        conn.execute("INSERT INTO conversation_messages VALUES (?,?,0,?)",
                     (f"j{j}", junk, "2026-08-25T09:00:00+00:00"))
    # an unknown handle with no address-book entry stays anonymous
    conn.execute("INSERT INTO conversation_messages VALUES ('u1','+15125550000',0,?)",
                 ("2026-08-25T09:00:00+00:00",))
    # the owner's own messages are not evidence of closeness to themselves
    conn.execute("INSERT INTO conversation_messages VALUES ('s1','+15127400415',1,?)",
                 ("2026-08-25T09:00:00+00:00",))
    conn.commit()
    return conn


def test_matcher_separates_closeness_from_family():
    assert matches_closeness("Who's in my close circle?")
    assert matches_closeness("who are my closest friends")
    assert matches_closeness("who do I text to most")
    # family is a ROLE question and keeps its fact-based answer
    assert not matches_closeness("Who's in my family?")
    assert not matches_closeness("what medications am I taking")
    # no owner frame
    assert not matches_closeness("who is in the inner circle of the company")


def test_ranks_by_interaction_and_joins_through_identifiers(db):
    got = compute_close_circle(db, now=NOW)
    assert [p["person"] for p in got] == [
        "Mike November", "Alpha Xray", "Alpine Xray", "Old Colleague"]
    assert got[0]["messages"] == 206
    # the join had to normalise "+1 (512) 740-0415" against a raw "+15127400415"
    assert got[0]["last_contact"] == "2026-08-24"


def test_excludes_speaker_labels_unknown_handles_and_own_messages(db):
    names = {p["person"] for p in compute_close_circle(db, now=NOW)}
    assert not names & {"Speaker 1", "Speaker 2", "sys", "rec"}
    assert len(names) == 4          # the unknown handle contributed nobody
    # the owner's outbound message must not inflate Mitch
    assert next(p for p in compute_close_circle(db, now=NOW)
                if p["person"] == "Mike November")["messages"] == 206


def test_cadence_is_relative_to_the_supplied_anchor(db):
    got = {p["person"]: p["cadence_band"] for p in compute_close_circle(db, now=NOW)}
    assert got["Mike November"] == "recent"        # 2 days
    assert got["Alpine Xray"] == "recent"          # 7 days
    assert got["Old Colleague"] == "dormant"        # ~7 months


def test_warmth_is_relative_to_this_corpus(db):
    bands = [p["warmth_band"] for p in compute_close_circle(db, now=NOW)]
    assert bands[0] == "high"       # top quartile of THIS owner's traffic
    assert bands[-1] == "low"


def test_scores_only_never_fires(db):
    assert try_close_circle(db, "Who's in my close circle?",
                            packet_resolution="scores_only") is None
    assert try_close_circle(db, "Who's in my close circle?",
                            packet_resolution="facts_all") is not None


def test_non_closeness_query_falls_through(db):
    assert try_close_circle(db, "Who's in my family?",
                            packet_resolution="facts_all") is None


def test_payload_carries_names_and_answer(db):
    out = try_close_circle(db, "who are my closest friends", packet_resolution="facts_all")
    assert out["answer_type"] == "facts" and out["close_circle_direct"] is True
    assert out["items"][0] == "Mike November"
    assert "Mike November" in out["answer"] and "206 messages" in out["answer"]


def test_empty_corpus_falls_through(tmp_path):
    conn = sqlite3.connect(tmp_path / "e.db")
    conn.executescript("""
      CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, display_name TEXT);
      CREATE TABLE contact_identifiers (contact_id TEXT, identifier TEXT, identifier_type TEXT);
      CREATE TABLE conversation_messages (message_id TEXT PRIMARY KEY, sender_id TEXT,
        is_from_self INTEGER DEFAULT 0, event_at TEXT);
    """)
    assert try_close_circle(conn, "Who's in my close circle?",
                            packet_resolution="facts_all") is None
