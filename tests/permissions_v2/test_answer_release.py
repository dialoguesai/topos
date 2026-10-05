"""A real signed ask and fetch through the node ledger and permitted retrieval."""
from __future__ import annotations

import time
import asyncio
import json
import threading
from types import SimpleNamespace
import pytest

from tests.permissions_v2.message_search_harness import recipient, owner
from tests.permissions_v2.test_reconciliation_provenance import legacy
from tests.permissions_v2.test_ingest_provenance import ingest_fixture
from tests.permissions_v2.test_knowledge_search import node_for
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.answer_protocol import ASK, FETCH, verify_node_answer
from topos.permissions_v2.answer_release import AnswerBusy, AnswerService
from topos.permissions_v2 import answer_transport
from topos.permissions_v2.answer_transport import dispatch_answer


def _service(node, answer):
    async def generate(_prompt, *, deadline):
        return answer
    runtime = SimpleNamespace(protocol=node.protocol, message_search=lambda: node.search)
    service = AnswerService(runtime, clock=lambda: node.now[0], generate=generate)
    service._enabled = lambda: True
    return service


def _ask(node, service, question):
    payload = {"question": question}
    signed = node._envelope("grant-search", ASK, payload, node.next_id("answer"))
    with recipient():
        result, output = service.submit(envelope=signed.model_dump(), payload=payload, request_id=signed.request_id)
    verify_node_answer(result, trusted_keys={"node-key": node.node_key.public_key().public_bytes_raw()},
                       envelope=signed, output=output, mode="only", now=node.now[0])
    return output["answer_id"]


def _fetch(node, service, answer_id):
    payload = {"answer_id": answer_id}
    signed = node._envelope("grant-search", FETCH, payload, node.next_id("fetch"))
    with recipient():
        result, output = service.fetch(envelope=signed.model_dump(), payload=payload, request_id=signed.request_id)
    verify_node_answer(result, trusted_keys={"node-key": node.node_key.public_key().public_bytes_raw()},
                       envelope=signed, output=output, mode="only", now=node.now[0])
    return output


def _finished(node, service, answer_id):
    for _ in range(100):
        output = _fetch(node, service, answer_id)
        if output.get("outcome"):
            return output
        time.sleep(.01)
    raise AssertionError("answer worker did not finish")


def test_answer_only_runs_one_model_and_fetches_once(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner(): node.index.rebuild("grant-search", now=node.now[0])
    service = _service(node, "The owner is working on a synthetic task [1].")
    try:
        answer_id = _ask(node, service, "Synthetic message")
        body = _finished(node, service, answer_id)
        assert body["outcome"] == "answered"
        assert set(body) == {"version", "outcome", "answer"}
        assert "[1]" not in body["answer"]
    finally:
        service.close()


def test_protected_question_finishes_as_no_answer(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner(): node.index.rebuild("grant-search", now=node.now[0])
    service = _service(node, "Irrelevant response [1].")
    try:
        answer_id = _ask(node, service, "secretperson")
        body = _finished(node, service, answer_id)
        assert body == {"version": "topos-answer/v1", "outcome": "no_answer"}
    finally:
        service.close()


def test_answer_is_handed_out_once_and_content_never_enters_either_database(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner(): node.index.rebuild("grant-search", now=node.now[0])
    answer = "The synthetic schedule moved to next week [1]."
    question = "When did the synthetic schedule move?"
    service = _service(node, answer)
    try:
        answer_id = _ask(node, service, question)
        body = _finished(node, service, answer_id)
        assert body["outcome"] == "answered"
        with pytest.raises(PolicyError):
            _fetch(node, service, answer_id)
        for path in (node.ledger.path, legacy[0].resolver.path):
            stored = path.read_bytes()
            assert question.encode() not in stored
            assert body["answer"].encode() not in stored
    finally:
        service.close()


def test_second_ask_for_one_share_is_busy_and_does_not_spend_a_question(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner(): node.index.rebuild("grant-search", now=node.now[0])
    release = threading.Event()

    async def generate(_prompt, *, deadline):
        await asyncio.to_thread(release.wait)
        return "The owner is working on a synthetic task [1]."

    service = _service(node, "unused")
    service.generate = generate
    try:
        _ask(node, service, "Synthetic message first question")
        with pytest.raises(AnswerBusy):
            _ask(node, service, "Synthetic message second question")
        with node.ledger._transaction() as db:
            assert db.execute("SELECT SUM(questions) FROM p2a_question_days").fetchone()[0] == 1
    finally:
        release.set()
        service.close()


def test_answer_transport_uses_websocket_send_for_uniform_refusal():
    class Socket:
        def __init__(self):
            self.frames = []

        async def send(self, frame):
            self.frames.append(json.loads(frame))

    socket = Socket()
    asyncio.run(dispatch_answer(socket, {"id": "test-request", "type": "unknown"}))
    assert socket.frames == [{"id": "test-request", "type": "unknown", "status": "error",
                              "code": 403, "error": "permission_denied"}]


def test_answer_transport_initializes_service_off_event_loop(monkeypatch):
    worker_threads = []
    event_thread = threading.get_ident()

    class Socket:
        async def send(self, frame):
            self.frame = json.loads(frame)

    class Service:
        def submit(self, **_kwargs):
            worker_threads.append(threading.get_ident())
            return {"signed": True}, {"state": "pending"}

    def answers():
        worker_threads.append(threading.get_ident())
        return Service()

    monkeypatch.setattr(answer_transport, "verify_relay_stamp", lambda _message: SimpleNamespace(
        cls="third_party", channel="cp_relay", acting_user="person", client_id="topos-app"))
    monkeypatch.setattr(answer_transport, "get_runtime", lambda: SimpleNamespace(answers=answers))
    socket = Socket()
    asyncio.run(dispatch_answer(socket, {"id": "test-request", "type": answer_transport.SUBMIT_TYPE,
                                         "payload": {"envelope": {}, "intent": {}}}))
    assert socket.frame == {"id": "test-request", "type": answer_transport.SUBMIT_TYPE,
                            "status": "ok", "payload": {"result": {"signed": True}, "output": {"state": "pending"}}}
    assert len(worker_threads) == 2 and all(value != event_thread for value in worker_threads)
