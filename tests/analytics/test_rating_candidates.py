"""The ratings door's charter: who is a candidate, in what order, from which rows, how fast.

The calibration screen's ratings card used to read the whole person graph (10 s on the
owner's node, on the app's loop) and keep five rows of it. The door answers the same
question from the dyad stats, the identity spine, the overlay and the disposition facts.
Every test here is a parity claim against the graph's own `worthRating` filter, or a
measured cost: the door must serve the people the graph would, in the graph's order,
from a fraction of the rows, in milliseconds.
"""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from topos.analytics import person_overlay as overlay
from topos.analytics.rating_candidates import RATED_DOMAINS, rated_domains, worth_rating
from topos.analytics.relationship_reads import read_person_graph, read_rating_candidates

DS = "ds"

#: The fields the app's card reads off a `PersonNode`, every one on every served row.
CARD_FIELDS = {"node_id", "entity_id", "label", "message_count", "mention_count", "needs_name",
               "is_owner", "evidence", "messenger_keys", "contact_id"}


def _conn(path=":memory:"):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT,
        canonical_name TEXT, normalized_name TEXT, aliases_json TEXT, is_self INTEGER,
        contact_id TEXT);
      CREATE INDEX idx_entities_contact ON entities(contact_id) WHERE contact_id IS NOT NULL;
      CREATE TABLE entity_edges (edge_id TEXT, src_entity_id TEXT, dst_entity_id TEXT,
        edge_type TEXT, weight REAL, metadata_json TEXT);
      CREATE TABLE entity_mentions (mention_id TEXT PRIMARY KEY, entity_id TEXT,
        record_id TEXT, source_id TEXT, authored_by_owner INTEGER);
      CREATE INDEX idx_entity_mentions_entity ON entity_mentions(entity_id);
      CREATE TABLE messenger_dyad_stats (dataset_id TEXT, a_key TEXT, b_key TEXT,
        involves_self INTEGER, peer_class TEXT, total_msgs INTEGER);
      CREATE INDEX idx_messenger_dyad_stats_self
        ON messenger_dyad_stats(dataset_id, involves_self, peer_class);
      CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, display_name TEXT,
        is_self INTEGER DEFAULT 0);
      CREATE TABLE contact_identifiers (contact_id TEXT, identifier TEXT, identifier_type TEXT);
      CREATE TABLE signal_objects (object_id TEXT PRIMARY KEY, object_type TEXT,
        payload_json TEXT, confidence REAL, source_refs_json TEXT, valid_from TEXT,
        valid_to TEXT);
      CREATE TABLE conversation_messages (dataset_id TEXT, message_id TEXT, content TEXT,
        is_from_self INTEGER, conversation_id TEXT, sender_id TEXT, event_at TEXT,
        source_id TEXT, reply_to_message_id TEXT);
      CREATE TABLE messenger_social_edges (dataset_id TEXT, period_key TEXT,
        source_scope TEXT, source_id TEXT, target_id TEXT, weight REAL, edge_type TEXT);
    """)
    return c


def _dyad(c, peer, msgs=10, klass="human"):
    c.execute("INSERT INTO messenger_dyad_stats VALUES (?,?,?,1,?,?)", (DS, "self", peer, klass, msgs))


def _person(c, eid, name, is_self=0, contact_id=None, aliases=()):
    c.execute("INSERT INTO entities VALUES (?,?,?,?,?,?,?)",
              (eid, "person", name, str(name).lower(), json.dumps(list(aliases)), is_self, contact_id))


def _contact(c, cid, name, *identifiers):
    c.execute("INSERT INTO contacts VALUES (?,?,0)", (cid, name))
    for ident in identifiers:
        c.execute("INSERT INTO contact_identifiers VALUES (?,?,?)",
                  (cid, ident, "email" if "@" in ident else "phone"))


def _mention(c, eid, n=1):
    for i in range(n):
        c.execute("INSERT INTO entity_mentions VALUES (?,?,?,?,1)",
                  (f"m-{eid}-{i}", eid, f"r-{eid}-{i}", "grow_journal"))


def _rating(c, eid, domain, level="high"):
    c.execute("INSERT INTO signal_objects VALUES (?,?,?,?,?,?,?)", (
        f"fact-{eid}-{domain}", "fact",
        json.dumps({"predicate": "trait.bfi2_domain", "subject_entity_id": eid,
                    "value_struct": {"domain": domain, "level": level}, "asserted_by": "owner"}),
        1.0, "[]", "2026-10-01T00:00:00Z", None))


def _corpus():
    """Every way a messaged person can be, or fail to be, a candidate. Names invented."""
    c = _conn()
    _person(c, "e-owner", "Owner", is_self=1)
    # Named through the contact, with an entity: a candidate. Two handles, one person.
    _contact(c, "c-ava", "Ava Lindqvist", "+15550000001", "ava@example.com")
    _person(c, "e-ava", "Ava Lindqvist", contact_id="c-ava")
    _dyad(c, "+15550000001", 900)
    _dyad(c, "ava@example.com", 100)
    # Named through the contact, no entity of their own; one mentioned person of that name.
    # The graph's fold gives the contact that person's entity, so a rating has a subject.
    _contact(c, "c-bo", "Bo Ferreira", "+15550000002")
    _dyad(c, "+15550000002", 800)
    _person(c, "e-bo", "Bo Ferreira")
    _mention(c, "e-bo", 3)
    # Two messaged contacts of one name and one mentioned person: a question, not a fold.
    _contact(c, "c-cy1", "Cy Okafor", "+15550000003")
    _contact(c, "c-cy2", "Cy Okafor", "+15550000004")
    _dyad(c, "+15550000003", 700)
    _dyad(c, "+15550000004", 650)
    _person(c, "e-cy", "Cy Okafor")
    _mention(c, "e-cy", 2)
    # A nameless handle: nothing to show on a card.
    _dyad(c, "+15550000005", 600)
    # Mentioned, never messaged: nothing to rate.
    _person(c, "e-dee", "Dee Vance")
    _mention(c, "e-dee", 9)
    # Rated on every domain the owner rates: done.
    _contact(c, "c-eli", "Eli Mbeki", "+15550000006")
    _person(c, "e-eli", "Eli Mbeki", contact_id="c-eli")
    _dyad(c, "+15550000006", 500)
    for domain in RATED_DOMAINS:
        _rating(c, "e-eli", domain)
    # Rated on one: still a candidate, and the row carries the rating.
    _contact(c, "c-fay", "Fay Nurmi", "+15550000007")
    _person(c, "e-fay", "Fay Nurmi", contact_id="c-fay")
    _dyad(c, "+15550000007", 400)
    _rating(c, "e-fay", "extraversion", "low")
    # Dismissed by the owner: off the card, still in the database.
    _contact(c, "c-gus", "Gus Petrov", "+15550000008")
    _person(c, "e-gus", "Gus Petrov", contact_id="c-gus")
    _dyad(c, "+15550000008", 300)
    overlay.create_overlay_table(c)
    overlay.record(c, DS, "ent:e-gus", "dismiss")
    # A shortcode is not a person, however much it texts.
    _dyad(c, "22000", 5000, klass="automated")
    c.commit()
    return c


def _graph_candidates(c, **kw):
    nodes = read_person_graph(c, dataset_id=DS, **kw)["nodes"]
    return sorted((n for n in nodes if worth_rating(n)),
                  key=lambda n: (-n["message_count"], n["node_id"]))


class TestWhoIsACandidate:
    def test_the_door_serves_the_people_the_graph_would_rate_in_the_graphs_order(self):
        c = _corpus()
        door = read_rating_candidates(c, dataset_id=DS, limit=50)
        expected = _graph_candidates(c)
        assert [n["node_id"] for n in door["candidates"]] == [n["node_id"] for n in expected]
        assert door["unrated_count"] == len(expected) == 3
        for served, graphed in zip(door["candidates"], expected):
            for field in ("entity_id", "label", "message_count", "needs_name", "contact_id"):
                assert served[field] == graphed[field], field
            assert sorted(served["messenger_keys"]) == sorted(graphed["messenger_keys"])

    def test_who_is_in_and_who_is_out(self):
        c = _corpus()
        rows = {n["entity_id"]: n for n in read_rating_candidates(c, dataset_id=DS, limit=50)["candidates"]}
        assert list(rows) == ["e-ava", "e-bo", "e-fay"], "busiest first"
        assert rows["e-ava"]["message_count"] == 1000, "two handles for one person are one relationship"
        assert rows["e-bo"]["node_id"] == "msg:+15550000002", "the fold keeps the messaged node"
        assert rows["e-fay"]["disposition"]["domains"]["extraversion"]["level"] == "low"
        assert rows["e-fay"]["disposition"]["excluded"] == ["negative_emotionality"]
        assert rated_domains(rows["e-fay"]) == 1
        for absent in ("e-owner", "e-cy", "e-dee", "e-eli", "e-gus"):
            assert absent not in rows

    def test_the_count_says_how_many_in_all_and_the_limit_how_many_now(self):
        c = _corpus()
        out = read_rating_candidates(c, dataset_id=DS, limit=2)
        assert [n["entity_id"] for n in out["candidates"]] == ["e-ava", "e-bo"]
        assert out["unrated_count"] == 3 and out["limit"] == 2
        assert out["rated_domains"] == list(RATED_DOMAINS)
        # eight messaged people (two handles are one), seven of them named
        assert out["coverage"]["messaged_people"] == 8 and out["coverage"]["named"] == 7

    def test_a_split_keeps_a_contact_apart_from_the_mentioned_person_on_both_reads(self):
        """The graph honours a `split` overlay row (no door records one yet, so it is
        written here as the graph reads it); the door runs the same fold with the same
        rows, so the two cannot disagree about who was pulled apart."""
        c = _corpus()
        c.execute(f"INSERT INTO {overlay.OVERLAY_TABLE} VALUES (?,?,?,?,?,?,NULL)",
                  ("ov-split", DS, "msg:+15550000002", "split", "ent:e-bo", "2026-10-02T00:00:00Z"))
        door = read_rating_candidates(c, dataset_id=DS, limit=50)
        assert [n["entity_id"] for n in door["candidates"]] == ["e-ava", "e-fay"]
        assert [n["node_id"] for n in door["candidates"]] == [n["node_id"] for n in _graph_candidates(c)]

    def test_a_name_the_owner_gave_a_handle_reaches_the_fold_on_both_reads(self):
        c = _corpus()
        _person(c, "e-hal", "Hal Brennan")
        _mention(c, "e-hal", 2)
        overlay.record(c, DS, "msg:+15550000005", "rename", "Hal Brennan")
        door = read_rating_candidates(c, dataset_id=DS, limit=50)
        assert "e-hal" in [n["entity_id"] for n in door["candidates"]]
        assert [n["node_id"] for n in door["candidates"]] == [n["node_id"] for n in _graph_candidates(c)]

    def test_every_served_row_has_the_fields_the_card_reads(self):
        c = _corpus()
        for row in read_rating_candidates(c, dataset_id=DS, limit=50)["candidates"]:
            assert CARD_FIELDS <= set(row), CARD_FIELDS - set(row)
            assert row["is_owner"] is False and row["needs_name"] is False
            assert row["evidence"]["messaged"] is True
            assert row["mention_count"] == (3 if row["entity_id"] == "e-bo" else 0)
        json.dumps(read_rating_candidates(c, dataset_id=DS, limit=50))


class TestTheFilterIsTheApps:
    """`worthRating` in the app, to the letter: the two must never disagree about a person."""

    BASE = {"entity_id": "e", "is_owner": False, "needs_name": False, "dismissed": False, "message_count": 5}

    @pytest.mark.parametrize("change, expected", [
        ({}, True),
        ({"entity_id": None}, False),
        ({"is_owner": True}, False),
        ({"needs_name": True}, False),
        ({"dismissed": True}, False),
        ({"message_count": 0}, False),
        ({"disposition": {"domains": {d: {"level": "high"} for d in RATED_DOMAINS}}}, False),
        ({"disposition": {"domains": {d: {"level": "high"} for d in RATED_DOMAINS[:3]}}}, True),
        # the withheld domain never counts toward "fully rated"
        ({"disposition": {"domains": {**{d: {"level": "high"} for d in RATED_DOMAINS[:3]},
                                      "negative_emotionality": {"level": "high"}}}}, True),
    ])
    def test_worth_rating(self, change, expected):
        assert worth_rating({**self.BASE, **change}) is expected


class TestReadsStayReads:
    def test_runs_on_a_read_only_connection(self, tmp_path):
        path = tmp_path / "rc.db"
        src = _corpus()
        dst = sqlite3.connect(str(path))
        src.backup(dst)
        dst.close()
        ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        ro.row_factory = sqlite3.Row
        assert read_rating_candidates(ro, dataset_id=DS, limit=50)["unrated_count"] == 3
        ro.close()

    def test_a_node_without_the_tables_answers_empty_with_the_reason(self, tmp_path):
        c = sqlite3.connect(str(tmp_path / "bare.db"))
        out = read_rating_candidates(c, dataset_id=DS, limit=5)
        assert out["candidates"] == [] and out["unrated_count"] == 0
        assert "entity extraction" in out["coverage"]["reason"]
        c.execute("CREATE TABLE entities (entity_id TEXT, entity_type TEXT, canonical_name TEXT,"
                  " normalized_name TEXT, aliases_json TEXT, is_self INTEGER, contact_id TEXT)")
        out = read_rating_candidates(c, dataset_id=DS, limit=5)
        assert out["candidates"] == [] and "messaging analytics" in out["coverage"]["reason"]

    def test_the_door_stays_within_the_render_budget_on_a_corpus_sized_fixture(self):
        """Measured on a copy of the owner's database (801 messaged people, 2,097 mentioned,
        157 candidates): 54-66 ms, against 7.9-10 s for the person graph. This fixture is
        larger than that corpus, and the budget is the read-cost guard's 300 ms."""
        c = _conn()
        _person(c, "e-owner", "Owner", is_self=1)
        for i in range(3000):
            peer = f"+1555{i:07d}"
            _dyad(c, peer, 3000 - i)
            if i % 2 == 0:
                _contact(c, f"c-{i}", f"Person {i} Invented", peer)
                if i % 4 == 0:
                    _person(c, f"e-{i}", f"Person {i} Invented", contact_id=f"c-{i}")
                    if i % 40 == 0:
                        _rating(c, f"e-{i}", "extraversion")
        for i in range(2500):
            _person(c, f"m-{i}", f"Mentioned {i} Invented" if i % 5 else f"Person {2 * i + 1} Invented")
            _mention(c, f"m-{i}", 1 + i % 3)
        c.commit()
        read_rating_candidates(c, dataset_id=DS, limit=25)  # warm
        start = time.perf_counter()
        out = read_rating_candidates(c, dataset_id=DS, limit=25)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert len(out["candidates"]) == 25 and out["unrated_count"] > 25
        assert elapsed_ms < 300, f"rating candidates took {elapsed_ms:.0f}ms"
