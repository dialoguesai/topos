"""NR185: the pinned rubric replaced while a share answer is prepared ends the job exactly as a refusal does.

For a knowledge grant, every retrieval of `retrieve_for_answer` reads the pinned rubric file (the index load's
`check_own` and the gated recheck: `automatic_message_review.rubric_revision`). Replaced or unreadable, that read
raises `shadow_labeler_local.RubricMismatch`, an `Unresolved` and a `ValueError` but not a `PolicyError`. The answers
service handled only `PolicyError` there (found by lane NR184 in passing), so before NR185:

- the job's retrieval, and its re-check before the body: the job's catch-all recorded `model_error` in the owner's
  receipt, for a model that was never asked (or that answered fine);
- the fetch's re-check (BL-159): the body was neither dropped (a final refusal) nor held (not served now). It stayed,
  every fetch was refused, and the node's fetch line said `error`.

Now a rubric mismatch, and any other `Unresolved`, ends at every call site exactly as a final `PolicyError` with the
`Unresolved`'s own code (`labeler_rubric_mismatch`, a receipt word; the other labeler codes are `refused`). Pinned per
call site (the job's retrieval, its re-check, the fetch's re-check), with the write gate held and with NR166 R1's
switch on, against a `PolicyError` raised at the same call: the same body bytes, the same receipt but for its
reason, the same kept or dropped job, the same refusal on the wire. A real bug (any other exception) keeps its own
path: it is never taken for a refusal.

The search and batch doors had no such gap: every failure after verification is their one refusal with one deny
receipt (`except Exception` in `_dispatch` / `_dispatch_batch` and in both transports' send checks). Pinned below
against a `PolicyError` at the same point, at the decision and at the send check.

Invented evidence only. No local model, personal database or network; the rubric replaced is a copy in tmp.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from concurrent.futures import Future
from types import SimpleNamespace

import pytest

from tests.permissions_v2.message_search_harness import owner, recipient
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- fixture
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 -- fixture the legacy one needs
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.test_message_search_batch import (QUERIES, comparable, ledger_rows, node,  # noqa: F401
                                                            send_batch, send_single, spent_with_refusal)
from topos.permissions_v2 import answer_transport, search_timing, shadow_labeler_local
from topos.permissions_v2.answer_protocol import ASK, FETCH, verify_node_answer
from topos.permissions_v2.answer_release import NOT_SERVED_NOW, RECEIPT_REASONS, AnswerService, receipt_reason
from topos.permissions_v2.canonical import PolicyError, canonical_bytes
from topos.permissions_v2.search_index import SearchIndexService
from topos.permissions_v2.search_release import MessageSearchRelease
from topos.permissions_v2.shadow_labeler_local import ModelUnreviewed, RubricMismatch, Unreachable, Unresolved

pytestmark = pytest.mark.public

GRANT = "grant-search"
QUESTION = "Synthetic message"
ANSWER = "The owner is working on a synthetic task [1]."
LOGGER = "topos.permissions_v2.answer_release"
SWITCH = "TOPOS_PERMISSIONS_V2_DECIDE_OUTSIDE_GATE"
#: The receipt word NR185 adds: the `RubricMismatch`'s own code, carried as amendment 6 carries a retrieval's codes.
WORD = RubricMismatch.reason
#: The reference refusal: a final code (not "not served now"), in the receipt vocabulary.
REFERENCE = "search_index_integrity"
#: The one no-answer body, byte for byte (A2A-4 §6.3).
NO_ANSWER = b'{"outcome":"no_answer","version":"topos-answer/v1"}'
#: Each retrieval call site of one ask, by the order the service makes them: the job's retrieval, the job's re-check
#: before the body is kept, and the fetch's re-check before an answered body leaves (BL-159).
SITES = {"job_retrieval": 1, "job_recheck": 2, "fetch_recheck": 3}
GATES = ("gated", "outside_gate")


@pytest.fixture(autouse=True)
def switch_unset(monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)


@pytest.fixture
def rubric(monkeypatch, tmp_path):
    """The pinned rubric, as a reviewed copy this test may replace: never the file in the tree."""
    copy = tmp_path / "rubric" / shadow_labeler_local.RUBRIC_PATH.name
    copy.parent.mkdir()
    copy.write_bytes(shadow_labeler_local.RUBRIC_PATH.read_bytes())
    monkeypatch.setattr(shadow_labeler_local, "RUBRIC_PATH", copy)
    shadow_labeler_local.rubric()   # the copy is the reviewed bytes
    return copy


def replace(rubric):
    def act():
        rubric.write_bytes(rubric.read_bytes() + b"\nAn invented rule nobody reviewed.\n")
    return act


def raising(error):
    def act():
        raise error
    return act


class _Deferred:
    """An executor that holds the job until the test runs it (as `test_bl156_receipt_reasons`): no thread."""

    def __init__(self):
        self.calls = []

    def submit(self, fn, *args):
        future = Future()
        self.calls.append((fn, args, future))
        return future

    def shutdown(self, **_kwargs):
        pass

    def run(self):
        for fn, args, future in self.calls:
            fn(*args)
            future.set_result(None)
        self.calls.clear()


class Retrievals:
    """Every retrieval the answers service makes of the share, in order, and how each ended. At the armed call, `act`
    runs first (replaces the rubric, or raises the reference refusal in the retrieval's place)."""

    def __init__(self, node, monkeypatch):
        self.real = node.search.retrieve_for_answer
        self.ended, self.call, self.act = [], None, None
        monkeypatch.setattr(node.search, "retrieve_for_answer", self)

    def arm(self, call, act):
        self.ended, self.call, self.act = [], call, act

    def __call__(self, **kwargs):
        self.ended.append(None)
        try:
            if len(self.ended) == self.call:
                self.act()
            result = self.real(**kwargs)
        except BaseException as error:
            self.ended[-1] = type(error).__name__
            raise
        self.ended[-1] = "returned"
        return result


class Refusals:
    """The exception each refused fetch was refused with inside the node (the wire only ever says permission_denied)."""

    def __init__(self, node, monkeypatch):
        self.codes = []
        original = node.protocol.ledger.refuse

        def refuse(*args, **kwargs):
            error = sys.exc_info()[1]
            self.codes.append(getattr(error, "code", type(error).__name__ if error else None))
            return original(*args, **kwargs)
        monkeypatch.setattr(node.protocol.ledger, "refuse", refuse)


def ready(legacy, tmp_path, monkeypatch, gate):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        assert node.index.rebuild(GRANT, now=node.now[0])["state"] == "ready"
    if gate == "outside_gate":
        import sqlite3
        with sqlite3.connect(node.corpus.path) as conn:   # a snapshot can be read beside a commit only in WAL
            assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        monkeypatch.setenv(SWITCH, "true")
    return node


def service_for(node):
    async def generate(_prompt, *, deadline):
        return ANSWER
    runtime = SimpleNamespace(protocol=node.protocol, message_search=lambda: node.search)
    service = AnswerService(runtime, clock=lambda: node.now[0], generate=generate)
    service._enabled = lambda: True
    service._executor = _Deferred()
    return service


def ask(node, service):
    payload = {"question": QUESTION}
    signed = node._envelope(GRANT, ASK, payload, node.next_id("answer"))
    with recipient():
        _result, output = service.submit(envelope=signed.model_dump(), payload=payload, request_id=signed.request_id)
    return signed.request_id, output["answer_id"]


def fetch(node, service, answer_id):
    payload = {"answer_id": answer_id}
    signed = node._envelope(GRANT, FETCH, payload, node.next_id("fetch"))
    with recipient():
        result, output = service.fetch(envelope=signed.model_dump(), payload=payload, request_id=signed.request_id)
    verify_node_answer(result, trusted_keys={"node-key": node.node_key.public_key().public_bytes_raw()},
                       envelope=signed, output=output, mode="only", now=node.now[0])
    return output


def receipt(node, request_id):
    with node.ledger._transaction() as db:
        row = db.execute("SELECT receipt_json FROM p2a_receipts WHERE request_id=?", (request_id,)).fetchone()
    assert row is not None, "the job ended with no receipt filed"
    return json.loads(row[0])


def fetch_lines(caplog):
    found = []
    for record in caplog.records:
        message = record.getMessage()
        if record.name == LOGGER and message.startswith("permissions answer fetch: "):
            outcome, reason = (part.split("=", 1)[1] for part in message.split(": ", 1)[1].split(" ")[:2])
            found.append((outcome, reason))
    return found


def one_ask(node, service, retrievals, refusals, caplog, call, act):
    """Ask; run the job; fetch once. What the recipient, the owner's receipt, the job store and the log each got."""
    retrievals.arm(call, act)
    refusals.codes.clear()
    caplog.clear()
    request_id, answer_id = ask(node, service)
    service._executor.run()                  # the job ends here, before any fetch
    try:
        wire = canonical_bytes(fetch(node, service, answer_id))
    except PolicyError as refused:
        wire = refused.code
    filed = receipt(node, request_id)
    rest = {key: value for key, value in filed.items() if key not in {"request_id", "answer_id", "question_hash",
                                                                      "reason"}}
    job = service._jobs.get(answer_id)
    return {"wire": wire, "reason": filed["reason"], "kept": job is not None,
            "refused_with": list(refusals.codes), "lines": fetch_lines(caplog)}, rest, list(retrievals.ended)


def expected(site, code):
    """How a final refusal with `code` raised at the site ends (BL-156, BL-159)."""
    if site == "fetch_recheck":
        # Written and receipted as answered; the fetch's re-check refuses it, finally: the body is dropped.
        return {"wire": "permission_denied", "reason": "answered", "kept": False, "refused_with": [code],
                "lines": [("refused", code)]}
    return {"wire": NO_ANSWER, "reason": receipt_reason(code), "kept": True, "refused_with": [],
            "lines": [("no_answer", "-")]}


def reached(ended, call, raised):
    """Not vacuous: every retrieval before the site returned, and the site's own one raised `raised`."""
    return ended == ["returned"] * (call - 1) + [raised]


# --- the answers service: every retrieval call site -------------------------------------------------------------------

@pytest.mark.parametrize("gate", GATES)
@pytest.mark.parametrize("site", sorted(SITES))
def test_the_rubric_replaced_at_each_retrieval_ends_as_a_final_refusal_does(legacy, tmp_path, monkeypatch, caplog,
                                                                             rubric, site, gate):
    node = ready(legacy, tmp_path, monkeypatch, gate)
    service = service_for(node)
    retrievals, refusals = Retrievals(node, monkeypatch), Refusals(node, monkeypatch)
    caplog.set_level(logging.INFO, logger=LOGGER)
    call = SITES[site]
    try:
        reference, reference_rest, reference_ended = one_ask(node, service, retrievals, refusals, caplog, call,
                                                             raising(PolicyError(REFERENCE)))
        replaced, replaced_rest, replaced_ended = one_ask(node, service, retrievals, refusals, caplog, call,
                                                          replace(rubric))
    finally:
        service.close()
    assert reached(reference_ended, call, "PolicyError")
    assert reached(replaced_ended, call, "RubricMismatch"), "the rubric was not read where it was replaced"
    assert reference == expected(site, REFERENCE)
    assert replaced == expected(site, WORD)
    assert replaced_rest == reference_rest      # the same receipt but for its reason (and the ask's own ids)
    if site != "fetch_recheck":
        assert replaced["reason"] == WORD and replaced["wire"] == NO_ANSWER and replaced_rest["outcome"] == "no_answer"
        assert replaced_rest["records_used"] == (0 if site == "job_retrieval" else 1)


@pytest.mark.parametrize("site", sorted(SITES))
@pytest.mark.parametrize("error", [RubricMismatch, ModelUnreviewed, Unreachable, Unresolved])
def test_every_unresolved_from_a_retrieval_is_the_refusal_of_its_own_code(legacy, tmp_path, monkeypatch, caplog,
                                                                           site, error):
    """The whole `Unresolved` family, each raised in the retrieval's place: `labeler_rubric_mismatch` is a receipt
    word; the other labeler codes are recorded as `refused`, as any code outside the vocabulary is (amendment 6)."""
    node = ready(legacy, tmp_path, monkeypatch, "gated")
    service = service_for(node)
    retrievals, refusals = Retrievals(node, monkeypatch), Refusals(node, monkeypatch)
    caplog.set_level(logging.INFO, logger=LOGGER)
    call = SITES[site]
    try:
        seen, _rest, ended = one_ask(node, service, retrievals, refusals, caplog, call,
                                     raising(error("invented diagnostic text")))
    finally:
        service.close()
    assert reached(ended, call, error.__name__)
    assert seen == expected(site, error.reason)
    assert "invented diagnostic text" not in caplog.text


@pytest.mark.parametrize("site", sorted(SITES))
@pytest.mark.parametrize("error", [ValueError, TypeError, KeyError, RuntimeError])
def test_a_real_bug_in_a_retrieval_is_never_taken_for_a_refusal(legacy, tmp_path, monkeypatch, caplog, site, error):
    """Only the `Unresolved` family is a refusal. Any other exception keeps the path it had: the job's catch-all
    (`model_error`), and at the fetch an `error` line with the job kept, so the bug shows where it did before."""
    node = ready(legacy, tmp_path, monkeypatch, "gated")
    service = service_for(node)
    retrievals, refusals = Retrievals(node, monkeypatch), Refusals(node, monkeypatch)
    caplog.set_level(logging.INFO, logger=LOGGER)
    call = SITES[site]
    try:
        seen, _rest, ended = one_ask(node, service, retrievals, refusals, caplog, call, raising(error("an invented bug")))
    finally:
        service.close()
    assert reached(ended, call, error.__name__)
    if site == "fetch_recheck":
        assert seen == {"wire": "permission_denied", "reason": "answered", "kept": True,
                        "refused_with": [error.__name__], "lines": [("refused", "error")]}
    else:
        assert seen == {"wire": NO_ANSWER, "reason": "model_error", "kept": True, "refused_with": [],
                        "lines": [("no_answer", "-")]}


def test_the_word_is_a_final_receipt_word_and_the_family_has_no_other(rubric):
    assert WORD == "labeler_rubric_mismatch" and WORD in RECEIPT_REASONS
    # Final at every site: a body resting on a rubric the node cannot verify is dropped, never held. The job's re-check
    # reaches its outer refusal without its own `except PolicyError`, which only those two cases would change.
    for kind in (RubricMismatch, ModelUnreviewed, Unreachable, Unresolved):
        assert kind.reason not in NOT_SERVED_NOW and kind.reason != "answer_evidence_moved"
    for other in (ModelUnreviewed, Unreachable, Unresolved):
        assert other.reason not in RECEIPT_REASONS and receipt_reason(other.reason) == "refused"
    assert all(issubclass(kind, Unresolved) and not issubclass(kind, PolicyError)
               for kind in (RubricMismatch, ModelUnreviewed, Unreachable))
    replace(rubric)()
    with pytest.raises(RubricMismatch):
        shadow_labeler_local.rubric()


@pytest.mark.parametrize("site", sorted(SITES))
def test_the_recipients_frame_is_the_one_refusal_or_the_one_no_answer(legacy, tmp_path, monkeypatch, rubric, site):
    """Through the answers transport (DR-01, DR-10): the fetch after a job site's mismatch carries the one no-answer
    body; the fetch site's mismatch is the one refusal frame, byte for byte that of a request of unknown type."""
    node = ready(legacy, tmp_path, monkeypatch, "gated")
    service = service_for(node)
    retrievals = Retrievals(node, monkeypatch)
    monkeypatch.setattr(answer_transport, "verify_relay_stamp", lambda _message: SimpleNamespace(
        cls="third_party", channel="cp_relay", acting_user="actor-1", client_id="client-2"))
    monkeypatch.setattr(answer_transport, "get_runtime", lambda: SimpleNamespace(answers=lambda: service))

    class Socket:
        def __init__(self):
            self.frames = []

        async def send(self, frame):
            self.frames.append(frame)

    def relayed(kind, request_id, envelope, intent):
        socket = Socket()
        asyncio.run(answer_transport.dispatch_answer(socket, {"id": request_id, "type": kind,
                                                              "payload": {"envelope": envelope, "intent": intent}}))
        [frame] = socket.frames
        return frame

    call = SITES[site]
    retrievals.arm(call, replace(rubric))
    try:
        intent = {"question": QUESTION}
        signed = node._envelope(GRANT, ASK, intent, node.next_id("answer"))
        asked = json.loads(relayed(answer_transport.SUBMIT_TYPE, signed.request_id, signed.model_dump(), intent))
        answer_id = asked["payload"]["output"]["answer_id"]
        service._executor.run()
        intent = {"answer_id": answer_id}
        signed = node._envelope(GRANT, FETCH, intent, node.next_id("fetch"))
        frame = relayed(answer_transport.FETCH_TYPE, signed.request_id, signed.model_dump(), intent)
    finally:
        service.close()
    assert reached(retrievals.ended, call, "RubricMismatch")
    if site == "fetch_recheck":
        unknown = Socket()
        asyncio.run(answer_transport.dispatch_answer(unknown, {"id": signed.request_id, "type": "unknown"}))
        assert frame == unknown.frames[0].replace('"type":"unknown"', f'"type":"{answer_transport.FETCH_TYPE}"')
        assert json.loads(frame) == {"id": signed.request_id, "type": answer_transport.FETCH_TYPE, "status": "error",
                                     "code": 403, "error": "permission_denied"}
    else:
        decoded = json.loads(frame)
        assert decoded["status"] == "ok"
        assert canonical_bytes(decoded["payload"]["output"]) == NO_ANSWER


# --- the search and batch doors: no gap, pinned -----------------------------------------------------------------------

def door_reasons(monkeypatch) -> list:
    """What refused inside: the exception each spent envelope was refused with, and the one each transport caught."""
    seen = []
    original = MessageSearchRelease._spend

    def spend(self, admission, grant_id):
        error = sys.exc_info()[1]
        seen.append(("spent", type(error).__name__))
        return original(self, admission, grant_id)

    def refusal(door, error, timing=None):
        seen.append((door, type(error).__name__))
    monkeypatch.setattr(MessageSearchRelease, "_spend", spend)
    monkeypatch.setattr(search_timing, "refusal", refusal)
    return seen


def armed_at(node, monkeypatch, door, point, act):
    """`act` runs once: as the door's decision begins, or once its checkpoint is written and before its send check."""
    fired = []
    if point == "decision":
        name = "_decide" if door == "search" else "_decide_batch"
        original = getattr(MessageSearchRelease, name)

        def decide(self, *args, **kwargs):
            if not fired:
                fired.append(point)
                act()
            return original(self, *args, **kwargs)
        monkeypatch.setattr(MessageSearchRelease, name, decide)
        return fired
    name = "dispatch" if door == "search" else "dispatch_batch"
    original = getattr(MessageSearchRelease, name).__get__(node.search)

    def dispatch(**kwargs):
        result = original(**kwargs)
        fired.append(point)
        act()
        return result
    monkeypatch.setattr(node.search, name, dispatch)
    return fired


def at_the_send_check(error):
    """The reference at the send check: its first read (the token, under the gate) raises `error`, once."""
    def act():
        original = SearchIndexService.send_token

        def send_token(self, *args, **kwargs):
            SearchIndexService.send_token = original
            raise error
        SearchIndexService.send_token = send_token
    return act


async def door_request(node, monkeypatch, door, tag):
    if door == "search":
        frame = await send_single(node, QUERIES[0], monkeypatch, tag)
        ids = [tag]
    else:
        frame = await send_batch(node, QUERIES[:3], monkeypatch, batch_id=tag)
        ids = [f"{tag}:{number}" for number in range(3)]
    rows = []
    for request_id in ids:
        status, filed = ledger_rows(node, request_id)
        rows.append((status, None if filed is None else (comparable(filed[0]), filed[1])))
    return {key: value for key, value in frame.items() if key != "id"}, rows


@pytest.mark.asyncio
@pytest.mark.parametrize("point", ["decision", "send"])
@pytest.mark.parametrize("door", ["search", "batch"])
async def test_the_search_and_batch_doors_refuse_a_replaced_rubric_as_they_refuse_a_policy_error(
        node, monkeypatch, rubric, door, point):
    """Every failure after verification is the door's one refusal (`_dispatch` / `_dispatch_batch`, and each
    transport's send check, catch any exception): the same frame and the same ledger rows as a PolicyError at the
    same point. At the decision every spent envelope has its deny receipt; at the send check the checkpoint stands."""
    original_send_token = SearchIndexService.send_token
    monkeypatch.setattr(SearchIndexService, "send_token", original_send_token)   # restored whatever the reference did
    quiet = await door_request(node, monkeypatch, door, "quiet")
    assert quiet[0]["status"] == "ok"
    seen = door_reasons(monkeypatch)
    fired = armed_at(node, monkeypatch, door, point,
                     raising(PolicyError(REFERENCE)) if point == "decision" else at_the_send_check(PolicyError(REFERENCE)))
    reference = await door_request(node, monkeypatch, door, "reference")
    assert fired == [point]
    reference_seen = list(seen)
    seen.clear()
    fired = armed_at(node, monkeypatch, door, point, replace(rubric))
    replaced = await door_request(node, monkeypatch, door, "replaced")
    assert fired == [point]
    transport = "search" if door == "search" else "search_batch"
    if point == "decision":
        spent = 1 if door == "search" else 3
        assert reference_seen == [("spent", "PolicyError")] * spent + [(transport, "PolicyError")]
        assert seen == [("spent", "RubricMismatch")] * spent + [(transport, "PolicyError")]
        assert all(spent_with_refusal(node, request_id) for request_id in
                   (["replaced"] if door == "search" else [f"replaced:{number}" for number in range(3)]))
    else:
        assert reference_seen == [(transport, "PolicyError")]
        assert seen == [(transport, "RubricMismatch")], "the send check did not read the replaced rubric"
    assert replaced == reference
    assert replaced[0] == {"type": reference[0]["type"], "status": "error", "code": 403, "error": "permission_denied"}
