"""Kept questions Wave 2 (lane OFF2N): the PII disclosure sweep's model calls wait as background work.

The node's own sweep (``run_at_startup``) called the privacy filter's model with no regard for a share's answer job or
the owner's chat. It now runs as background work, and before each batch it waits for an answer job in hand and, under
the yield rule, an owner frame in flight (at most 110 s). A sweep run outside that mark (the owner asked for it) never
waits. The filter is the suite's stand-in: no model is called. Every row is invented.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from topos.disclosure import disclosure_sweep
from topos.disclosure.disclosure_sweep import Sweep, run_at_startup
from topos.permissions_v2 import answer_gate, away_config
from tests.disclosure.test_disclosure_sweep import Filter, conn, layer_on  # noqa: F401 -- the fixtures


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(answer_gate, "POLL_SECONDS", 0.01)
    monkeypatch.delenv("TOPOS_PERMISSIONS_V2_BACKGROUND_YIELDS_TO_OWNER", raising=False)
    away_config.apply(away_config.DEFAULT)
    yield
    while answer_gate.owner_frames():
        answer_gate.owner_frame_ended()
    while answer_gate.active():
        answer_gate.delta(-1)


class TimedFilter(Filter):
    def __init__(self):
        super().__init__()
        self.at = []

    async def redact_batch(self, items):
        self.at.append(time.monotonic())
        return await super().redact_batch(items)


def one_walk(conn, client, monkeypatch):
    """`run_at_startup` for one walk: the shutdown flag goes up once the first walk has ended."""
    import topos.runtime_shutdown as runtime
    walks = []
    real = disclosure_sweep.run_sweep

    done = []

    async def recording(connect, **kwargs):
        walks.append(kwargs.get("mode"))
        try:
            return await Sweep(connect, client=kwargs.get("client"), pause=0, poll=0,
                               stage_active=lambda: False).run(mode=kwargs.get("mode", "verify"))
        finally:
            done.append(True)
    monkeypatch.setattr(disclosure_sweep, "run_sweep", recording)
    monkeypatch.setattr(runtime, "is_shutdown_requested", lambda: bool(done))
    asyncio.run(run_at_startup(lambda: conn, delay=0, client=client))
    assert walks == ["verify"] and real is not recording


def test_the_nodes_own_sweep_waits_for_the_owners_frame_before_a_batch(conn, monkeypatch):
    client = TimedFilter()
    answer_gate.owner_frame_began()
    ended = []
    threading.Timer(0.3, lambda: (ended.append(time.monotonic()), answer_gate.owner_frame_ended())).start()
    one_walk(conn, client, monkeypatch)
    assert client.at and client.at[0] >= ended[0]


def test_the_nodes_own_sweep_waits_for_a_share_answer_before_a_batch(conn, monkeypatch):
    client = TimedFilter()
    answer_gate.delta(1)
    ended = []
    threading.Timer(0.3, lambda: (ended.append(time.monotonic()), answer_gate.delta(-1))).start()
    one_walk(conn, client, monkeypatch)
    assert client.at[0] >= ended[0]


def test_a_sweep_outside_the_nodes_own_run_never_waits(conn):
    client = TimedFilter()
    answer_gate.owner_frame_began()
    answer_gate.delta(1)
    began = time.monotonic()
    asyncio.run(Sweep(lambda: conn, client=client, pause=0, poll=0, stage_active=lambda: False).run(mode="verify"))
    assert client.at and client.at[0] - began < 0.2
