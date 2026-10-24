"""OFF2N (scope §10): the owner-chat-under-backlog harness runs, and the yield rule shortens the owner's wait.

A short run of ``scripts/permissions_v2/owner_chat_backlog_harness.py``: a stubbed one-slot model host kept busy by
two background lanes, and the owner's stamped generations (three model calls each, as a home-chat turn makes) through
the real client. With the yield rule on, a turn waits only for the background calls already sent when it arrived; off,
each of its later calls also queues behind a background call sent meanwhile. Timing is a sleep stand-in, so the
bounds below are wide: they compare the two settings, nothing absolute.
"""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from topos.permissions_v2 import answer_gate, away_config

HARNESS = Path(__file__).resolve().parents[2] / "scripts" / "permissions_v2" / "owner_chat_backlog_harness.py"


@pytest.fixture
def harness(monkeypatch):
    spec = importlib.util.spec_from_file_location("owner_chat_backlog_harness", HARNESS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("TOPOS_CP_STAMP_PUBKEY", "TOPOS_PERMISSIONS_V2_OWNER_LANE",
                 "TOPOS_PERMISSIONS_V2_BACKGROUND_YIELDS_TO_OWNER"):
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(answer_gate, "POLL_SECONDS", answer_gate.POLL_SECONDS)
    yield module
    away_config.apply(away_config.DEFAULT)
    while answer_gate.owner_frames():
        answer_gate.owner_frame_ended()


def test_the_yield_rule_shortens_the_owners_wait_under_a_background_backlog(harness):
    how = dict(frames=6, interval=0.5, owner_call=0.03, background_call=0.1, workers=2, owner_calls=3, poll=0.005)
    off = asyncio.run(harness.run_one(yields=False, **how))
    on = asyncio.run(harness.run_one(yields=True, **how))
    assert off["owner_frames"] == on["owner_frames"] == 6
    assert on["owner_wait_ms"]["p50"] < off["owner_wait_ms"]["p50"]
    assert on["owner_wait_ms"]["p95"] <= 2 * 100 + 80      # at most the background calls already sent, and slack
    assert on["background_calls_per_s"] > 0 and off["background_calls_per_s"] > 0
    assert answer_gate.owner_frames() == 0                  # every frame counted out
