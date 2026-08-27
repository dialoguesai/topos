"""SGU-13 — people who turn up in the same subjects, as a pull on the layout only.

Every constant here was chosen by measuring a failure, and each test pins the failure
rather than the number.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.analytics.person_graph import (
    CONTEXT_MIN_SHARED,
    CONTEXT_STOPWORD_MIN_PEOPLE,
    CONTEXT_STOPWORD_SHARE,
    CONTEXT_TOP_PER_PERSON,
    _context_label_key,
    shared_context_affinity,
)


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "a.db"))
    c.executescript("""
      CREATE TABLE conversation_messages (conversation_id TEXT, message_id TEXT PRIMARY KEY,
        sender_id TEXT, event_at TEXT, is_from_self INTEGER, source_id TEXT,
        reply_to_message_id TEXT, dataset_id TEXT);
      CREATE TABLE topic_cluster_members (member_id TEXT PRIMARY KEY, cluster_id TEXT,
        record_id TEXT, record_type TEXT);
      CREATE TABLE topic_clusters (cluster_id TEXT PRIMARY KEY, label TEXT);
    """)
    yield c
    c.close()


def _person(key, node_id=None):
    return {"node_id": node_id or f"msg:{key}", "band": "core", "is_owner": False,
            "messenger_keys": [key], "entity_id": None}


def _talk(conn, conv, key, clusters, n_from=0):
    """One conversation with `key`, whose messages sit in `clusters`."""
    for i, cid in enumerate(clusters):
        mid = f"m{conv}-{i}-{n_from}"
        conn.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,NULL,'ds')",
                     (conv, mid, key, f"2026-01-{(i % 27) + 1:02d}T09:00:00", 0, "imessage"))
        conn.execute("INSERT INTO topic_cluster_members VALUES (?,?,?,?)",
                     (f"tcm-{mid}", cid, mid, "conversation_message"))


def _cluster(conn, cid, label):
    conn.execute("INSERT INTO topic_clusters VALUES (?,?)", (cid, label))


def test_two_people_in_the_same_subjects_are_pulled_together(conn):
    for cid, label in (("c1", "Blue Hillbillies"), ("c2", "Bandmates"), ("c3", "Karaoke")):
        _cluster(conn, cid, label)
    _talk(conn, "v1", "+1111", ["c1", "c2", "c3"])
    _talk(conn, "v2", "+2222", ["c1", "c2", "c3"])
    _talk(conn, "v3", "+3333", ["c3"])
    conn.commit()
    out = shared_context_affinity(conn, "ds", [_person("+1111"), _person("+2222"),
                                               _person("+3333")])
    pairs = {(p["source"], p["target"]) for p in out["pairs"]}
    assert ("msg:+1111", "msg:+2222") in pairs
    assert all("msg:+3333" not in p for p in pairs), \
        "one shared subject is a coincidence, not a pattern"


def test_a_subject_almost_everyone_shares_relates_nobody(conn):
    """`Friends` held 37 of 103 people on the live node and `Blackjack Team` 49. A subject
    that broad is not a subject, it is a stopword, and it would have pulled half the graph
    into one clump."""
    _cluster(conn, "wide", "Friends")
    _cluster(conn, "narrow", "Flashbots Community")
    keys = [f"+{i:04d}" for i in range(CONTEXT_STOPWORD_MIN_PEOPLE + 4)]
    for i, k in enumerate(keys):
        _talk(conn, f"v{i}", k, ["wide", "narrow"] if i < 2 else ["wide"])
    conn.commit()
    out = shared_context_affinity(conn, "ds", [_person(k) for k in keys])
    assert any("Friends" in d for d in out["coverage"]["dropped_as_too_broad"]), \
        "the wide subject is dropped, and says so"
    assert out["pairs"] == [], "a stopword alone cannot relate anyone"


def test_a_perfect_match_with_nothing_behind_it_does_not_top_the_list(conn):
    """Cosine cannot tell a perfect match from a match with nothing behind it: two people
    whose ONLY two subjects are the same score 1.00. Live, the top of the list was three
    unnamed numbers who had each received the same lead-generation blast."""
    for cid, label in (("s1", "Clear Business Funding"), ("s2", "Google Slides"),
                       ("r1", "Blue Hillbillies"), ("r2", "Bandmates"),
                       ("r3", "Karaoke"), ("r4", "Cedar Springs")):
        _cluster(conn, cid, label)
    _talk(conn, "spam1", "+9001", ["s1", "s2"])
    _talk(conn, "spam2", "+9002", ["s1", "s2"])
    _talk(conn, "real1", "+8001", ["r1", "r2", "r3", "r4"])
    _talk(conn, "real2", "+8002", ["r1", "r2", "r3", "r4"])
    conn.commit()
    out = shared_context_affinity(conn, "ds", [_person(k) for k in
                                               ("+9001", "+9002", "+8001", "+8002")])
    by = {(p["source"], p["target"]): p for p in out["pairs"]}
    thin = by[("msg:+9001", "msg:+9002")]
    thick = by[("msg:+8001", "msg:+8002")]
    assert thick["weight"] > thin["weight"], \
        "four shared subjects must outrank two, even when both matches are perfect"
    assert thin["weight"] < 1.0, "and a two-subject match is never full confidence"


def test_near_duplicate_subject_labels_count_once(conn):
    """The cluster set itself holds `Friends`/`Friend` and `Blackjack Team`/`Blackjack Team
    (httpurl)`. Counted twice, a coincidence is promoted to a pattern."""
    assert _context_label_key("Friends") == _context_label_key("Friend")
    assert _context_label_key("Blackjack Team (httpurl)") == _context_label_key("Blackjack Team")
    assert _context_label_key("Blue Hillbillies") != _context_label_key("Bandmates")


def test_ambient_names_are_left_to_their_own_grouping(conn):
    """Ambient already gets a lobe from `group_ambient_people`; pulling them here too would
    apply the same signal twice."""
    _cluster(conn, "c1", "Blue Hillbillies")
    _cluster(conn, "c2", "Bandmates")
    _talk(conn, "v1", "+1111", ["c1", "c2"])
    _talk(conn, "v2", "+2222", ["c1", "c2"])
    conn.commit()
    people = [_person("+1111"), dict(_person("+2222"), band="ambient")]
    out = shared_context_affinity(conn, "ds", people)
    assert out["pairs"] == []


def test_nobody_is_related_to_half_the_graph(conn):
    """A person in many subjects would otherwise acquire an affinity to everyone, and the
    layout would read their VOLUME as everyone else's closeness."""
    for i in range(30):
        _cluster(conn, f"c{i}", f"Subject {i}")
    # One person turns up in every subject; the rest in a couple each.
    _talk(conn, "hub", "+0000", [f"c{j}" for j in range(30)])
    keys = ["+0000"]
    for i in range(20):
        k = f"+{i + 1:04d}"
        keys.append(k)
        _talk(conn, f"v{i}", k, [f"c{i}", f"c{(i + 1) % 30}", f"c{(i + 2) % 30}"])
    conn.commit()
    out = shared_context_affinity(conn, "ds", [_person(k) for k in keys])
    held = {}
    for p in out["pairs"]:
        held[p["source"]] = held.get(p["source"], 0) + 1
        held[p["target"]] = held.get(p["target"], 0) + 1
    assert out["pairs"], "the hub does share subjects with people"
    hub_pulls = held.get("msg:+0000", 0)
    assert hub_pulls < len(keys) - 1, \
        f"the hub kept a pull to {hub_pulls} of {len(keys) - 1} others — volume became closeness"


def test_the_reason_travels_with_the_pair(conn):
    """The canvas never labels these. The person card can, and cannot if the read drops it:
    a layout that moves people for reasons nothing can state cannot be argued with."""
    for cid, label in (("c1", "Blue Hillbillies"), ("c2", "Bandmates")):
        _cluster(conn, cid, label)
    _talk(conn, "v1", "+1111", ["c1", "c2"])
    _talk(conn, "v2", "+2222", ["c1", "c2"])
    conn.commit()
    out = shared_context_affinity(conn, "ds", [_person("+1111"), _person("+2222")])
    assert out["pairs"][0]["shared"], "the subjects come back with the pair"
    assert out["pairs"][0]["shared_count"] == 2
    assert "layout only" in out["coverage"]["meaning"]


def test_a_small_graph_is_not_told_every_subject_is_too_broad(conn):
    """A share alone is meaningless when few people are covered: at 0.30 with six people
    the bound falls below two and EVERY subject is discarded, so a node with a real answer
    is told it has none."""
    for cid, label in (("c1", "Blue Hillbillies"), ("c2", "Bandmates")):
        _cluster(conn, cid, label)
    _talk(conn, "v1", "+1111", ["c1", "c2"])
    _talk(conn, "v2", "+2222", ["c1", "c2"])
    conn.commit()
    out = shared_context_affinity(conn, "ds", [_person("+1111"), _person("+2222")])
    assert out["pairs"], "two people sharing two subjects is an answer, not a stopword"
    assert not out["coverage"]["dropped_as_too_broad"]


def test_floors_are_what_the_docstrings_say():
    assert CONTEXT_MIN_SHARED == 2
    assert CONTEXT_STOPWORD_SHARE == 0.30
    assert CONTEXT_STOPWORD_MIN_PEOPLE == 8
    assert CONTEXT_TOP_PER_PERSON == 6
