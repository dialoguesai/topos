"""NR166 R4a: with attribution on, an answer job says where its seconds went.

An own-AI answer took about 100 s on the owner's node and nothing in the log said whether that was the queue, the
retrieval and its wait for the write gate, the writer's model call or the judge's. One `answer_job` line per job
now carries the split.

Pinned here: each part is the time actually spent in it; the gate wait is this thread's own; the line carries
counts and durations and one closed word, nothing else; with attribution off there is no timer and no line; and
the job's writer and judge are put back as they were.
"""
from __future__ import annotations

import asyncio
import inspect
import threading
import time

import pytest

from tests.permissions_v2.test_nr166_attribution import (  # noqa: F401 -- the autouse fixture resets attribution
    attribution_ends_with_the_test, lines, on,
)
from tests.permissions_v2.test_search_timing_attribution import LOGGER
from topos.permissions_v2 import answer_release, search_release, search_timing
from topos.storage.db import write_gate

pytestmark = pytest.mark.public


async def a_model_call(seconds, value="a-sentence-no-line-may-carry"):
    await asyncio.sleep(seconds)
    return value


def test_the_split_is_what_was_spent_in_each_part(monkeypatch, caplog):
    on(monkeypatch)
    holding, release = threading.Event(), threading.Event()

    def another_writer():
        with write_gate.with_db_write():
            holding.set()
            release.wait(10)

    with caplog.at_level("INFO", logger=LOGGER):
        split = search_timing.answer_job_timer()
        writer, judge = split.around("writer", a_model_call), split.around("judge", a_model_call)
        assert asyncio.run(writer(0.12)) == "a-sentence-no-line-may-carry"      # the call's own result, untouched
        for _ in range(3):
            asyncio.run(judge(0.04))
        search_timing.retrieval_spent(0.5)                                       # as search_release reports one
        holder = threading.Thread(target=another_writer)
        holder.start()
        assert holding.wait(5)
        threading.Timer(0.3, release.set).start()
        with write_gate.with_db_write():                                         # this thread waits for the gate
            pass
        holder.join(10)
        split.end("nothing_matched", 7)
    [line] = [found for found in lines(caplog) if found["stage"] == "answer_job"]
    assert line["outcome"] == "nothing_matched" and line["corr"] == "-"
    assert float(line["queued_ms"]) == 7000.0 and float(line["retrieval_ms"]) == 500.0
    assert 110 <= float(line["writer_ms"]) < 1000 and line["writer_calls"] == "1"
    assert 110 <= float(line["judge_ms"]) < 1000 and line["judge_calls"] == "3"
    assert 250 <= float(line["gate_wait_ms"]) < 3000
    assert line["ms"] >= float(line["writer_ms"]) + float(line["judge_ms"]) + float(line["gate_wait_ms"])
    text = " ".join(record.getMessage() for record in caplog.records if record.name == LOGGER)
    assert "a-sentence-no-line-may-carry" not in text
    # A retrieval after the job has ended reports to no job.
    search_timing.retrieval_spent(9.0)
    assert split.parts["retrieval"] == 0.5


def test_a_model_call_that_raises_is_timed_and_still_raises(monkeypatch):
    on(monkeypatch)
    split = search_timing.answer_job_timer()

    async def unavailable():
        await asyncio.sleep(0.05)
        raise RuntimeError("as raised")

    with pytest.raises(RuntimeError, match="as raised"):
        asyncio.run(split.around("judge", unavailable)())
    assert split.calls["judge"] == 1 and split.parts["judge"] >= 0.04
    split.end(None, 0)


def test_without_attribution_there_is_no_timer_and_no_line(caplog):
    with caplog.at_level("INFO", logger=LOGGER):
        assert search_timing.answer_job_timer() is None
        search_timing.retrieval_spent(1.0)
    assert lines(caplog) == []


def test_the_job_times_its_own_writer_and_judge_and_puts_them_back():
    """Read from the job's source: it stands in for the two calls only while one job computes, restores them
    whatever happens, and writes the line after the receipt has committed."""
    run = inspect.getsource(answer_release.AnswerService._run)
    assert "split = search_timing.answer_job_timer()" in run
    assert 'split.around("writer", generate), split.around("judge", judge)' in run
    assert "finally:\n                    self.generate, self.judge = generate, judge" in run
    assert run.index("checkpoint_answer_receipt") < run.index("split.end(")
    assert "search_timing.retrieval_spent(" in inspect.getsource(search_release._once_more_if_moved)
