"""BL-147 (node half): the background assessments yield to an answer (A2A-4 Q4), which 1.5.0 counted but never applied.

The answer model is also the labelling model. On the owner's Mac an answer-sized request waited in the model host
behind other calls of the same model (the battery: about 3 s of model work inside a 5 to 27 s wait). Q4: no
assessment call starts while an answer job is queued or running; a call already running is not interrupted. The
wait is bounded by an answer's own deadline, so a count that never came down cannot stop the labelling for good.
Waiting changes when an assessment runs, never what it decides. Invented data only.
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_answer_release import _ask, _finished
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- the fixture
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 -- the fixture's fixture
from topos.permissions_v2 import answer_gate, answer_release
from topos.permissions_v2 import automatic_message_review, interest_relabel, interest_review
from topos.permissions_v2.answer_release import AnswerService, answer_jobs_active

ASSESSMENTS = {"message_labels": automatic_message_review.assess, "interest_labels": interest_review.assess,
               "interest_second_tries": interest_relabel.ask}


class Stopped(Exception):
    pass


class Transport:
    """Records when the call started (its host check comes first), then stops it before any model call."""
    base_url = "http://127.0.0.1:11434"

    def __init__(self):
        self.client, self.started = self, []

    async def verify(self):
        self.started.append(time.monotonic())

    async def post(self, url, **kwargs):
        raise Stopped()


@pytest.fixture(autouse=True)
def no_job_left():
    assert not answer_gate.active()
    yield
    assert not answer_gate.active()


@pytest.mark.parametrize("name", sorted(ASSESSMENTS))
def test_no_assessment_call_starts_while_an_answer_job_is_in_hand(name):
    transport, ended = Transport(), []
    answer_gate.delta(1)

    def answer_ends():
        time.sleep(0.4)
        ended.append(time.monotonic())
        answer_gate.delta(-1)

    worker = threading.Thread(target=answer_ends)
    worker.start()
    try:
        with pytest.raises(Exception):
            asyncio.run(ASSESSMENTS[name]({"input": {}}, transport=transport))
    finally:
        worker.join()
    assert len(transport.started) == 1 and transport.started[0] >= ended[0], name


@pytest.mark.parametrize("name", sorted(ASSESSMENTS))
def test_with_no_answer_in_hand_an_assessment_call_starts_at_once(name):
    transport = Transport()
    began = time.monotonic()
    with pytest.raises(Exception):
        asyncio.run(ASSESSMENTS[name]({"input": {}}, transport=transport))
    assert len(transport.started) == 1 and transport.started[0] - began < 0.2


def test_the_wait_is_bounded_by_an_answers_deadline():
    assert answer_gate.LIMIT_SECONDS == answer_release.MAX_END_SECONDS
    assert asyncio.run(answer_gate.yield_to_answers()) == 0.0
    answer_gate.delta(1)
    try:
        waited = asyncio.run(answer_gate.yield_to_answers(limit=0.3, poll=0.05))
    finally:
        answer_gate.delta(-1)
    assert 0.3 <= waited < 1.0


@pytest.mark.parametrize("legacy", ["goal"], indirect=True)
def test_an_ask_counts_as_in_hand_from_acceptance_until_it_ends(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with owner():
        node.index.rebuild("grant-search", now=node.now[0])
    release, writing = threading.Event(), threading.Event()

    async def generate(_prompt, *, deadline):
        writing.set()
        await asyncio.to_thread(release.wait, 10)
        return "The aim is a finished build before the weekend [1]."

    service = AnswerService(SimpleNamespace(protocol=node.protocol, message_search=lambda: node.search),
                            clock=lambda: node.now[0], generate=generate)
    service._enabled = lambda: True
    try:
        assert not answer_jobs_active()
        answer_id = _ask(node, service, "What goals were shared about the compiler?")
        assert answer_jobs_active()
        assert writing.wait(10) and answer_jobs_active()
        release.set()
        assert _finished(node, service, answer_id)["outcome"] == "answered"
        for _ in range(100):
            if not answer_jobs_active():
                break
            time.sleep(0.01)
        assert not answer_jobs_active()
    finally:
        release.set()
        service.close()
