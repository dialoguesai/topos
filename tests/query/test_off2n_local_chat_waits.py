"""Kept questions Wave 2 (lane OFF2N): the truth inbound lane's local client waits as background work.

``LocalChat`` (the local Ollama client verify_claim uses) went to the model host at once on every path. The owner's
inbound truth lane runs as a pipeline job, which ``process_job`` marks background, so its checks now wait for a share's
answer job and, under the yield rule, the owner's own frames, at most 110 s; the owner's own check (the Fun Camera's
mirror) and a share's verify_claim door are not background and never wait. No model is called: the transport records
when the call would have gone.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from topos.permissions_v2 import answer_gate, away_config
from topos.query import verify_v2_models
from topos.query.verify_v2_models import LocalChat, Usage


@pytest.fixture(autouse=True)
def host(monkeypatch):
    monkeypatch.setattr(answer_gate, "POLL_SECONDS", 0.01)
    monkeypatch.delenv("TOPOS_PERMISSIONS_V2_BACKGROUND_YIELDS_TO_OWNER", raising=False)
    away_config.apply(away_config.DEFAULT)
    reached = []

    def post(url, body, headers, timeout):
        reached.append(time.monotonic())
        return {"message": {"content": "{}"}, "prompt_eval_count": 1, "eval_count": 1}
    monkeypatch.setattr(verify_v2_models, "_post_json", post)
    yield reached
    while answer_gate.owner_frames():
        answer_gate.owner_frame_ended()
    while answer_gate.active():
        answer_gate.delta(-1)


def call():
    return LocalChat(model="an-invented-model", base_url="http://model-host.invalid")("system", "user", {}, 5.0, Usage())


def ended_after(seconds, end):
    ended = []
    threading.Timer(seconds, lambda: (ended.append(time.monotonic()), end())).start()
    return ended


def test_in_the_lanes_background_job_it_waits_for_the_owners_frame(host):
    answer_gate.owner_frame_began()
    ended = ended_after(0.3, answer_gate.owner_frame_ended)

    async def job():                      # as process_job runs the lane: background, the call in a worker thread
        with answer_gate.background_work():
            return await asyncio.to_thread(call)
    asyncio.run(job())
    assert len(host) == 1 and host[0] >= ended[0]


def test_in_the_lanes_background_job_it_waits_for_a_share_answer(host):
    answer_gate.delta(1)
    ended = ended_after(0.3, lambda: answer_gate.delta(-1))
    with answer_gate.background_work():
        call()
    assert host[0] >= ended[0]


def test_the_owners_own_check_and_a_share_door_never_wait(host):
    answer_gate.owner_frame_began()
    answer_gate.delta(1)
    began = time.monotonic()
    call()                                # not marked: the Fun Camera's mirror, a recipient's verify_claim door
    with answer_gate.owners_own_work():
        call()
    assert len(host) == 2 and host[-1] - began < 0.2


def test_with_the_yield_rule_off_only_the_answer_is_waited_for(host, monkeypatch):
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_BACKGROUND_YIELDS_TO_OWNER", "false")
    answer_gate.owner_frame_began()
    began = time.monotonic()
    with answer_gate.background_work():
        call()
    assert host[0] - began < 0.2
