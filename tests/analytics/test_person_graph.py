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
      CREATE TABLE conversation_messages (dataset_id TEXT, message_id TEXT, content TEXT,
        is_from_self INTEGER, conversation_id TEXT, sender_id TEXT, event_at TEXT,
        source_id TEXT, reply_to_message_id TEXT);
      CREATE TABLE messenger_social_edges (dataset_id TEXT, period_key TEXT,
        source_scope TEXT, source_id TEXT, target_id TEXT, weight REAL, edge_type TEXT);
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


class TestBandsFollowTheSourceContract:
    """The first draft of this model ranked grow_journal > imessage > browser_visits — three
    source_ids that happen to be on one node, and worthless to anyone who connects Slack.
    Bands key on POSTURE (the source contract) and ROLE (who authored the row) instead."""

    def test_no_connector_name_appears_in_the_salience_path(self):
        """The guard against overfitting to one person's data. The moment a source_id literal
        appears in a band condition, this model has started describing one node again."""
        import inspect
        import re as _re
        from topos.analytics import person_graph as mod

        src = inspect.getsource(mod.classify_band) + inspect.getsource(mod.source_postures)
        for connector in ("grow_journal", "browser_visits", "imessage", "github_activity",
                          "grow_data_file", "chatgpt", "slack", "gmail", "notion"):
            assert connector not in src, f"{connector!r} hardcoded in the salience path"
        # and the band constants themselves must not be connector names
        assert not _re.search(r"_visits|_journal|imessage", " ".join(mod.BAND_ORDER))

    def test_messaging_is_core(self):
        assert PG.classify_band(messaged=True, owner_authored=0, distinct_sources=0,
                                non_ambient_mentions=0, mention_count=0)[0] == PG.BAND_CORE

    def test_an_owner_authored_mention_is_named_whatever_the_posture(self):
        """github_activity is AMBIENT posture, yet 196 of its mentions are owner-authored —
        that is the "GitHub repo owners are relevant, ambient browsing is not" distinction,
        and it must come from the row, not from the connector."""
        band, reason = PG.classify_band(messaged=False, owner_authored=3, distinct_sources=1,
                                        non_ambient_mentions=0, mention_count=3)
        assert band == PG.BAND_NAMED
        assert "wrote their name" in reason

    def test_corroboration_across_sources_is_named(self):
        assert PG.classify_band(messaged=False, owner_authored=0, distinct_sources=2,
                                non_ambient_mentions=0, mention_count=2)[0] == PG.BAND_NAMED

    def test_recurring_non_ambient_mentions_are_discussed(self):
        assert PG.classify_band(messaged=False, owner_authored=0, distinct_sources=1,
                                non_ambient_mentions=4, mention_count=4)[0] == PG.BAND_DISCUSSED

    def test_a_single_ambient_sighting_is_ambient(self):
        band, reason = PG.classify_band(messaged=False, owner_authored=0, distinct_sources=1,
                                        non_ambient_mentions=0, mention_count=1)
        assert band == PG.BAND_AMBIENT and "passing" in reason

    def test_many_ambient_sightings_are_still_ambient(self):
        """38 browser sightings of a name is not a relationship. Volume alone never promotes."""
        assert PG.classify_band(messaged=False, owner_authored=0, distinct_sources=1,
                                non_ambient_mentions=0, mention_count=38)[0] == PG.BAND_AMBIENT

    def test_every_band_states_a_reason(self):
        for kw in ({"messaged": True}, {"owner_authored": 2}, {"distinct_sources": 3},
                   {"non_ambient_mentions": 5, "mention_count": 5}, {"mention_count": 1}):
            args = {"messaged": False, "owner_authored": 0, "distinct_sources": 0,
                    "non_ambient_mentions": 0, "mention_count": 0, **kw}
            band, reason = PG.classify_band(**args)
            assert band in PG.BAND_ORDER and len(reason) > 10

    def test_an_unknown_connector_resolves_to_mixed(self):
        """Neither promoted nor buried until its rows say more."""
        c = _conn()
        assert PG.source_postures(c, "ds", {"a_connector_shipped_next_year"}) \
            == {"a_connector_shipped_next_year": "mixed"}


class TestTheOwnerIsNotTheirOwnContact:
    """`is_self` alone missed SIX owner entities on the live node, so the owner was drawn on
    their own social graph as up to six separate people."""

    def test_a_shared_surname_is_never_evidence(self):
        """`Bravo Yankee` and `Charlie Yankee` are real other people on this corpus. A fuzzy
        name rule would have swallowed them into the owner and deleted two humans."""
        c = _conn()
        _person(c, "e-owner", "Owner", is_self=1)
        _person(c, "e-other", "Bravo Yankee")
        c.execute("CREATE TABLE user_identity (key TEXT, display_name TEXT, updated_at TEXT)")
        c.execute("INSERT INTO user_identity VALUES ('k','Jonny Johnson','t')")
        owner = PG.resolve_owner_identity(c)
        assert "e-other" not in owner["ids"], "a surname match must not merge a stranger"
        assert any(x["entity_id"] == "e-other" for x in owner["merge_candidates"])

    def test_the_name_the_owner_gave_the_node_does_merge(self):
        c = _conn()
        _person(c, "e-owner", "Owner", is_self=1)
        _person(c, "e-me", "Jonny Johnson")
        c.execute("CREATE TABLE user_identity (key TEXT, display_name TEXT, updated_at TEXT)")
        c.execute("INSERT INTO user_identity VALUES ('k','Jonny Johnson','t')")
        assert "e-me" in PG.resolve_owner_identity(c)["ids"]

    def test_messaging_yourself_is_not_a_relationship(self):
        """A self-thread arrives as an ordinary peer that resolves to NO entity, so the
        entity check never fires and the owner appears as one of their own contacts."""
        c = _conn()
        _person(c, "e-owner", "Owner", is_self=1)
        c.execute("CREATE TABLE signal_identity (dataset_id TEXT, my_phone_number TEXT,"
                  " my_signal_id TEXT, updated_at TEXT)")
        c.execute("INSERT INTO signal_identity VALUES ('ds','+15125084318',NULL,'t')")
        _dyad(c, "+15125084318", msgs=25)
        assert [n for n in PG.build_person_nodes(c, "ds") if not n["is_owner"]] == []


class TestGroupChatsMakeARealNetwork:
    """Moving from the messaging view to the person view silently dropped every
    peer-to-peer link, so the graph became a star around the owner. Two of your contacts
    being in a room WITH you is something you witnessed — first-party, and it renders."""

    def _msg(self, c, conv, sender, from_self=0, n=1, ds="ds"):
        for i in range(n):
            c.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?,?)",
                      (ds, f"{conv}-{sender}-{i}", "hi", from_self, conv, sender,
                       f"2026-08-0{(i % 8) + 1}T00:00:00Z", "imessage", None))

    def _graph(self, c, **kw):
        nodes = PG.build_person_nodes(c, "ds")
        return nodes, PG.build_person_edges(c, "ds", nodes, **kw)

    def _corpus(self):
        c = _conn()
        _person(c, "e-owner", "Owner", is_self=1)
        for peer in ("+15550000001", "+15550000002", "+15550000003"):
            _dyad(c, peer, msgs=20)
        return c

    def test_two_peers_in_a_group_with_the_owner_are_linked(self):
        c = self._corpus()
        for peer in ("+15550000001", "+15550000002"):
            self._msg(c, "group-1", peer, n=4)
        self._msg(c, "group-1", "self", from_self=1, n=4)
        _, edges = self._graph(c)
        co = [e for e in edges if e["attribution"] == PG.ATTRIBUTION_CO_PRESENT]
        assert len(co) == 1, "a shared room is a relationship the owner saw"

    def test_co_presence_is_first_party_and_not_gated(self):
        """Distinct from a third party ASSERTING that two strangers know each other."""
        c = self._corpus()
        for peer in ("+15550000001", "+15550000002"):
            self._msg(c, "group-1", peer, n=4)
        self._msg(c, "group-1", "self", from_self=1, n=4)
        _, default_edges = self._graph(c)          # include_third_party defaults False
        assert any(e["attribution"] == PG.ATTRIBUTION_CO_PRESENT for e in default_edges)

    def test_a_dm_creates_no_co_presence(self):
        """One peer is not a room."""
        c = self._corpus()
        self._msg(c, "dm-1", "+15550000001", n=6)
        self._msg(c, "dm-1", "self", from_self=1, n=6)
        _, edges = self._graph(c)
        assert not [e for e in edges if e["attribution"] == PG.ATTRIBUTION_CO_PRESENT]

    def test_a_mailing_list_sized_roster_is_skipped(self):
        """Pairing everyone on a 200-person blast would invent n-squared relationships."""
        c = _conn()
        _person(c, "e-owner", "Owner", is_self=1)
        roster = [f"+1555000{i:04d}" for i in range(PG.MAX_CO_PRESENT_ROSTER + 5)]
        for peer in roster:
            _dyad(c, peer, msgs=3)
            self._msg(c, "blast", peer, n=1)
        self._msg(c, "blast", "self", from_self=1, n=1)
        _, edges = self._graph(c)
        assert not [e for e in edges if e["attribution"] == PG.ATTRIBUTION_CO_PRESENT]

    def test_sharing_more_rooms_weighs_more(self):
        c = self._corpus()
        for conv in ("group-1", "group-2"):
            for peer in ("+15550000001", "+15550000002"):
                self._msg(c, conv, peer, n=4)
            self._msg(c, conv, "self", from_self=1, n=4)
        _, edges = self._graph(c)
        co = [e for e in edges if e["attribution"] == PG.ATTRIBUTION_CO_PRESENT]
        assert co and co[0]["weight"] == 2


def test_the_attribution_summary_counts_every_class_present():
    """Adding co-presence left a hardcoded four-key summary silently omitting a whole class
    it had never heard of — the quiet way a summary starts lying about its own data."""
    import sqlite3 as _sq
    from topos.analytics.relationship_reads import read_person_graph

    c = _conn()
    _person(c, "e-owner", "Owner", is_self=1)
    for peer in ("+15550000001", "+15550000002"):
        _dyad(c, peer, msgs=20)
        for i in range(3):
            c.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?,?)",
                      ("ds", f"g-{peer}-{i}", "hi", 0, "group-1", peer,
                       "2026-08-01T00:00:00Z", "imessage", None))
    c.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?,?)",
              ("ds", "g-self", "hi", 1, "group-1", "self", "2026-08-01T00:00:00Z",
               "imessage", None))
    out = read_person_graph(c, dataset_id="ds")
    present = {e["attribution"] for e in out["edges"]}
    assert set(out["attribution"]) == present
    assert out["attribution"].get("co_present", 0) >= 1
