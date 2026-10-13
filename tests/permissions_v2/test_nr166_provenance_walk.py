"""NR166 R2, first item: one provenance pass per candidate walk, and one per sweep pass.

September's N3c gave a search's member loop one provenance service. Two places still built one for every
recovered iMessage row they loaded (a new resolver that enters the write gate, the store check twice, a re-hash of
the native snapshot file): the candidate walk of every release, and the daemon sweep. Pinned here:

- a walk builds one service where each candidate built one, and a sweep one per index where each member built one;
- what both doors release, and the receipts they write, are byte-identical to proving each candidate on its own,
  on the same node;
- a proof that is gone before the walk releases nothing either way;
- a proof that changes BETWEEN two candidates: the old walk released the candidates decided before the change;
  the pass's one check, after the last candidate, refuses, and the walk then releases none of them. Stricter,
  never wider: nothing is released that each candidate's own check would have refused;
- a walk whose pass refuses decides again from nothing: no decision made under the refused pass is reused;
- the sweep keeps a current index and drops one whose proof is gone, before the sweep or between two members.

Invented evidence only (the N3c suite's p2c-v3 node, Off-limits active so members carry dependencies).
"""
from __future__ import annotations

import json

import pytest

from tests.permissions_v2.message_search_harness import owner, recipient
from tests.permissions_v2.test_message_search_batch import comparable, ledger_rows
from tests.permissions_v2.test_search_provenance_pass import (  # noqa: F401 -- `node` is the fixture
    BEFORE, BETWEEN, MEMBERS, after_first_member, node, recorded_passes, replace_snapshot, revoke,
)
from topos.permissions_v2.canonical import PolicyError, canonical_bytes
from topos.permissions_v2.ingest_provenance import IngestProvenanceService
from topos.permissions_v2.search_index import index_path
from topos.permissions_v2.search_release import MessageSearchRelease

pytestmark = pytest.mark.public
GRANT = "grant-search"


def services_built(monkeypatch) -> list:
    built = []
    original = IngestProvenanceService.__init__

    def init(self, *args, **kwargs):
        built.append(self)
        original(self, *args, **kwargs)
    monkeypatch.setattr(IngestProvenanceService, "__init__", init)
    return built


def each_candidate_by_its_own_check(monkeypatch) -> None:
    """The walk of before this change: no pass, so every recovered row is proven by a service of its own."""
    monkeypatch.setattr(MessageSearchRelease, "_walk", MessageSearchRelease._walk_candidates)


def queries(node, count=3):
    from tests.permissions_v2 import direct_search_twins as dst
    return [{"query": query, "k": 5} for query in list(dict.fromkeys(dst.queries(MEMBERS, 9, 20)))[:count]]


def search(node, payload, request_id):
    envelope = node._envelope(GRANT, "permissions.v2.search", payload, request_id)
    with recipient():
        _result, output = node.search.dispatch(envelope=envelope.model_dump(), payload=payload, request_id=request_id)
    return output


def batch(node, payloads, batch_id):
    envelopes = [node._envelope(GRANT, "permissions.v2.search", payload, f"{batch_id}:{number}")
                 for number, payload in enumerate(payloads)]
    items = [{"envelope": envelope.model_dump(), "payload": payload, "request_id": envelope.request_id}
             for envelope, payload in zip(envelopes, payloads)]
    with recipient():
        return [output for _result, output in node.search.dispatch_batch(items=items)]


def answer(node, question):
    with node.ledger._transaction() as conn:
        node.protocol._sync_protection(conn)
    with owner():
        authority = node.ledger.authority_snapshot(GRANT, now=node.now[0])
    with recipient():
        _current, _policy, output, decision = node.search.retrieve_for_answer(
            grant_id=GRANT, question=question, admitted_authority=authority)
    return canonical_bytes(output.model_dump()), canonical_bytes(decision.model_dump())


def everything(node, tag):
    """Both doors once: what was released, and each search's receipt and decision but for what names the request."""
    asked = queries(node)
    released = [search(node, asked[0], f"{tag}-single")] + batch(node, asked, f"{tag}-batch")
    answers = [answer(node, payload["query"]) for payload in asked]
    receipts = []
    for request_id in [f"{tag}-single"] + [f"{tag}-batch:{number}" for number in range(len(asked))]:
        status, (receipt, decision) = ledger_rows(node, request_id)
        receipts.append((status, comparable(receipt), decision))
    return [canonical_bytes(output) for output in released], answers, receipts


# --- one service per walk -----------------------------------------------------------------------------------------

def test_a_walk_builds_one_provenance_service_where_each_candidate_built_one(node, monkeypatch):
    built = services_built(monkeypatch)
    asked = queries(node)
    released = batch(node, asked, "one-pass")
    decided = len({record["record_id"] for output in released for record in output["records"]})
    assert decided >= 3, "the walks re-decided too few recovered messages to tell the two paths apart"
    # The gated recheck's member loop (N3c), then one per walk.
    assert len(built) == 1 + len(asked)
    with_passes = len(built)
    built.clear()
    each_candidate_by_its_own_check(monkeypatch)
    again = batch(node, asked, "each-own")
    assert again == released
    assert len(built) >= 1 + decided > with_passes   # a service for every candidate it re-decided

    built.clear()
    monkeypatch.undo()
    built = services_built(monkeypatch)
    answer(node, asked[0]["query"])
    assert len(built) == 2                           # the retrieval's member loop and its one walk


def test_both_doors_release_and_receipt_exactly_what_each_candidates_own_check_does(node, monkeypatch):
    with_passes = everything(node, "pass")
    each_candidate_by_its_own_check(monkeypatch)
    each_own = everything(node, "own")
    assert with_passes == each_own
    released, answers, receipts = with_passes
    assert all(json.loads(output)["records"] for output in released), "an empty release compares nothing"
    assert all(json.loads(output)["records"] for output, _decision in answers)
    assert [status for status, _receipt, _decision in receipts] == ["checkpointed"] * 4


# --- a proof that is gone, or goes during the walk ----------------------------------------------------------------

def changed_at_candidate(monkeypatch, change, number: int) -> list:
    """Run `change` once, just before the walk re-decides its candidate `number` (0: before the first)."""
    seen, fired = [], []
    original = MessageSearchRelease._accept

    def accept(self, *args, **kwargs):
        if len(seen) == number and not fired:
            fired.append(True)
            change()
        seen.append(True)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(MessageSearchRelease, "_accept", accept)
    return fired


@pytest.mark.parametrize("own_checks", [False, True], ids=["one_pass", "each_candidates_own_check"])
def test_a_snapshot_replaced_before_the_first_candidate_releases_nothing_either_way(node, monkeypatch, own_checks):
    """The member loop has proven the index; then the native snapshot file its proof pins is replaced. Every
    candidate's own check re-hashes the file and refuses. The pass re-hashes it once, after the last candidate, and
    refuses the walk; decided again by each candidate's own check, nothing is released."""
    payload = queries(node, 1)[0]
    assert search(node, payload, "before-the-change")["records"]
    if own_checks:
        each_candidate_by_its_own_check(monkeypatch)
    made = recorded_passes(monkeypatch)
    fired = changed_at_candidate(monkeypatch, lambda: replace_snapshot(node), 0)
    output = search(node, payload, "replaced-first")
    assert fired == [True] and output["records"] == []
    status, (receipt, decision) = ledger_rows(node, "replaced-first")
    assert status == "checkpointed" and decision["member_count"] == 0 and decision["verdict"] == "permit"
    if not own_checks:
        loop, walk = made
        assert loop.events[-1] == "finished" and walk.events.count("validate") >= 1 and walk.events[-1] == "refused"


def test_a_snapshot_replaced_between_two_candidates_releases_none_of_them(node, monkeypatch):
    """The one place the two walks differ. Each candidate's own check passed the candidates decided before the
    change and released them. The pass's check comes after the last candidate, sees the change, and the walk,
    decided again, releases none: fewer, never more."""
    payload = queries(node, 1)[0]
    before = search(node, payload, "before")["records"]
    assert len(before) >= 3
    fired = changed_at_candidate(monkeypatch, lambda: replace_snapshot(node), 2)
    made = recorded_passes(monkeypatch)
    output = search(node, payload, "replaced-between")
    assert fired == [True] and output["records"] == []
    assert made[1].events[-1] == "refused"


def test_what_each_candidates_own_check_released_before_such_a_change_is_the_only_difference(node, monkeypatch):
    payload = queries(node, 1)[0]
    before = search(node, payload, "before")["records"]
    each_candidate_by_its_own_check(monkeypatch)
    changed_at_candidate(monkeypatch, lambda: replace_snapshot(node), 2)
    output = search(node, payload, "replaced-between-own")
    assert [record["record_id"] for record in output["records"]] == [record["record_id"] for record in before[:2]]


def test_a_walk_whose_pass_refuses_reuses_no_decision_made_under_it(node, monkeypatch):
    """`decided` is shared by a batch's walks. A walk whose pass refuses must forget what it decided under that
    pass before it decides again; a decision kept would release a row no check had proven."""
    asked = queries(node)
    quiet = batch(node, asked, "quiet")
    seen = {"walks": 0}
    original = MessageSearchRelease._walk_candidates

    def walk(self, *args, **kwargs):
        seen["walks"] += 1
        if seen["walks"] == 2:   # after the first walk's pass finished, during the second walk's
            replace_snapshot(node)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(MessageSearchRelease, "_walk_candidates", walk)
    outputs = batch(node, asked, "second-walk")
    # The first walk's pass finished before the change: its release stands, as its candidates' own checks would
    # have let it. The second and third walks may reuse only what that first, finished pass decided.
    assert outputs[0] == quiet[0]
    first = {record["record_id"] for record in quiet[0]["records"]}
    for output in outputs[1:]:
        assert {record["record_id"] for record in output["records"]} <= first
    assert seen["walks"] == 5    # walk 1; walk 2 and its second run; walk 3 and its second run


# --- the sweep ------------------------------------------------------------------------------------------------------

def sweep(node) -> int:
    return node.index.sweep(now=node.now[0])


def test_the_sweep_builds_one_service_for_an_index_and_keeps_a_current_one(node, monkeypatch):
    path = index_path(node.index.root, GRANT)
    before = path.read_bytes()
    built = services_built(monkeypatch)
    made = recorded_passes(monkeypatch)
    assert sweep(node) == 0 and path.read_bytes() == before
    assert len(built) == 1                       # each member built one before
    [provenance] = made
    assert provenance.events.count("validate") >= MEMBERS - 1 and provenance.events[-1] == "finished"


@pytest.mark.parametrize("change", sorted(BEFORE))
def test_the_sweep_drops_an_index_whose_proof_is_gone(node, change):
    BEFORE[change](node)
    assert sweep(node) == 1 and not index_path(node.index.root, GRANT).exists()


@pytest.mark.parametrize("change", sorted(BETWEEN))
def test_the_sweep_drops_an_index_whose_proof_goes_between_two_members(node, monkeypatch, change):
    from tests.permissions_v2.test_search_provenance_pass import wal
    wal(node)   # only in WAL can a commit land while the sweep's read is open
    fired = after_first_member(monkeypatch, lambda: BETWEEN[change](node))
    assert sweep(node) == 1 and fired == [True] and not index_path(node.index.root, GRANT).exists()


def test_a_search_after_a_revocation_is_refused_as_before(node):
    payload = queries(node, 1)[0]
    assert search(node, payload, "live")["records"]
    revoke(node)
    with pytest.raises(PolicyError) as refused:
        search(node, payload, "revoked")
    assert refused.value.code == "permission_denied"
