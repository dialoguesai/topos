"""The person-centric graph's charter. Every test is an owner decision or a measured defect.

This graph makes claims about who someone knows. A missing person reads as "you don't know
them"; a person with no warmth reads as "you are not close". Absence is a claim here, so the
guards are the feature.
"""

from __future__ import annotations

import json
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
        edge_type TEXT, weight REAL, metadata_json TEXT);
      CREATE TABLE entity_mentions (mention_id TEXT PRIMARY KEY, entity_id TEXT,
        record_id TEXT, source_id TEXT, authored_by_owner INTEGER);
      CREATE TABLE messenger_dyad_stats (dataset_id TEXT, a_key TEXT, b_key TEXT,
        involves_self INTEGER, peer_class TEXT, total_msgs INTEGER);
      CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, display_name TEXT);
      CREATE TABLE contact_identifiers (contact_id TEXT, identifier TEXT, identifier_type TEXT);
      CREATE TABLE signal_objects (object_id TEXT PRIMARY KEY, payload_json TEXT,
        confidence REAL, source_refs_json TEXT, valid_from TEXT);
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
            c.execute("INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id, edge_type) VALUES (?,?,?,?)",
                      (f"x{i}", "e-owner", f"p{i}", "communicates_with"))
        c.execute("INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id, edge_type) VALUES ('y','e-self2','p9','co_occurrence')")
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
        c.execute("INSERT INTO user_identity VALUES ('k','Sierra Yankee','t')")
        owner = PG.resolve_owner_identity(c)
        assert "e-other" not in owner["ids"], "a surname match must not merge a stranger"
        assert any(x["entity_id"] == "e-other" for x in owner["merge_candidates"])

    def test_the_name_the_owner_gave_the_node_does_merge(self):
        c = _conn()
        _person(c, "e-owner", "Owner", is_self=1)
        _person(c, "e-me", "Sierra Yankee")
        c.execute("CREATE TABLE user_identity (key TEXT, display_name TEXT, updated_at TEXT)")
        c.execute("INSERT INTO user_identity VALUES ('k','Sierra Yankee','t')")
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


class TestClosenessIsReciprocityNotVolume:
    """98 of this node's 151 human ties are `broadcast_only` — high volume flowing one way.
    Ranking by message count would put mailing lists ahead of the people the owner talks to."""

    def test_a_broadcaster_is_not_close(self):
        loud = PG.relationship_closeness(
            {"tie_state": "broadcast_only", "total_msgs": 5000, "reciprocal_periods": 0})
        quiet = PG.relationship_closeness(
            {"tie_state": "active", "total_msgs": 40, "reciprocal_periods": 4})
        assert quiet["closeness"] > loud["closeness"]
        assert "nothing comes back" in loud["closeness_reason"]

    def test_more_reciprocal_months_beats_more_messages(self):
        deep = PG.relationship_closeness(
            {"tie_state": "active", "total_msgs": 200, "reciprocal_periods": 5})
        loud = PG.relationship_closeness(
            {"tie_state": "active", "total_msgs": 2000, "reciprocal_periods": 1})
        assert deep["closeness"] > loud["closeness"]

    def test_going_quiet_lowers_it(self):
        recent = PG.relationship_closeness(
            {"tie_state": "active", "total_msgs": 100, "reciprocal_periods": 3,
             "recent_gap_days": 2})
        stale = PG.relationship_closeness(
            {"tie_state": "active", "total_msgs": 100, "reciprocal_periods": 3,
             "recent_gap_days": 300})
        assert recent["closeness"] > stale["closeness"]

    def test_no_messaging_tie_is_unknown_not_zero(self):
        """Zero would place someone the owner has never texted at the same distance as
        someone who ignores them — the record does not say that."""
        c = _conn()
        _person(c, "e-owner", "Owner", is_self=1)
        _person(c, "e-m", "Dana")
        c.execute("INSERT INTO entity_mentions VALUES ('m1','e-m','r1','grow_journal',1)")
        nodes = PG.build_person_nodes(c, "ds")
        PG.attach_closeness(c, "ds", nodes)
        dana = [n for n in nodes if n["label"] == "Dana"][0]
        assert dana["closeness"] is None
        assert "unknown" in dana["closeness_reason"]

    def test_every_score_states_its_reason(self):
        for state in PG.TIE_STATE_CLOSENESS:
            out = PG.relationship_closeness(
                {"tie_state": state, "total_msgs": 30, "reciprocal_periods": 2})
            assert 0.0 <= out["closeness"] <= 1.0 and len(out["closeness_reason"]) > 5


class TestDuplicatesFoldToTheirStrongestSighting:
    """Every duplicate here has the same shape: one `core` node holding the messaging
    identity beside one `named` node holding the extracted entity."""

    def _pair(self, band_a="core", band_b="named", **kw):
        keep = {"node_id": "a", "label": "Dasha", "band": band_a, "band_reason": "msgs",
                "evidence": {"messaged": True, "mentioned": False}, "is_owner": False,
                "message_count": 606, "mention_count": 0, "needs_name": False,
                "entity_id": "ent_a", "contact_id": None, "messenger_keys": ["+1555"],
                "sources": []}
        other = {"node_id": "b", "label": "Dasha", "band": band_b, "band_reason": "named",
                 "evidence": {"messaged": False, "mentioned": True}, "is_owner": False,
                 "message_count": 0, "mention_count": 4, "needs_name": False,
                 "entity_id": "ent_b", "contact_id": None, "messenger_keys": [],
                 "sources": ["grow_journal"]}
        keep.update(kw)
        return [keep, other]

    def test_complementary_sightings_fold_into_one(self):
        out = PG.auto_link_duplicates(self._pair())
        assert len(out) == 1
        assert out[0]["mention_count"] == 4 and out[0]["message_count"] == 606
        assert out[0]["auto_linked"] is True

    def test_differing_ENTITY_ids_do_not_block(self):
        """Extraction emits one human twice — that split IS the duplicate problem, not
        evidence of two people."""
        assert len(PG.auto_link_duplicates(self._pair())) == 1

    def test_differing_CONTACT_ids_DO_block(self):
        """Two address-book entries sharing a name are two people the owner kept apart."""
        pair = self._pair(contact_id="c1")
        pair[1]["contact_id"] = "c2"
        assert len(PG.auto_link_duplicates(pair)) == 2

    def test_two_MESSAGED_nodes_are_never_folded(self):
        """Two phone numbers may be two humans. That is a question for the owner."""
        pair = self._pair()
        pair[1]["evidence"] = {"messaged": True, "mentioned": False}
        pair[1]["message_count"] = 31
        assert len(PG.auto_link_duplicates(pair)) == 2

    def test_the_strongest_band_wins(self):
        out = PG.auto_link_duplicates(self._pair(band_a="ambient", band_b="core"))
        assert out[0]["band"] == PG.BAND_CORE

    def test_a_split_keeps_them_apart(self):
        out = PG.auto_link_duplicates(self._pair(), split_ids=["b"])
        assert len(out) == 2


class TestFactsCanSayWhatBehaviourCannot:
    """A mother texted monthly is closer than a colleague texted daily. Reciprocity
    arithmetic cannot find that.

    But only STATED facts may move the number. Every `rel.closeness_tier` on the live node is
    `altitude: inferred` from `messenger_dyad_stats` — a model reading the same statistics the
    score is built from, so letting it move the score is the graph agreeing with itself.
    """

    def _fact(self, c, *, predicate, altitude, person="Foxtrot Romeo", tier=None, event=None,
              quote=None, confidence=0.9, asserted="owner"):
        payload = json.dumps({
            "subject_entity_id": "e-owner", "predicate": predicate,
            "object_entity_id": None, "confidence": confidence, "altitude": altitude,
            "asserted_by": asserted, "pack": "relationships.social",
            "value_struct": {k: v for k, v in
                             (("person", person), ("tier", tier), ("event", event)) if v},
            **({"quote": quote} if quote else {}),
        })
        c.execute("INSERT INTO signal_objects (object_id, payload_json, confidence,"
                  " source_refs_json) VALUES (?,?,?,?)",
                  (f"o{abs(hash(payload)) % 10**8}", payload, confidence,
                   json.dumps([{"table": "journal_entries", "note": "a journal entry"}])))

    def _corpus(self, messaged=True):
        c = _conn()
        _person(c, "e-owner", "Owner", is_self=1)
        _person(c, "e-friend", "Foxtrot Romeo", contact_id="c-friend")
        c.execute("INSERT INTO entity_mentions VALUES ('m1','e-friend','r1','grow_journal',1)")
        if messaged:
            c.execute("INSERT INTO contacts VALUES ('c-friend','Foxtrot Romeo')")
            c.execute("INSERT INTO contact_identifiers VALUES"
                      " ('c-friend','+15551230000','phone')")
            _dyad(c, "+15551230000", msgs=4)
        return c

    def _closeness(self, c):
        nodes = PG.build_person_nodes(c, "ds")
        PG.attach_closeness(c, "ds", nodes)
        PG.attach_fact_closeness(c, nodes)
        return {n["label"]: n for n in nodes}

    def test_a_stated_tier_raises_a_thin_messaging_tie(self):
        c = self._corpus()
        self._fact(c, predicate="rel.closeness_tier", altitude="stated", tier="inner_circle")
        friend = self._closeness(c)["Foxtrot Romeo"]
        assert friend["closeness"] >= PG.TIER_CLOSENESS["inner_circle"] - 0.01
        assert friend["closeness_source"] == "facts"
        assert "inner circle" in friend["closeness_reason"]

    def test_an_INFERRED_tier_does_not_move_the_number(self):
        """It is a reading of the message statistics the score already uses."""
        c = self._corpus()
        self._fact(c, predicate="rel.closeness_tier", altitude="inferred",
                   tier="inner_circle", asserted="extracted:synthesis")
        friend = self._closeness(c)["Foxtrot Romeo"]
        assert friend["closeness_source"] == "messages"
        assert friend["closeness"] < 0.5

    def test_but_an_inferred_tier_still_reaches_the_card(self):
        """The owner should see it and be able to disagree — it just is not corroboration."""
        c = self._corpus()
        self._fact(c, predicate="rel.closeness_tier", altitude="inferred",
                   tier="close", asserted="extracted:synthesis")
        friend = self._closeness(c)["Foxtrot Romeo"]
        assert friend["relationship_tier"] == "close"
        assert any(f["tier"] == "close" for f in friend["facts"])

    def test_facts_never_push_someone_away(self):
        """A `peripheral` tier must not demote someone the traffic says is close: absence of
        closeness in a fact is not evidence of distance."""
        c = self._corpus()
        self._fact(c, predicate="rel.closeness_tier", altitude="stated", tier="peripheral")
        nodes = PG.build_person_nodes(c, "ds")
        PG.attach_closeness(c, "ds", nodes)
        for n in nodes:
            if not n["is_owner"]:
                n["closeness"] = 0.95
        PG.attach_fact_closeness(c, nodes)
        assert all(n["closeness"] >= 0.95 for n in nodes if not n["is_owner"])

    def test_caregiving_outranks_a_regular_tier(self):
        c = self._corpus()
        self._fact(c, predicate="rel.caregiving", altitude="stated")
        care = self._closeness(c)["Foxtrot Romeo"]["closeness"]
        c2 = self._corpus()
        self._fact(c2, predicate="rel.closeness_tier", altitude="stated", tier="regular")
        tier = self._closeness(c2)["Foxtrot Romeo"]["closeness"]
        assert care > tier

    def test_a_conflict_does_not_lower_closeness(self):
        """A falling-out happens between people who matter to each other."""
        c = self._corpus()
        self._fact(c, predicate="rel.relationship_event", altitude="stated", event="conflict",
                   quote="I spoke very measured.")
        friend = self._closeness(c)["Foxtrot Romeo"]
        assert friend["closeness"] >= PG.EVENT_CLOSENESS["conflict"] - 0.01

    def test_a_fact_about_someone_never_messaged_is_capped(self):
        c = self._corpus(messaged=False)
        self._fact(c, predicate="rel.relationship_event", altitude="stated", event="met")
        friend = self._closeness(c)["Foxtrot Romeo"]
        assert friend["closeness"] <= PG.FACT_CAP_WITHOUT_INTERACTION
        assert "not messaged them" in friend["closeness_reason"]

    def test_the_quote_reaches_the_card(self):
        c = self._corpus()
        self._fact(c, predicate="rel.relationship_event", altitude="stated", event="met",
                   quote="Dasha stayed over last night on the couch.")
        friend = self._closeness(c)["Foxtrot Romeo"]
        assert any("stayed over" in str(f.get("quote")) for f in friend["facts"])

    def test_facts_match_by_name_when_entity_ids_are_absent(self):
        """`object_entity_id` is often null; the fact names its person instead."""
        c = self._corpus()
        self._fact(c, predicate="rel.closeness_tier", altitude="stated", tier="close",
                   person="Foxtrot Romeo")
        assert self._closeness(c)["Foxtrot Romeo"]["closeness_source"] == "facts"


class TestAmbientPeopleGroupByWhatTheyAppearBeside:
    """Ambient is 173 of 437 people and reads as one undifferentiated fringe. It is not one
    thing: classical poets, GitHub collaborators, LinkedIn contacts, film actors, and several
    pieces of software extraction mistook for people."""

    def _corpus(self):
        c = _conn()
        c.executescript("""
          CREATE TABLE topic_clusters (cluster_id TEXT PRIMARY KEY, label TEXT);
          CREATE TABLE topic_cluster_members (member_id TEXT, cluster_id TEXT,
            record_id TEXT, source_id TEXT);
        """)
        _person(c, "e-owner", "Owner", is_self=1)
        return c

    def _ambient(self, c, name, records):
        _person(c, f"e-{name}", name)
        for i, rid in enumerate(records):
            c.execute("INSERT INTO entity_mentions VALUES (?,?,?,?,0)",
                      (f"m-{name}-{i}", f"e-{name}", rid, "browser_visits"))

    def _cluster(self, c, cid, label, records):
        c.execute("INSERT INTO topic_clusters VALUES (?,?)", (cid, label))
        for i, rid in enumerate(records):
            c.execute("INSERT INTO topic_cluster_members VALUES (?,?,?,?)",
                      (f"tcm-{cid}-{i}", cid, rid, "browser_visits"))

    def test_people_sharing_a_topic_become_a_group(self):
        c = self._corpus()
        # The domain must not share a word with the label, or the site-echo guard fires —
        # which is correct behaviour on real data and merely awkward to fixture.
        poets = ["Sappho", "Homer", "Dante"]
        for p in poets:
            self._ambient(c, p, [f"browser:https://litjournal.example/{p}"])
        self._cluster(c, "c1", "Classical Poetry",
                      [f"browser:https://litjournal.example/{p}" for p in poets])
        nodes = PG.build_person_nodes(c, "ds")
        PG.group_ambient_people(c, nodes)
        grouped = {n["label"]: n.get("ambient_group") for n in nodes if n.get("ambient_group")}
        assert set(grouped) == set(poets)
        assert set(grouped.values()) == {"Classical Poetry"}

    def test_a_cluster_named_after_its_own_website_is_rejected(self):
        """`Google Trends` and `YouTube Studio` held 44 of 153 people and would have been the
        two largest groups on screen. Detected by the label echoing the domain, not a list."""
        c = self._corpus()
        people = ["Alice Adams", "Bob Brown", "Carol Clark"]
        for p in people:
            self._ambient(c, p, [f"browser:https://www.youtube.com/watch/{p}"])
        self._cluster(c, "c1", "YouTube Studio",
                      [f"browser:https://www.youtube.com/watch/{p}" for p in people])
        nodes = PG.build_person_nodes(c, "ds")
        PG.group_ambient_people(c, nodes)
        assert not [n for n in nodes if n.get("ambient_group_kind") == "topic"]

    def test_a_site_groups_people_when_no_topic_does(self):
        c = self._corpus()
        for p in ("Dev One", "Dev Two", "Dev Three"):
            self._ambient(c, p, [f"browser:https://github.com/{p}"])
        nodes = PG.build_person_nodes(c, "ds")
        PG.group_ambient_people(c, nodes)
        assert {n.get("ambient_group") for n in nodes if n.get("ambient_group")} == {"github.com"}

    def test_search_engines_never_group_anyone(self):
        """google.com alone holds 42 of this node's ambient people — everyone ever looked up."""
        c = self._corpus()
        for p in ("Someone A", "Someone B", "Someone C"):
            self._ambient(c, p, [f"browser:https://www.google.com/search?q={p}"])
        nodes = PG.build_person_nodes(c, "ds")
        assert PG.group_ambient_people(c, nodes)["grouped"] == 0

    def test_a_pair_is_not_a_group(self):
        c = self._corpus()
        for p in ("Solo One", "Solo Two"):
            self._ambient(c, p, [f"browser:https://example.org/{p}"])
        nodes = PG.build_person_nodes(c, "ds")
        assert PG.group_ambient_people(c, nodes)["grouped"] == 0

    def test_a_group_left_undersized_by_a_bigger_one_is_released(self):
        """A cluster passes the size test on its FULL membership, but larger clusters claim
        shared members first — live that left a group of one on screen."""
        c = self._corpus()
        shared = ["A", "B", "C"]
        for p in shared:
            self._ambient(c, p, [f"browser:https://alpha.example/{p}",
                                 f"browser:https://beta.example/{p}"])
        self._cluster(c, "big", "Russian History",
                      [f"browser:https://alpha.example/{p}" for p in shared])
        self._cluster(c, "small", "Coffee Roasters",
                      [f"browser:https://beta.example/{p}" for p in shared])
        nodes = PG.build_person_nodes(c, "ds")
        PG.group_ambient_people(c, nodes)
        labels = {n.get("ambient_group") for n in nodes if n.get("ambient_group")}
        assert labels == {"Russian History"}, "the smaller duplicate must not survive empty"

    def test_grouping_only_touches_ambient_people(self):
        c = self._corpus()
        _person(c, "e-core", "Core Person", contact_id="c1")
        c.execute("INSERT INTO contacts VALUES ('c1','Core Person')")
        c.execute("INSERT INTO contact_identifiers VALUES ('c1','+15551230000','phone')")
        _dyad(c, "+15551230000", msgs=50)
        for p in ("Amb One", "Amb Two", "Amb Three"):
            self._ambient(c, p, [f"browser:https://example.org/{p}"])
        nodes = PG.build_person_nodes(c, "ds")
        PG.group_ambient_people(c, nodes)
        core = [n for n in nodes if n["label"] == "Core Person"][0]
        assert core.get("ambient_group") is None


class TestAppearancesAreConnectorAgnostic:
    """C-6: mentioned-in-text vs participated-in-a-record, with no source_id switch."""

    def _node(self, **kw):
        row = {
            "node_id": "p1", "entity_id": None, "contact_id": None,
            "messenger_keys": [], "is_owner": False, "label": "Peer",
            "band": "core", "band_reason": "you message them",
        }
        row.update(kw)
        return row

    def test_no_connector_name_in_the_appearance_path(self):
        import inspect
        from topos.analytics import person_graph as mod

        src = "".join(inspect.getsource(fn) for fn in (
            mod.batch_person_appearances,
            mod.person_provenance,
            mod._appearance_mentions,
            mod._appearance_participation,
            mod._load_appearance_record_texts,
            mod._merge_appearances,
        ))
        for connector in ("grow_journal", "browser_visits", "imessage", "github_activity",
                          "grow_data_file", "chatgpt", "slack", "gmail", "notion"):
            assert connector not in src, f"{connector!r} hardcoded in the appearance path"
        assert "source_id =" not in src.replace(" ", "")
        assert "source_id IN" not in src

    def test_a_mention_from_any_source_appears(self):
        c = _conn()
        c.execute("ALTER TABLE entity_mentions ADD COLUMN surface_text TEXT")
        c.execute("ALTER TABLE entity_mentions ADD COLUMN event_at TEXT")
        c.execute("ALTER TABLE entity_mentions ADD COLUMN created_at TEXT")
        c.execute(
            "INSERT INTO entity_mentions VALUES (?,?,?,?,?,?,?,?)",
            ("m1", "e-wiki", "rec-j1", "custom_wiki", 1, "Wiki", "2026-08-01", "2026-08-01"),
        )
        c.execute("""CREATE TABLE journal_entries (entry_id TEXT PRIMARY KEY, content TEXT)""")
        c.execute("INSERT INTO journal_entries VALUES ('rec-j1', 'Had lunch with Wiki at the cafe')")
        node = self._node(node_id="ent:e-wiki", entity_id="e-wiki", label="Wiki")
        out = PG.person_provenance(c, node, limit=6)
        assert out["mentions"], "a named-in-text person must show the line"
        assert out["mentions"][0]["kind"] == PG.APPEARANCE_MENTIONED
        assert out["mentions"][0]["source_id"] == "custom_wiki"
        assert "lunch" in out["mentions"][0]["text"].lower()
        assert out["coverage"]["mentioned"] == 1
        assert out["coverage"]["participated"] == 0

    def test_a_messenger_peer_without_ner_still_shows_the_exchange(self):
        """The bug: 89 DMs, zero entity_mentions, empty card. Participation fills it.

        Source_id is 'slack' on purpose — a connector pull_live has never heard of.
        """
        c = _conn()
        c.execute("""CREATE TABLE conversation_participants (
            conversation_id TEXT, dataset_id TEXT, source_id TEXT, contact_id TEXT)""")
        c.execute("INSERT INTO contacts VALUES ('c-mom','Mom')")
        c.execute("INSERT INTO contact_identifiers VALUES ('c-mom','+15550001111','phone')")
        c.execute(
            "INSERT INTO conversation_participants VALUES ('conv-1','ds','slack','c-mom')"
        )
        c.execute(
            "INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?,?)",
            ("ds", "msg-1", "hi from the other side", 0, "conv-1", "+15550001111",
             "2026-08-20", "slack", None),
        )
        c.execute(
            "INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?,?)",
            ("ds", "msg-2", "owner reply in the thread", 1, "conv-1", "self",
             "2026-08-21", "slack", None),
        )
        node = self._node(
            node_id="msg:+15550001111", contact_id="c-mom",
            messenger_keys=["+15550001111"], label="Mom", message_count=2,
        )
        out = PG.person_provenance(c, node, limit=6)
        kinds = {row["kind"] for row in out["mentions"]}
        sources = {row["source_id"] for row in out["mentions"]}
        assert out["mentions"], "a messaged peer with no NER must still have excerpts"
        assert PG.APPEARANCE_PARTICIPATED in kinds
        assert sources == {"slack"}
        assert out["coverage"]["mentioned"] == 0
        assert out["coverage"]["participated"] >= 1
        assert any(row.get("authored_by_owner") for row in out["mentions"]), (
            "1:1 owner messages belong on the card too"
        )

    def test_unknown_source_id_is_data_not_a_branch(self):
        c = _conn()
        c.execute("ALTER TABLE entity_mentions ADD COLUMN surface_text TEXT")
        c.execute("ALTER TABLE entity_mentions ADD COLUMN event_at TEXT")
        c.execute("ALTER TABLE entity_mentions ADD COLUMN created_at TEXT")
        c.execute(
            "INSERT INTO entity_mentions VALUES (?,?,?,?,?,?,?,?)",
            ("m1", "e1", "r1", "future_connector_xz", 0, "Rousseau", "2026-07-01", None),
        )
        node = self._node(node_id="ent:e1", entity_id="e1", label="Rousseau")
        out = PG.person_provenance(c, node)
        assert out["mentions"][0]["source_id"] == "future_connector_xz"
        assert out["mentions"][0]["source_label"]

    def test_batch_is_keyed_by_node_not_entity(self):
        """Mom has no entity_id. Provenance must still attach to the person node."""
        c = _conn()
        c.execute("""CREATE TABLE conversation_participants (
            conversation_id TEXT, dataset_id TEXT, source_id TEXT, contact_id TEXT)""")
        c.execute(
            "INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?,?)",
            ("ds", "msg-1", "ping", 0, "c1", "+1555", "2026-08-01", "signal", None),
        )
        node = self._node(node_id="msg:+1555", messenger_keys=["+1555"], contact_id=None)
        packed = PG.batch_person_appearances(c, [node], show=6, fetch=8)
        assert packed["msg:+1555"]["mentions"]
        assert packed["msg:+1555"]["participation_total"] >= 1



class TestAFactSaysWhenAndWhatFromV:
    """A fact the owner cannot date or trace back is an assertion, not a record.

    The card shows a sentence about a relationship. Without a date it floats free of the
    life it describes, and without its sources it cannot be checked — which is the whole
    difference between "the record says this" and "trust me".
    """

    def _facts_conn(self):
        c = _conn()
        c.execute("""CREATE TABLE journal_entries (entry_id TEXT PRIMARY KEY, entry_at TEXT,
                     content TEXT, place_name TEXT)""")
        c.execute(
            "INSERT INTO journal_entries VALUES (?,?,?,?)",
            ("tl-8", "2026-05-02T17:00:00", "Dasha stayed over last night.", "Williamsburg"))
        return c

    def _fact(self, c, *, refs, valid_from="2026-05-02", object_id="s1"):
        payload = json.dumps({
            "predicate": "rel.relationship_event", "asserted_by": "owner",
            "altitude": "stated", "quote": "Dasha stayed over last night.",
            "value_struct": {"person": "Dasha", "event": "met"},
        })
        c.execute("INSERT INTO signal_objects VALUES (?,?,?,?,?)",
                  (object_id, payload, 0.9, json.dumps(refs), valid_from))

    def test_a_fact_carries_the_day_it_is_about(self):
        c = self._facts_conn()
        self._fact(c, refs=[{"table": "journal_entries", "record_id": "tl-8"}])
        nodes = [{"entity_id": "e1", "label": "Dasha", "is_owner": False}]
        PG.person_relationship_facts(c, nodes)
        assert nodes[0]["facts"][0]["at"] == "2026-05-02"

    def test_a_journal_ref_resolves_to_the_text_the_owner_can_read(self):
        c = self._facts_conn()
        self._fact(c, refs=[{"table": "journal_entries", "record_id": "tl-8"}])
        nodes = [{"entity_id": "e1", "label": "Dasha", "is_owner": False}]
        PG.person_relationship_facts(c, nodes)
        src = nodes[0]["facts"][0]["sources"][0]
        assert src["kind"] == "record"
        assert "Dasha stayed over" in src["text"]
        assert src["where"] == "Williamsburg"
        assert src["at"] == "2026-05-02T17:00:00"

    def test_a_derived_ref_is_shown_as_the_statistic_it_is(self):
        """`entity_edges`/`messenger_dyad_stats` refs carry an entity id, not that table's
        key — 0 of 23 resolve by `edge_id` on the live node. Their evidence is the note."""
        c = self._facts_conn()
        self._fact(c, refs=[{"table": "messenger_dyad_stats", "record_id": "ent_x",
                             "note": "550 msgs, balance -0.16"}])
        nodes = [{"entity_id": "e1", "label": "Dasha", "is_owner": False}]
        PG.person_relationship_facts(c, nodes)
        src = nodes[0]["facts"][0]["sources"][0]
        assert src["kind"] == "measure"
        assert src["detail"] == "550 msgs, balance -0.16"
        assert "text" not in src, "a statistic must not be dressed up as a quoted document"

    def test_a_dead_ref_is_reported_not_dropped(self):
        """Silently omitting it leaves the card showing fewer sources than the fact was
        built from, which reads as a smaller claim rather than a broken link."""
        c = self._facts_conn()
        self._fact(c, refs=[{"table": "journal_entries", "record_id": "tl-gone"}])
        nodes = [{"entity_id": "e1", "label": "Dasha", "is_owner": False}]
        PG.person_relationship_facts(c, nodes)
        sources = nodes[0]["facts"][0]["sources"]
        assert len(sources) == 1
        assert sources[0]["kind"] == "missing"

    def test_every_ref_gets_a_source_so_the_numbering_is_stable(self):
        c = self._facts_conn()
        self._fact(c, refs=[
            {"table": "journal_entries", "record_id": "tl-8"},
            {"table": "messenger_dyad_stats", "record_id": "ent_x", "note": "550 msgs"},
            {"table": "journal_entries", "record_id": "tl-gone"},
        ])
        nodes = [{"entity_id": "e1", "label": "Dasha", "is_owner": False}]
        PG.person_relationship_facts(c, nodes)
        kinds = [s["kind"] for s in nodes[0]["facts"][0]["sources"]]
        assert kinds == ["record", "measure", "missing"]

    def test_long_evidence_is_bounded_and_says_so(self):
        c = self._facts_conn()
        c.execute("INSERT INTO journal_entries VALUES (?,?,?,?)",
                  ("tl-long", "2026-05-02", "x" * 900, None))
        self._fact(c, refs=[{"table": "journal_entries", "record_id": "tl-long"}])
        nodes = [{"entity_id": "e1", "label": "Dasha", "is_owner": False}]
        PG.person_relationship_facts(c, nodes)
        src = nodes[0]["facts"][0]["sources"][0]
        assert len(src["text"]) == PG.EVIDENCE_TEXT_CHARS
        assert src["truncated"] is True

    def test_a_missing_valid_from_column_loses_the_DATE_not_the_FACTS(self):
        """The `except sqlite3.Error` here swallows the whole read. Adding a column to the
        SELECT therefore emptied every person's card with nothing logged — a fact without
        its date is still the fact, and losing all of them to one column is not a trade."""
        c = self._facts_conn()
        self._fact(c, refs=[{"table": "journal_entries", "record_id": "tl-8"}])
        c.execute("ALTER TABLE signal_objects DROP COLUMN valid_from")
        nodes = [{"entity_id": "e1", "label": "Dasha", "is_owner": False}]
        stats = PG.person_relationship_facts(c, nodes)
        assert stats["attached"] == 1
        assert stats["dated"] is False
        assert nodes[0]["facts"][0]["at"] is None
        assert nodes[0]["facts"][0]["sources"][0]["kind"] == "record"
