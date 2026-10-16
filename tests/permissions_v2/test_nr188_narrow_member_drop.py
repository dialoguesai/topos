"""NR188 (DR-31, owner ruling D-24, 16 Oct 2026): new data never takes a share's index dark; restrictions still do.

16 Oct, live: ordinary ingestion moved a member's `classification_context` (12:42Z) or its `dependencies` (14:01Z), the
daemon sweep removed both of a recipient's share indexes, and an answer the node had already written was held on every
fetch until the refresh loop rebuilt them nine minutes later. Option A (`lanes/NR188_BRIEF.md`), behind
`TOPOS_PERMISSIONS_V2_INDEX_NARROW_MEMBERS` (on by default since the owner's ruling of 16 Oct; off is main):

- attribution, no switch: the stale line and the `index_stale` event carry a fixed sub-reason word;
- a member that fails ONLY at those two classes, as a clean revision mismatch, is withheld (narrowed) and every other
  member is still checked; everything else drops the whole index exactly as main does;
- the sweep publishes the index minus the narrowed members (same basis, under the write gate and BUILD_SLOT, only over
  the file it checked, members removed only), which settles no restore and owes the refresh loop one rebuild;
- the request path never shreds on a narrow-only failure and never serves a narrowed member: it refuses as not served
  now (`search_index_stale`), and the sweep narrows within one sweep gap.

A share over invented iMessages in two conversations (`direct_search_twins.build`, an ACTIVE Off-limits boundary on an
invented person no member mentions), so a change in one conversation can be seen not to touch the other.
"""
from __future__ import annotations

import copy
import logging
import sqlite3
import threading
import time

import pytest

from tests.permissions_v2 import direct_search_twins as twins
from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_answer_release import _ask, _fetch, _service
from tests.permissions_v2.test_refresh_loop import Clock, loop_for
from topos.permissions_v2 import search_index, search_timing
from topos.permissions_v2.automatic_message_review import context_for, parse_assessment, prepare, publish
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.opaque_ids import opaque_record_id
from topos.permissions_v2.refresh_loop import DROP_CAUSES, REFRESH_CAUSES
from topos.permissions_v2.search_index import BUILD_SLOT, SearchIndexService, _file_state, index_path

NARROW = "TOPOS_PERMISSIONS_V2_INDEX_NARROW_MEMBERS"
ROADMAP = [f"roadmap planning note item{n}" for n in range(4)]     # conversation "7": imessage:1..4
GARDEN = [f"garden watering chore item{n}" for n in range(4, 8)]   # conversation "8": imessage:5..8
LATER = "2099-01-01T00:00:00Z"   # after every member: the neighbour of the conversation's last two members
QUESTION = "What is the roadmap planning note?"
ANSWER = "The owner wrote a roadmap planning note [1]."


# --- the node ---------------------------------------------------------------------------------------------------

def _two_chats(monkeypatch):
    """The twins' native snapshot with its second half moved to a second chat (conversation "8")."""
    original = twins.native_snapshot

    def snapshot(*, count=2, mutate=None):
        def both(db):
            if mutate:
                mutate(db)
            db.execute("INSERT INTO chat(ROWID, guid, chat_identifier) VALUES (8, 'synthetic-chat-b', 'synthetic-b')")
            db.execute("UPDATE chat_message_join SET chat_id=8 WHERE message_id>4")
        return original(count=count, mutate=both)
    monkeypatch.setattr(twins, "native_snapshot", snapshot)


def _answers_policy():
    raw = twins.knowledge_policy()
    raw["search"]["answers"] = "only"
    return raw


def _node(tmp_path, monkeypatch, *, answers=False):
    _two_chats(monkeypatch)
    return twins.build(tmp_path / "twin", members=8, hidden_facts=0, seed=3, protected=True, bodies=ROADMAP + GARDEN,
                       policy_factory=_answers_policy if answers else None)


@pytest.fixture
def node(tmp_path, monkeypatch):
    return _node(tmp_path, monkeypatch)


@pytest.fixture
def narrow(monkeypatch):
    monkeypatch.setenv(NARROW, "true")


@pytest.fixture
def off(monkeypatch):
    """The switch set off: main's whole-index drop (the default is on since the owner's ruling of 16 Oct)."""
    monkeypatch.setenv(NARROW, "false")


def _grant(node):
    return node.search_raw["binding"]["grant_id"]


def _path(node):
    return index_path(node.index.root, _grant(node))


def _members(node) -> set:
    return {opaque for opaque, _sealed in node.index._open(_path(node))["sealed"]}


def _opaque(node, number: int) -> str:
    key = node.index.keys.get(_grant(node), create=False)
    return opaque_record_id(key, grant_id=_grant(node), table="conversation_messages", source_id="imessage",
                            dataset_id=twins.DATASET, record_id=f"imessage:{number}")


def _ops(node, *numbers) -> set:
    return {_opaque(node, number) for number in numbers}


def _served(node) -> set:
    """What a search over every member releases now, by the members' bodies; raises when it refuses."""
    output, refused = node.search_request("roadmap planning note garden watering chore", k=10)
    assert refused is None, refused
    return {record["content"] for record in output["records"]}


def _bodies(*numbers) -> set:
    return {(ROADMAP + GARDEN)[number - 1] for number in numbers}


def _sweep(node, caplog) -> list:
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        node.index.sweep(now=node.now[0])
    return [record.getMessage() for record in caplog.records if "message search index" in record.getMessage()]


@pytest.fixture
def events(monkeypatch):
    """Every share-index event the node reports (search_timing.index_event), with its fields."""
    seen = []
    original = search_timing.index_event

    def record(stage, path, seconds=0.0, **fields):
        seen.append((stage, dict(fields)))
        return original(stage, path, seconds, **fields)
    monkeypatch.setattr(search_timing, "index_event", record)
    return seen


# --- what ordinary ingestion and the owner write -----------------------------------------------------------------

def _write(node, sql, args=()):
    with sqlite3.connect(node.corpus.path) as conn:
        conn.execute(sql, args)


def _participant(node, conversation, contact):
    _write(node, "INSERT INTO conversation_participants(conversation_id,dataset_id,source_id,contact_id,role) "
                 "VALUES(?,?,?,?,'participant')", (conversation, twins.DATASET, "imessage", contact))


def _append(node, conversation, *, sender="self", from_self=1, message_id="new-after-last", content="an invented reply"):
    _write(node, "INSERT INTO conversation_messages(message_id,conversation_id,dataset_id,source_id,source_record_id,"
                 "sender_id,sender_type,is_from_self,event_at,content) VALUES (?,?,?,?,?,?,?,?,?,?)",
           (message_id, conversation, twins.DATASET, "imessage", message_id, sender, "human", from_self, LATER, content))


def _reassess(node, *numbers):
    """A machine assessment of each member again, on its current context (a catch-up pass's work)."""
    resolver, reviews = node.corpus.resolver, node.corpus.reviews
    with owner():
        for number in numbers:
            identity = resolver._identity("conversation_messages", f"imessage:{number}", "imessage", twins.DATASET)
            prepared = prepare(resolver, reviews, identity)
            publish(resolver, reviews, prepared, parse_assessment(dict(domains=["work"], sensitivity="none",
                    speech="original_message", protected_content="none"), prepared["snapshot"].message),
                    now=node.now[0])


def _revoke(node):
    with owner():
        node.ledger.revoke(_grant(node), expected_epoch=node.epoch(), command_id="revoke-nr188")


# --- 1. attribution: a fixed sub-reason word, with or without the switch ------------------------------------------

def test_control_nothing_moved_nothing_is_said(node, caplog, events):
    assert _sweep(node, caplog) == [] and _path(node).exists()
    assert [stage for stage, _ in events if stage == "index_stale"] == []


def test_a_new_participant_is_attributed_to_the_conversation_context(node, caplog, events, off):
    _participant(node, "7", "contact-new")
    assert _sweep(node, caplog) == ["message search index stale (dependencies)",
                                    "message search index member drift (dependencies: conversation_context)"]
    assert ("index_stale", {"reason": "dependencies", "sub": "conversation_context"}) in events
    assert not _path(node).exists()          # switch off: main's whole drop


def test_a_new_sender_is_attributed_to_the_conversation_context(node, caplog, off):
    _append(node, "7", sender="contact-new", from_self=0)
    assert _sweep(node, caplog) == ["message search index stale (dependencies)",
                                    "message search index member drift (dependencies: conversation_context)"]


def test_a_new_neighbour_is_attributed_as_inserted(node, caplog, events, off):
    _append(node, "7")
    assert _sweep(node, caplog) == ["message search index stale (classification_context)",
                                    "message search index member drift (classification_context: neighbour_inserted)"]
    assert ("index_stale", {"reason": "classification_context", "sub": "neighbour_inserted"}) in events


def test_the_dependency_word_tells_the_rows_binding_from_its_stored_columns(node):
    """`row_binding`: the member's own row moved only in what the node binds onto it at load (its source's posture,
    its proof, its AI chat's row), its stored columns did not; `row_revision`: they did (or another row moved)."""
    index = node.index
    opened = index._open(_path(node))
    key = index.keys.get(_grant(node), create=False)
    member = next(search_index.unseal(key, opaque, sealed) for opaque, sealed in opened["sealed"]
                  if opaque == _opaque(node, 5))
    own = index._own_key(member)
    resolver = node.corpus.resolver
    with resolver._read(gated=False) as (conn, _floor):
        conn.row_factory = sqlite3.Row
        assert index._dependency_word(conn, member, {"moved": {own: "row_revision"}}) == "row_binding"
        assert index._dependency_word(conn, member, {"moved": {own: "conversation_context"}}) == "conversation_context"
        assert index._dependency_word(conn, member, {"moved": {"another-row": "row_revision"}}) == "row_revision"
        assert index._dependency_word(conn, member, {"sub": "missing"}) == "missing"
    _write(node, "UPDATE conversation_messages SET content='garden watering chore item5 edited' "
                 "WHERE message_id='imessage:5'")
    with resolver._read(gated=False) as (conn, _floor):
        conn.row_factory = sqlite3.Row
        assert index._dependency_word(conn, member, {"moved": {own: "row_revision"}}) == "row_revision"


@pytest.mark.parametrize("switch", ["off", "on"])
def test_a_source_posture_change_drops_the_whole_index_at_once(node, caplog, monkeypatch, switch):
    """Withdrawn consent (the owner sets the source's posture): the recovered rows' proof no longer holds, so every
    member is unavailable and the whole index goes, with the switch on as off."""
    monkeypatch.setenv(NARROW, "true" if switch == "on" else "false")
    _write(node, "UPDATE user_ingestion_sources SET posture='ambient'")
    assert _gone_after_sweep(node, caplog) == ["message search index stale (member_unavailable)"]


def test_the_attribution_tells_an_inserted_neighbour_from_a_changed_one(node):
    """`_context_drift` (attribution only) on the sealed revision: rows added next to the member, or anything else."""
    resolver = node.corpus.resolver
    with resolver._read(gated=False) as (conn, _floor):
        identity = resolver._identity("conversation_messages", "imessage:4", "imessage", twins.DATASET)
        row = resolver._load(conn, identity)
        boundary = resolver.entity_boundary(conn)
        sealed = context_for(conn, identity, row, boundary=boundary)[0]
        # The attribution's own reading of the window gives the revision `context_for` gives (drift is caught here).
        assert search_index._context_revision(conn, identity, row, boundary, frozenset()) == sealed
    _append(node, "7")
    _append(node, "7", message_id="new-after-last-2", content="a second invented reply")
    with resolver._read(gated=False) as (conn, _floor):
        row = resolver._load(conn, identity)
        boundary = resolver.entity_boundary(conn)
        assert context_for(conn, identity, row, boundary=boundary)[0] != sealed
        assert search_index._context_drift(conn, identity, row, boundary, sealed) == "neighbour_inserted"
    _write(node, "UPDATE conversation_messages SET content='an edited roadmap note' WHERE message_id='imessage:3'")
    with resolver._read(gated=False) as (conn, _floor):
        row = resolver._load(conn, identity)
        boundary = resolver.entity_boundary(conn)
        assert search_index._context_drift(conn, identity, row, boundary, sealed) == "neighbour_changed"


# --- 2/3. the switch on: the moved members are withheld, the rest keep serving -----------------------------------

def test_switched_off_main_drops_the_whole_index(node, caplog, off):
    _append(node, "7")
    assert _sweep(node, caplog)[0] == "message search index stale (classification_context)"
    assert not _path(node).exists()


def test_the_switch_is_on_by_default(node, caplog, monkeypatch):
    monkeypatch.delenv(NARROW, raising=False)
    _append(node, "7")
    assert _sweep(node, caplog)[-1] == "message search index narrowed (2 withheld)"
    assert _members(node) == _ops(node, 1, 2, 5, 6, 7, 8)


def test_a_new_neighbour_withholds_only_the_members_next_to_it(node, caplog, narrow, events):
    before = _members(node)
    assert _served(node) == _bodies(*range(1, 9))
    node.index.take_published()   # the build's own publish, already seen
    _append(node, "7")      # next to imessage:3 and imessage:4 only
    assert _sweep(node, caplog) == [
        "message search index member drift (classification_context: neighbour_inserted)",
        "message search index narrowed (2 withheld)"]
    assert _path(node).exists() and _members(node) == before - _ops(node, 3, 4)
    assert _served(node) == _bodies(1, 2, 5, 6, 7, 8)
    assert ("index_rebuild", {"state": "narrowed", "members": 6}) in events
    assert node.index.take_narrowed() == {_path(node).name}     # owes the refresh loop one rebuild
    assert _path(node).name not in node.index.take_published()  # and settles no restore
    assert _sweep(node, caplog) == [] and _members(node) == before - _ops(node, 3, 4)   # the narrowed index is current


def test_a_new_participant_withholds_its_conversation_and_the_other_keeps_serving(node, caplog, narrow):
    _participant(node, "7", "contact-new")
    assert _sweep(node, caplog) == [
        "message search index member drift (dependencies: conversation_context)",
        "message search index narrowed (4 withheld)"]
    assert _members(node) == _ops(node, 5, 6, 7, 8)
    assert _served(node) == _bodies(5, 6, 7, 8)


def test_a_new_sender_withholds_its_conversation_and_the_other_keeps_serving(node, caplog, narrow):
    _append(node, "8", sender="contact-new", from_self=0)
    lines = _sweep(node, caplog)
    assert lines[-1] == "message search index narrowed (4 withheld)" and not any("stale" in line for line in lines)
    assert "message search index member drift (dependencies: conversation_context)" in lines
    assert _members(node) == _ops(node, 1, 2, 3, 4)
    assert _served(node) == _bodies(1, 2, 3, 4)


# --- fail closed: restrictions still drop the whole index, at once --------------------------------------------------

def _gone_after_sweep(node, caplog) -> list:
    lines = _sweep(node, caplog)
    assert not _path(node).exists()
    assert not any(line.startswith("message search index narrowed") for line in lines)
    return lines


def test_a_revocation_during_narrowing_drops_the_whole_index(node, caplog, narrow):
    _append(node, "7")
    _revoke(node)
    assert _gone_after_sweep(node, caplog) == ["message search index stale (authority)"]
    assert node.index.keys.get(_grant(node), create=False) is None


def test_an_off_limits_change_during_narrowing_drops_the_whole_index(node, caplog, narrow):
    _append(node, "7")
    _write(node, "INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name) "
                 "VALUES('protected-entity-2','person','Jules Sample','jules sample')")
    _write(node, "INSERT INTO entity_blackholes(blackhole_id,entity_id,canonical_name,normalized_name,rebuild_state) "
                 "VALUES('bh-2','protected-entity-2','Jules Sample','jules sample','complete')")
    assert _gone_after_sweep(node, caplog)[0] == "message search index stale (basis)"


def test_a_policy_narrowing_during_narrowing_drops_the_whole_index(node, caplog, narrow):
    _append(node, "7")
    narrower = copy.deepcopy(node.search_raw)
    narrower["policy_version_id"] = "policy-grant-search-narrower"
    narrower["search"]["max_k"] = 5
    node.activate(narrower, generation=2)
    assert _gone_after_sweep(node, caplog)[0] == "message search index stale (basis)"


def test_a_review_change_of_a_member_during_narrowing_drops_the_whole_index(node, caplog, narrow):
    _append(node, "7")
    _reassess(node, 1)
    assert _gone_after_sweep(node, caplog)[0] == "message search index stale (basis)"


@pytest.mark.parametrize("change", ["edit", "delete", "scrub", "reflag"])
def test_a_changed_indexed_member_is_never_served_after_the_change(node, caplog, narrow, change):
    """An edit, a delete, a scrub or a re-flag of an indexed member is a change to evidence already in the index: the
    whole index goes, as on main, on the request path and in the sweep alike (its fingerprint or its row)."""
    if change == "edit":
        _write(node, "UPDATE conversation_messages SET content='garden watering chore item5 edited' "
                     "WHERE message_id='imessage:5'")
    elif change == "delete":
        _write(node, "DELETE FROM conversation_messages WHERE message_id='imessage:5'")
    elif change == "scrub":
        _write(node, "UPDATE conversation_messages SET content='' WHERE message_id='imessage:5'")
    else:
        _write(node, "ALTER TABLE conversation_messages ADD COLUMN content_nsfw INTEGER")
        _write(node, "UPDATE conversation_messages SET content_nsfw=1 WHERE message_id='imessage:5'")
    output, refused = node.search_request("garden watering chore", k=10)
    assert output is None and refused is not None
    assert not _path(node).exists()            # the request path's own whole drop, as main
    assert node.rebuild()[_grant(node)] == "ready"
    assert GARDEN[0] not in _served(node)


def test_a_protected_contact_joining_hits_the_veto_and_drops_the_whole_index(node, caplog, narrow):
    _participant(node, "7", "protected-contact")
    assert _gone_after_sweep(node, caplog) == ["message search index stale (member_unavailable)"]


def test_a_new_neighbour_and_an_edited_member_drop_the_whole_index(node, caplog, narrow):
    _append(node, "7")
    _write(node, "ALTER TABLE conversation_messages ADD COLUMN content_nsfw INTEGER")
    _write(node, "UPDATE conversation_messages SET content_nsfw=1 WHERE message_id='imessage:6'")
    _gone_after_sweep(node, caplog)


# --- the narrowed publish: removes members only, over the file it checked, never against a build -------------------

def _after_the_check(monkeypatch, action):
    """Run `action` once, right after the sweep's check of the file and before anything it decided is applied."""
    original = SearchIndexService._current
    done = []

    def current(self, *args, **kwargs):
        verdict = original(self, *args, **kwargs)
        if kwargs.get("refresh") and not done:
            done.append(True)
            action()
        return verdict
    monkeypatch.setattr(SearchIndexService, "_current", current)


def test_a_revocation_between_the_check_and_the_publish_publishes_nothing(node, caplog, narrow, monkeypatch):
    _append(node, "7")
    state = _file_state(_path(node))
    _after_the_check(monkeypatch, lambda: _revoke(node))
    lines = _sweep(node, caplog)
    assert "message search index narrowed (2 withheld)" not in lines
    assert _file_state(_path(node)) in (state, None)      # untouched, never a narrowed copy for a revoked grant
    monkeypatch.undo()
    monkeypatch.setenv(NARROW, "true")
    _sweep(node, caplog)
    assert not _path(node).exists()


def test_a_file_replaced_between_the_check_and_the_publish_is_left_as_it_is(node, caplog, narrow, monkeypatch):
    _append(node, "7")

    def replace():
        with owner():   # an owner build publishing a new index over the file the sweep checked
            node.index._publish(_grant(node), node.index._open(_path(node))["basis"], "ready", None, None, [])
    _after_the_check(monkeypatch, replace)
    _sweep(node, caplog)
    assert _path(node).exists() and _members(node) == set()     # the replacement, not a narrowed copy of the old file


def test_a_build_in_progress_holds_the_narrowed_publish_back(node, caplog, narrow):
    _append(node, "7")
    before = _file_state(_path(node))
    held, release = threading.Event(), threading.Event()

    def build():
        with BUILD_SLOT:
            held.set()
            release.wait(10)
    worker = threading.Thread(target=build)
    worker.start()
    try:
        assert held.wait(5)
        lines = _sweep(node, caplog)
        assert not any(line.startswith("message search index narrowed") for line in lines)
        assert _file_state(_path(node)) == before          # neither narrowed nor shredded while the build runs
    finally:
        release.set()
        worker.join(5)
    assert _sweep(node, caplog)[-1] == "message search index narrowed (2 withheld)"


def test_the_narrowed_publish_settles_no_restore_and_owes_one_rebuild(node, caplog, narrow):
    clock = Clock(node.now[0])
    loop = loop_for(node, clock)
    loop.observe(node.index)
    grant = _grant(node)
    loop._pending[grant] = {"causes": {"review_changed"}, "attempts": 0, "not_before": 0.0, "first_drop_at": clock.now}
    _append(node, "7")
    assert _sweep(node, caplog)[-1] == "message search index narrowed (2 withheld)"
    loop.observe(node.index)
    assert loop._pending[grant]["causes"] == {"review_changed", "members_narrowed"}   # still owed, now for both
    assert "members_narrowed" in REFRESH_CAUSES and "members_narrowed" in DROP_CAUSES
    receipt = loop.run_pending()
    assert receipt is not None and receipt.cause_classes == ["members_narrowed", "review_changed"]
    assert receipt.grants[0].state == "ready" and grant not in loop._pending


def test_the_narrowed_members_come_back_with_the_next_full_rebuild(node, caplog, narrow):
    _append(node, "7")
    _sweep(node, caplog)
    assert _members(node) == _ops(node, 1, 2, 5, 6, 7, 8)
    _reassess(node, 3, 4)        # the catch-up pass assesses them on their new context (DR-31's delay)
    assert node.rebuild()[_grant(node)] == "ready"
    assert _members(node) == _ops(node, *range(1, 9)) and _served(node) == _bodies(*range(1, 9))


# --- 4. the request path: refuses as not served now, never shreds, never serves a moved member -----------------------

def test_the_request_path_refuses_without_shredding_then_the_sweep_narrows(node, caplog, narrow):
    _append(node, "7")
    state = _file_state(_path(node))
    output, refused = node.search_request("roadmap planning note", k=10)
    assert output is None and refused is not None
    assert _file_state(_path(node)) == state                    # kept for the sweep
    batch, refused = node.search_batch_request(["roadmap planning note", "garden watering chore"], k=10)
    assert batch is None and refused is not None
    assert _file_state(_path(node)) == state
    _sweep(node, caplog)
    assert _served(node) == _bodies(1, 2, 5, 6, 7, 8)


def test_switch_off_the_request_path_shreds_as_main(node, off):
    _append(node, "7")
    output, refused = node.search_request("roadmap planning note", k=10)
    assert output is None and refused is not None
    assert not _path(node).exists()


def test_check_own_on_a_narrow_only_failure_refuses_and_keeps_the_file(node, narrow):
    _append(node, "7")
    state = _file_state(_path(node))
    with node.ledger._transaction() as db:
        authority = node.ledger._authority(db, _grant(node), node.now[0])[0]
    with pytest.raises(PolicyError, match="search_index_stale"):
        node.index.check_own(_grant(node), authority, now=node.now[0])
    assert _file_state(_path(node)) == state


def test_the_send_check_refuses_a_released_member_the_index_no_longer_holds(node, caplog, narrow, monkeypatch):
    with node.ledger._transaction() as db:
        authority = node.ledger._authority(db, _grant(node), node.now[0])[0]
    released = frozenset(_ops(node, 3, 5))
    node.index.check_own(_grant(node), authority, now=node.now[0], released=released)   # both held: sends
    _append(node, "7")
    _sweep(node, caplog)                                                                 # imessage:3 narrowed
    with pytest.raises(PolicyError, match="search_index_stale"):
        node.index.check_own(_grant(node), authority, now=node.now[0], released=released)
    assert _path(node).exists()                                                          # refused, never shredded
    node.index.check_own(_grant(node), authority, now=node.now[0], released=frozenset(_ops(node, 5)))
    monkeypatch.setenv(NARROW, "false")
    node.index.check_own(_grant(node), authority, now=node.now[0], released=released)   # switch off: main's check


# --- answers: a written answer is held while its share is narrowed, then delivered or ended for good ---------------

class _Codes:
    def __init__(self, node, monkeypatch):
        import sys
        self.codes = []
        original = node.protocol.ledger.refuse

        def refuse(*args, **kwargs):
            self.codes.append(getattr(sys.exc_info()[1], "code", None))
            return original(*args, **kwargs)
        monkeypatch.setattr(node.protocol.ledger, "refuse", refuse)


def _written(node, service):
    answer_id = _ask(node, service, QUESTION)
    for _ in range(500):
        job = service._jobs.get(answer_id)
        if job is not None and job.state == "ended":
            assert job.body is not None and job.body.outcome == "answered"
            return answer_id, job
        time.sleep(.01)
    raise AssertionError("the answer worker did not finish")


@pytest.fixture
def answering(tmp_path, monkeypatch):
    node = _node(tmp_path, monkeypatch, answers=True)
    service = _service(node, ANSWER)
    yield node, service
    service.close()


def test_a_written_answer_whose_item_was_narrowed_ends_answer_evidence_moved(answering, caplog, narrow, monkeypatch):
    node, service = answering
    codes = _Codes(node, monkeypatch)
    answer_id, job = _written(node, service)
    assert _ops(node, 3, 4) <= set(job.record_ids)       # written from the roadmap members, 3 and 4 among them
    _append(node, "7")                                     # their context moves
    assert _fetch(node, service, answer_id)["state"] == "pending"     # held, not refused: the share is narrowing
    assert answer_id in service._jobs and _path(node).exists()
    _sweep(node, caplog)                                   # narrowed: 3 and 4 are no longer in the index
    with pytest.raises(PolicyError, match="permission_denied"):
        _fetch(node, service, answer_id)
    assert codes.codes[-1] == "answer_evidence_moved" and answer_id not in service._jobs   # final


def test_a_written_answer_from_untouched_items_is_delivered_while_another_conversation_narrows(answering, caplog,
                                                                                                 narrow):
    node, service = answering
    answer_id, job = _written(node, service)
    assert not _ops(node, 7, 8) & set(job.record_ids)     # garden members are not what it was written from
    _append(node, "8")
    assert _fetch(node, service, answer_id)["outcome"] == "answered"   # before the sweep
    _sweep(node, caplog)
    assert _fetch(node, service, answer_id)["outcome"] == "answered"   # and after it


# --- what only a sealed member can show: every dependency is checked, and only the duplicate context is forgiven ---

def _tamper(node, number, change):
    """Re-seal one member of the index file with `change` applied (an index built by an older or a broken build)."""
    key = node.index.keys.get(_grant(node), create=False)
    opaque = _opaque(node, number)
    with sqlite3.connect(_path(node)) as conn:
        sealed = conn.execute("SELECT sealed FROM members WHERE opaque_id=?", (opaque,)).fetchone()[0]
        member = search_index.unseal(key, opaque, sealed)
        change(member)
        conn.execute("UPDATE members SET sealed=? WHERE opaque_id=?", (search_index.seal(key, opaque, member), opaque))


def test_a_member_with_no_dependencies_sealed_drops_the_whole_index(node, caplog, narrow):
    _tamper(node, 1, lambda member: member.update(entity_dependencies=[]))
    assert _gone_after_sweep(node, caplog) == ["message search index stale (dependencies)",
                                               "message search index member drift (dependencies: missing)"]


def test_every_dependency_of_a_moved_member_is_still_checked(node, caplog, narrow):
    """The first dependency moved cleanly (a new participant); a later one cannot be read: the whole index goes."""
    _tamper(node, 1, lambda member: member["entity_dependencies"].append(
        {"table": "conversation_messages", "record_id": "imessage:999", "source_id": "imessage",
         "dataset_id": twins.DATASET, "revision": "x", "context": "y"}))
    _participant(node, "7", "contact-new")
    assert _gone_after_sweep(node, caplog)[-1] == "message search index stale (member_unavailable)"


def test_an_entity_context_sealed_apart_from_the_dependency_is_not_forgiven(node, caplog, narrow):
    _tamper(node, 1, lambda member: member.update(entity_context_revision="sealed-apart"))
    _participant(node, "7", "contact-new")
    assert _gone_after_sweep(node, caplog)[-1] == "message search index stale (context)"


def test_an_entity_context_that_moved_otherwise_than_the_dependency_is_not_forgiven(node, caplog, narrow, monkeypatch):
    """The `context` check reads a value other than the one the dependency check found moved: a failure of its own."""
    from topos.permissions_v2.entity_boundary import EntityBoundary
    original = EntityBoundary.check

    def check(self, **kwargs):
        revision = original(self, **kwargs)
        loaded = any(name.startswith("_p2b") for name in kwargs["row"])
        return revision if loaded or kwargs["record_id"] != "imessage:1" else "read-otherwise"
    monkeypatch.setattr(EntityBoundary, "check", check)
    _participant(node, "7", "contact-new")
    assert _gone_after_sweep(node, caplog)[-1] == "message search index stale (context)"


def test_without_an_active_boundary_a_deleted_member_drops_the_whole_index(tmp_path, monkeypatch, caplog, narrow):
    _two_chats(monkeypatch)
    plain = twins.build(tmp_path / "plain", members=8, hidden_facts=0, seed=3, protected=False,
                        bodies=ROADMAP + GARDEN)
    _write(plain, "DELETE FROM conversation_messages WHERE message_id='imessage:2'")
    # Its neighbours' contexts moved too (withheld, said once, if the loop meets them first); its own row is gone.
    assert _gone_after_sweep(plain, caplog)[-2:] == [
        "message search index stale (classification_context)",
        "message search index member drift (classification_context: row_unavailable)"]


# --- a build that new data kept from publishing keeps the narrowing index; replaced files; vectors -----------------

@pytest.mark.parametrize("switch", ["off", "on"])
def test_a_build_that_could_not_publish_keeps_an_index_new_data_only_narrows(node, monkeypatch, switch):
    monkeypatch.setenv(NARROW, "true" if switch == "on" else "false")
    _append(node, "7")
    state = _file_state(_path(node))
    monkeypatch.setattr(SearchIndexService, "_rebuild_once", lambda self, *args, **kwargs: None)
    with owner():
        assert node.index.rebuild(_grant(node), now=node.now[0])["state"] == "stale"
    if switch == "on":
        assert _file_state(_path(node)) == state       # kept: the sweep narrows it
    else:
        assert not _path(node).exists()                # main: purged


def test_a_file_replaced_after_the_copy_is_left_as_it_is(node, caplog, narrow, monkeypatch):
    _append(node, "7")
    original = SearchIndexService._narrowed_copy

    def copy_then_replace(self, *args, **kwargs):
        made = original(self, *args, **kwargs)
        self._publish(_grant(node), self._open(_path(node))["basis"], "ready", None, None, [])
        return made
    monkeypatch.setattr(SearchIndexService, "_narrowed_copy", copy_then_replace)
    lines = _sweep(node, caplog)
    assert not any(line.startswith("message search index narrowed") for line in lines)
    assert _path(node).exists() and _members(node) == set()          # the replacement stands
    assert list(node.index.root.glob(".build-*.db")) == []             # and the copy was shredded


def test_the_narrowed_copy_drops_the_withheld_members_vectors(node, caplog, narrow):
    with sqlite3.connect(_path(node)) as conn:
        for opaque in _members(node):
            conn.execute("INSERT INTO vectors VALUES (?, 0, ?)", (opaque, search_index._f32([1.0, 0.0])))
    _append(node, "7")
    assert _sweep(node, caplog)[-1] == "message search index narrowed (2 withheld)"
    with sqlite3.connect(_path(node)) as conn:
        assert {row[0] for row in conn.execute("SELECT opaque_id FROM vectors")} == _ops(node, 1, 2, 5, 6, 7, 8)
    with node.ledger._transaction() as db:
        authority = node.ledger._authority(db, _grant(node), node.now[0])[0]
    assert len(node.index.load(_grant(node), authority).members) == 6


# --- the send check, through the relay door: a record withheld after the checkpoint never leaves ----------------

@pytest.mark.parametrize("conversation, sent", [("7", False), ("8", True)], ids=["its_item_withheld", "other_items"])
def test_a_record_withheld_between_checkpoint_and_send_is_never_sent(node, caplog, narrow, monkeypatch,
                                                                       conversation, sent):
    import asyncio
    from tests.permissions_v2.test_search_send_token import after_checkpoint, relayed
    node.query = {"query": "roadmap planning note", "k": 5}

    def narrow_now():
        _append(node, conversation)
        assert _sweep(node, caplog)[-1] == "message search index narrowed (2 withheld)"
    after_checkpoint(node, monkeypatch, narrow_now)
    frame = asyncio.run(relayed(node, monkeypatch, "nr188-send"))
    assert (frame["status"] == "ok") is sent
    assert _path(node).exists()                       # refused or sent, never shredded


def test_a_batch_whose_record_was_withheld_after_its_checkpoint_is_never_sent(node, caplog, narrow, monkeypatch):
    import asyncio
    from tests.permissions_v2.test_message_search_batch import send_batch
    from tests.permissions_v2.test_search_send_token import after_batch_checkpoint

    def narrow_now():
        _append(node, "7")
        assert _sweep(node, caplog)[-1] == "message search index narrowed (2 withheld)"
    after_batch_checkpoint(node, monkeypatch, narrow_now)
    payloads = [{"query": "roadmap planning note", "k": 5}, {"query": "garden watering chore", "k": 5}]
    frame = asyncio.run(send_batch(node, payloads, monkeypatch))
    assert frame["status"] == "error" and _path(node).exists()


# --- a moved member with a lineage, copy or rebuild of its own still follows main --------------------------------------

def test_a_moved_member_whose_lineage_also_moved_drops_the_whole_index(node, caplog, narrow):
    _write(node, f"INSERT INTO signal_objects({twins.FACT_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?)",
           ("sibling-late", "profile", "fact", "sibling-late",
            '{"subject_entity_id": "owner-entity", "predicate": "mentions", "object_value": "a late note", '
            '"disclosure": "scoped", "asserted_by": "owner"}',
            '[{"table": "conversation_messages", "record_id": "imessage:1", "source_id": "imessage"}]', "t", "t", "t"))
    _participant(node, "7", "contact-new")
    assert _gone_after_sweep(node, caplog)[-1] == "message search index stale (lineage)"


def test_a_narrowed_copy_that_cannot_be_made_takes_the_index_as_main_would(node, caplog, narrow, monkeypatch):
    def broken(self, *args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(SearchIndexService, "_narrowed_copy", broken)
    _append(node, "7")
    lines = _gone_after_sweep(node, caplog)
    assert lines[-1] == "message search index narrowing unavailable (OSError)"


def test_a_build_publish_settles_the_rebuild_a_narrowing_owed(node, caplog, narrow):
    clock = Clock(node.now[0])
    loop = loop_for(node, clock)
    loop.observe(node.index)
    _append(node, "7")
    _sweep(node, caplog)
    loop.observe(node.index)
    assert loop._pending[_grant(node)]["causes"] == {"members_narrowed"}
    assert node.rebuild()[_grant(node)] == "ready"   # an owner build published the share's index again
    loop.observe(node.index)
    assert _grant(node) not in loop._pending


def test_the_sub_reason_reaches_the_timing_line(node, caplog, monkeypatch, off):
    from tests.permissions_v2.test_search_timing_attribution import FLAG, LOGGER
    monkeypatch.setenv(FLAG, "true")
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_SEARCH_ATTRIBUTION", "true")
    _participant(node, "7", "contact-new")
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        node.index.sweep(now=node.now[0])
    lines = [record.getMessage() for record in caplog.records
             if record.name == LOGGER and " stage=index_stale " in record.getMessage()]
    assert len(lines) == 1 and " class=dependencies sub=conversation_context" in lines[0]


# --- the live shape: an AI-chat share (an imported ChatGPT export) under an active Off-limits boundary ---------------

from tests.ingestion.test_ai_chat_writer_class import captured_jobs  # noqa: E402,F401 (fixture)
from tests.permissions_v2.test_fresh_node_message_tables import (  # noqa: E402,F401 (fixture)
    EXPORT, NOW as AI_NOW, OWNER as AI_OWNER, _bound, _own_pass, _share_policy, ai_chat_only)


@pytest.fixture
def ai_share(ai_chat_only):
    """One assessed owner line in one imported chat, shared, with an Off-limits entry no row mentions (so the member
    carries its row as a dependency, as on the owner's node)."""
    from tests.permissions_v2.message_search_harness import Node
    from tests.permissions_v2.test_ingest_provenance import owner as owner_as
    from types import SimpleNamespace
    db = ai_chat_only.db
    db.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name) "
               "VALUES('protected-entity','person','Mara Example','mara example')")
    db.execute("INSERT INTO entity_blackholes(blackhole_id,entity_id,canonical_name,normalized_name,rebuild_state) "
               "VALUES('bh','protected-entity','Mara Example','mara example','complete')")
    db.commit()
    resolver, reviews = _bound(ai_chat_only)
    assert _own_pass(resolver, reviews)["assessed"] == 1
    share = Node(SimpleNamespace(resolver=resolver, reviews=reviews, path=resolver.path), ai_chat_only.root / "share",
                 model=None, search_raw=_share_policy(), now=AI_NOW)
    with owner_as(actor=AI_OWNER):
        assert share.index.rebuild("grant-search", now=AI_NOW) == {"state": "ready", "member_count": 1}
    share.db = db
    return share


def _ai(share, sql, args=()):
    share.db.execute(sql, args)
    share.db.commit()


NEW_TURN = ("INSERT INTO ai_chat_messages(message_id, conversation_id, sender_type, sender_id, event_at, content, "
            "sequence, source_id) VALUES ('m-later','chatgpt:thread-file','human',NULL,'2026-09-01T10:00:09Z',"
            "'and one more invented question',2,?)")


def test_ai_chat_a_new_owner_turn_is_attributed_and_main_drops_the_index(ai_share, caplog, off):
    _ai(ai_share, NEW_TURN, (EXPORT,))
    assert _sweep(ai_share, caplog) == ["message search index stale (classification_context)",
                                        "message search index member drift (classification_context: neighbour_inserted)"]
    assert not _path(ai_share).exists()


def test_ai_chat_a_new_owner_turn_withholds_the_line_next_to_it(ai_share, caplog, narrow):
    _ai(ai_share, NEW_TURN, (EXPORT,))
    assert _sweep(ai_share, caplog) == ["message search index member drift (classification_context: neighbour_inserted)",
                                        "message search index narrowed (1 withheld)"]
    assert _path(ai_share).exists() and _members(ai_share) == set()
    output, refused = ai_share.search_request("tide pools talk", k=10)
    assert refused is None and output["records"] == []          # served, holding nothing it may not


def test_ai_chat_a_rewritten_stored_row_still_drops_the_whole_index(ai_share, caplog, narrow):
    """A re-sent row whose stored columns moved (here its metadata) is a change to indexed evidence: its dependency
    moved, and so did its own fingerprint, which no narrowing forgives."""
    _ai(ai_share, "UPDATE ai_chat_messages SET metadata_json='{\"captured\": 2}' WHERE message_id='m-line'")
    assert _sweep(ai_share, caplog) == ["message search index member drift (dependencies: row_revision)",
                                        "message search index stale (fingerprint)"]
    assert not _path(ai_share).exists()


def test_ai_chat_a_chat_row_change_is_the_rows_binding(ai_share, caplog, off):
    _ai(ai_share, "UPDATE ai_chat_conversations SET title='an invented other title'")
    assert _sweep(ai_share, caplog) == ["message search index stale (dependencies)",
                                        "message search index member drift (dependencies: row_binding)"]


def test_ai_chat_a_chat_row_change_withholds_its_lines(ai_share, caplog, narrow):
    """The chat's own row is bound onto each line at load and is its conversation context: both moved alike, so the
    line is withheld (its own drop), the rest of the share would keep serving."""
    _ai(ai_share, "UPDATE ai_chat_conversations SET title='an invented other title'")
    assert _sweep(ai_share, caplog) == ["message search index member drift (dependencies: row_binding)",
                                        "message search index narrowed (1 withheld)"]
    assert _members(ai_share) == set()


# --- review #2: a removal decided about a file also removes the narrowed copy published in its place ---------------

def _narrowed_in_place(node, number=3):
    """What a concurrent sweep does: publish a narrowed copy of the index file in its place, now."""
    with node.ledger._transaction() as db:
        authority = node.ledger._authority(db, _grant(node), node.now[0])[0]
    assert node.index._narrow(_path(node), _grant(node), authority, _file_state(_path(node)),
                              frozenset(_ops(node, number)), now=node.now[0])


def _off_limits_changed(node):
    _write(node, "INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name) "
                 "VALUES('protected-entity-2','person','Jules Sample','jules sample')")
    _write(node, "INSERT INTO entity_blackholes(blackhole_id,entity_id,canonical_name,normalized_name,rebuild_state) "
                 "VALUES('bh-2','protected-entity-2','Jules Sample','jules sample','complete')")


def test_the_sweep_removes_the_narrowed_copy_of_a_file_it_found_stale(node, caplog, narrow, monkeypatch):
    _off_limits_changed(node)
    _after_the_check(monkeypatch, lambda: _narrowed_in_place(node))
    assert _sweep(node, caplog)[0] == "message search index stale (basis)"
    assert not _path(node).exists()


def test_check_own_removes_the_narrowed_copy_of_a_file_it_found_stale(node, narrow, monkeypatch):
    _off_limits_changed(node)
    original = SearchIndexService._current

    def current(self, *args, **kwargs):
        verdict = original(self, *args, **kwargs)
        monkeypatch.setattr(SearchIndexService, "_current", original)
        _narrowed_in_place(node)
        return verdict
    monkeypatch.setattr(SearchIndexService, "_current", current)
    with node.ledger._transaction() as db:
        authority = node.ledger._authority(db, _grant(node), node.now[0])[0]
    with pytest.raises(PolicyError, match="search_index_stale"):
        node.index.check_own(_grant(node), authority, now=node.now[0])
    assert not _path(node).exists()


def test_a_build_published_in_place_is_never_taken_for_a_narrowed_copy(node, caplog, narrow, monkeypatch):
    state = _file_state(_path(node))
    _narrowed_in_place(node)
    assert node.index.same_or_narrowed(_path(node), state)
    assert node.rebuild()[_grant(node)] == "ready"            # a build in its place
    assert not node.index.same_or_narrowed(_path(node), state)
