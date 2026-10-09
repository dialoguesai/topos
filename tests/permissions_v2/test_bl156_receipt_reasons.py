"""BL-156 (1.5.2): an answer receipt names the real reason a job could not retrieve.

`AnswerService._compute` catches every PolicyError that `retrieve_for_answer` (and its index load and boundary)
raises. Before 1.5.2 it recorded `authority_moved` for all of them, so a share whose index was missing while it
rebuilt (BL-155) read in the owner's receipts as "authority moved". Now the receipt carries the refusal's own code,
from the closed vocabulary `RECEIPT_REASONS`; `authority_moved` is kept for the one case it names (the permitted set
changed between retrieval and the body). One test per code, through the real ask, the worker and the filed receipt.
"""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import Future

import pytest

from tests.permissions_v2.message_search_harness import owner, recipient
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- fixture
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 -- fixture the legacy one needs
from tests.permissions_v2.test_knowledge_search import node_for
from topos.permissions_v2 import answer_release
from topos.permissions_v2.answer_protocol import ASK
from topos.permissions_v2.answer_release import RECEIPT_REASONS, AnswerService, receipt_reason
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.search_index import index_path

ANSWER = "The owner is working on a synthetic task [1]."
QUESTION = "Synthetic message"
GRANT = "grant-search"

# A2A-4 §7.1's list, with amendment 2's private reason: every one is a receipt word.
CONTRACT_REASONS = frozenset({"answered", "nothing_matched", "question_protected", "question_not_supported",
    "all_sentences_dropped", "answer_protected", "authority_moved", "body_invalid", "model_unavailable",
    "model_error", "queue_deadline", "deadline"})


class _Deferred:
    """An executor that holds the worker until the test runs it: the refusal is set up between ask and run."""

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


def _service(node):
    from types import SimpleNamespace

    async def generate(_prompt, *, deadline):
        return ANSWER
    runtime = SimpleNamespace(protocol=node.protocol, message_search=lambda: node.search)
    service = AnswerService(runtime, clock=lambda: node.now[0], generate=generate)
    service._enabled = lambda: True
    service._executor = _Deferred()
    return service


def _ask(node, service):
    payload = {"question": QUESTION}
    signed = node._envelope(GRANT, ASK, payload, node.next_id("answer"))
    with recipient():
        service.submit(envelope=signed.model_dump(), payload=payload, request_id=signed.request_id)
    return signed.request_id


def _receipt(node, request_id):
    with node.ledger._transaction() as db:
        return json.loads(db.execute("SELECT receipt_json FROM p2a_receipts WHERE request_id=?", (request_id,)).fetchone()[0])


def _ready(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        assert node.index.rebuild(GRANT, now=node.now[0])["state"] == "ready"
    return node


def _reason_after(node, service, change, *, records_used=0):
    try:
        request_id = _ask(node, service)
        change()
        service._executor.run()
        receipt = _receipt(node, request_id)
    finally:
        service.close()
    assert receipt["outcome"] == "no_answer" and receipt["records_used"] == records_used
    return receipt["reason"]


def test_control_the_fixture_answers(legacy, tmp_path, monkeypatch):
    node = _ready(legacy, tmp_path, monkeypatch)
    service = _service(node)
    try:
        request_id = _ask(node, service)
        service._executor.run()
        assert _receipt(node, request_id)["reason"] == "answered"
    finally:
        service.close()


def test_a_share_whose_index_is_gone_is_named_in_the_receipt(legacy, tmp_path, monkeypatch):
    """The dark share of BL-155: the index file is gone while it rebuilds. The request path's `check_own` runs
    before `load`, so the refusal's code is `search_index_stale` (not `search_index_missing`, which `load` alone
    would say); the receipt carries that code, and no longer `authority_moved`."""
    node = _ready(legacy, tmp_path, monkeypatch)
    assert _reason_after(node, _service(node), lambda: index_path(node.index.root, GRANT).unlink()) == "search_index_stale"


def test_a_share_whose_record_key_is_gone_is_named_in_the_receipt(legacy, tmp_path, monkeypatch):
    node = _ready(legacy, tmp_path, monkeypatch)
    assert _reason_after(node, _service(node), lambda: node.index.keys.delete(GRANT)) == "search_index_stale"


def test_a_malformed_index_is_named_in_the_receipt(legacy, tmp_path, monkeypatch):
    node = _ready(legacy, tmp_path, monkeypatch)

    def malformed():
        with sqlite3.connect(index_path(node.index.root, GRANT)) as conn:
            conn.execute("UPDATE meta SET state='over_cap' WHERE singleton=1")   # a state the members contradict
    assert _reason_after(node, _service(node), malformed) == "search_index_integrity"


@pytest.mark.parametrize("code", ["search_index_missing", "search_index_over_cap", "search_index_unavailable",
                                  "entity_protection_lineage_unavailable", "policy_time"])
def test_a_retrieval_refusal_is_carried_by_its_own_code(legacy, tmp_path, monkeypatch, code):
    """The load-level codes (`search_index_missing`, `search_index_over_cap`) are not reachable through
    `retrieve_for_answer` today (`check_own` refuses first); each is still carried verbatim when raised."""
    node = _ready(legacy, tmp_path, monkeypatch)

    def refuses(**_kwargs):
        raise PolicyError(code)
    monkeypatch.setattr(node.search, "retrieve_for_answer", refuses)
    assert _reason_after(node, _service(node), lambda: None) == code


def test_a_stale_index_is_named_in_the_receipt(legacy, tmp_path, monkeypatch):
    node = _ready(legacy, tmp_path, monkeypatch)

    def stale():
        with sqlite3.connect(index_path(node.index.root, GRANT)) as conn:
            basis = json.loads(conn.execute("SELECT basis_json FROM meta WHERE singleton=1").fetchone()[0])
            basis["grant_generation"] = basis["grant_generation"] + 1
            conn.execute("UPDATE meta SET basis_json=? WHERE singleton=1", (json.dumps(basis),))
    assert _reason_after(node, _service(node), stale) == "search_index_stale"


def test_an_inactive_grant_is_named_in_the_receipt(legacy, tmp_path, monkeypatch):
    node = _ready(legacy, tmp_path, monkeypatch)

    def revoke():
        with owner():
            node.ledger.revoke(GRANT, expected_epoch=node.epoch(), command_id="revoke-it")
    assert _reason_after(node, _service(node), revoke) == "grant_inactive"


def test_a_stale_authority_is_named_in_the_receipt(legacy, tmp_path, monkeypatch):
    node = _ready(legacy, tmp_path, monkeypatch)
    assert _reason_after(node, _service(node), lambda: node.activate(node.search_raw, generation=2)) == "authority_stale"


def test_authority_moved_is_kept_for_a_permitted_set_that_changed_under_the_job(legacy, tmp_path, monkeypatch):
    node = _ready(legacy, tmp_path, monkeypatch)
    real, calls = node.search.retrieve_for_answer, []

    def moved(**kwargs):
        current, policy, output, decision = real(**kwargs)
        calls.append(len(output.records))
        if len(calls) == 2:   # the re-retrieval before the body: one record fewer than retrieval saw
            output = output.model_copy(update={"records": output.records[:-1]}) if len(output.records) > 1 else \
                output.model_copy(update={"records": [output.records[0].model_copy(update={"record_id": "r.moved"})]})
        return current, policy, output, decision
    monkeypatch.setattr(node.search, "retrieve_for_answer", moved)
    # `records_used` is what the first retrieval saw (the fixture's one message): a set was used, then moved.
    assert _reason_after(node, _service(node), lambda: None, records_used=1) == "authority_moved"
    assert calls == [1, 1]


def test_a_code_outside_the_vocabulary_is_recorded_as_refused(legacy, tmp_path, monkeypatch):
    node = _ready(legacy, tmp_path, monkeypatch)

    def refuses(**_kwargs):
        raise PolicyError("not_a_receipt_word")
    monkeypatch.setattr(node.search, "retrieve_for_answer", refuses)
    assert _reason_after(node, _service(node), lambda: None) == "refused"


def test_the_vocabulary_is_closed_and_holds_the_contract_and_the_retrieval_codes():
    assert CONTRACT_REASONS <= RECEIPT_REASONS
    assert {"search_index_missing", "search_index_stale", "search_index_over_cap", "grant_inactive",
            "authority_stale", "refused"} <= RECEIPT_REASONS
    assert all(word.replace("_", "").isalpha() and word == word.lower() for word in RECEIPT_REASONS)
    assert receipt_reason("search_index_missing") == "search_index_missing"
    assert receipt_reason("an invented person") == answer_release.REASON_OTHER == "refused"
