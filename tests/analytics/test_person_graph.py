"""The person-centric graph's charter. Every test is an owner decision or a measured defect.

This graph makes claims about who someone knows. A missing person reads as "you don't know
them"; a person with no warmth reads as "you are not close". Absence is a claim here, so the
guards are the feature.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.analytics import person_graph as PG


def _conn():
    c = sqlite3.connect(":memory:")
    c.executescript("""
      CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT,
        canonical_name TEXT, normalized_name TEXT, aliases_json TEXT, is_self INTEGER,
        contact_id TEXT);
      CREATE TABLE entity_edges (edge_id TEXT, src_entity_id TEXT, dst_entity_id TEXT,
        edge_type TEXT);
      CREATE TABLE entity_mentions (mention_id TEXT PRIMARY KEY, entity_id TEXT,
        record_id TEXT, source_id TEXT, authored_by_owner INTEGER);
      CREATE TABLE messenger_dyad_stats (dataset_id TEXT, a_key TEXT, b_key TEXT,
        involves_self INTEGER, peer_class TEXT, total_msgs INTEGER);
      CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, display_name TEXT);
      CREATE TABLE contact_identifiers (contact_id TEXT, identifier TEXT, identifier_type TEXT);
    """)
    return c


def _dyad(c, peer, msgs=10, klass="human", ds="ds"):
    c.execute("INSERT INTO messenger_dyad_stats VALUES (?,?,?,1,?,?)",
              (ds, "self", peer, klass, msgs))


def _person(c, eid, name, is_self=0, contact_id=None):
    c.execute("INSERT INTO entities VALUES (?,?,?,?,?,?,?)",
              (eid, "person", name, str(name).lower(), "[]", is_self, contact_id))


class TestTheOwnerIsOneNode:
    """Extraction emitted the owner THREE times on the live node — `Owner` (1,239 edges),
    `self` (95) and a second `self` (0). Any "me" node built from one of them is wrong."""

    def test_fragmented_selves_collapse_to_the_one_carrying_the_edges(self):
        c = _conn()
        _person(c, "e-owner", "Owner", is_self=1)
        _person(c, "e-self1", "self", is_self=1)
        _person(c, "e-self2", "self", is_self=1)
        for i in range(5):
            c.execute("INSERT INTO entity_edges VALUES (?,?,?,?)",
                      (f"x{i}", "e-owner", f"p{i}", "communicates_with"))
        c.execute("INSERT INTO entity_edges VALUES ('y','e-self2','p9','co_occurrence')")
        owner = PG.resolve_owner_identity(c)
        assert owner["canonical_id"] == "e-owner"
        assert owner["ids"] == {"e-owner", "e-self1", "e-self2"}

    @pytest.mark.parametrize("raw", ["Owner", "self", "me", "", None])
    def test_extraction_artifacts_are_not_what_a_person_calls_themselves(self, raw):
        c = _conn()
        _person(c, "e1", raw, is_self=1)
        assert PG.resolve_owner_identity(c)["label"] == "You"

    def test_the_owner_leads_the_node_list(self):
        """Owner decision D-4: centred. Position is the cheap half of that."""
        c = _conn()
        _person(c, "e-owner", "Owner", is_self=1)
        _dyad(c, "+15551230000", msgs=99)
        nodes = PG.build_person_nodes(c, "ds")
        assert nodes[0]["is_owner"] is True
        assert sum(1 for n in nodes if n["is_owner"]) == 1

    def test_the_owner_is_never_also_a_peer(self):
        c = _conn()
        _person(c, "e-owner", "Owner", is_self=1, contact_id="c-own")
        _dyad(c, "+15551230000")
        c.execute("INSERT INTO contacts VALUES ('c-own','Owner')")
        c.execute("INSERT INTO contact_identifiers VALUES ('c-own','+15551230000','phone')")
        nodes = PG.build_person_nodes(c, "ds")
        assert sum(1 for n in nodes if n["is_owner"]) == 1


class TestEvidenceOnly:
    """Owner decision D-2: the address book is a NAMING source, never a node source.
    Importing it would add ~1,106 people with no evidence of any relationship."""

    def test_an_address_book_row_alone_is_not_a_node(self):
        c = _conn()
        _person(c, "e-stranger", "Someone In My Phone", contact_id="c1")
        c.execute("INSERT INTO contacts VALUES ('c1','Someone In My Phone')")
        assert [n for n in PG.build_person_nodes(c, "ds") if not n["is_owner"]] == []

    def test_a_mention_is_enough(self):
        c = _conn()
        _person(c, "e-m", "Dana")
        c.execute("INSERT INTO entity_mentions VALUES ('m1','e-m','r1','grow_journal',1)")
        people = [n for n in PG.build_person_nodes(c, "ds") if not n["is_owner"]]
        assert [n["label"] for n in people] == ["Dana"]
        assert people[0]["evidence"] == {"messaged": False, "mentioned": True}

    def test_automated_shortcodes_are_excluded_not_merely_ranked_low(self):
        """29 of this node's 180 peers are 2FA/delivery shortcodes. They are not people."""
        c = _conn()
        _dyad(c, "262966", msgs=500, klass="automated")
        _dyad(c, "+15551230000", msgs=5, klass="human")
        keys = [k for n in PG.build_person_nodes(c, "ds") if not n["is_owner"]
                for k in n["messenger_keys"]]
        assert keys == ["+15551230000"]


class TestOneNodePerPerson:
    """Owner decision D-3: somebody who is both a messenger peer and an extracted entity is
    ONE node holding both identities."""

    def test_messenger_and_entity_identities_merge(self):
        c = _conn()
        _person(c, "e-dana", "Dana", contact_id="c-dana")
        c.execute("INSERT INTO contacts VALUES ('c-dana','Dana')")
        c.execute("INSERT INTO contact_identifiers VALUES ('c-dana','+15551230000','phone')")
        c.execute("INSERT INTO entity_mentions VALUES ('m1','e-dana','r1','imessage',1)")
        _dyad(c, "+15551230000", msgs=40)
        people = [n for n in PG.build_person_nodes(c, "ds") if not n["is_owner"]]
        assert len(people) == 1, "one human must not appear as two nodes"
        assert people[0]["evidence"] == {"messaged": True, "mentioned": True}
        assert people[0]["entity_id"] == "e-dana"
        assert people[0]["messenger_keys"] == ["+15551230000"]

    def test_two_handles_for_one_person_are_one_node(self):
        c = _conn()
        _person(c, "e-dana", "Dana", contact_id="c-dana")
        c.execute("INSERT INTO contacts VALUES ('c-dana','Dana')")
        for ident in ("+15551230000", "dana@example.com"):
            c.execute("INSERT INTO contact_identifiers VALUES ('c-dana',?,'phone')", (ident,))
            _dyad(c, ident, msgs=10)
        people = [n for n in PG.build_person_nodes(c, "ds") if not n["is_owner"]]
        assert len(people) == 1
        assert people[0]["message_count"] == 20, "traffic must sum across identities"


class TestEvidenceGatesTheMaths:
    def test_a_mention_only_node_is_marked_as_having_no_cadence(self):
        """No messages means no cadence, so warmth/drift/reciprocity are UNAVAILABLE — not
        zero. A zero would render as "you are not close"."""
        c = _conn()
        _person(c, "e-m", "Dana")
        c.execute("INSERT INTO entity_mentions VALUES ('m1','e-m','r1','grow_journal',1)")
        node = [n for n in PG.build_person_nodes(c, "ds") if not n["is_owner"]][0]
        assert node["evidence"]["messaged"] is False
        assert node["message_count"] == 0


class TestTheNamingQueueIsOrdered:
    """Automatic recovery is exhausted — only 32 of 136 peer numbers are in the address book
    and 30 are already named, so a digit-match recovers ZERO. Order is the whole value."""

    def test_busiest_unknown_first(self):
        c = _conn()
        for peer, n in (("+15550000001", 5), ("+15550000002", 300), ("+15550000003", 50)):
            _dyad(c, peer, msgs=n)
        q = PG.naming_queue(c, "ds")
        assert [r["message_count"] for r in q["queue"]] == [300, 50, 5]

    def test_it_reports_how_much_traffic_the_page_covers(self):
        c = _conn()
        _dyad(c, "+15550000002", msgs=300)
        _dyad(c, "+15550000001", msgs=5)
        q = PG.naming_queue(c, "ds", limit=1)
        assert q["messages_covered_by_this_page"] == 300
        assert q["messages_behind_unnamed"] == 305

    def test_named_people_leave_the_queue(self):
        c = _conn()
        _person(c, "e-dana", "Dana", contact_id="c-dana")
        c.execute("INSERT INTO contacts VALUES ('c-dana','Dana')")
        c.execute("INSERT INTO contact_identifiers VALUES ('c-dana','+15551230000','phone')")
        _dyad(c, "+15551230000", msgs=40)
        assert PG.naming_queue(c, "ds")["unnamed_count"] == 0

    def test_mention_only_people_are_not_in_a_MESSAGING_naming_queue(self):
        c = _conn()
        _person(c, "e-m", "")
        c.execute("INSERT INTO entity_mentions VALUES ('m1','e-m','r1','grow_journal',1)")
        assert PG.naming_queue(c, "ds")["unnamed_count"] == 0


class TestReadsNeverWrite:
    def test_runs_on_a_read_only_connection(self, tmp_path):
        src = _conn()
        _person(src, "e-owner", "Owner", is_self=1)
        _dyad(src, "+15551230000", msgs=12)
        src.commit()
        path = tmp_path / "ro.db"
        dest = sqlite3.connect(path)
        src.backup(dest)
        dest.commit(); dest.close(); src.close()
        ro = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        assert PG.build_person_nodes(ro, "ds")
        PG.naming_queue(ro, "ds")
        ro.close()


class TestLabelsAreSafeToRender:
    """Extraction emits fragments of records as names — `Topos\\n\\nAccomplished` is a real
    canonical_name on the live node. A literal newline in a label produces JSON that strict
    parsers reject outright; it broke a live verification of this very read."""

    def test_control_characters_never_reach_a_label(self):
        c = _conn()
        _person(c, "e1", "Dana\r\n\tReyes\n\nAccomplished")
        c.execute("INSERT INTO entity_mentions VALUES ('m1','e1','r1','grow_journal',1)")
        label = [n for n in PG.build_person_nodes(c, "ds") if not n["is_owner"]][0]["label"]
        assert not any(ch in label for ch in "\r\n\t")
        assert label == "Dana Reyes Accomplished"

    def test_the_whole_graph_serialises(self):
        import json
        c = _conn()
        _person(c, "e-owner", "Owner\n", is_self=1)
        _person(c, "e1", "Dana\nReyes")
        c.execute("INSERT INTO entity_mentions VALUES ('m1','e1','r1','grow_journal',1)")
        _dyad(c, "+15551230000", msgs=4)
        json.dumps(PG.build_person_nodes(c, "ds"))

    def test_a_runaway_label_is_bounded(self):
        c = _conn()
        _person(c, "e1", "x" * 900)
        c.execute("INSERT INTO entity_mentions VALUES ('m1','e1','r1','grow_journal',1)")
        assert len([n for n in PG.build_person_nodes(c, "ds")
                    if not n["is_owner"]][0]["label"]) <= 120


class TestEdgesCarryTheirAttribution:
    """Owner decision D-1. Four ways an edge can be known, and they are NOT interchangeable:
    one is the owner's lived experience, one their own account of it, one somebody telling
    them about a person, and one somebody else's claim about two OTHER people."""

    def _corpus(self):
        c = _conn()
        _person(c, "e-owner", "Owner", is_self=1)
        _person(c, "e-dana", "Dana")
        _person(c, "e-priya", "Priya")
        _dyad(c, "+15551230000", msgs=40)
        return c

    def _edges(self, c, **kw):
        nodes = PG.build_person_nodes(c, "ds")
        return nodes, PG.build_person_edges(c, "ds", nodes, **kw)

    def test_messaging_is_observed(self):
        c = self._corpus()
        _, edges = self._edges(c)
        obs = [e for e in edges if e["attribution"] == PG.ATTRIBUTION_OBSERVED]
        assert len(obs) == 1 and obs[0]["weight"] == 40

    def test_the_owners_own_mention_is_owner_asserted(self):
        c = self._corpus()
        c.execute("INSERT INTO entity_mentions VALUES ('m1','e-dana','r1','grow_journal',1)")
        _, edges = self._edges(c)
        assert any(e["attribution"] == PG.ATTRIBUTION_OWNER_ASSERTED for e in edges)

    def test_somebody_naming_a_person_TO_the_owner_is_still_first_party(self):
        """The privacy boundary runs between owner-to-person and person-to-person, not
        between authored and received. Withholding received mentions left 189 people
        floating unconnected in a graph that knew exactly how the owner heard of them."""
        c = self._corpus()
        c.execute("INSERT INTO entity_mentions VALUES ('m1','e-dana','r1','imessage',0)")
        _, edges = self._edges(c)
        assert any(e["attribution"] == PG.ATTRIBUTION_RECEIVED for e in edges)

    def test_an_edge_between_two_OTHER_people_is_off_by_default(self):
        c = self._corpus()
        for eid in ("e-dana", "e-priya"):
            c.execute("INSERT INTO entity_mentions VALUES (?,?,?,?,0)",
                      (f"m-{eid}", eid, "shared-record", "imessage"))
        _, default_edges = self._edges(c)
        assert not [e for e in default_edges
                    if e["attribution"] == PG.ATTRIBUTION_THIRD_PARTY], \
            "a claim about two non-consenting third parties must not render unasked"
        _, opted_in = self._edges(c, include_third_party=True)
        assert [e for e in opted_in if e["attribution"] == PG.ATTRIBUTION_THIRD_PARTY]

    def test_the_owners_own_account_of_two_people_meeting_is_not_gated(self):
        """"I met Dana with Priya" is the owner's own memory of their own life."""
        c = self._corpus()
        for eid in ("e-dana", "e-priya"):
            c.execute("INSERT INTO entity_mentions VALUES (?,?,?,?,1)",
                      (f"m-{eid}", eid, "my-journal-entry", "grow_journal"))
        _, edges = self._edges(c)
        co = [e for e in edges if e["kind"] == "co_mentioned"]
        assert co and all(e["attribution"] == PG.ATTRIBUTION_OWNER_ASSERTED for e in co)

    def test_no_edge_points_at_a_node_that_does_not_exist(self):
        c = self._corpus()
        c.execute("INSERT INTO entity_mentions VALUES ('m1','e-dana','r1','grow_journal',1)")
        nodes, edges = self._edges(c, include_third_party=True)
        ids = {n["node_id"] for n in nodes}
        for e in edges:
            assert e["source"] in ids and e["target"] in ids

    def test_no_self_loops(self):
        c = self._corpus()
        c.execute("INSERT INTO entity_mentions VALUES ('m1','e-owner','r1','grow_journal',1)")
        _, edges = self._edges(c)
        assert all(e["source"] != e["target"] for e in edges)
