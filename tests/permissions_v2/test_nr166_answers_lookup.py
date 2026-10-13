"""NR166: handing back the node's one answer queue does not take the write gate.

An ask must be accepted within about 15 s or the app gives up on it. Before its admission, every ask asked the
runtime for the answer queue, and the runtime took the write gate to hand back an object it had built long ago:
one more wait behind whoever held the gate (a share decision holds it for seconds), for nothing.

Pinned here: once built, the queue is handed back while another thread holds the gate; it is built under the
gate, once, however many asks arrive first; and the hold for the older excludes' carry still refuses before it.
"""
from __future__ import annotations

import threading
import time

import pytest

from types import SimpleNamespace

from topos.permissions_v2 import answer_release
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.runtime import Runtime
from topos.storage.db import write_gate

pytestmark = pytest.mark.public


class Queue:
    """Stands in for the answer service: built with the runtime, and nothing else."""

    built: list = []

    def __init__(self, owner):
        Queue.built.append(write_gate._WRITE_LOCK._is_owned())      # whether its builder holds the gate
        self.owner = owner


@pytest.fixture
def runtime(monkeypatch):
    """The runtime's `answers`, on a stand-in that has only what the method reads."""
    Queue.built = []
    monkeypatch.setattr(answer_release, "AnswerService", Queue)
    stand_in = SimpleNamespace(hold_for_the_exclude_carry=lambda: None, _answers=None)
    stand_in.answers = lambda: Runtime.answers(stand_in)
    return stand_in


class GateHeld:
    """The write gate, held by another thread until `release()`."""

    def __init__(self):
        self._holding, self._release = threading.Event(), threading.Event()
        self._thread = threading.Thread(target=self._hold, name="a-share-decision")
        self._thread.start()
        assert self._holding.wait(5)

    def _hold(self):
        with write_gate.with_db_write():
            self._holding.set()
            self._release.wait(30)

    def release(self):
        self._release.set()
        self._thread.join(10)


def in_a_thread(call) -> tuple:
    """(thread, results): `call` running on its own thread, so the test can see whether it returned."""
    results = []
    thread = threading.Thread(target=lambda: results.append(call()))
    thread.start()
    return thread, results


def test_the_built_queue_is_handed_back_while_another_thread_holds_the_gate(runtime):
    queue = runtime.answers()
    held = GateHeld()
    try:
        started = time.monotonic()
        thread, results = in_a_thread(runtime.answers)
        thread.join(2)
        assert results == [queue] and time.monotonic() - started < 1.0
    finally:
        held.release()


def test_it_is_built_under_the_gate_and_only_once(runtime):
    held = GateHeld()
    try:
        asks = [in_a_thread(runtime.answers) for _ in range(4)]
        time.sleep(0.3)
        assert Queue.built == [] and all(not results for _thread, results in asks)   # nothing is built past the gate
    finally:
        held.release()
    for thread, _results in asks:
        thread.join(10)
    assert Queue.built == [True]                                                      # once, by the gate's holder
    assert len({id(results[0]) for _thread, results in asks}) == 1 and asks[0][1][0] is runtime.answers()
    assert asks[0][1][0].owner is runtime


def test_the_hold_for_the_older_excludes_still_refuses_first(runtime):
    runtime.answers()

    def owed():
        raise PolicyError("contact_excludes_owed")
    runtime.hold_for_the_exclude_carry = owed
    with pytest.raises(PolicyError):
        runtime.answers()
