"""NR166 R1: a release decided outside the write gate, behind `TOPOS_PERMISSIONS_V2_DECIDE_OUTSIDE_GATE` (off).

With the switch off a release is decided as always: one section that holds the write gate from its first read to its
checkpoint. With it on, the member loop and the candidate walks run on a read snapshot with the gate NOT held; a
short gated step then requires every store the decision read to be unchanged since before the snapshot, re-reads
the grant's authority, and only then writes the checkpoint. A moved token: the decision runs once more, then the
uniform refusal. Pinned here:

- off by default, bound or not; with it off the gate is held through the walk and nothing reads the review store
  outside it;
- with it on the gate is free during the walk, and both doors release the same bytes and write the same receipts
  as with it off;
- each consent write of the design note, landed in WAL mode right after the snapshot or right before the confirm:
  the first attempt confirms nothing and writes nothing; the second decides on the new state and ends as a request
  made after the write ends under the gate (the same release, or the same refusal for the same reason);
- a write in both attempts: refused as `release_snapshot_moved`, no third attempt, no checkpoint;
- an authority that lapses between the decision and its confirm is refused by the confirm's own re-read;
- a stale verdict reached while the stores moved removes no index;
- the review store's reads of such a decision take no gate and can write nothing; its clock observation and its
  rollback check still take the gate.

Invented evidence only (the batch suite's p2c-v3 node). The design is
`audits/.../lanes/reports/NR166_R1_DESIGN.md` in the program folder.
"""
from __future__ import annotations

import copy
import sqlite3
import sys
import threading

import pytest

from tests.permissions_v2 import direct_search_twins as dst
from tests.permissions_v2.message_search_harness import owner, pin_record_key, recipient
from tests.permissions_v2.test_message_search_batch import QUERIES, ledger_rows, node, written  # noqa: F401
from tests.permissions_v2.test_nr166_provenance_walk import GRANT, answer, batch, everything, search
from tests.permissions_v2.test_search_provenance_pass import disable_source, replace_snapshot, revoke
from tests.permissions_v2.test_search_verification import ALIAS, UNRELATED, commit, wal
from topos.permissions_v2 import answer_release, evidence, search_release, search_timing, switches
from topos.permissions_v2.automatic_message_review import parse_assessment, prepare, publish
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.message_evidence import message_key
from topos.permissions_v2.search_index import SearchIndexService, index_path
from topos.permissions_v2.search_release import MOVED, MessageSearchRelease, _Decision
from topos.storage.db import write_gate

pytestmark = pytest.mark.public
SWITCH = "TOPOS_PERMISSIONS_V2_DECIDE_OUTSIDE_GATE"
PAYLOAD = QUERIES[0]


@pytest.fixture(autouse=True)
def switch_unset(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)


def on(monkeypatch) -> None:
    monkeypatch.setenv(SWITCH, "true")


def member(node, number=0):
    unit = node.corpus.units[number]
    return node.corpus.resolver._identity("conversation_messages", unit.message_id, "imessage", dst.DATASET)


# --- the writes of the design note, each as the owner or the node makes it -----------------------------------------

def grant_revoked(node):
    with owner():
        node.ledger.revoke(GRANT, expected_epoch=node.epoch(), command_id="r1-revoke")


def share_remoded(node):
    raw = copy.deepcopy(node.search_raw)
    raw["search"]["answers"] = "only"
    raw["policy_version_id"] = "synthetic-policy-remoded"
    node.activate(raw, generation=2)


def share_narrowed(node):
    raw = copy.deepcopy(node.search_raw)
    raw["search"]["max_k"] = 2
    raw["policy_version_id"] = "synthetic-policy-narrowed"
    node.activate(raw, generation=2)


def off_limits_alias(node):
    commit(node, ALIAS)   # the protected person gains an alias several members' text carries


def off_limits_mark(node):
    with sqlite3.connect(node.corpus.path, timeout=0.5) as conn:
        conn.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name) "
                     "VALUES('second-entity','person','Ilse Sample','ilse sample')")
        conn.execute("INSERT INTO entity_blackholes(blackhole_id,entity_id,canonical_name,normalized_name,rebuild_state) "
                     "VALUES('bh-second','second-entity','Ilse Sample','ilse sample','complete')")


def owner_only_mark(node):
    with sqlite3.connect(node.corpus.path, timeout=0.5) as conn:
        conn.execute("INSERT INTO owner_only_records(canonical_table,record_id,created_at,updated_at) "
                     "VALUES('conversation_messages',?,'t','t')", (node.corpus.units[0].message_id,))


def opted_out(node):
    with owner():
        node.corpus.reviews.opt_out(message_key(member(node)), now=node.now[0])


def reassessed(node):
    resolver, reviews = node.corpus.resolver, node.corpus.reviews
    with owner():
        prepared = prepare(resolver, reviews, member(node))
        publish(resolver, reviews, prepared, parse_assessment(dict(domains=["health"], sensitivity="special",
            speech="original_message", protected_content="none"), prepared["snapshot"].message), now=node.now[0])


def record_key_rotated(node):
    pin_record_key(node, key=bytes(range(50, 82)))


WRITES = {
    "unrelated_commit": lambda node: commit(node, UNRELATED),      # ingestion: a row no member touches
    "index_republished": lambda node: node.rebuild(),              # the owner's rebuild publishes the same share
    "grant_revoked": grant_revoked,
    "share_remoded": share_remoded,
    "share_narrowed": share_narrowed,
    "off_limits_alias": off_limits_alias,
    "off_limits_mark": off_limits_mark,                            # moves the protection revision: a sync is owed
    "owner_only_mark": owner_only_mark,
    "opted_out": opted_out,
    "reassessed": reassessed,                                       # a review of a member, no longer shareable
    "source_clock": disable_source,
    "native_proof_revoked": revoke,
    "snapshot_replaced": replace_snapshot,
    "record_key_rotated": record_key_rotated,
}
#: How a search asked AFTER each write ends under the gate (the switch off): "released", or the refusal's own code.
#: `test_a_request_after_each_write_ends_like_this_under_the_gate` pins this table; the races below must end the same.
AFTER = {
    "unrelated_commit": "released", "index_republished": "released", "grant_revoked": "grant_inactive",
    "share_remoded": "authority_stale", "share_narrowed": "authority_stale", "off_limits_alias": "search_index_stale",
    "off_limits_mark": "search_index_stale", "owner_only_mark": "search_index_stale", "opted_out": "search_index_stale",
    "reassessed": "search_index_stale", "source_clock": "search_index_stale",
    "native_proof_revoked": "search_index_stale", "snapshot_replaced": "search_index_stale",
    "record_key_rotated": "search_index_stale",
}


def reasons(monkeypatch) -> list:
    """The code each refusal was refused with inside the node; the caller only ever sees `permission_denied`."""
    seen = []
    original = MessageSearchRelease._spend

    def spend(self, admission, grant_id):
        error = sys.exc_info()[1]
        seen.append(getattr(error, "code", type(error).__name__ if error else None))
        return original(self, admission, grant_id)
    monkeypatch.setattr(MessageSearchRelease, "_spend", spend)
    return seen


def signed(node, request_id, payload=PAYLOAD):
    """A request as the control plane signs it, before anything it may race with."""
    return node._envelope(GRANT, "permissions.v2.search", payload, request_id), payload, request_id


def decided(node, request):
    """(records, None) or (None, "permission_denied")."""
    envelope, payload, request_id = request
    try:
        with recipient():
            _result, output = node.search.dispatch(envelope=envelope.model_dump(), payload=payload, request_id=request_id)
        return output["records"], None
    except PolicyError as refused:
        return None, refused.code


def asked(node, request_id, payload=PAYLOAD):
    return decided(node, signed(node, request_id, payload))


def watched(node, monkeypatch) -> list:
    """In order: each confirm that ran ("moved", "confirmed" or another refusal's code) and each checkpoint."""
    events = []
    confirm = _Decision.confirm

    def recorded(self):
        if self.gated or self._confirmed:
            return confirm(self)
        try:
            confirm(self)
        except PolicyError as refused:
            events.append("moved" if refused.code == MOVED else refused.code)
            raise
        events.append("confirmed")
    monkeypatch.setattr(_Decision, "confirm", recorded)
    for name in ("checkpoint_set_decision", "admit_and_checkpoint_search_batch"):
        original = getattr(node.ledger, name)

        def checkpoint(*args, _original=original, **kwargs):
            events.append("checkpoint")
            return _original(*args, **kwargs)
        monkeypatch.setattr(node.ledger, name, checkpoint)
    return events


def elsewhere(change) -> None:
    """Run `change` to its end on another thread, as the owner's app, the control plane or ingestion would: a
    write made on the deciding thread itself is not what a decision races with (and the review store hands that
    thread a read-only connection while it decides outside the gate)."""
    failed = []

    def run():
        try:
            change()
        except BaseException as error:  # noqa: BLE001 -- reported on the test's own thread
            failed.append(error)
    thread = threading.Thread(target=run, name="another-writer")
    thread.start()
    thread.join()
    if failed:
        raise failed[0]


def landing(monkeypatch, point: str, change, *, attempts=(1,)) -> list:
    """Run `change` during the attempts named: right after the decision's snapshot is established (the walk has
    begun), or right before its confirm (every read is done; only the token can see the write)."""
    fired, count = [], {"n": 0}
    if point == "after_the_snapshot":
        original = MessageSearchRelease._walk_candidates

        def walk(self, *args, **kwargs):
            count["n"] += 1
            if count["n"] in attempts:
                fired.append(count["n"])
                elsewhere(change)
            return original(self, *args, **kwargs)
        monkeypatch.setattr(MessageSearchRelease, "_walk_candidates", walk)
    else:
        original = _Decision.confirm

        def confirm(self):
            if not self.gated and not self._confirmed:
                count["n"] += 1
                if count["n"] in attempts:
                    fired.append(count["n"])
                    elsewhere(change)
            return original(self)
        monkeypatch.setattr(_Decision, "confirm", confirm)
    return fired


def gate_during_the_walk(monkeypatch) -> list:
    """Whether another thread could take the write gate while the candidate walk ran: True means it was free."""
    free = []
    original = MessageSearchRelease._walk_candidates

    def walk(self, *args, **kwargs):
        def take():
            got = write_gate._WRITE_LOCK.acquire(timeout=0.2)
            if got:
                write_gate._WRITE_LOCK.release()
            free.append(got)
        thread = threading.Thread(target=take)
        thread.start()
        thread.join()
        return original(self, *args, **kwargs)
    monkeypatch.setattr(MessageSearchRelease, "_walk_candidates", walk)
    return free


# --- off by default, and nothing changes while it is off --------------------------------------------------------------

def test_the_switch_is_off_on_every_node_and_a_decision_is_then_the_gate_itself(node):
    item = switches.DECIDE_OUTSIDE_GATE
    assert (item.kind, item.unbound, item.bound) == ("bool", False, False) and switches.on(item, {}) is False
    with node.search.verification() as verified:
        deciding = _Decision(node.search, GRANT, verified, None)
        assert deciding.gated is True
        deciding.expect(None, None)
        deciding.confirm()   # nothing to do: the caller holds the gate from first read to checkpoint
        assert not write_gate._WRITE_LOCK._is_owned() and deciding._before is None
    assert MOVED in answer_release.NOT_SERVED_NOW   # an answers retrieval that could not be confirmed decided nothing


def test_with_it_off_the_gate_is_held_through_the_walk_and_the_review_store_is_read_under_it(node, monkeypatch):
    free = gate_during_the_walk(monkeypatch)
    reads, sites = [], []
    original = MessageSearchRelease._walk_candidates

    def walk(self, *args, **kwargs):
        reads.append(evidence._release_reads.get())
        return original(self, *args, **kwargs)
    monkeypatch.setattr(MessageSearchRelease, "_walk_candidates", walk)
    write_gate.set_section_observer(lambda site, caller, waited, held, nested: sites.append((site, nested)))
    try:
        assert search(node, PAYLOAD, "gated-single")["records"]
        assert all(output["records"] for output in batch(node, QUERIES[:3], "gated-batch"))
        answer(node, PAYLOAD["query"])
    finally:
        write_gate.set_section_observer(None)
    assert free == [False] * 5 and reads == [None] * 5     # one walk, three walks, one walk
    own = {site.rsplit(" in ", 1)[1] for site, nested in sites if not nested}
    assert {"_decide", "_decide_batch", "retrieve_for_answer"} <= own   # the decision's own section, named as before
    assert "confirm" not in own and "__enter__" not in own


# --- with it on -----------------------------------------------------------------------------------------------------------

def test_with_it_on_the_gate_is_free_during_the_walk_and_held_from_confirm_to_checkpoint(node, monkeypatch):
    on(monkeypatch)
    free = gate_during_the_walk(monkeypatch)
    held_at_checkpoint = []
    original = node.ledger.checkpoint_set_decision

    def checkpoint(*args, **kwargs):
        held_at_checkpoint.append(write_gate._WRITE_LOCK._is_owned())
        return original(*args, **kwargs)
    monkeypatch.setattr(node.ledger, "checkpoint_set_decision", checkpoint)
    sites = []
    write_gate.set_section_observer(lambda site, caller, waited, held, nested: sites.append((site, caller, held, nested)))
    try:
        assert search(node, PAYLOAD, "outside-single")["records"]
    finally:
        write_gate.set_section_observer(None)
    assert free == [True] and held_at_checkpoint == [True] and not write_gate._WRITE_LOCK._is_owned()
    own = [(site.rsplit(" in ", 1)[1], held) for site, _caller, held, nested in sites if not nested]
    assert "confirm" in [name for name, _held in own]
    assert not [name for name, _held in own if name in ("_decide", "_read")]   # no section spans the decision


def test_both_doors_release_and_receipt_the_same_with_it_on_as_with_it_off(node, monkeypatch):
    wal(node)
    off = everything(node, "off")
    on(monkeypatch)
    events = watched(node, monkeypatch)
    with_it = everything(node, "on")
    assert off == with_it
    # One search, one batch, three retrievals: each confirmed once on its first attempt; the searches checkpointed.
    assert events == ["confirmed", "checkpoint", "confirmed", "checkpoint", "confirmed", "confirmed", "confirmed"]


# --- each consent write, landed during a decision ---------------------------------------------------------------------

@pytest.mark.parametrize("write", sorted(WRITES))
def test_a_request_after_each_write_ends_like_this_under_the_gate(node, monkeypatch, write):
    """The reference: the switch off, and the write finished after the request was admitted and before its decision
    began. That is the request a race leaves behind: signed and admitted on the old state, decided on the new."""
    wal(node)
    quiet, _ = asked(node, "quiet")
    seen = reasons(monkeypatch)
    original, fired = MessageSearchRelease._decide, []

    def decide(self, *args, **kwargs):
        if not fired:
            fired.append(True)
            elsewhere(lambda: WRITES[write](node))
        return original(self, *args, **kwargs)
    monkeypatch.setattr(MessageSearchRelease, "_decide", decide)
    records, refused = asked(node, "after")
    assert fired == [True]
    if AFTER[write] == "released":
        assert records == quiet and refused is None and not seen
    else:
        assert records is None and refused == "permission_denied" and seen == [AFTER[write]]


@pytest.mark.parametrize("point", ["after_the_snapshot", "before_the_confirm"])
@pytest.mark.parametrize("write", sorted(WRITES))
def test_a_write_that_lands_during_a_decision_is_never_decided_past(node, monkeypatch, write, point):
    wal(node)
    quiet, _ = asked(node, "quiet")
    on(monkeypatch)
    events, seen = watched(node, monkeypatch), reasons(monkeypatch)
    fired = landing(monkeypatch, point, lambda: WRITES[write](node))
    before = written(node)
    records, refused = asked(node, "raced")
    assert fired == [1]
    # The first attempt confirmed nothing and wrote nothing; whatever came next came from a fresh snapshot.
    assert events[0] == "moved" and events.count("moved") == 1
    if AFTER[write] == "released":
        assert records == quiet and refused is None and events == ["moved", "confirmed", "checkpoint"] and not seen
        assert ledger_rows(node, "raced")[0] == "checkpointed" and written(node) - before == 2
    else:
        assert records is None and refused == "permission_denied" and "checkpoint" not in events
        assert seen == [AFTER[write]]                      # refused for the reason the write itself gives
        status, receipt = ledger_rows(node, "raced")
        assert status == "refused" and (receipt is None or receipt[0]["verdict"] == "deny")


@pytest.mark.parametrize("point", ["after_the_snapshot", "before_the_confirm"])
def test_a_write_in_both_attempts_is_refused_and_there_is_no_third(node, monkeypatch, point):
    wal(node)
    assert asked(node, "quiet")[0]
    on(monkeypatch)
    events, seen = watched(node, monkeypatch), reasons(monkeypatch)
    numbers = iter(range(100))

    def one_more_row():
        commit(node, UNRELATED.replace("unrelated-entity", f"unrelated-entity-{next(numbers)}"))
    fired = landing(monkeypatch, point, one_more_row, attempts=(1, 2))   # a third attempt would meet no write
    records, refused = asked(node, "twice")
    assert fired == [1, 2] and events == ["moved", "moved"]
    assert records is None and refused == "permission_denied" and seen == [MOVED]
    status, receipt = ledger_rows(node, "twice")
    assert status == "refused" and receipt[0]["verdict"] == "deny" and receipt[0]["output_hash"] is None


@pytest.mark.parametrize("door", ["batch", "answers"])
def test_the_other_two_sites_confirm_the_same_way(node, monkeypatch, door):
    wal(node)
    quiet = batch(node, QUERIES[:3], "quiet-batch") if door == "batch" else answer(node, PAYLOAD["query"])
    on(monkeypatch)
    events = watched(node, monkeypatch)
    numbers = iter(range(100))

    def one_more_row():
        commit(node, UNRELATED.replace("unrelated-entity", f"unrelated-entity-{next(numbers)}"))
    fired = landing(monkeypatch, "before_the_confirm", one_more_row)
    raced = batch(node, QUERIES[:3], "raced-batch") if door == "batch" else answer(node, PAYLOAD["query"])
    assert fired == [1] and raced == quiet
    assert events == (["moved", "confirmed", "checkpoint"] if door == "batch" else ["moved", "confirmed"])
    fired.clear()
    events.clear()
    landing(monkeypatch, "after_the_snapshot", one_more_row, attempts=(1, 2, 3, 4, 5, 6))
    with pytest.raises(PolicyError) as refused:
        batch(node, QUERIES[:3], "twice-batch") if door == "batch" else answer(node, PAYLOAD["query"])
    assert refused.value.code == ("permission_denied" if door == "batch" else MOVED)
    assert events.count("moved") == 2 and "checkpoint" not in events and "confirmed" not in events


def test_an_authority_that_lapses_before_the_confirm_is_refused_by_the_confirms_own_read(node, monkeypatch):
    """No store moves when a grant simply runs out, so no token does. The confirm reads the authority again."""
    on(monkeypatch)
    events, seen = watched(node, monkeypatch), reasons(monkeypatch)
    expires = node.search_raw["validity"]["expires_at"]
    fired = landing(monkeypatch, "before_the_confirm", lambda: node.now.__setitem__(0, expires + 1))
    records, refused = asked(node, "lapsed")
    assert fired == [1] and records is None and refused == "permission_denied"
    assert "checkpoint" not in events and "confirmed" not in events and seen and seen[0] != MOVED


def test_a_stale_verdict_reached_while_the_stores_moved_removes_no_index(node, monkeypatch):
    """A verdict from a snapshot is acted on only if it was about the current state. Here the member loop is made
    to answer "stale" on the first attempt while an unrelated row lands: the index stays, and the second attempt,
    on a fresh snapshot, releases."""
    wal(node)
    quiet, _ = asked(node, "quiet")
    on(monkeypatch)
    calls = {"n": 0}
    original = SearchIndexService._current

    def current(self, *args, **kwargs):
        if kwargs.get("members", True) and kwargs.get("verified") is not None:
            calls["n"] += 1
            if calls["n"] == 1:
                elsewhere(lambda: commit(node, UNRELATED))
                return False
        return original(self, *args, **kwargs)
    monkeypatch.setattr(SearchIndexService, "_current", current)
    records, refused = asked(node, "raced")
    assert records == quiet and refused is None and index_path(node.index.root, GRANT).exists()


def test_a_stale_index_found_on_an_unmoved_snapshot_is_removed_as_before(node, monkeypatch):
    on(monkeypatch)
    seen = reasons(monkeypatch)
    owner_only_mark(node)
    records, refused = asked(node, "stale")
    assert records is None and refused == "permission_denied" and seen == ["search_index_stale"]
    assert not index_path(node.index.root, GRANT).exists()


# --- the review store's reads --------------------------------------------------------------------------------------------

def test_a_decisions_review_reads_take_no_gate_and_can_write_nothing(node):
    reviews = node.corpus.reviews
    sites = []
    write_gate.set_section_observer(lambda site, caller, waited, held, nested: sites.append(site.rsplit(" in ", 1)[1]))
    try:
        with reviews.release_reads():
            with reviews._db() as db:
                assert not write_gate._WRITE_LOCK._is_owned()
                assert reviews._opt_outs_in(db) is not None
            assert sites == []                                  # no gate for a read
            with pytest.raises(PolicyError) as refused:
                with reviews._db() as db:
                    db.execute("DELETE FROM fact_reviews")
            assert refused.value.code == MOVED                  # a read-only connection: not confirmed, decided again
            digest = reviews.current_authority_digest()         # the rollback check: always the gated transaction
            assert sites == ["_db"]
        assert evidence._release_reads.get() is None and reviews.current_authority_digest() == digest
        with reviews._db() as db:                               # outside a decision: the gated transaction of always
            assert write_gate._WRITE_LOCK._is_owned()
    finally:
        write_gate.set_section_observer(None)


def test_the_confirm_is_attributed_with_the_part_that_moved(node, monkeypatch):
    wal(node)
    on(monkeypatch)
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_SEARCH_TIMINGS", "true")
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_SEARCH_ATTRIBUTION", "true")
    try:
        search_timing.attributing()
        search_timing.totals(reset=True)
        landing(monkeypatch, "before_the_confirm", lambda: opted_out_another(node))
        asked(node, "attributed")
        confirm = {(outcome, moved): count for (path, outcome, moved), count in
                   search_timing.totals(reset=True)["paths"].items() if path == "r1_confirm"}
    finally:
        monkeypatch.delenv("TOPOS_PERMISSIONS_V2_SEARCH_ATTRIBUTION")
        search_timing.attributing()
    [(outcome, moved)] = [key for key in confirm if key[0] == "miss"]
    assert moved.startswith("reviews.") and all(part.startswith("reviews.") for part in moved.split(":"))


def opted_out_another(node):
    with owner():
        node.corpus.reviews.opt_out("fact-nobody-has", now=node.now[0])
