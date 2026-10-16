"""NR166: the owner's own chat and queries are not made to wait, are not shed, and send what they sent.

This branch touches code an owner's own request runs through: the shared local-model clients
(`engine/backends/ollama.py`, `services/llm/openai.py`), the relay client and the handler dispatcher. Its changes
there are for a recipient's request and for background work. This file pins the other side: what an owner's own
request must still get.

- A generation the owner asks for goes to the model host at once while a share's answer job is active, with the
  same request as when none is; only a call inside background work waits.
- An owner's query, generation, chat write or ingestion push is handled however long it waited for a handler slot
  and whatever happened to the socket it came on: the relay client sheds only the share doors' four types.
- No query, generation or chat type runs off the app's loop by this branch's mark: the ten marked types are reads
  of tables and settings.

No model is called: the host is a stub.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from tests.services.test_ollama_generate_failure_logging import _FakeAdapter, _patch_post, _Resp
from tests.topos.test_nr166_inbound_age import Node, bounds
from topos.control_plane_client import INBOUND_AGE_BOUND_SECONDS
from topos.core.handlers.registry import OFF_LOOP_MESSAGE_TYPES
from topos.permissions_v2 import answer_gate
from topos.services.llm import openai as llm_openai

#: What an owner asks for in their own app: queries, generations, chat, and what they send in.
OWNERS_OWN = ("query", "query_live", "llm_generation", "tools_retrieve", "upsert_home_chat_session",
              "delete_home_chat_session", "home_chat_memory", "store_message", "app_ingest", "start_ingestion",
              "verify_claim", "aggregate")


@pytest.fixture(autouse=True)
def no_answer_left_active():
    yield
    while answer_gate.active():
        answer_gate.delta(-1)


def host(monkeypatch) -> list:
    """The local model host behind the owner's chat client, stubbed: when each request reached it, and its body."""
    reached = []

    async def post(url, body):
        reached.append((time.monotonic(), url, body))
        return _Resp(200, {"response": "an invented reply", "done": True})

    _patch_post(monkeypatch, post)
    monkeypatch.setattr(llm_openai, "_stream_ollama_adapter", lambda _base: _FakeAdapter())
    return reached


PROMPT = {"prompt": "an invented question about an invented week", "model": "an-invented-model", "max_tokens": 64}


@pytest.mark.asyncio
async def test_the_owners_generation_is_not_delayed_by_an_answer_job_and_sends_the_same_request(monkeypatch):
    reached = host(monkeypatch)
    quiet = await llm_openai._ollama_generate(dict(PROMPT))            # no answer job anywhere
    answer_gate.delta(1)                                                # a share's answer job is queued or running
    started = time.monotonic()
    busy = await llm_openai._ollama_generate(dict(PROMPT))             # the owner asks again, as before
    assert time.monotonic() - started < 0.5 and reached[1][0] - started < 0.5
    assert reached[1][1:] == reached[0][1:]                             # the same URL and the same body
    assert {k: v for k, v in busy.items() if "ms" not in k and "timing" not in k} == \
           {k: v for k, v in quiet.items() if "ms" not in k and "timing" not in k}


@pytest.mark.asyncio
async def test_only_background_work_waits_at_that_client(monkeypatch):
    monkeypatch.setattr(answer_gate, "POLL_SECONDS", 0.01)
    reached = host(monkeypatch)
    answer_gate.delta(1)
    asyncio.get_running_loop().call_later(0.25, answer_gate.delta, -1)
    started = time.monotonic()
    with answer_gate.background_work():
        await llm_openai._ollama_generate(dict(PROMPT))
    assert reached[0][0] - started >= 0.2                               # it waited for the answer job to end
    assert reached[0][2] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", OWNERS_OWN)
async def test_the_owners_own_request_is_handled_however_long_it_waited(kind, monkeypatch):
    bounds(monkeypatch, 0.05)                    # the share doors' bounds, shrunk: none of these has one
    node = Node(monkeypatch)
    await node.receive("held", "holds_the_slot")
    await node.receive("the-owners", kind)
    await asyncio.sleep(0.2)
    await node.drain()
    assert node.handled == ["held", "the-owners"] and node.client.inbound_shed_counts() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", OWNERS_OWN)
async def test_the_owners_own_request_is_handled_when_its_socket_closed_while_it_waited(kind, monkeypatch):
    node = Node(monkeypatch)
    await node.receive("held", "holds_the_slot")
    await node.receive("the-owners", kind)
    node.connect()                               # the connection it came on is gone
    await node.drain()
    assert node.handled == ["held", "the-owners"] and node.client.inbound_shed_counts() == {}


def test_what_this_branch_sheds_and_what_it_moves_off_the_loop():
    assert set(INBOUND_AGE_BOUND_SECONDS) == {
        "permissions_v2_message_search", "permissions_v2_message_search_batch", "permissions_v2_answer_submit",
        "permissions_v2_answer_fetch"}
    assert not set(OWNERS_OWN) & set(INBOUND_AGE_BOUND_SECONDS)
    assert not set(OWNERS_OWN) & OFF_LOOP_MESSAGE_TYPES
    assert OFF_LOOP_MESSAGE_TYPES == {
        "list_database_tables", "get_table_count", "get_table_rows", "get_table_schema", "signal_data_health",
        "enrichment_progress", "get_upgrade_status", "get_user_identity", "put_user_identity", "connection_info",
        # Lane 172: reads every JSONL file under the ingestion directory to count its lines.
        "list_jsonl_files",
        # HT lane: the owner's home-chat timing rows. Written after the turn has ended, so no owner waits on them,
        # and read back as aggregates; off the loop so a write that waits for the gate never stalls it.
        "record_home_chat_turn_timings", "get_home_chat_turn_timings_summary"}
