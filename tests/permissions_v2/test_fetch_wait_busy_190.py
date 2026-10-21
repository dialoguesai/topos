"""Lane 190 (the 21 Oct 2026 UI gate, Q7): a fetch whose relay wait runs out is `answer_busy`, never a refusal.

The node had answered (receipt: answered, 68 s) while the fetch's own store read waited behind the write gate for more
than `WAIT_SECONDS`. The transport answered the generic refusal (403 `permission_denied`); the control plane forgot the
held answer id on a refusal and the app ended the turn with "didn't answer this one". A finished answer was lost at the
last step, the class of fault 1.5.3 was meant to end. Now a FETCH's wait running out is 429 `answer_busy`: the control
plane keeps the id and the app keeps polling. An ASK's wait running out stays the refusal: a retried ask is a second job.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from types import SimpleNamespace

import pytest

from topos.permissions_v2 import answer_transport

pytestmark = pytest.mark.public


class Socket:
    async def send(self, frame):
        self.frame = json.loads(frame)


def _stamped(monkeypatch, service):
    monkeypatch.setattr(answer_transport, "WAIT_SECONDS", 0.05)
    monkeypatch.setattr(answer_transport, "verify_relay_stamp", lambda _message: SimpleNamespace(
        cls="third_party", channel="cp_relay", acting_user="person", client_id="topos-app"))
    monkeypatch.setattr(answer_transport, "get_runtime", lambda: SimpleNamespace(answers=lambda: service))


def _dispatch(kind):
    socket = Socket()
    asyncio.run(answer_transport.dispatch_answer(socket, {"id": "req-1", "type": kind,
                                                          "payload": {"envelope": {}, "intent": {}}}))
    return socket.frame


def test_a_fetch_whose_wait_runs_out_is_answer_busy(monkeypatch, caplog):
    finish = threading.Event()

    class Service:
        def fetch(self, **_kwargs):
            finish.wait(5)
            return {}, {}

    _stamped(monkeypatch, Service())
    caplog.set_level(logging.INFO, logger="topos.permissions_v2.answer_transport")
    try:
        frame = _dispatch(answer_transport.FETCH_TYPE)
    finally:
        finish.set()
    assert frame == {"id": "req-1", "type": answer_transport.FETCH_TYPE, "status": "error",
                     "code": 429, "error": "answer_busy"}
    assert not any("req-1" in record.getMessage() for record in caplog.records)


def test_an_ask_whose_wait_runs_out_stays_the_refusal(monkeypatch):
    """A retried ask would be a second job: the ask door's wait running out is still `permission_denied`."""
    finish = threading.Event()

    class Service:
        def submit(self, **_kwargs):
            finish.wait(5)
            return {}, {}

    _stamped(monkeypatch, Service())
    try:
        frame = _dispatch(answer_transport.SUBMIT_TYPE)
    finally:
        finish.set()
    assert (frame["code"], frame["error"]) == (403, "permission_denied")


def test_a_fetch_that_answers_inside_the_wait_is_unchanged(monkeypatch):
    class Service:
        def fetch(self, **_kwargs):
            return {"state": "answered"}, {"answer": "x"}

        def _current(self, _envelope):
            return None, SimpleNamespace(search=SimpleNamespace(selective_facts=None))

    _stamped(monkeypatch, Service())
    monkeypatch.setattr("topos.permissions_v2.signing.parse_envelope", lambda _e: None)
    frame = _dispatch(answer_transport.FETCH_TYPE)
    assert frame["status"] == "ok" and frame["payload"]["result"] == {"state": "answered"}


def test_a_fetch_the_service_refuses_is_still_the_refusal(monkeypatch):
    """Only the wait running out changed. A refusal raised by the service is the uniform refusal, as before."""
    from topos.permissions_v2.canonical import PolicyError

    class Service:
        def fetch(self, **_kwargs):
            raise PolicyError("permission_denied")

    _stamped(monkeypatch, Service())
    frame = _dispatch(answer_transport.FETCH_TYPE)
    assert (frame["code"], frame["error"]) == (403, "permission_denied")


def _payload(answer_id):
    return {"envelope": {}, "intent": {"answer_id": answer_id}}


def test_a_second_fetch_for_an_answer_still_waiting_is_busy_at_once_with_no_second_thread(monkeypatch):
    """The app polls every few seconds for up to twelve minutes; while one fetch of an answer still waits behind
    the write gate, the next fetch of the same answer is answered busy without a second thread. Another answer's
    fetch is not held back, and once the first thread ends the same answer is fetched again as usual."""
    gate = threading.Event()
    calls = []

    class Service:
        def fetch(self, *, payload, **_kwargs):
            calls.append(payload["answer_id"])
            if payload["answer_id"] == "ans_slow":
                gate.wait(5)
            return {"state": "answered"}, {"answer": "x"}

        def _current(self, _envelope):
            return None, SimpleNamespace(search=SimpleNamespace(selective_facts=None))

    _stamped(monkeypatch, Service())
    monkeypatch.setattr("topos.permissions_v2.signing.parse_envelope", lambda _e: None)
    answer_transport._FETCHES_WAITING.clear()

    async def one(answer_id):
        socket = Socket()
        await answer_transport.dispatch_answer(socket, {"id": "req-" + answer_id, "type": answer_transport.FETCH_TYPE,
                                                        "payload": _payload(answer_id)})
        return socket.frame

    async def main():
        first = await one("ans_slow")                      # the wait runs out: busy, thread still waiting
        second = await one("ans_slow")                     # answered busy at once, no second thread
        other = await one("ans_other")                     # another answer is not held back
        gate.set()
        await asyncio.sleep(0.2)                           # the first thread ends and drops its key
        third = await one("ans_slow")
        return first, second, other, third

    first, second, other, third = asyncio.run(main())
    assert (first["code"], first["error"]) == (429, "answer_busy")
    assert (second["code"], second["error"]) == (429, "answer_busy")
    assert other["status"] == "ok"
    assert third["status"] == "ok"
    assert calls == ["ans_slow", "ans_other", "ans_slow"]
    assert not answer_transport._FETCHES_WAITING


def test_the_waiting_key_is_dropped_when_the_service_raises(monkeypatch):
    from topos.permissions_v2.canonical import PolicyError

    class Service:
        def fetch(self, **_kwargs):
            raise PolicyError("permission_denied")

    _stamped(monkeypatch, Service())
    answer_transport._FETCHES_WAITING.clear()
    socket = Socket()
    asyncio.run(answer_transport.dispatch_answer(socket, {"id": "req-1", "type": answer_transport.FETCH_TYPE,
                                                          "payload": _payload("ans_a")}))
    assert socket.frame["code"] == 403 and not answer_transport._FETCHES_WAITING
