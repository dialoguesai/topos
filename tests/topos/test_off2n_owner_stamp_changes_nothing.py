"""Kept questions Wave 2 (lane OFF2N; ``lanes/OFF2_CONTRACT.md`` §E): the ``owner_app`` stamp the control plane will put
on the owner's ``llm_generation`` changes nothing on the node but its scheduling.

The same ``llm_generation`` frame, once with a verified ``owner_app`` stamp (``client_id`` ``topos_home_chat``, the
owner as ``acting_user``) and once unstamped, through the client and the relay dispatcher, with the model host stubbed:

- the reply, the provider and model the host was asked for, the request body it received, and the log lines (times
  and durations masked) are the same; so are the refusals (an unusable context size, a model host that fails);
- what differs is the lane class (a kept slot for the stamped frame, a shared one for the unstamped) and the yield
  (the stamped frame counts as the owner's in flight while it runs, so background work waits for it).

Keys are made at run time; every id and text here is invented. No model is called.
"""
from __future__ import annotations

import asyncio
import logging
import re

import pytest

from topos.core.handlers import dispatch_relay_message
from topos.permissions_v2 import answer_gate, away_config
from topos.permissions_v2.owner_lane import GENERAL, RESERVED
from topos.services.llm import openai as llm_openai
from tests.services.test_ollama_generate_failure_logging import _FakeAdapter, _patch_post, _patch_stream, _Resp
from tests.topos.test_nr183_owner_lane import Node, OWNER, stamped
from tests.topos.test_nr183_owner_lane import pinned_key  # noqa: F401 -- autouse: the stamp key, no switch set

PAYLOAD = {"provider": "ollama", "model": "an-invented-model", "prompt": "an invented question about an invented week",
           "max_tokens": 64, "stream": False}
_TIMES = re.compile(r"(elapsed_s|wall_s|_ms|seconds?)=?[ =]?[0-9.]+")


@pytest.fixture(autouse=True)
def owner_first():
    away_config.apply(away_config.DEFAULT)
    yield
    while answer_gate.owner_frames():
        answer_gate.owner_frame_ended()


def generation(request_id="gen-1", **payload):
    return {"id": request_id, "type": "llm_generation", "payload": {**PAYLOAD, **payload}}


def as_the_owner(message):
    return stamped(message, cls="owner_app", acting=OWNER)


class Host:
    """The local model host behind the owner's chat client, stubbed: what it was asked, and the owner frames in flight
    and the yield rule's answer at that moment."""

    def __init__(self, monkeypatch, *, fails=False, stream=False):
        self.asked, self.seen = [], []

        async def post(url, body):
            self.asked.append((url, body))
            self.seen.append((answer_gate.owner_frames(), answer_gate._owner_holds()))
            if fails:
                return _Resp(500, {"error": "an invented host failure"})
            return _Resp(200, {"response": "an invented reply", "done": True})

        class Streamed:
            status_code = 200

            async def aiter_lines(self):
                for line in ('{"model": "an-invented-model", "response": "an invented "}',
                             '{"model": "an-invented-model", "response": "reply", "done": true}'):
                    yield line

        async def enter():
            self.asked.append(("stream", None))
            self.seen.append((answer_gate.owner_frames(), answer_gate._owner_holds()))
            return Streamed()
        if stream:
            _patch_stream(monkeypatch, enter)
        else:
            _patch_post(monkeypatch, post)
        monkeypatch.setattr(llm_openai, "_stream_ollama_adapter", lambda _base: _FakeAdapter())


def masked(records) -> list:
    return [(record.name, record.levelno, _TIMES.sub(r"\1=<t>", record.getMessage())) for record in records
            if record.name.startswith("topos")]


def comparable(reply):
    """A reply without what is measured in time (a generation reports its own durations)."""
    if not isinstance(reply, dict):
        return reply

    def clean(value):
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()
                    if not any(part in key for part in ("_ms", "duration", "elapsed", "timing", "wall"))}
        if isinstance(value, list):
            return [clean(item) for item in value]
        return value
    return clean(reply)


async def through_the_client(monkeypatch, message, caplog):
    node = Node(monkeypatch, limit=3, reserved=1)
    node.client.handler = dispatch_relay_message
    caplog.clear()
    with caplog.at_level(logging.INFO):
        await node.receive(message)
        for _ in range(300):
            if message["id"] in node.replies():
                break
            await asyncio.sleep(0.01)
        await node.settle()
    return node.replies()[message["id"]], node.pool, masked(caplog.records)


@pytest.mark.parametrize("stream", [False, True], ids=["whole", "streamed"])
@pytest.mark.asyncio
async def test_the_stamp_changes_the_lane_and_the_yield_and_nothing_else(monkeypatch, caplog, stream):
    host = Host(monkeypatch, stream=stream)
    plain_reply, plain_pool, plain_logs = await through_the_client(monkeypatch, generation(stream=stream), caplog)
    owner_reply, owner_pool, owner_logs = await through_the_client(
        monkeypatch, as_the_owner(generation(stream=stream)), caplog)

    assert plain_reply["status"] == "ok" and comparable(owner_reply) == comparable(plain_reply)
    assert plain_reply["payload"]["model"] == "an-invented-model"
    assert len(host.asked) == 2 and host.asked[1] == host.asked[0]     # the same URL and body: model, prompt
    assert owner_logs == plain_logs and plain_logs                     # the same lines, in the same order
    # What differs: the lane class and the yield.
    assert (plain_pool.granted[(False, GENERAL)], plain_pool.granted[(True, RESERVED)]) == (1, 0)
    assert (owner_pool.granted[(False, GENERAL)], owner_pool.granted[(True, RESERVED)]) == (0, 1)
    assert host.seen[0][0] == 0 and host.seen[1][0] == 1               # in flight as the owner's while it ran
    with answer_gate.background_work():                                # and background work would have waited
        answer_gate.owner_frame_began()
        assert answer_gate._owner_holds() is True
        answer_gate.owner_frame_ended()


@pytest.mark.asyncio
async def test_the_same_refusals_stamped_or_not(monkeypatch, caplog):
    Host(monkeypatch, fails=True)
    plain, _pool, plain_logs = await through_the_client(monkeypatch, generation("gen-2"), caplog)
    owner, _pool, owner_logs = await through_the_client(monkeypatch, as_the_owner(generation("gen-2")), caplog)
    assert plain["status"] == "error" and comparable(owner) == comparable(plain) and owner_logs == plain_logs
    unusable = {"num_ctx": "an invented size"}
    plain, _pool, _logs = await through_the_client(monkeypatch, generation("gen-3", **unusable), caplog)
    owner, _pool, _logs = await through_the_client(monkeypatch, as_the_owner(generation("gen-3", **unusable)), caplog)
    assert plain["status"] == "error" and owner == plain


@pytest.mark.asyncio
async def test_with_the_lane_off_the_stamped_frame_is_not_even_told_apart(monkeypatch, caplog):
    Host(monkeypatch)
    node = Node(monkeypatch, limit=3, lane=False)
    node.client.handler = dispatch_relay_message
    seen = []
    real = answer_gate.owner_frame_began
    monkeypatch.setattr(answer_gate, "owner_frame_began", lambda: (seen.append(1), real()))
    await node.receive(as_the_owner(generation("gen-4")))
    for _ in range(300):
        if "gen-4" in node.replies():
            break
        await asyncio.sleep(0.01)
    await node.settle()
    assert node.replies()["gen-4"]["status"] == "ok" and seen == []
