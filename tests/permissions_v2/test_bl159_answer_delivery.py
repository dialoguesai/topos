"""BL-159: an answer the node wrote must reach the person who asked, unless what it rests on is no longer theirs.

The fetch of an answered body re-decides its evidence before the body leaves. These tests compute an answer through
`AnswerService` (stand-in generation, no model), change the node's state BETWEEN the answer being written and the
recipient's fetch, then fetch:

- new permitted evidence that only moves the ranking: the body is delivered;
- a review of an unrelated item: the body is delivered once the share is served again;
- while the share's index is not served (dropped by a sweep, being rebuilt), the fetch says `pending` and keeps the
  job: nothing is released, and a later fetch decides again;
- a restriction on an item the answer used (Off-limits, owner-only, a new assessment, an owner correction, an opt-out,
  an edit, the window passing it, the share's authority moving): the body is never delivered; once decided the
  refusal is final and the body is dropped.

All evidence is invented. No local model, personal database or network.
"""
from __future__ import annotations

import sqlite3
import sys
import time
from types import SimpleNamespace

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, owner
from tests.permissions_v2.test_answer_release import _ask, _fetch, _service
from tests.permissions_v2.test_journal_family import (
    node as journal_database, _entry, _journal_policy, _labels, _publish, AFTER_ITS_DAY, OWNER,
)
from topos.permissions_v2.automatic_message_review import prepare, publish
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.search_index import index_path

pytestmark = pytest.mark.public

TEXT = "I am working on a synthetic task at work."
NEW = "I am working on a second synthetic draft at work."
UNRELATED = "Walked the long way home along the river."
QUESTION = "What is the owner working on?"
ANSWER = "The owner is working on a synthetic task [1]."


@pytest.fixture
def first_day():
    return "2026-09-10T08:30:00"


@pytest.fixture
def answers_node(journal_database, tmp_path, monkeypatch, first_day):
    _entry(journal_database, "e1", TEXT, entry_at=first_day)
    _entry(journal_database, "e2", NEW)
    _entry(journal_database, "e3", UNRELATED)
    resolver, reviews = _publish(journal_database, "e1", domains=["work"])
    monkeypatch.setattr(mc, "NOW", AFTER_ITS_DAY)
    raw = _journal_policy(kinds=("journal_entry",))
    raw["search"]["answers"] = "only"
    node = Node(SimpleNamespace(resolver=resolver, reviews=reviews, path=resolver.path), tmp_path / "search",
                model=None, search_raw=raw, now=AFTER_ITS_DAY)
    assert node.rebuild() == {"grant-search": "ready"}
    return node


def assess(node, entry_id, **labels):
    resolver, reviews = node.corpus.resolver, node.corpus.reviews
    identity = resolver._identity("journal_entries", entry_id, "time_log")
    with owner():
        prepared = prepare(resolver, reviews, identity)
        return publish(resolver, reviews, prepared, _labels(prepared, **{"domains": ["work"], **labels}),
                       now=node.now[0])


def members(node):
    return node.index._open(index_path(node.index.root, "grant-search"))["count"]


def rebuild(node):
    with owner():
        assert node.index.rebuild("grant-search", now=node.now[0])["state"] == "ready"


class Refusals:
    """The code each refused fetch was refused with inside the node (the wire only ever says permission_denied)."""

    def __init__(self, node, monkeypatch):
        self.codes = []
        original = node.protocol.ledger.refuse

        def refuse(*args, **kwargs):
            exc = sys.exc_info()[1]
            self.codes.append(getattr(exc, "code", type(exc).__name__ if exc else None))
            return original(*args, **kwargs)

        monkeypatch.setattr(node.protocol.ledger, "refuse", refuse)


def rebuild_any(node):
    with owner():
        return node.index.rebuild("grant-search", now=node.now[0])["state"]


def written(node, service):
    """Ask, and wait for the job to END without fetching it (a fetch of an ended job hands the body out once)."""
    answer_id = _ask(node, service, QUESTION)
    for _ in range(500):
        job = service._jobs.get(answer_id)
        if job is not None and job.state == "ended":
            assert job.body is not None and job.body.outcome == "answered", "the stand-in answer was not kept"
            return answer_id
        time.sleep(.01)
    raise AssertionError("the answer worker did not finish")


def fetched(node, service, answer_id, refusals):
    try:
        return _fetch(node, service, answer_id), None
    except PolicyError as exc:
        assert exc.code == "permission_denied"            # the wire's one refusal, whatever refused inside
        return None, refusals.codes[-1]


@pytest.fixture
def service(answers_node):
    service = _service(answers_node, ANSWER)
    yield service
    service.close()


# --- delivered -------------------------------------------------------------------------------------------------------

def test_new_permitted_evidence_between_write_and_fetch_still_delivers(answers_node, service, monkeypatch):
    """The live cause (1.5.2): a new item ranks into the question's results after the answer was written. The
    re-check ranked the question again, the output digest moved, and the body was refused as `authority_moved`."""
    node = answers_node
    refusals = Refusals(node, monkeypatch)
    answer_id = written(node, service)
    assert members(node) == 1
    assess(node, "e2")                       # a new item becomes shareable ...
    rebuild(node)                            # ... and the refresh publishes it: the index gains a member
    assert members(node) == 2
    _authority, _policy, now, _decision = node.search.retrieve_for_answer(
        grant_id="grant-search", question=QUESTION, admitted_authority=service._jobs[answer_id].admitted_authority)
    assert len(now.records) == 2             # the same question now ranks two items: the ranking moved
    body, refused = fetched(node, service, answer_id, refusals)
    assert refused is None, f"the written answer was refused at fetch ({refused})"
    assert body["outcome"] == "answered"
    assert answer_id not in service._jobs     # handed out once


def test_an_unrelated_review_between_write_and_fetch_still_delivers_once_served(answers_node, service, monkeypatch):
    node = answers_node
    refusals = Refusals(node, monkeypatch)
    answer_id = written(node, service)
    assess(node, "e3", domains=["hobbies"], sensitivity="special")   # reviewed, and never shareable under this rule
    rebuild(node)
    assert members(node) == 1
    body, refused = fetched(node, service, answer_id, refusals)
    assert refused is None, f"the written answer was refused at fetch ({refused})"
    assert body["outcome"] == "answered"


def unguard(node):
    """The index as 1.5.2 built it: no review guard in its basis, so any review anywhere makes it stale."""
    import json
    with sqlite3.connect(index_path(node.index.root, "grant-search")) as db:
        basis = json.loads(db.execute("SELECT basis_json FROM meta").fetchone()[0])
        basis.pop("review_guard_version", None)
        basis.pop("opt_out_revision", None)
        db.execute("UPDATE meta SET basis_json=?", (json.dumps(basis),))


@pytest.mark.parametrize("change", ["dropped_by_a_sweep", "unguarded_index_and_a_review"])
def test_while_the_index_is_not_served_the_fetch_waits_and_keeps_the_job(answers_node, service, monkeypatch, change):
    """The share's index is not served for a while: a sweep dropped it, or (an index built before BL-157) a review
    anywhere made its basis stale. That decides nothing about the items the answer rests on: the fetch says
    `pending`, the job is kept, and the fetch after the rebuild delivers. (1.5.2 refused it, and the app stops
    polling on a refusal.)"""
    node = answers_node
    refusals = Refusals(node, monkeypatch)
    answer_id = written(node, service)
    if change == "dropped_by_a_sweep":
        index_path(node.index.root, "grant-search").unlink()
    else:
        unguard(node)
        assess(node, "e3", domains=["hobbies"], sensitivity="special")
    body, refused = fetched(node, service, answer_id, refusals)
    assert refused is None, f"refused while the index was not served ({refused})"
    assert body == {"version": "topos-answer/v1", "state": "pending", "answer_id": answer_id}
    assert service._jobs[answer_id].body is not None             # kept, with its body, for a later fetch
    body, refused = fetched(node, service, answer_id, refusals)  # still not served: still pending, still kept
    assert refused is None and body["state"] == "pending" and answer_id in service._jobs
    rebuild(node)
    body, refused = fetched(node, service, answer_id, refusals)
    assert refused is None and body["outcome"] == "answered"
    assert answer_id not in service._jobs


def test_new_evidence_during_generation_does_not_cancel_the_answer(answers_node, service):
    """The same, inside the job: the re-check after the model walks the items the model saw, not a new ranking."""
    node = answers_node

    async def generate(_prompt, *, deadline):
        assess(node, "e2")
        rebuild(node)
        return ANSWER

    service.generate = generate
    answer_id = _ask(node, service, QUESTION)
    body = None
    for _ in range(500):
        job = service._jobs.get(answer_id)
        if job is not None and job.state == "ended":
            body = job.body
            break
        time.sleep(.01)
    assert members(node) == 2
    assert body is not None and body.outcome == "answered"


def test_the_fetch_of_a_written_answer_neither_ranks_nor_embeds(answers_node, service, monkeypatch):
    """The fetch walks the recorded items only: no query embedding, no ranking (it is cheap, and new items are never
    read). The index is given vectors and a model here, so the write itself does embed and rank (not vacuous)."""
    from topos.permissions_v2 import search_release
    from topos.permissions_v2.search_index import LoadedIndex
    from tests.permissions_v2.message_search_harness import fake_embedder
    node = answers_node
    calls = {"embed": 0, "rank": 0}
    load = node.index.load

    def with_vectors(grant_id, authority):
        loaded = load(grant_id, authority)
        return LoadedIndex(loaded.basis, "fake-model", 32, loaded.members,
                           {member.opaque_id: [[1.0] + [0.0] * 31] for member in loaded.members})

    def embedder(query, model):
        calls["embed"] += 1
        return fake_embedder(query, model)

    rank = search_release.rank

    def ranked(*args, **kwargs):
        calls["rank"] += 1
        return rank(*args, **kwargs)

    monkeypatch.setattr(node.index, "load", with_vectors)
    monkeypatch.setattr(node.search, "embedder", embedder)
    monkeypatch.setattr(search_release, "rank", ranked)
    refusals = Refusals(node, monkeypatch)
    answer_id = written(node, service)
    assert calls["embed"] >= 1 and calls["rank"] >= 1      # the write ranked the question
    calls.update(embed=0, rank=0)
    body, refused = fetched(node, service, answer_id, refusals)
    assert refused is None and body["outcome"] == "answered"
    assert calls == {"embed": 0, "rank": 0}


# --- withheld -------------------------------------------------------------------------------------------------------

def _restrict(node, restriction):
    resolver, reviews = node.corpus.resolver, node.corpus.reviews
    identity = resolver._identity("journal_entries", "e1", "time_log")
    if restriction == "assessment":
        assess(node, "e1", sensitivity="special")
    elif restriction == "correction":
        from topos.permissions_v2.message_evidence import record_message_review
        with owner():
            prepared = prepare(resolver, reviews, identity)
            record_message_review(resolver, reviews, review_id="owner-correction",
                expected_snapshot=prepared["snapshot"].model_dump(),
                classification=_labels(prepared, domains=["work"], sensitivity="special").model_dump(),
                expected_current_review_revision=None, reviewed_at=node.now[0])
    elif restriction == "opt_out":
        from topos.permissions_v2.message_evidence import message_key
        with owner():
            reviews.opt_out(message_key(identity), now=node.now[0])
    elif restriction == "off_limits":
        from topos.storage.db.migrations.entity_blackhole_v1 import apply_entity_blackhole_v1_up
        with sqlite3.connect(node.corpus.path) as conn:
            apply_entity_blackhole_v1_up(conn)
            conn.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, normalized_name, canonical_name, "
                         "aliases_json, created_at) VALUES ('b-1','','synthetic task','Synthetic Task','[]','t')")
    else:
        with sqlite3.connect(resolver.path) as db:
            if restriction == "content":
                db.execute("UPDATE journal_entries SET content='I am working on a changed private matter at work.' "
                           "WHERE entry_id='e1'")
            else:
                db.execute("INSERT INTO owner_only_records(canonical_table,record_id,created_at,updated_at) "
                           "VALUES('journal_entries','e1','t','t')")


RESTRICTIONS = ["assessment", "correction", "opt_out", "off_limits", "content", "owner_only"]


@pytest.mark.parametrize("restriction", RESTRICTIONS)
def test_a_restriction_on_the_used_item_withholds_the_answer(answers_node, service, monkeypatch, restriction):
    """The item the answer was written from is no longer released as it was. Before the refresh catches up the fetch
    releases nothing (refused, or `pending` while the index is not served); once the index is rebuilt without the item
    the refusal is final, the body is dropped, and no later fetch can deliver it."""
    node = answers_node
    refusals = Refusals(node, monkeypatch)
    answer_id = written(node, service)
    _restrict(node, restriction)
    body, refused = fetched(node, service, answer_id, refusals)
    assert refused is not None or body.get("state") == "pending", f"delivered after {restriction}"
    rebuild_any(node)
    body, refused = fetched(node, service, answer_id, refusals)
    assert body is None and refused is not None, f"delivered after {restriction} and a rebuild"
    assert answer_id not in service._jobs                         # final: the body is gone
    body, refused = fetched(node, service, answer_id, refusals)
    assert body is None and refused == "answer_unknown"


def test_an_authority_change_withholds_and_drops(answers_node, service, monkeypatch):
    node = answers_node
    refusals = Refusals(node, monkeypatch)
    answer_id = written(node, service)
    node.activate(node.search_raw, generation=2)
    body, refused = fetched(node, service, answer_id, refusals)
    assert body is None and refused == "authority_stale"
    assert answer_id not in service._jobs


@pytest.mark.parametrize("first_day", ["2026-06-15T08:30:00"])     # one day inside the 90-day window at write time
def test_the_window_passing_the_used_item_withholds_and_drops(answers_node, service, monkeypatch, first_day):
    """Decided in the walk only (the index stays served): the item is outside the share's window at the fetch's
    clock, so the walk no longer releases it and the body is dropped as final. (A journal item's window is day-wide,
    so the clock moves two days; the unfetched-job expiry is lifted for that, or it would answer first.)"""
    from topos.permissions_v2 import answer_release
    monkeypatch.setattr(answer_release, "UNFETCHED_SECONDS", 10 * 86_400)
    node = answers_node
    refusals = Refusals(node, monkeypatch)
    answer_id = written(node, service)
    node.now[0] += 2 * 86_400
    body, refused = fetched(node, service, answer_id, refusals)
    assert body is None and refused == "authority_moved"
    assert answer_id not in service._jobs


def test_a_recorded_item_missing_from_the_index_refuses_as_evidence_moved(answers_node, service):
    """`retrieve_for_answer(record_ids=...)` walks only ids that are in the current index; any other id refuses."""
    node = answers_node
    answer_id = written(node, service)
    job = service._jobs[answer_id]
    for ids in [("r." + "0" * 64,), job.record_ids + job.record_ids, ()]:
        with pytest.raises(PolicyError) as refused:
            node.search.retrieve_for_answer(grant_id="grant-search", question=QUESTION,
                                            admitted_authority=job.admitted_authority, record_ids=ids)
        assert refused.value.code == "answer_evidence_moved"


def test_the_same_items_released_with_different_words_withhold(answers_node, service, monkeypatch):
    """The re-check compares the whole released output, not only which items: should the walk ever release one of
    the recorded items with other words than the answer was written from, the body is withheld and dropped."""
    node = answers_node
    refusals = Refusals(node, monkeypatch)
    answer_id = written(node, service)
    original = node.search.retrieve_for_answer

    def reworded(**kwargs):
        current, policy, output, decision = original(**kwargs)
        records = [record.model_copy(update={"content": record.content + " Changed."}) for record in output.records]
        return current, policy, output.model_copy(update={"records": records}), decision

    monkeypatch.setattr(node.search, "retrieve_for_answer", reworded)
    body, refused = fetched(node, service, answer_id, refusals)
    assert body is None and refused == "authority_moved"
    assert answer_id not in service._jobs


# --- the recorded set is every item the model saw (review R-N153, F2) ----------------------------------------------

@pytest.fixture
def two_item_node(answers_node):
    """A second shareable item the question also ranks: the model sees two items and the answer cites [1] only."""
    node = answers_node
    assess(node, "e2")
    rebuild(node)
    assert members(node) == 2
    return node


@pytest.mark.parametrize("restriction", ["assessment", "opt_out"])
def test_a_restriction_on_an_item_the_model_saw_but_did_not_cite_withholds(two_item_node, monkeypatch, restriction):
    node = two_item_node
    svc = _service(node, ANSWER)                       # cites [1] only
    try:
        refusals = Refusals(node, monkeypatch)
        answer_id = written(node, svc)
        job = svc._jobs[answer_id]
        assert len(job.record_ids) == 2, "the model did not see two items: the case is vacuous"
        if restriction == "assessment":
            assess(node, "e2", sensitivity="special")
        else:
            from topos.permissions_v2.message_evidence import message_key
            identity = node.corpus.resolver._identity("journal_entries", "e2", "time_log")
            with owner():
                node.corpus.reviews.opt_out(message_key(identity), now=node.now[0])
        body, refused = fetched(node, svc, answer_id, refusals)
        assert refused is not None or body.get("state") == "pending", f"delivered after {restriction} of the uncited item"
        rebuild_any(node)
        assert members(node) == 1
        body, refused = fetched(node, svc, answer_id, refusals)
        assert body is None and refused in ("answer_evidence_moved", "authority_moved"), refused
        assert answer_id not in svc._jobs
    finally:
        svc.close()


# --- the words, against the boundary at fetch (review R-N153, F1) ------------------------------------------------

def test_a_protected_word_in_the_body_but_in_no_item_withholds_at_fetch(answers_node, service, monkeypatch):
    """A boundary change that moves no protection clock (an alias, a contact, a mention link) leaves the items
    releasable; a word of the BODY that is now protected, in no cited item, still withholds it, finally."""
    node = answers_node
    refusals = Refusals(node, monkeypatch)
    answer_id = written(node, service)
    assert "owner" in service._jobs[answer_id].body.answer and "owner" not in TEXT.lower()
    resolver = node.search.resolver
    original = resolver.entity_boundary

    class Boundary:
        def __init__(self, inner):
            self.inner = inner

        def mentions_protected(self, text):
            return "owner" in text.lower() or self.inner.mentions_protected(text)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(resolver, "entity_boundary", lambda conn: Boundary(original(conn)))
    body, refused = fetched(node, service, answer_id, refusals)
    assert body is None and refused == "answer_protected"
    assert answer_id not in service._jobs


# --- the post-generation re-check while the index is not served (review R-N153, backlog 3) ------------------------

def test_the_index_dropped_during_generation_holds_the_answer_for_the_fetch(answers_node, service):
    """1.5.3 before this: the re-check after the model met a dropped index and the job ended `no_answer`
    (`search_index_stale`). Now the body is kept; the fetch re-checks the same items once the index is served."""
    node = answers_node
    refusals_codes = []

    async def generate(_prompt, *, deadline):
        index_path(node.index.root, "grant-search").unlink()       # a sweep's drop while the model writes
        return ANSWER

    service.generate = generate
    answer_id = _ask(node, service, QUESTION)
    for _ in range(500):
        job = service._jobs.get(answer_id)
        if job is not None and job.state == "ended":
            break
        time.sleep(.01)
    assert job.body.outcome == "answered"
    with node.ledger._transaction() as db:
        receipt = db.execute("SELECT receipt_json FROM p2a_receipts WHERE request_id=?", (job.request_id,)).fetchone()
    assert receipt is not None and '"reason":"answered"' in receipt[0]
    body = _fetch(node, service, answer_id)
    assert body == {"version": "topos-answer/v1", "state": "pending", "answer_id": answer_id}   # not served yet
    rebuild(node)
    assert _fetch(node, service, answer_id)["outcome"] == "answered"
    assert refusals_codes == []
