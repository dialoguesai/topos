"""Third fix round, ruling P.1: every path whose answer can reach someone who is not the owner withholds a carried
person at once, exactly as it did before there were two views; nothing toward other people is looser.

A carried, waiting entry is invisible only to a reader that asked for the owner's own view, and a reader gets that
view only for a caller the node itself verified as its owner (`off_limits_view`). Each test here takes one caller the
node cannot take for its owner, or one path that answers such a caller, and shows the entry is read there in full
while it still waits.

protects:
  - who reads which view: the whole table of callers (`for_request`, `for_own_processing`);
  - the query pipeline's exit, its derived-mode floor and a real retrieval, for a recipient, a stamp that names
    nobody, a node that cannot say who its owner is, a frame with no stamp, a request with no principal, and the
    routine lane (whose result can be mailed to other people, and whose frames do not say whether it is);
  - the read-time guard for a grantee, a plugin and a caller it cannot place (the default);
  - the relay's inspection floor, the model gate when it is reached for a recipient, and the boundary every share
    builds.
Every person, handle and id here is invented.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.topos.test_carried_entry_owner_paths import (APP, LOCAL_CLIENT, NAMING, OUTSIDE_CLIENT, OWNER_ID, UNRELATED,
                                                        as_caller, items, the_node_knows_its_owner)  # noqa: F401
from tests.topos.test_carried_entry_waits import ORDINARY, excluded
from tests.topos.test_carry_step_review_r1 import cid, conn  # noqa: F401 (conn: fixture)
from topos.features.lifecycle import off_limits_view
from topos.features.lifecycle.blackhole import EVERYONE, OWNER
from topos.features.lifecycle.blackhole_guard import BlackholeGuard, CallerClass, guard_from_message
from topos.features.lifecycle.blackhole_llm import evaluate
from topos.features.lifecycle.contact_excludes import carry_contact_excludes
from topos.permissions_v2.entity_boundary import EntityBoundary
from topos.principal import RELAY_PRINCIPAL, THIRD_PARTY, Principal
from topos.query.retrieval import _blackhole_policy_for_summary

pytestmark = pytest.mark.public

RECIPIENT = Principal(cls=THIRD_PARTY, channel="cp_relay", client_id="app", acting_user="someone-else")
NAMES_NOBODY = Principal(cls=THIRD_PARTY, channel="cp_relay")               # what a stamp that did not verify becomes
ROUTINE = Principal(cls="owner_automation", channel="cp_relay", client_id="routine_executor", acting_user=OWNER_ID)
UNPLACED = Principal(cls=THIRD_PARTY, channel="internal")
NOT_THE_OWNER = {"a_recipient": RECIPIENT, "a_stamp_that_names_nobody": NAMES_NOBODY, "the_routine_lane": ROUTINE,
                 "a_frame_with_no_stamp": RELAY_PRINCIPAL, "no_principal": None, "a_third_party_of_no_door": UNPLACED}


def test_who_reads_which_view(monkeypatch):
    """The whole table. OWNER only on positive evidence at the node's own door; everything else, EVERYONE."""
    import topos.core.handlers as hub

    def request(principal):
        return off_limits_view.for_request(principal, current=False)

    assert [request(p) for p in (APP, OUTSIDE_CLIENT, LOCAL_CLIENT)] == [OWNER, OWNER, OWNER]
    assert {name: request(p) for name, p in NOT_THE_OWNER.items()} == dict.fromkeys(NOT_THE_OWNER, EVERYONE)
    # the node's own processing: the owner's, unless it is running for someone who is positively another person
    own = {name: off_limits_view.for_own_processing(p, current=False) for name, p in NOT_THE_OWNER.items()}
    assert own == {"a_recipient": EVERYONE, "a_stamp_that_names_nobody": EVERYONE, "the_routine_lane": OWNER,
                   "a_frame_with_no_stamp": OWNER, "no_principal": OWNER, "a_third_party_of_no_door": OWNER}
    assert off_limits_view.is_another_person(RECIPIENT) and not off_limits_view.is_another_person(OUTSIDE_CLIENT)
    # the owner is looked up here, not taken from the dispatcher having let the frame through
    monkeypatch.setattr(hub, "_relay_owner_id", lambda: None)             # a node that cannot say who its owner is
    assert request(OUTSIDE_CLIENT) == EVERYONE and off_limits_view.is_another_person(OUTSIDE_CLIENT)
    monkeypatch.setattr(hub, "_relay_owner_id", lambda: "another-owner")
    assert request(OUTSIDE_CLIENT) == EVERYONE
    assert request(APP) == OWNER and request(LOCAL_CLIENT) == OWNER       # verified at the node's own door


def test_the_routine_lane_reads_every_entry():
    """A routine's result goes to the owner and, when the routine lists consented recipients, is mailed to other
    people as well; nothing on its frame says which. The node cannot serve "his own routines to him" unchanged
    without loosening "routine mail addressed to anyone else", so the lane reads every entry. This is one named
    word (`off_limits_view.ROUTINE_LANE`); changing it is the program lead's decision, not a fix.

    The fourth round ruled HOW a carried entry is applied on that lane (item by item, as the share doors match it,
    closing nothing by itself: `test_routine_lane_carried_items.py`). It did not change this: the lane still reads
    every entry, and a carried person is still withheld from every routine."""
    assert off_limits_view.ROUTINE_LANE == EVERYONE
    assert off_limits_view.for_request(ROUTINE, current=False) == EVERYONE
    assert BlackholeGuard.view_of(CallerClass.ROUTINE) == EVERYONE


@pytest.mark.parametrize("who", list(NOT_THE_OWNER))
@pytest.mark.parametrize("label", ["saved as Sam", "alias Ed", "username al"])
def test_the_query_pipelines_exit_still_drops_the_carried_person_for_everyone_else(conn, who, label):
    """Released before the step, dropped the moment after, with the entry still waiting."""
    excluded(conn, ORDINARY[label])
    given = items(label)
    before = as_caller(NOT_THE_OWNER[who], _blackhole_policy_for_summary, given, conn=conn,
                       disclosure_tier="default_disclosure")
    assert before == given
    carry_contact_excludes(conn)
    conn.commit()
    after = as_caller(NOT_THE_OWNER[who], _blackhole_policy_for_summary, given, conn=conn,
                      disclosure_tier="default_disclosure")
    assert given[-1] not in after and NAMING[label] not in json.dumps(after)
    assert conn.execute("SELECT rebuild_state FROM entity_blackholes").fetchall() == [("pending",)]


@pytest.mark.parametrize("who", list(NOT_THE_OWNER))
def test_the_derived_mode_floor_and_the_row_filter_still_hold_for_everyone_else(conn, who):
    """The pipeline's own guards, built with the request's view: active, and the carried contact's row withheld.
    (For the routine lane the floor asks one more question since the fourth round, held in
    `test_routine_lane_carried_items.py`; its view, its guard and its row filter are these.)"""
    from topos.query.retrieval import _off_limits_view

    excluded(conn, ORDINARY["saved as Sam"])
    rows = [{"contact_id": cid("0a"), "display_name": "Sam"}, {"contact_id": cid("zz"), "display_name": "Perrin Ashgrove"}]
    carry_contact_excludes(conn)
    conn.commit()
    view = as_caller(NOT_THE_OWNER[who], _off_limits_view)
    guard = BlackholeGuard(conn, caller_class=CallerClass.GRANTEE, view=view)
    assert view == EVERYONE and guard.active
    assert guard.filter_observed_canonical_rows(rows, canonical_table="contacts") == rows[1:]


def test_a_real_retrieval_for_a_caller_the_node_cannot_place_is_emptied_by_the_step(tmp_path):
    """The other half of the owner-path test: the same corpus, the same query, a caller who is not verified as the
    owner. Answered before the step, emptied the moment after it, the entry still waiting."""
    from tests.evals.privacy.blackhole.corpus import OK_CANONICAL, SOURCE_ID, build_blackhole_corpus
    from topos.features.lifecycle.blackhole import BlackholeStore
    from topos.query.manifest_validation import resolve_scope_manifest
    from topos.query.retrieval import DefaultSignalRetrievalAdapter
    from topos.query.types import RetrievalRequest
    from topos.storage.adapters.factory import AdapterFactory
    from topos.storage.canonical import ConversationsTablesManager

    c = build_blackhole_corpus(str(tmp_path / "corpus.db")).conn
    ConversationsTablesManager(c).ensure_tables()
    store = BlackholeStore(c)
    for entry in store.list():
        store.unblackhole_entity(entity_ref=entry["blackhole_id"])
    c.commit()

    def summaries(principal):
        adapter = DefaultSignalRetrievalAdapter(AdapterFactory.create("local_database", conn=c))
        return as_caller(principal, adapter.retrieve, RetrievalRequest(
            manifest=resolve_scope_manifest("messages:read"), access_mode="summary",
            query_text=f"what happened with the {OK_CANONICAL} thread", installed_source_ids=[SOURCE_ID],
            owner_mode=False, disclosure_tier="default_disclosure")).context_packet["summaries"]

    assert len(summaries(ROUTINE)) >= 3 and len(summaries(RELAY_PRINCIPAL)) >= 3     # control
    excluded(c, ORDINARY["username al"])
    carry_contact_excludes(c)
    c.commit()
    assert summaries(RELAY_PRINCIPAL) == [] and summaries(RECIPIENT) == []
    # The routine lane is ruled apart since the fourth round: a carried entry is applied to its items one by one
    # and does not empty its query (`test_routine_lane_carried_items.py`, on this corpus with the table the share
    # boundary needs). THIS database has no merge-tombstone table, so the share boundary cannot be built over what
    # is carried, the routine's rule cannot be built either, and the floor stands for it as before.
    assert summaries(ROUTINE) == []


@pytest.mark.parametrize("caller_class", [CallerClass.UNKNOWN, CallerClass.GRANTEE, CallerClass.PLUGIN,
                                          CallerClass.ROUTINE])
def test_the_guard_withholds_from_every_class_that_is_not_the_owners_own(conn, caller_class):
    """The re-check's run by caller class: while an entry waits, summaries are withheld from these as before."""
    excluded(conn, ORDINARY["saved as Sam"])
    artifacts = [{"text": "The week went to the compiler and the bouldering trip."}]
    guard = lambda: BlackholeGuard(conn, caller_class=caller_class)       # noqa: E731
    assert guard().filter_name_string_artifacts(artifacts, text_keys=("text",)) == artifacts
    carry_contact_excludes(conn)
    conn.commit()
    assert guard().view == EVERYONE and guard().active and guard().withhold_pending_rebuild()
    assert guard().filter_name_string_artifacts(artifacts, text_keys=("text",)) == []
    assert guard().text_mentions_blackholed("Sam is bringing the ladder.") and guard().blocks_name("Sam")
    assert BlackholeGuard(conn).view == EVERYONE                          # the default class is the one that filters


def test_a_caller_the_node_cannot_place_gets_the_guard_that_reads_everything(conn):
    excluded(conn, ORDINARY["saved as Sam"])
    carry_contact_excludes(conn)
    conn.commit()
    for who in ("a_recipient", "a_stamp_that_names_nobody", "the_routine_lane", "a_frame_with_no_stamp", "no_principal"):
        guard = as_caller(NOT_THE_OWNER[who], guard_from_message, conn, {})
        assert (guard.caller_class, guard.view, guard.active) == (CallerClass.UNKNOWN, EVERYONE, True), who


def test_the_relays_inspection_floor_still_closes_once_a_carried_entry_exists(conn, monkeypatch):
    """`_legacy_inspection_refusal`: a frame with no stamp and the routine lane read tables only while nothing is
    Off-limits. The step's entries count there, as before: the node cannot tell whose frame an unstamped one is."""
    import topos.core.handlers as hub

    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)
    message = {"id": "m1", "type": "get_table_rows"}
    refusal = lambda principal: as_caller(principal, hub._legacy_inspection_refusal, message, "get_table_rows")   # noqa: E731
    excluded(conn, ORDINARY["saved as Sam"])
    assert [refusal(p) for p in (RELAY_PRINCIPAL, ROUTINE, None, APP)] == [None, None, None, None]
    carry_contact_excludes(conn)
    conn.commit()
    refused = {"id": "m1", "status": "error", "code": 403, "error": "owner_mode_required"}
    assert [refusal(p) for p in (RELAY_PRINCIPAL, ROUTINE, None)] == [refused, refused, refused]
    assert refusal(APP) is None and refusal(RECIPIENT) == refused


def test_the_model_gate_reads_every_entry_when_it_is_reached_for_a_recipient(conn, monkeypatch):
    """No share's answer is made through this gate (the share doors run their own pinned local model). If one ever
    were, the recipient is the principal for the length of the read, and the gate would read every entry."""
    import topos.core.handlers as hub
    from topos.core.handlers.signal_features import handle_blackhole_status

    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)
    excluded(conn, ORDINARY["saved as Sam"])
    carry_contact_excludes(conn)
    conn.commit()
    verdict = as_caller(RECIPIENT, evaluate, conn, {"prompt": NAMING["saved as Sam"]}, provider="openai")
    assert verdict.tainted and verdict.provider != "openai"
    assert not as_caller(RECIPIENT, evaluate, conn, {"prompt": UNRELATED[0]}, provider="openai").tainted
    status = as_caller(RECIPIENT, lambda: asyncio.run(handle_blackhole_status({"id": "s"})))
    assert status["payload"] == {"has_blackholes": True, "pending_rebuild": True}


def test_no_share_side_module_reaches_the_model_gate():
    """What `for_own_processing` rests on, held where it can be seen: nothing under `permissions_v2` imports the
    gate or the engine's task entry."""
    from pathlib import Path

    import topos.permissions_v2 as package

    for path in sorted(Path(package.__file__).parent.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert "blackhole_llm" not in text and "apply_blackhole_egress_policy" not in text, path.name
        assert "off_limits_view" not in text, path.name                   # and none asks for the owner's view


def test_every_share_builds_the_boundary_that_reads_what_waits(conn):
    """The default of `EntityBoundary` is every entry; the one caller that passes `waiting=False` is the legacy
    guard, for the owner's own client."""
    excluded(conn, ORDINARY["saved as Sam"])
    carry_contact_excludes(conn)
    conn.commit()
    shares = EntityBoundary(conn)
    assert shares.active and cid("0a") in shares.contacts and shares.mentions_protected("Sam is bringing the ladder.")
    mine = EntityBoundary(conn, waiting=False)
    assert not mine.active and shares.revision != mine.revision
