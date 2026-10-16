"""NR184: the rules NR166 R1 (`TOPOS_PERMISSIONS_V2_DECIDE_OUTSIDE_GATE`, off) relies on that its own file did not pin.

The security review of R1 (15 Oct, `lanes/reports/NR166_R1_SECURITY_REVIEW.md` in the program folder) wrote mutants
of `search_release._Decision` that pass all 55 tests of `test_nr166_decide_outside_gate.py`. Each test here fails on
one of them (`scripts/permissions_v2/nr184_mutants.py`) and passes on main:

- finding 1: the token is read BEFORE the decision's snapshot is established. A consent write landed right after
  the snapshot's BEGIN (before the clock observation, the authority read and `expect()`) is invisible to that
  snapshot, which each test asserts, so only a token read before the snapshot can see it. Read at `expect()`, the
  token would already include the write and a newly hidden alias would be released. All three doors: the search,
  the batch and the answers retrieval (which has no N5 send check behind it);
- finding 3: an unreadable token (None) never confirms, not even against another None;
- the confirm requires the floor it was given on its own, not because the door compared it first;
- finding 4: each part of the token that no consent write moves alone (the kinds switched on, the pinned rubric,
  the native proof's marker -- mid-revocation: the pending marker written, the commit never made -- and the two
  directories) is in the token. Each change moves that part and no other (asserted), and a decision it lands
  under is not confirmed and ends as a request made after it ends under the gate;
- the batch door and the answers retrieval raced against every consent write the search door is raced against, at
  both points, each ending as the same request ends under the gate (pinned per door).

Invented evidence only (the batch suite's p2c-v3 node, `direct_search_twins`).
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

import pytest

from tests.permissions_v2 import direct_search_twins as dst
from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_message_search_batch import QUERIES, ledger_rows, node, written  # noqa: F401
from tests.permissions_v2.test_nr166_decide_outside_gate import (AFTER, PAYLOAD, WRITES, decided, elsewhere,  # noqa: F401
                                                                landing, on, reasons, signed, switch_unset, watched)
from tests.permissions_v2.test_nr166_provenance_walk import GRANT, answer, batch
from tests.permissions_v2.test_search_provenance_pass import enrollment_id, service_for
from tests.permissions_v2.test_search_verification import wal
from topos.permissions_v2 import evidence, shadow_labeler_local, switches
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence import EvidenceResolver
from topos.permissions_v2.search_index import SearchIndexService
from topos.permissions_v2.search_release import MOVED, MessageSearchRelease, _Decision

pytestmark = pytest.mark.public
DOORS = ("search", "batch", "answers")
JOURNAL = switches.JOURNAL_SOURCES.name


# --- one request at each door ------------------------------------------------------------------------------------------

def ask(node, door, tag):
    """("released", what the caller receives) or ("refused", the code it is refused with)."""
    try:
        if door == "search":
            records, refused = decided(node, signed(node, tag))
            return ("released", records) if refused is None else ("refused", refused)
        if door == "batch":
            return "released", batch(node, QUERIES[:3], tag)
        return "released", answer(node, PAYLOAD["query"])
    except PolicyError as refused:
        return "refused", refused.code
    except Exception as refused:  # noqa: BLE001 -- a refusal that is not a PolicyError is named by its type
        return "refused", type(refused).__name__


def released_something(door, output) -> bool:
    if door == "search":
        return bool(output)
    if door == "batch":
        return any(item["records"] for item in output)
    return b'"records":[]' not in output[0]


def ended(door, outcome, seen) -> str:
    """"released", or the code the request was refused with inside the node. A search's and a batch's caller only
    ever sees `permission_denied` (`seen` holds the code each spent envelope was refused with); the answers
    retrieval's caller is the node's answers service, which reads the code."""
    kind, value = outcome
    if kind == "released":
        assert not seen
        return "released"
    if door == "answers":
        return value
    assert value == "permission_denied" and seen and len(set(seen)) == 1, (value, seen)
    return seen[0]


def before_the_decision(monkeypatch, door, change) -> list:
    """The reference: the switch off, and `change` finished after the request was admitted (the answers retrieval:
    after its caller read the admitted authority) and before its decision began."""
    name = {"search": "_decide", "batch": "_decide_batch", "answers": "retrieve_for_answer"}[door]
    original, fired = getattr(MessageSearchRelease, name), []

    def decide(self, *args, **kwargs):
        if not fired:
            fired.append(True)
            elsewhere(change)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(MessageSearchRelease, name, decide)
    return fired


def nothing_kept(node, door, tag, before):
    """A refused request checkpointed nothing: each spent envelope has its refusal and no released output."""
    if door == "answers":
        assert written(node) == before     # a retrieval writes nothing, released or not
        return
    for request_id in ([tag] if door == "search" else [f"{tag}:{number}" for number in range(3)]):
        status, receipt = ledger_rows(node, request_id)
        assert status == "refused" and (receipt is None or (receipt[0]["verdict"] == "deny"
                                                            and receipt[0]["output_hash"] is None))


# --- finding 1: the token is read before the snapshot is established ----------------------------------------------------

def protected_joins_the_conversations(node):
    """A context change: the protected person joins every conversation a member is in."""
    with sqlite3.connect(node.corpus.path, timeout=0.5) as conn:
        for conversation, source in conn.execute(
                "SELECT DISTINCT conversation_id, source_id FROM conversation_messages").fetchall():
            conn.execute("INSERT INTO conversation_participants(conversation_id,dataset_id,source_id,contact_id,role) "
                         "VALUES(?,?,?,'protected-contact','participant')", (conversation, dst.DATASET, source))


#: Canonical writes that change a member's standing and move no protection clock, each with a read that tells
#: whether a snapshot sees it. A write that moves the protection revision (an Off-limits mark, an owner-only mark) is
#: no test of the token here: the door's own authority read, after the snapshot, refuses it as `authority_stale`.
SNAPSHOT_BLIND = {
    "off_limits_alias": (WRITES["off_limits_alias"],
                         "SELECT aliases_json FROM entities WHERE entity_id='protected-entity'"),
    "protected_joins_the_conversations": (protected_joins_the_conversations,
                                          "SELECT COUNT(*) FROM conversation_participants "
                                          "WHERE contact_id='protected-contact'"),
}


def at_the_snapshot(monkeypatch, node, change, probe) -> list:
    """Run `change` to its end on another thread, once, inside the first decision made outside the gate: right after
    its canonical read's BEGIN and the read's own first queries (the snapshot is established), before its clock
    observation, its authority read and `expect()`. Records what `probe` reads on the decision's snapshot and on a
    fresh connection once the write has landed."""
    found = []
    original = EvidenceResolver._read

    @contextmanager
    def read(self, *, gated=True):
        with original(self, gated=gated) as (conn, floor):
            if not gated and not found and evidence._release_reads.get() is not None:
                assert conn.in_transaction
                elsewhere(change)
                fresh = sqlite3.connect(f"file:{node.corpus.path}?mode=ro", uri=True)
                try:
                    found.append((tuple(conn.execute(probe).fetchone()), tuple(fresh.execute(probe).fetchone())))
                finally:
                    fresh.close()
            yield conn, floor
    monkeypatch.setattr(EvidenceResolver, "_read", read)
    return found


@pytest.mark.parametrize("write", sorted(SNAPSHOT_BLIND))
@pytest.mark.parametrize("door", DOORS)
def test_a_write_landed_right_after_the_snapshot_began_is_not_decided_past(node, monkeypatch, door, write):
    """Review finding 1. The write lands before `expect()`, so a token read there would include it and confirm a
    decision made on the snapshot from before it: the reviewer's mutant released a record carrying the new alias."""
    wal(node)
    quiet = ask(node, door, "quiet")
    assert quiet[0] == "released" and released_something(door, quiet[1])   # the share releases before the write
    on(monkeypatch)
    events, seen = watched(node, monkeypatch), reasons(monkeypatch)
    change, probe = SNAPSHOT_BLIND[write]
    found = at_the_snapshot(monkeypatch, node, lambda: change(node), probe)
    before = written(node)
    outcome = ask(node, door, "raced-early")
    [(in_the_snapshot, on_the_node)] = found
    assert in_the_snapshot != on_the_node      # the decision's reads cannot see the write: only its token can
    assert events[0] == "moved" and "checkpoint" not in events
    assert ended(door, outcome, seen) == AT_THE_GATE[door][write] != "released"
    nothing_kept(node, door, "raced-early", before)


# --- finding 3: an unreadable token never confirms ----------------------------------------------------------------------

@pytest.mark.parametrize("door", DOORS)
def test_an_unreadable_token_never_confirms_not_even_against_another_unreadable_one(node, monkeypatch, door):
    """Review finding 3: None at both ends is not "unmoved". Both attempts are refused as moved, nothing is kept."""
    quiet = ask(node, door, "quiet")
    assert quiet[0] == "released" and released_something(door, quiet[1])
    on(monkeypatch)
    events, seen = watched(node, monkeypatch), reasons(monkeypatch)
    monkeypatch.setattr(SearchIndexService, "send_token", lambda self, *args, **kwargs: None)  # a part unreadable
    before = written(node)
    outcome = ask(node, door, "unreadable")
    assert events == ["moved", "moved"]
    assert ended(door, outcome, seen) == MOVED
    nothing_kept(node, door, "unreadable", before)


@pytest.mark.parametrize("end", ["entry", "confirm"])
def test_a_token_unreadable_at_either_end_is_not_confirmed_and_the_next_attempt_decides(node, monkeypatch, end):
    """None at one end only: that attempt is moved, and the next one, read at both ends, releases as before."""
    quiet, _ = decided(node, signed(node, "quiet"))
    on(monkeypatch)
    events, seen = watched(node, monkeypatch), reasons(monkeypatch)
    original, reads = _Decision._token, []

    def token(self):
        reads.append(True)
        unreadable = len(reads) == (1 if end == "entry" else 2)   # the first attempt's entry, or its confirm
        return None if unreadable else original(self)
    monkeypatch.setattr(_Decision, "_token", token)
    records, refused = decided(node, signed(node, "one-end"))
    assert len(reads) == 4 and events == ["moved", "confirmed", "checkpoint"]
    assert records == quiet and refused is None and not seen


# --- the confirm's own floor ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("floor", ["none", "another"])
def test_the_confirm_requires_its_own_floor_not_only_the_doors(node, monkeypatch, floor):
    """`confirm` refuses an authority whose protection revision is not the floor `expect` recorded. Each door has
    already refused such a pair before `expect` (`authority_stale`), so no door can reach this today; the confirm's
    rule must still hold on its own (it is the one a narrower token will rely on, review finding 4), so this calls
    the decision directly, with the same authority the ledger holds and only the floor wrong."""
    on(monkeypatch)
    with node.ledger._transaction() as db:
        node.protocol._sync_protection(db)
        current = node.ledger._authority(db, GRANT, node.now[0])[0]
    revision = current.protection_revision
    another = ("0" if revision[-1] != "0" else "1").join((revision[:-1], ""))   # one character different
    assert another != revision and len(another) == len(revision)
    given = {"none": None, "another": another}[floor]
    with node.search.verification() as verified:
        with _Decision(node.search, GRANT, verified, None) as deciding:   # the same pair, right: confirmed
            assert not deciding.gated
            deciding.expect(current.protection_revision, current)
            deciding.confirm()
            assert deciding._confirmed
        with pytest.raises(PolicyError) as moved:
            with _Decision(node.search, GRANT, verified, None) as deciding:
                deciding.expect(given, current)
                deciding.confirm()
        assert moved.value.code == MOVED and not deciding._confirmed


# --- finding 4: each part of the token, moved alone ---------------------------------------------------------------------

class _CommitNeverLands(sqlite3.Connection):
    def commit(self):
        raise sqlite3.OperationalError("disk I/O error")   # invented: the revocation's commit fails


def kinds_switched_on(node, monkeypatch, tmp_path):
    """`families`: the journal kind switched on (a family a full check reads, in its basis and its member loop)."""
    monkeypatch.delenv(JOURNAL, raising=False)
    assert not switches.on(switches.JOURNAL_SOURCES)
    return lambda: monkeypatch.setenv(JOURNAL, "true")


def rubric_replaced(node, monkeypatch, tmp_path):
    """`rubric`: the pinned rubric file the full check reads for a knowledge grant replaced by other bytes. The node
    reads a private copy of the reviewed file here, so the repository's own file is never touched."""
    copy = tmp_path / "rubric" / shadow_labeler_local.RUBRIC_PATH.name
    copy.parent.mkdir()
    copy.write_bytes(shadow_labeler_local.RUBRIC_PATH.read_bytes())
    monkeypatch.setattr(shadow_labeler_local, "RUBRIC_PATH", copy)
    shadow_labeler_local.rubric()   # the copy is the reviewed bytes

    def change():
        fresh = copy.with_name("replacement.md")
        fresh.write_bytes(copy.read_bytes() + b"\nAn invented line the review never saw.\n")
        os.replace(fresh, copy)
    return change


def revocation_marker_only(node, monkeypatch, tmp_path):
    """`marker`, mid-revocation: the store's own revocation publishes its pending marker, then its commit fails. The
    enrollment is still active in the canonical rows and nothing committed; only the marker says a revocation is
    in flight (`state: pending`), and a node that reads it refuses the native proof while it stands. A live
    revocation holds the gate from marker to commit, so a confirm cannot see that moment; a failed commit leaves it."""
    enrollment = enrollment_id(node)

    def change():
        conn = sqlite3.connect(node.corpus.path, timeout=0.5, factory=_CommitNeverLands)
        try:
            with owner(), pytest.raises(sqlite3.OperationalError):
                service_for(node).revoke(conn, enrollment_id=enrollment, source_id="imessage")
        finally:
            conn.close()
    return change


def directory_opened(node, monkeypatch, tmp_path):
    """`directories`: the node's `permissions-v2` directory made readable by its group. The provenance pass refuses
    a non-private directory there; no file's own state moves."""
    base = node.corpus.path.parent / "permissions-v2"
    assert base.stat().st_mode & 0o077 == 0
    return lambda: base.chmod(0o750)


PARTS = {
    "families": kinds_switched_on,
    "rubric": rubric_replaced,
    "marker": revocation_marker_only,
    "directories": directory_opened,
}


@pytest.mark.parametrize("part", sorted(PARTS))
def test_each_change_moves_its_own_part_of_the_token_and_no_other(node, monkeypatch, tmp_path, part):
    """Non-vacuity for the races below: each change is seen by one part of the decision's token, and only by it, so a
    token without that part would confirm a decision it lands under."""
    wal(node)
    change = PARTS[part](node, monkeypatch, tmp_path)
    with node.search.verification() as verified:
        deciding = _Decision(node.search, GRANT, verified, None)
        before = deciding._token()
        elsewhere(change)
        after = deciding._token()
    assert before is not None and after is not None and set(before) == set(after)
    assert {name for name in before if before[name] != after[name]} == {part}


# --- every write at the other two doors, and each part at all three -----------------------------------------------------

#: The writes of the R1 file's table, finding 1's context change, and each part of the token alone.
OTHER_WRITES = {"protected_joins_the_conversations": protected_joins_the_conversations}
CHANGES = sorted(WRITES) + sorted(OTHER_WRITES) + sorted(PARTS)


def change_for(node, monkeypatch, tmp_path, name):
    """The change, ready to run (a part's change sets up what it changes first)."""
    if name in PARTS:
        return PARTS[name](node, monkeypatch, tmp_path)
    write = WRITES.get(name) or OTHER_WRITES[name]
    return lambda: write(node)


#: How a request made after each change ends under the gate (the switch off), per door: "released", or the code it is
#: refused with inside the node. The search door's writes are the R1 file's `AFTER`; the batch door ends each as the
#: search door does. The answers retrieval compares the admitted authority with the protection revision in it
#: (`same_answer_authority`), so a write that moves the protection clock is `authority_stale` there. A replaced rubric
#: is refused by the full check's own read of it (`RubricMismatch`, at every door). The races below must end the same.
AT_THE_GATE = {"search": {**AFTER, "protected_joins_the_conversations": "search_index_stale",
                          "families": "search_index_stale", "rubric": "RubricMismatch",
                          "marker": "search_index_stale", "directories": "search_index_stale"}}
AT_THE_GATE["batch"] = dict(AT_THE_GATE["search"])
AT_THE_GATE["answers"] = {**AT_THE_GATE["search"], "off_limits_mark": "authority_stale",
                          "owner_only_mark": "authority_stale", "native_proof_revoked": "authority_stale"}
#: Changes a decision's candidate walk reads for itself (a candidate's machine review is current only under the rubric
#: revision, read from the file: `automatic_message_review.is_current`): landed
#: after the snapshot, the walk refuses them on the first attempt, so only a change before the confirm tests the token.
READ_BY_THE_WALK = {"rubric"}
#: The door and change pairs the R1 file does not already cover at the search door.
UNCOVERED = [(door, name) for door in DOORS for name in CHANGES if door != "search" or name not in WRITES]


@pytest.mark.parametrize(("door", "change"), UNCOVERED)
def test_a_request_after_each_change_ends_like_this_under_the_gate(node, monkeypatch, tmp_path, door, change):
    wal(node)
    act = change_for(node, monkeypatch, tmp_path, change)
    quiet = ask(node, door, "quiet")
    assert quiet[0] == "released" and released_something(door, quiet[1])
    seen = reasons(monkeypatch)
    fired = before_the_decision(monkeypatch, door, act)
    outcome = ask(node, door, "after")
    assert fired == [True]
    assert ended(door, outcome, seen) == AT_THE_GATE[door][change]
    if outcome[0] == "released":
        assert outcome[1] == quiet[1]


@pytest.mark.parametrize("point", ["after_the_snapshot", "before_the_confirm"])
@pytest.mark.parametrize(("door", "change"), UNCOVERED)
def test_a_change_that_lands_during_a_decision_is_never_decided_past(node, monkeypatch, tmp_path, door, change, point):
    wal(node)
    act = change_for(node, monkeypatch, tmp_path, change)
    quiet = ask(node, door, "quiet")
    on(monkeypatch)
    events, seen = watched(node, monkeypatch), reasons(monkeypatch)
    fired = landing(monkeypatch, point, act)
    before = written(node)
    outcome = ask(node, door, "raced")
    assert fired == [1]
    if point == "after_the_snapshot" and change in READ_BY_THE_WALK:
        # The walk reads it itself, from the file and not from the snapshot: the first attempt is refused by that
        # read, for the reason the gate gives, before any confirm.
        assert events == []
    else:
        # The first attempt confirmed nothing and wrote nothing; whatever came next came from a fresh snapshot.
        assert events[0] == "moved" and events.count("moved") == 1
    expected = AT_THE_GATE[door][change]
    assert ended(door, outcome, seen) == expected
    if expected == "released":
        assert outcome[1] == quiet[1]
        assert events == ["moved", "confirmed"] + (["checkpoint"] if door != "answers" else [])
        assert written(node) - before == {"search": 2, "batch": 6, "answers": 0}[door]
    else:
        assert "checkpoint" not in events
        nothing_kept(node, door, "raced", before)
