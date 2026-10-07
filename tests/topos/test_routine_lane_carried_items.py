"""Fourth round, the routine lane: a carried, waiting Off-limits entry is applied to a routine ITEM BY ITEM, matched
the way the share doors match it, and closes nothing by itself.

A routine's result goes to the owner and may be mailed to other people, and nothing on its frames says which. Until
this round the lane therefore read every entry as it read one the owner had made: one carried contact, whatever its
names, and a routine's summary-mode query came back empty (the derived-mode floor), with every item that held the
contact's letters anywhere dropped by the name scan before that ("J": 246 of 1,066 on the reviewer's four homes).
The program lead's ruling (7 Oct 2026): the owner's rule on Off-limits is that only the protected items are hidden,
and a floor put on his routines by an upgrade he was not asked about is the opposite. So, for a frame the control
plane stamped `owner_automation` and whose stamp verified, and for no other caller:

  - an entry that is carried and waiting does not trip the derived-mode floor by itself;
  - it is still applied to every item: by the ids the item carries, and in its text the way the share boundary
    matches it (a name as the boundary reads names, an identifier only as itself), never a bare substring, never a key;
  - an entry the owner made, on that lane as everywhere, is read exactly as before.

protects, each with the fault that undoes it:
  - who the rule is for: only the class and channel the relay's stamp check mints;
  - the exit filter, the cluster filter and the thread roster: what names the person goes, what does not stays;
  - the floor: not closed by a carried entry, closed by one the owner made, by a record protection, for every other
    caller, and whenever the rule cannot be built;
  - the whole packet: the lanes the exit filter never sees (scores, hits, rows, the thread);
  - a real retrieval, through the function a turn calls.
Every person, handle and id here is invented; the short ordinary names are the ones this rule is about.
"""

from __future__ import annotations

import json

import pytest

from tests.topos.test_carried_entry_outward_paths import NAMES_NOBODY, RECIPIENT, ROUTINE, UNPLACED
from tests.topos.test_carried_entry_owner_paths import (APP, LOCAL_CLIENT, NAMING, OUTSIDE_CLIENT, OWNER_ID, UNRELATED,
                                                        as_caller, items, the_node_knows_its_owner,  # noqa: F401
                                                        the_owner_acts)
from tests.topos.test_carried_entry_waits import EXOTIC, ORDINARY, excluded
from tests.topos.test_carry_step_review_r1 import PHONE, cid, conn  # noqa: F401 (conn: fixture)
from topos.features.lifecycle import blackhole_guard, off_limits_view
from topos.features.lifecycle.blackhole import EVERYONE, FULL, OWNER, BlackholeStore
from topos.features.lifecycle.blackhole_guard import (BlackholeGuard, CallerClass, CarriedItems, anything_is_carried,
                                                      carried_items_for_routine)
from topos.features.lifecycle.contact_excludes import carry_contact_excludes
from topos.features.lifecycle.record_protection import RecordProtectionStore
from topos.principal import RELAY_PRINCIPAL, Principal
from topos.query import retrieval
from topos.query.retrieval import (_blackhole_policy_for_clusters, _blackhole_policy_for_summary,
                                   _derived_floor_applies, _thread_participants)

pytestmark = pytest.mark.public

#: Callers that are not a verified routine frame. The rule must change nothing for any of them.
NOT_A_ROUTINE = {
    "a_recipient": RECIPIENT, "a_stamp_that_names_nobody": NAMES_NOBODY, "a_frame_with_no_stamp": RELAY_PRINCIPAL,
    "no_principal": None, "a_third_party_of_no_door": UNPLACED,
    # the routine's CLASS from a door that is not the relay's stamp check: nothing mints it, and it is not the lane
    "the_class_at_the_nodes_own_door": Principal(cls="owner_automation", channel="local_http"),
    "the_class_on_the_socket": Principal(cls="owner_automation", channel="uds"),
    "the_class_with_no_channel": Principal(cls="owner_automation", channel="internal"),
}


def carried(c, label="saved as Sam"):
    excluded(c, ORDINARY[label])
    carry_contact_excludes(c)
    c.commit()


def exit_filter(principal, c, given, tier="owner_raw"):
    return as_caller(principal, _blackhole_policy_for_summary, given, conn=c, disclosure_tier=tier)


def item(text, **more):
    return {"topic": text[:120], "summary_text": text, "record_id": "m-x", "source_id": "src",
            "relevance_score": 0.5, "retrieval_source": "canonical:conversation_messages", **more}


# ----------------------------------------------------------------------------------------------- who it is for

def test_the_routine_lane_is_the_class_and_the_channel_the_stamp_check_mints_and_nothing_else(conn):
    """Rule: `off_limits_view.is_routine_lane` asks for the class AND the relay's channel. Ask for the class alone
    and a principal of that class from any other door gets the rule; ask for neither and everyone does."""
    assert off_limits_view.is_routine_lane(ROUTINE, current=False)
    assert as_caller(ROUTINE, off_limits_view.is_routine_lane)
    for name, principal in {**NOT_A_ROUTINE, "the_owners_app": APP, "his_outside_client": OUTSIDE_CLIENT,
                            "a_client_at_his_own_door": LOCAL_CLIENT}.items():
        assert not off_limits_view.is_routine_lane(principal, current=False), name
    # the view every OTHER reader takes on the lane has not moved: every entry
    assert off_limits_view.ROUTINE_LANE == EVERYONE
    assert off_limits_view.for_request(ROUTINE, current=False) == EVERYONE
    carried(conn)
    assert isinstance(carried_items_for_routine(conn, ROUTINE, current=False), CarriedItems)
    for name, principal in NOT_A_ROUTINE.items():
        assert carried_items_for_routine(conn, principal, current=False) is None, name
    for principal in (APP, OUTSIDE_CLIENT, LOCAL_CLIENT):
        assert carried_items_for_routine(conn, principal, current=False) is None


def test_the_rule_is_there_only_while_something_is_carried_and_waiting(conn):
    """Nothing carried, or an entry the owner made, or a carried entry he has acted on: no rule, and every reader
    does what it did before."""
    assert not anything_is_carried(conn) and carried_items_for_routine(conn, ROUTINE, current=False) is None
    BlackholeStore(conn).blackhole_entity(entity_ref="Perrin Ashgrove", processing_tier="secure", note=None)
    conn.commit()
    assert not anything_is_carried(conn) and carried_items_for_routine(conn, ROUTINE, current=False) is None
    carried(conn)
    assert anything_is_carried(conn) and carried_items_for_routine(conn, ROUTINE, current=False) is not None
    the_owner_acts(conn)
    assert not anything_is_carried(conn) and carried_items_for_routine(conn, ROUTINE, current=False) is None


def test_the_views_a_whole_list_rule_reads(conn):
    """FULL is every entry but one that is carried and waiting; an entry the owner made is read whole there, with
    what the upgrade added to it (OWNER takes those additions off)."""
    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref=EXOTIC, processing_tier="secure", note=None)   # the owner's own entry...
    excluded(conn, dict(display=EXOTIC, handles=[("vellaby_q", "username")]))          # ...for a contact he had excluded
    excluded(conn, ORDINARY["saved as Sam"], tail="0b")
    carry_contact_excludes(conn)
    conn.commit()
    names = lambda view: sorted(entry["canonical_name"] for entry in store.list(view=view))   # noqa: E731
    assert names(EVERYONE) == [EXOTIC, "Sam"] and names(FULL) == [EXOTIC] and names(OWNER) == [EXOTIC]
    (own,) = store.list(view=FULL)
    added = set(own["carried_waiting_aliases"])                            # what the step added to his entry
    assert added and not own["carried_waiting"]
    assert added <= set(store.terms(view=EVERYONE)) and added <= set(store.terms(view=FULL))
    assert not added & set(store.terms(view=OWNER))
    assert "sam" in store.terms(view=EVERYONE) and "sam" not in store.terms(view=FULL)
    assert store.is_blackholed("Sam") and not store.is_blackholed("Sam", view=FULL)
    assert store.is_blackholed(EXOTIC, view=FULL)
    assert store.pending_rebuild_names(view=FULL) == store.pending_rebuild_names(view=OWNER)


# --------------------------------------------------------------------------------------- the exit filter, by text

@pytest.mark.parametrize("label", list(ORDINARY))
def test_a_routine_loses_the_item_that_names_the_person_and_no_other(conn, label):
    """The re-check's own shape: five items that hold each alias's letters inside other words, one that names the
    contact. Before the step all six; after it, with the entry waiting, exactly the five. Rule: the exit filter asks
    `CarriedItems.names` about each item (`_carried_items`). Take that question out and the sixth is released to a
    routine; put the name scan back for a waiting entry and "al", "Ed" and "J" drop the other five again."""
    excluded(conn, ORDINARY[label])
    given = items(label)
    assert exit_filter(ROUTINE, conn, given) == given
    carry_contact_excludes(conn)
    conn.commit()
    assert exit_filter(ROUTINE, conn, given) == given[:-1]
    assert NAMING[label] not in json.dumps(exit_filter(ROUTINE, conn, given))


@pytest.mark.parametrize("who", list(NOT_A_ROUTINE))
def test_every_other_caller_is_read_exactly_as_before_the_fourth_round(conn, who):
    """The rule is the routine lane's. For a recipient, a frame with no stamp, a request with no principal, and the
    routine's class from any door but the relay's, the name scan still reads every entry: "Ed" inside "finished",
    "Edited", "pushed" and "fixed" still drops those two items, as at 9386a335."""
    carried(conn, "alias Ed")
    given = items("alias Ed")
    after = exit_filter(NOT_A_ROUTINE[who], conn, given, tier="default_disclosure")
    assert after == given[2:5], who                                        # the three items with no "ed" in them
    assert exit_filter(ROUTINE, conn, given) == given[:-1]
    assert as_caller(NOT_A_ROUTINE[who], retrieval._carried_items, conn) is None


@pytest.mark.parametrize("label, named, not_named", [
    ("saved as Sam", ["Sam is bringing the ladder.", "Is that Sam's ladder?", "ask SAM", "Sammy rang twice."],
     ["Same plan as before.", "The samples came late.", "A flotsam of notes."]),
    ("saved as J", ["J has the spare keys.", "Ask J."], ["Just a short note in the journal about the project."]),
    ("alias Ed", ["Ed rang about the boiler.", "Lunch with Quorra Vellaby.", "quorra vellaby again"],
     ["Edited the homework and pushed the fixed branch."]),
    ("username al", ["Lunch with Al went late.", "ping @al about it"],
     ["We also finished the normal walk before the usual rain.", "Ally and Allie were there."]),
    ("handle work", ["Left a message for work about Friday.", "Work: the release."],
     ["The network was slow all week.", "Homework, then paperwork.", "Working late."]),
])
def test_text_is_matched_the_way_the_share_boundary_matches_it(conn, label, named, not_named):
    """One sentence states the rule: a carried, waiting entry is matched everywhere it applies the way the share doors
    match it. So this asks the boundary every share builds the same question, text by text, and the two must agree:
    a name as the boundary reads names (a whole word, its possessive, its pet forms), a handle or a username only as
    itself, nothing inside another word."""
    from topos.permissions_v2.entity_boundary import EntityBoundary

    carried(conn, label)
    doors = EntityBoundary(conn)
    for text in named:
        assert doors.mentions_protected(text), text
        assert exit_filter(ROUTINE, conn, [item(text)]) == [], text
    for text in not_named:
        assert not doors.mentions_protected(text), text
        assert exit_filter(ROUTINE, conn, [item(text)]) == [item(text)], text


def test_a_number_or_an_address_is_found_as_the_doors_find_it(conn):
    """An identifier with a digit or an "@" keeps the reading it has at the doors: a number written with spaces or
    dashes, an address inside a link."""
    excluded(conn, dict(display=EXOTIC, handles=[(PHONE, "phone"), ("q.vellaby@fernmail.example", "email")]))
    carry_contact_excludes(conn)
    conn.commit()
    for text in ("Call +1 555 0142 0137 after six.", "call 555-0142-0137", "mailto:q.vellaby@fernmail.example?subject=hi"):
        assert exit_filter(ROUTINE, conn, [item(text)]) == [], text
    assert exit_filter(ROUTINE, conn, [item("Call the office after six.")]) == [item("Call the office after six.")]


def test_nothing_is_matched_against_a_key(conn):
    """The re-check's finding for the owner's outside client, on this lane: every item has the keys `retrieval_source`
    and `relevance_score`, and the name scan read them. Rule: `EntityBoundary.item_names_protected` reads values only.
    Read keys and the username "al" and the handle "work" drop every item that has a field so named."""
    excluded(conn, dict(display=EXOTIC, usernames=["al", "score", "topic"], handles=[("work", "username")]))
    carry_contact_excludes(conn)
    conn.commit()
    plain = item("Notes for the week.", al="x", work={"score": 1, "topic": ["plan"]})
    assert exit_filter(ROUTINE, conn, [plain]) == [plain]
    assert exit_filter(ROUTINE, conn, [item("Notes for the week.", area="work")]) == []     # the same word as a VALUE
    # a stored JSON column inside an item is read as the boundary reads that column: its keys for a name, never
    # for a handle
    column = item("Notes.", metadata_json=json.dumps({"work": "the release", "Quorra Vellaby": "birthday"}))
    assert exit_filter(ROUTINE, conn, [column]) == []
    assert exit_filter(ROUTINE, conn, [item("Notes.", metadata_json=json.dumps({"work": "the release"}))]) != []


# ---------------------------------------------------------------------------------------------- by id, as before

def test_an_item_that_carries_one_of_the_persons_ids_is_withheld(conn):
    """By id as before, and by every id the share boundary reaches from the entry: the contact, the linked entity,
    and a record that entity is mentioned in. None of these items says a name."""
    carried(conn, "alias Ed")                                             # a contact with a linked entity, ent-0a
    conn.execute("INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id, canonical_table, "
                 "surface_text, event_at) VALUES ('mn-1','ent-0a','rec-77','src','conversation_messages',?,?)",
                 ("the plumber", "2026-07-01T10:00:00Z"))
    conn.commit()
    quiet = "Nothing here names anyone."
    for carrying in (item(quiet, entity_id="ent-0a"), item(quiet, sender_id=cid("0a")), item(quiet, record_id="rec-77"),
                     item(quiet, source_refs=[{"record_id": "rec-77"}]), item("The plumber came at nine.")):
        assert exit_filter(ROUTINE, conn, [carrying]) == [], carrying
    assert exit_filter(ROUTINE, conn, [item(quiet, record_id="rec-78")]) != []


def test_a_journal_item_is_read_by_the_journal_rule(conn):
    """At the doors a journal entry withholds on a bare part of a protected name in lower case too; any other kind,
    where it is written as a name. An item says which it is, and is read the same way."""
    excluded(conn, dict(display="Rose Ashgrove"))
    carry_contact_excludes(conn)
    conn.commit()
    lower = "Bought a rose for the table."
    assert exit_filter(ROUTINE, conn, [item(lower)]) == [item(lower)]
    assert exit_filter(ROUTINE, conn, [item(lower, retrieval_source="canonical:journal_entries")]) == []
    assert exit_filter(ROUTINE, conn, [item("Rose came by.")]) == []


def test_an_item_that_cannot_be_judged_is_withheld(conn):
    """Bytes, or a structure nested past the boundary's own limit: the boundary refuses such a row, and so does this."""
    carried(conn)
    deep = current = {}
    for _ in range(40):
        current["next"] = {}
        current = current["next"]
    rule = carried_items_for_routine(conn, ROUTINE, current=False)
    assert rule.names({"blob": b"\x00\x01"}) and rule.names(deep)
    assert not rule.names({"text": "The week went to the compiler.", "n": 3, "ok": True, "none": None})


# ------------------------------------------------------------------------------------------------------ the floor

def test_a_carried_entry_does_not_close_the_derived_modes_to_a_routine_by_itself(conn):
    """Rule: `_derived_floor_applies` asks, on the routine lane, whether anything is Off-limits APART from what is
    carried. Ask the old question and one carried contact empties every routine's summary-mode query again."""
    floor = lambda principal: as_caller(principal, _derived_floor_applies, conn)   # noqa: E731
    assert not floor(ROUTINE) and not floor(RELAY_PRINCIPAL)
    carried(conn, "username al")
    assert not floor(ROUTINE)
    for name, principal in NOT_A_ROUTINE.items():
        assert floor(principal), name                                     # every other caller: closed, as before
    assert not floor(OUTSIDE_CLIENT) and not floor(LOCAL_CLIENT)          # the owner himself: the third round's rule


def test_an_entry_the_owner_made_closes_them_as_before_whatever_else_is_carried(conn):
    """A node with one full entry and one carried entry behaves as a node with one full entry."""
    BlackholeStore(conn).blackhole_entity(entity_ref="Perrin Ashgrove", processing_tier="secure", note=None)
    conn.commit()
    assert as_caller(ROUTINE, _derived_floor_applies, conn)
    carried(conn)
    assert as_caller(ROUTINE, _derived_floor_applies, conn)
    guard = BlackholeGuard(conn, caller_class=CallerClass.GRANTEE)
    assert guard.active and guard.active_apart_from_what_is_carried()


def test_the_owners_act_on_the_carried_entry_closes_them(conn):
    carried(conn)
    assert not as_caller(ROUTINE, _derived_floor_applies, conn)
    the_owner_acts(conn)
    assert as_caller(ROUTINE, _derived_floor_applies, conn)


def test_a_record_protection_closes_them_as_before(conn):
    carried(conn)
    conn.execute("INSERT INTO owner_only_records (canonical_table, record_id) VALUES ('conversation_messages', 'm-1')")
    conn.commit()
    assert RecordProtectionStore(conn).blocked_ids() == {"m-1"}
    assert as_caller(ROUTINE, _derived_floor_applies, conn)


def test_if_the_rule_cannot_be_built_the_floor_stands(conn, monkeypatch):
    """Something is carried and the boundary over it refuses (here: a table it needs is gone). That is never read as
    "nothing is carried": the derived modes are closed, as they were before this round."""
    carried(conn)
    assert not as_caller(ROUTINE, _derived_floor_applies, conn)
    conn.execute("ALTER TABLE contact_identifiers RENAME TO contact_identifiers_gone")
    with pytest.raises(Exception):
        carried_items_for_routine(conn, ROUTINE, current=False)
    assert as_caller(ROUTINE, _derived_floor_applies, conn)
    with pytest.raises(Exception):
        exit_filter(ROUTINE, conn, items("saved as Sam"))                 # and the exit does not answer unfiltered


# ------------------------------------------------------------------------------------- clusters, roster, the packet

def test_a_cluster_and_a_roster_entry_that_name_the_person_go_and_the_others_stay(conn):
    carried(conn, "alias Ed")
    clusters = [{"cluster_id": "c1", "label": "Boiler repairs", "label_terms": ["boiler", "edited"]},
                {"cluster_id": "c2", "label": "Calls with Ed", "label_terms": ["calls"]},
                {"cluster_id": "c3", "label": "Planning", "label_terms": ["quorra vellaby", "launch"]}]
    kept = as_caller(ROUTINE, _blackhole_policy_for_clusters, clusters, conn=conn, disclosure_tier="owner_raw")
    assert [cluster["cluster_id"] for cluster in kept] == ["c1"]
    speakers = [{"kind": "person", "label": "Perrin Ashgrove", "sender_id": "+1 555 0100 0199"},
                {"kind": "person", "label": "Edmund Hale", "sender_id": "+1 555 0100 0188"},
                {"kind": "person", "label": "Ed", "sender_id": "+1 555 0100 0177"},
                {"kind": "person", "label": "", "sender_id": cid("0a")},
                {"kind": "person", "entity_id": "ent-0a", "label": "the plumber", "sender_id": ""}]
    from topos.query.manifest_validation import resolve_scope_manifest

    roster, _owner, _withheld = as_caller(ROUTINE, _thread_participants, speakers, conn=conn,
                                          disclosure_tier="owner_raw", manifest=resolve_scope_manifest("messages:read"))
    assert [entry.get("label") for entry in roster] == ["Perrin Ashgrove", "Edmund Hale"]


def test_the_whole_answer_is_walked_item_by_item(conn):
    """`CarriedItems.withhold_from`: an element of a list goes as a whole when anything in it names the person; the
    answer's own structure stays, less any text of it that names the person."""
    carried(conn)
    rule = carried_items_for_routine(conn, ROUTINE, current=False)
    answer = {
        "scope_id": "messages:read", "answer_type": "facts",
        "answer": "You are closest to Perrin Ashgrove and Sam.",
        "items": ["Perrin Ashgrove", "Sam"],
        "summaries": [item("The week went to the compiler."), item("Sam is bringing the ladder.")],
        "scores": [{"record_id": "m-1", "score": 0.4}, {"record_id": "m-2", "speaker_label": "Sam", "score": 0.9}],
        "topic_thread": {"cross_source": False, "items": [{"record_id": "m-1"}],
                         "participants": [{"kind": "person", "label": "Sam"}, {"kind": "person", "label": "Perrin"}]},
        "rows": [{"_table": "contacts", "contact_id": cid("0a")}, {"_table": "contacts", "contact_id": cid("zz")}],
        "count": 2, "truncated": None,
    }
    out = rule.withhold_from(answer)
    assert out == {
        "scope_id": "messages:read", "answer_type": "facts",
        "items": ["Perrin Ashgrove"],
        "summaries": [item("The week went to the compiler.")],
        "scores": [{"record_id": "m-1", "score": 0.4}],
        "topic_thread": {"cross_source": False, "items": [{"record_id": "m-1"}],
                         "participants": [{"kind": "person", "label": "Perrin"}]},
        "rows": [{"_table": "contacts", "contact_id": cid("zz")}],
        "count": 2, "truncated": None,
    }
    assert "Sam" not in json.dumps(out) and cid("0a") not in json.dumps(out)
    # the pipeline's own pass leaves the packet's own words alone and takes the items
    packet = rule.withhold_from(answer, text=False)
    assert packet["answer"] == answer["answer"] and packet["summaries"] == out["summaries"]
    assert answer["items"] == ["Perrin Ashgrove", "Sam"]                   # nothing rewritten in place


# ------------------------------------------------------------------------------------------- through a real turn

def _corpus(tmp_path):
    from tests.evals.privacy.blackhole.corpus import build_blackhole_corpus
    from topos.permissions_v2.protection_clock import TOMBSTONES_SQL
    from topos.storage.canonical import ConversationsTablesManager

    c = build_blackhole_corpus(str(tmp_path / "corpus.db")).conn
    ConversationsTablesManager(c).ensure_tables()
    c.execute(TOMBSTONES_SQL)
    store = BlackholeStore(c)
    for entry in store.list():
        store.unblackhole_entity(entity_ref=entry["blackhole_id"])
    c.commit()
    return c


def _retrieve(c, principal, *, mode="summary", query=None):
    from tests.evals.privacy.blackhole.corpus import OK_CANONICAL, SOURCE_ID
    from topos.query.manifest_validation import resolve_scope_manifest
    from topos.query.retrieval import DefaultSignalRetrievalAdapter
    from topos.query.types import RetrievalRequest
    from topos.storage.adapters.factory import AdapterFactory

    adapter = DefaultSignalRetrievalAdapter(AdapterFactory.create("local_database", conn=c))
    return as_caller(principal, adapter.retrieve, RetrievalRequest(
        manifest=resolve_scope_manifest("messages:read"), access_mode=mode,
        query_text=query if query is not None else f"what happened with the {OK_CANONICAL} thread",
        installed_source_ids=[SOURCE_ID], owner_mode=False, disclosure_tier="owner_raw")).context_packet


def test_a_real_retrieval_for_a_routine_is_the_same_packet_when_the_carried_person_is_not_in_it(tmp_path):
    """What the floor did: one carried contact, with nothing of theirs in the answer, and a routine's query came back
    empty. Now the packet is the one the routine got before the step, item for item."""
    c = _corpus(tmp_path)
    before = _retrieve(c, ROUTINE)
    assert len(before["summaries"]) >= 3
    excluded(c, ORDINARY["username al"])
    carry_contact_excludes(c)
    c.commit()
    assert _retrieve(c, ROUTINE) == before
    assert _retrieve(c, RELAY_PRINCIPAL)["summaries"] == [] and _retrieve(c, RECIPIENT)["summaries"] == []


def test_a_real_retrieval_for_a_routine_leaves_out_what_names_the_carried_person(tmp_path):
    """The same corpus with the person the query is about carried: every item that names them is gone from every list
    of the packet, the rest is there, and the answer is not the floor's empty one."""
    from tests.evals.privacy.blackhole.corpus import OK_CANONICAL

    c = _corpus(tmp_path)
    surname = OK_CANONICAL.split()[1]
    before = _retrieve(c, ROUTINE)
    naming = [entry for entry in before["summaries"] if surname.lower() in json.dumps(entry).lower()]
    others = [entry for entry in before["summaries"] if entry not in naming]
    assert naming, "the corpus must name the person in at least one item for this test to mean anything"
    excluded(c, dict(display=OK_CANONICAL))
    carry_contact_excludes(c)
    c.commit()
    after = _retrieve(c, ROUTINE)
    assert surname.lower() not in json.dumps(after).lower()
    assert [entry for entry in after["summaries"] if entry in others] == after["summaries"]
    assert all(entry in after["summaries"] for entry in others if surname.lower() not in json.dumps(entry).lower())
    # an unrelated question is answered as before the step
    unrelated = _retrieve(c, ROUTINE, query="what did I write about the compiler")
    assert surname.lower() not in json.dumps(unrelated).lower()


def test_a_real_retrieval_with_an_entry_the_owner_made_is_emptied_as_before(tmp_path):
    c = _corpus(tmp_path)
    excluded(c, ORDINARY["username al"])
    carry_contact_excludes(c)
    BlackholeStore(c).blackhole_entity(entity_ref="Perrin Ashgrove", processing_tier="secure", note=None)
    c.commit()
    assert _retrieve(c, ROUTINE)["summaries"] == []
    assert _retrieve(c, ROUTINE, mode="inference")["scores"] == []


def test_the_rule_is_built_once_for_one_retrieval(tmp_path, monkeypatch):
    """The floor, the exit filter, the cluster filter, the roster and the packet pass ask for the same rule; it is
    read from the database once for the turn."""
    c = _corpus(tmp_path)
    excluded(c, ORDINARY["username al"])
    carry_contact_excludes(c)
    c.commit()
    built = []
    real = blackhole_guard.carried_items_for_routine
    monkeypatch.setattr(blackhole_guard, "carried_items_for_routine",
                        lambda *args, **kwargs: built.append(1) or real(*args, **kwargs))
    assert len(_retrieve(c, ROUTINE)["summaries"]) >= 3
    assert built == [1]
