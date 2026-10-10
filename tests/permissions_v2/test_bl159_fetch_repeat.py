"""N153b: a delivered body stays fetchable by the same authority for the rest of its keep window.

The live finding (10 Oct 2026 00:01Z, the owner node on the 1.5.3 candidate): the node delivered a `no_answer` body
(fetch log line `outcome=no_answer`, 0.1 s); one second later a second fetch for the same answer id arrived (the
control plane's relay re-sending, or the app retrying after a lost reply) and was refused `answer_unknown`, because the
body had been popped on delivery. The person who asked saw "refused" and never the body: a one-shot fetch loses the
body to any duplicate dispatch.

Now a fetch of an ENDED body is idempotent inside the body's keep window (`UNFETCHED_SECONDS` from the job's end):

- the same signed authority (grant, assignment, actor and client) fetching the same answer id again gets the same
  body, byte for byte, after the same re-checks as the first fetch (the cited-evidence walk, the boundary screen, the
  authority);
- a restriction that lands between two fetches withholds the second (the re-check is never skipped on a repeat);
- any other authority is refused, and the body stays with its asker;
- past the keep window the job is gone (`answer_unknown`), as for a body never fetched;
- a repeat spends no question and moves no count the owner's "This week" line or the recipient can see: the ledger's
  one trace of it is one more `answer_fetch` request row, which nothing reads.

Invented evidence only; no model, network or personal data.
"""
from __future__ import annotations

import time

import pytest

from tests.permissions_v2.message_search_harness import recipient
from tests.permissions_v2.test_answer_release import _ask, _service
from tests.permissions_v2.test_bl159_answer_delivery import (  # noqa: F401 -- fixtures
    ANSWER, QUESTION, RESTRICTIONS, Refusals, _restrict, answers_node, fetched, first_day, journal_database,
    rebuild_any, service, written,
)
from topos.permissions_v2 import answer_release, share_week
from topos.permissions_v2.answer_protocol import FETCH
from topos.permissions_v2.canonical import PolicyError, canonical_bytes

pytestmark = pytest.mark.public

NOTHING_CITED = "Nothing here cites anything."


def ended(service, answer_id):
    """Wait for the job to end, whatever its outcome (`written` insists on an answered body)."""
    for _ in range(500):
        job = service._jobs.get(answer_id)
        if job is not None and job.state == "ended":
            return job
        time.sleep(.01)
    raise AssertionError("the answer worker did not finish")


def questions_spent(node):
    with node.ledger._transaction() as db:
        return db.execute("SELECT COALESCE(SUM(questions), 0) FROM p2a_question_days").fetchone()[0]


def fetch_rows(node):
    with node.ledger._transaction() as db:
        return db.execute("SELECT COUNT(*) FROM p2a_requests WHERE status='answer_fetch'").fetchone()[0]


def this_week(node):
    """The owner's "This week" counts for the share, from the receipts (`share_week`)."""
    now = node.now[0]
    return share_week.week(node.ledger.path, ["grant-search"], since=now - 7 * 86_400, until=now + 86_400)


# --- the same authority, inside the window: the same body again ------------------------------------------------------

@pytest.mark.parametrize("outcome", ["answered", "no_answer"])
def test_a_second_fetch_inside_the_keep_window_delivers_the_same_body(answers_node, monkeypatch, outcome):
    """The live case is the `no_answer` body; an answered one is kept the same way. Three fetches, all equal."""
    node = answers_node
    svc = _service(node, ANSWER if outcome == "answered" else NOTHING_CITED)
    try:
        refusals = Refusals(node, monkeypatch)
        answer_id = _ask(node, svc, QUESTION)
        assert ended(svc, answer_id).body.outcome == outcome
        spent, before = questions_spent(node), this_week(node)
        first, refused = fetched(node, svc, answer_id, refusals)
        assert refused is None and first["outcome"] == outcome
        assert answer_id in svc._jobs                                 # kept: not popped on delivery
        second, refused = fetched(node, svc, answer_id, refusals)
        assert refused is None, f"the repeat fetch was refused ({refused})"
        assert canonical_bytes(second) == canonical_bytes(first)      # byte for byte
        third, refused = fetched(node, svc, answer_id, refusals)
        assert refused is None and canonical_bytes(third) == canonical_bytes(first)
        # Admission counts. A fetch is admitted with `charge=False`: no question spent, no receipt written; the owner's
        # "This week" line reads receipts only. The repeat's one trace is a request row that nothing reads.
        assert questions_spent(node) == spent == 1
        assert this_week(node) == before
        assert fetch_rows(node) == 3
    finally:
        svc.close()


def test_a_fetch_past_the_keep_window_is_answer_unknown_and_one_just_inside_it_still_delivers(answers_node, service,
                                                                                             monkeypatch):
    node = answers_node
    refusals = Refusals(node, monkeypatch)
    answer_id = written(node, service)
    first, refused = fetched(node, service, answer_id, refusals)
    assert refused is None and first["outcome"] == "answered"
    ended_at = service._jobs[answer_id].ended_at
    node.now[0] = ended_at + answer_release.UNFETCHED_SECONDS - 1
    again, refused = fetched(node, service, answer_id, refusals)
    assert refused is None and canonical_bytes(again) == canonical_bytes(first)
    node.now[0] = ended_at + answer_release.UNFETCHED_SECONDS
    body, refused = fetched(node, service, answer_id, refusals)
    assert body is None and refused == "answer_unknown"
    assert answer_id not in service._jobs                             # the reaper's pop: the only exit for a delivered body


# --- another authority: refused, and the body stays with its asker --------------------------------------------------

def test_a_fetch_by_another_authority_is_refused_and_the_body_stays_with_its_asker(answers_node, service, monkeypatch):
    node = answers_node
    refusals = Refusals(node, monkeypatch)
    answer_id = written(node, service)
    first, refused = fetched(node, service, answer_id, refusals)
    assert refused is None and first["outcome"] == "answered"
    payload = {"answer_id": answer_id}
    for actor, client in (("actor-2", "client-2"), ("actor-1", "client-3")):
        signed = node._envelope("grant-search", FETCH, payload, node.next_id("fetch"))
        with recipient(actor, client), pytest.raises(PolicyError) as exc:
            service.fetch(envelope=signed.model_dump(), payload=payload, request_id=signed.request_id)
        # The ledger's envelope binding refuses before the job is looked up (`request_binding`; the uniform 403 on the
        # wire); a mismatch past it is `answer_unknown` -> `permission_denied`. Nothing leaves either way.
        assert exc.value.code in ("permission_denied", "request_binding")
        assert service._jobs[answer_id].body is not None              # kept for the asker: not released, not dropped
    again, refused = fetched(node, service, answer_id, refusals)
    assert refused is None and canonical_bytes(again) == canonical_bytes(first)


# --- the re-check runs on every fetch, the repeat included ----------------------------------------------------------

@pytest.mark.parametrize("restriction", RESTRICTIONS)
def test_a_restriction_between_two_fetches_withholds_the_second(answers_node, service, monkeypatch, restriction):
    """The first fetch delivered. An item the answer rests on is then no longer released as it was: the repeat runs the
    same re-check and withholds the body (at once, or `pending` while the index is rebuilt and then finally)."""
    node = answers_node
    refusals = Refusals(node, monkeypatch)
    answer_id = written(node, service)
    first, refused = fetched(node, service, answer_id, refusals)
    assert refused is None and first["outcome"] == "answered"
    _restrict(node, restriction)
    body, refused = fetched(node, service, answer_id, refusals)
    assert refused is not None or body.get("state") == "pending", f"delivered again after {restriction}"
    rebuild_any(node)
    body, refused = fetched(node, service, answer_id, refusals)
    assert body is None and refused is not None, f"delivered again after {restriction} and a rebuild"
    assert answer_id not in service._jobs                             # final: the body is gone
    body, refused = fetched(node, service, answer_id, refusals)
    assert body is None and refused == "answer_unknown"


def test_an_authority_change_between_two_fetches_withholds_the_second_and_drops(answers_node, service, monkeypatch):
    node = answers_node
    refusals = Refusals(node, monkeypatch)
    answer_id = written(node, service)
    first, refused = fetched(node, service, answer_id, refusals)
    assert refused is None and first["outcome"] == "answered"
    node.activate(node.search_raw, generation=2)
    body, refused = fetched(node, service, answer_id, refusals)
    assert body is None and refused == "authority_stale"
    assert answer_id not in service._jobs


def test_a_word_of_the_body_protected_between_two_fetches_withholds_the_second(answers_node, service, monkeypatch):
    """The boundary screen (review R-N153 F1) runs on the repeat too: a word of the body protected after the first
    delivery withholds the second, finally."""
    node = answers_node
    refusals = Refusals(node, monkeypatch)
    answer_id = written(node, service)
    first, refused = fetched(node, service, answer_id, refusals)
    assert refused is None and "owner" in first["answer"]
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
