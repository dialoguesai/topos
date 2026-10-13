"""NR166 R4a: the node's background model calls wait for a share's answer job; an owner's and the answer's own do not.

An answers-mode ask is a few calls of the local model with a person waiting. Three background callers already
waited for it (`answer_gate.yield_to_answers`: message review, interest review, relabelling). Ingestion's
enrichment jobs, fact derivation and anything else a pipeline job sends to the model host did not, and the host
serves calls in the order they arrive.

Pinned here: inside background work a model call does not reach the host while an answer job is active, and does
the moment it ends; the wait is bounded; outside background work (an owner's own chat goes through the same
clients) nothing waits; the mark follows awaited coroutines and worker threads and ends with its block; a wait is
never taken on a thread that runs an event loop; a pipeline job, an enrichment run and a derivation extraction are
marked; the answer's own calls do not go through the gate at all.

No model is called: the host is a stub.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time

import pytest

from topos.permissions_v2 import answer_gate, answer_release

pytestmark = pytest.mark.public


@pytest.fixture(autouse=True)
def no_answer_left_active():
    assert not answer_gate.active()
    yield
    while answer_gate.active():
        answer_gate.delta(-1)


class AnswerJob:
    """An answer job that is active from now until `end()`."""

    def __init__(self):
        answer_gate.delta(1)
        self.ended = False

    def end(self):
        if not self.ended:
            self.ended = True
            answer_gate.delta(-1)

    def end_after(self, seconds: float) -> threading.Timer:
        timer = threading.Timer(seconds, self.end)
        timer.start()
        return timer


def host(monkeypatch) -> list:
    """The model host of `engine/backends/ollama.py`, stubbed: records when each call reached it."""
    import io
    import json
    import urllib.request

    reached = []

    def urlopen(request, timeout=None):
        reached.append(time.monotonic())
        return io.BytesIO(json.dumps({"response": "ok", "done": True}).encode("utf-8"))

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return reached


def call_the_model():
    from topos.engine.backends.ollama import OllamaAdapter

    adapter = OllamaAdapter(base_url="http://model-host.invalid")
    return adapter._generate_once("an-invented-model", "a prompt", num_predict=8, keep_alive=None, think=None,
                                  temperature=0.0, num_ctx=None, timeout=5.0)


# -- the wait itself ---------------------------------------------------------------------------------------------

def test_no_answer_no_wait():
    with answer_gate.background_work():
        assert answer_gate.yield_if_background() == 0.0 and answer_gate.wait_for_answers() == 0.0


def test_background_work_waits_until_the_answer_ends(monkeypatch):
    monkeypatch.setattr(answer_gate, "POLL_SECONDS", 0.01)
    job = AnswerJob()
    job.end_after(0.2)
    with answer_gate.background_work():
        waited = answer_gate.yield_if_background(poll=0.01)
    assert 0.15 <= waited < 2.0 and not answer_gate.active()


def test_the_wait_is_bounded_and_the_call_then_starts(caplog):
    AnswerJob()                                     # never ends
    clock = iter(x * 10.0 for x in range(100))
    with caplog.at_level(logging.INFO, logger=answer_gate.__name__), answer_gate.background_work():
        waited = answer_gate.yield_if_background(clock=lambda: next(clock), sleep=lambda _seconds: None)
    assert answer_gate.LIMIT_SECONDS == 110.0 and 110.0 <= waited <= 120.0       # the clock here steps by ten
    [line] = [record.getMessage() for record in caplog.records]
    assert line == f"background model call waited {waited:.1f} s for an answer job (the limit; it starts anyway)"


def test_outside_background_work_nothing_waits():
    AnswerJob()
    started = time.monotonic()
    assert answer_gate.yield_if_background() == 0.0
    assert asyncio.run(answer_gate.yield_to_answers_if_background()) == 0.0
    assert time.monotonic() - started < 0.2 and answer_gate.is_background() is False


def test_a_wait_is_never_taken_on_a_thread_that_runs_an_event_loop():
    AnswerJob()

    async def on_the_loop():
        with answer_gate.background_work():
            started = time.monotonic()
            waited = answer_gate.yield_if_background()      # the call that is not a coroutine, made on a loop
            return waited, time.monotonic() - started

    waited, took = asyncio.run(on_the_loop())
    assert waited == 0.0 and took < 0.2


@pytest.mark.asyncio
async def test_a_coroutine_in_background_work_waits_and_the_loop_keeps_running():
    job = AnswerJob()
    ticks = []

    async def tick():
        while not job.ended:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    ticker = asyncio.create_task(tick())
    asyncio.get_running_loop().call_later(0.2, job.end)
    with answer_gate.background_work():
        waited = await answer_gate.yield_to_answers_if_background(poll=0.01)
    await ticker
    assert 0.15 <= waited < 2.0 and len(ticks) >= 5


@pytest.mark.asyncio
async def test_the_mark_follows_the_work_and_ends_with_its_block():
    seen = {}

    async def awaited():
        seen["awaited"] = answer_gate.is_background()

    def in_a_worker_thread():
        seen["worker"] = answer_gate.is_background()

    @answer_gate.in_background
    async def decorated():
        return answer_gate.is_background()

    with answer_gate.background_work():
        await awaited()
        await asyncio.to_thread(in_a_worker_thread)
        other = asyncio.create_task(awaited())           # a task made inside takes the mark with it
        await other
    await asyncio.to_thread(lambda: seen.setdefault("worker_after", answer_gate.is_background()))
    assert seen == {"awaited": True, "worker": True, "worker_after": False}
    assert await decorated() is True and answer_gate.is_background() is False


# -- the model clients -------------------------------------------------------------------------------------------

def test_a_background_model_call_reaches_the_host_only_when_the_answer_has_ended(monkeypatch):
    monkeypatch.setattr(answer_gate, "POLL_SECONDS", 0.01)
    reached = host(monkeypatch)
    job = AnswerJob()
    started = time.monotonic()
    job.end_after(0.25)
    with answer_gate.background_work():
        assert call_the_model()["text"] == "ok"
    assert len(reached) == 1 and reached[0] - started >= 0.2


def test_the_same_call_outside_background_work_reaches_the_host_at_once(monkeypatch):
    reached = host(monkeypatch)
    AnswerJob()                                     # active throughout
    started = time.monotonic()
    assert call_the_model()["text"] == "ok"         # an owner's own request, through the same client
    assert len(reached) == 1 and reached[0] - started < 0.2


# -- what is marked ----------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_pipeline_job_is_background_work_and_what_follows_it_is_not(tmp_path, monkeypatch):
    """A real job through the runner: its executor, and a worker thread the executor starts, see the mark."""
    import sqlite3

    from topos.pipeline import job_runner
    from topos.pipeline.job_store import enqueue_job, get_job
    from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up

    conn = sqlite3.connect(str(tmp_path / "pipeline.db"), check_same_thread=False)
    apply_pipeline_jobs_v1_up(conn)
    await asyncio.to_thread(enqueue_job, conn, kind="file_ingestion", payload={}, job_id="an-invented-job")
    seen = {}

    async def executor(payload):
        seen["executor"] = answer_gate.is_background()
        seen["its_worker_thread"] = await asyncio.to_thread(answer_gate.is_background)
        return {"status": "ok", "messages_processed": 1, "records_created": {}}

    monkeypatch.setitem(job_runner.EXECUTORS, "file_ingestion", executor)
    assert await job_runner.process_pending_jobs_once(lambda: conn, limit=5) == 1
    assert seen == {"executor": True, "its_worker_thread": True}
    assert answer_gate.is_background() is False
    assert (await asyncio.to_thread(get_job, conn, "an-invented-job"))["status"] == "done"


def test_an_enrichment_run_and_a_derivation_extraction_are_background_work():
    from topos.enrichment.orchestrator import EnrichmentOrchestrator
    from topos.features.facts import llm_extract

    assert getattr(EnrichmentOrchestrator.run_canonical, "__wrapped__", None) is not None
    assert inspect.iscoroutinefunction(EnrichmentOrchestrator.run_canonical)
    assert "with answer_gate.background_work():   # NR166 R4a" in inspect.getsource(llm_extract)


def test_the_answers_own_calls_do_not_go_through_the_gate():
    """The writer and the judge call the host from `answer_release` itself. None of the waits is in it, and the
    answer's worker is never inside background work, so even the shared clients would not hold it."""
    source = inspect.getsource(answer_release)
    for wait in ("yield_to_answers", "wait_for_answers", "yield_if_background", "background_work"):
        assert wait not in source
    AnswerJob()
    assert answer_gate.yield_if_background() == 0.0
