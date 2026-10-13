"""NR166 R3b: the loop-lag watchdog names the code an event loop is stuck in, and nothing else.

On 13 Oct the owner's node logged that requests were late and never what its loop was doing. With the attribution
switch on, a loop that has not run for a second now gets one `loop_lag` line and its thread's stack as
`file:line:function` frames, at most once per ten seconds, and one `loop_lag_end` line with the stall's length.

Pinned here: the frames name the blocking function; a line carries no local, argument or path; a responsive loop
writes nothing; the rate limit holds and the stalls it kept quiet are still counted; with the switch off nothing
is started; a loop that ended is forgotten.
"""
from __future__ import annotations

import asyncio
import re
import threading
import time

import pytest

from tests.permissions_v2.test_nr166_attribution import (  # noqa: F401 -- the autouse fixture resets attribution
    attribution_ends_with_the_test, lines, on,
)
from tests.permissions_v2.test_search_timing_attribution import LOGGER
from topos.permissions_v2 import loop_lag

pytestmark = pytest.mark.public
FRAME = re.compile(r"[A-Za-z0-9_]+\.py:[0-9]+:[A-Za-z0-9_]+")


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    """A tenth of the real thresholds, and no watch left behind."""
    monkeypatch.setattr(loop_lag, "LAG_SECONDS", 0.15)
    monkeypatch.setattr(loop_lag, "BEAT_SECONDS", 0.02)
    monkeypatch.setattr(loop_lag, "EVERY_SECONDS", 10.0)
    yield
    for name in ("app", "client"):
        loop_lag.unwatch(name)


def the_query_that_never_returns(seconds: float) -> None:
    held_in_a_local = "a-value-no-line-may-carry"   # noqa: F841
    time.sleep(seconds)


async def settle(seconds: float = 0.12) -> None:
    await asyncio.sleep(seconds)


def of(caplog, stage: str) -> list:
    return [line for line in lines(caplog) if line["stage"] == stage]


@pytest.mark.asyncio
async def test_a_stalled_loop_is_named_with_the_code_it_is_in(monkeypatch, caplog):
    on(monkeypatch)
    with caplog.at_level("INFO", logger=LOGGER):
        assert loop_lag.watch(asyncio.get_running_loop(), "app") is True
        await settle()
        the_query_that_never_returns(0.6)
        await settle()
    [lag] = of(caplog, "loop_lag")
    assert lag["loop"] == "app" and lag["ms"] >= 150 and lag["stalls"] == "0"
    frames = of(caplog, "loop_lag_frame")
    assert int(lag["frames"]) == len(frames) and [int(frame["depth"]) for frame in frames] == list(range(len(frames)))
    # Innermost first: the function that blocked, then the test that called it.
    assert frames[0]["site"].endswith(":the_query_that_never_returns")
    assert frames[0]["site"].startswith("test_nr166_loop_lag.py:")
    assert frames[1]["site"].endswith(":test_a_stalled_loop_is_named_with_the_code_it_is_in")
    # A frame is a file's base name, a line and a function: no path, and never a value.
    assert all(FRAME.fullmatch(frame["site"]) for frame in frames)
    text = " ".join(record.getMessage() for record in caplog.records if record.name == LOGGER)
    assert "a-value-no-line-may-carry" not in text and "/" not in text
    # When the stall ends, its whole length is written once.
    [ended] = of(caplog, "loop_lag_end")
    assert ended["loop"] == "app" and 550 <= ended["ms"] < 5000


@pytest.mark.asyncio
async def test_a_loop_that_keeps_running_writes_nothing(monkeypatch, caplog):
    on(monkeypatch)
    with caplog.at_level("INFO", logger=LOGGER):
        assert loop_lag.watch(asyncio.get_running_loop(), "app") is True
        for _ in range(8):
            await asyncio.to_thread(the_query_that_never_returns, 0.05)   # the same work, in a worker thread
        await settle()
    assert [line for line in lines(caplog) if line["stage"].startswith("loop_lag")] == []


@pytest.mark.asyncio
async def test_one_report_in_the_window_and_the_quiet_stalls_are_counted(monkeypatch, caplog):
    on(monkeypatch)
    with caplog.at_level("INFO", logger=LOGGER):
        loop_lag.watch(asyncio.get_running_loop(), "app")
        await settle()
        for _ in range(3):
            the_query_that_never_returns(0.4)
            await settle()
        assert len(of(caplog, "loop_lag")) == 1                  # the second and third are inside the window
        monkeypatch.setattr(loop_lag, "EVERY_SECONDS", 0.0)      # the window has passed
        the_query_that_never_returns(0.4)
        await settle()
    first, second = of(caplog, "loop_lag")
    assert first["stalls"] == "0" and second["stalls"] == "2" and 350 <= float(second["longest_ms"]) < 5000
    assert len(of(caplog, "loop_lag_end")) == 2                  # one for each stall a line was written for


@pytest.mark.asyncio
async def test_with_the_switch_off_nothing_is_started(monkeypatch, caplog):
    before = {thread.name for thread in threading.enumerate()}
    with caplog.at_level("INFO", logger=LOGGER):
        assert loop_lag.watch(asyncio.get_running_loop(), "app") is False
        the_query_that_never_returns(0.4)
        await settle()
    assert loop_lag._watched == {} and "loop-lag-watch" not in {t.name for t in threading.enumerate()} - before
    assert lines(caplog) == []


@pytest.mark.asyncio
async def test_only_the_two_loops_are_watched_by_name(monkeypatch):
    on(monkeypatch)
    assert loop_lag.watch(asyncio.get_running_loop(), "worker-7") is False and loop_lag._watched == {}


def test_a_loop_that_ended_is_forgotten_and_its_own_thread_is_the_one_read(monkeypatch, caplog):
    on(monkeypatch)
    seen = {}

    def client():
        loop = asyncio.new_event_loop()
        try:
            seen["watched"] = loop_lag.watch(loop, "client")     # before the loop runs, as the client thread does

            async def run():
                await settle()
                the_query_that_never_returns(0.5)
                await settle()
            loop.run_until_complete(run())
        finally:
            loop.close()

    with caplog.at_level("INFO", logger=LOGGER):
        thread = threading.Thread(target=client, name="a-client-thread")
        thread.start()
        thread.join(10)
        time.sleep(0.4)       # the loop is closed: no stamp moves, and that is not a stall
    assert seen == {"watched": True}
    [lag] = of(caplog, "loop_lag")
    assert lag["loop"] == "client"
    assert of(caplog, "loop_lag_frame")[0]["site"].endswith(":the_query_that_never_returns")
    assert "client" not in loop_lag._watched
