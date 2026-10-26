"""Kept questions Wave 2 (lane OFF2N; scope §6.2 item 4): the catch-up lanes check the answer gate before every batch.

With the control plane delivering kept questions back to back ("Others first"), a catch-up batch must wait between
answers, and under the yield rule for the owner's own frames, as the node's other background work does. Pinned here,
for the embeddings catch-up, the AI-tool catch-up (each job batch and each chunk) and the refresh loop's slices:

- a batch starts only once the owner frame in flight has ended (yield rule on), or the share answer in hand has
  (``ANSWER_HOLDS_BACKGROUND`` on);
- with both off, or under "Share evenly", it starts at once;
- the owner's own work and a thread holding the write gate never wait;
- the refresh loop waits outside its own lock: the daemon sweep's observation goes on meanwhile.

No model is called: the model steps are stubbed and record when they start. Every id and text here is invented.
"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest

from topos.permissions_v2 import answer_gate, away_config
from topos.storage.db.migrations import apply_all_migrations

HOLDS = "TOPOS_PERMISSIONS_V2_ANSWER_HOLDS_BACKGROUND"
YIELDS = "TOPOS_PERMISSIONS_V2_BACKGROUND_YIELDS_TO_OWNER"
EVEN = {"v": 1, "while_using": "even", "others_when_idle": False}


@pytest.fixture(autouse=True)
def quiet_gate(monkeypatch):
    monkeypatch.setattr(answer_gate, "POLL_SECONDS", 0.01)
    monkeypatch.setenv(HOLDS, "false")
    monkeypatch.delenv(YIELDS, raising=False)
    away_config.apply(away_config.DEFAULT)
    yield
    while answer_gate.owner_frames():
        answer_gate.owner_frame_ended()
    while answer_gate.active():
        answer_gate.delta(-1)
    away_config.apply(away_config.DEFAULT)


def owner_frame_for(seconds: float) -> list:
    """An owner frame in flight for `seconds`; the list gets the time it ended."""
    ended: list = []
    answer_gate.owner_frame_began()

    def end():
        ended.append(time.monotonic())
        answer_gate.owner_frame_ended()
    threading.Timer(seconds, end).start()
    return ended


# -- the embeddings catch-up ----------------------------------------------------------------------------------------------

@pytest.fixture
def local_sync(tmp_path, monkeypatch):
    from topos.storage.canonical.conversations_tables import ConversationsTablesManager
    conn = sqlite3.connect(str(tmp_path / "catchup.db"), check_same_thread=False)
    apply_all_migrations(conn)
    ConversationsTablesManager(conn).ensure_tables()
    for number in range(3):
        conn.execute("INSERT INTO conversation_messages (message_id, conversation_id, dataset_id, sender_type, content, "
                     "event_at, source_id) VALUES (?, 'thread-1', 'owner:default', 'user', ?, ?, 'imessage')",
                     (f"m{number}", f"synthetic message {number}", f"2026-09-0{number + 1}T12:00:00Z"))
    conn.commit()
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    monkeypatch.delenv("TOPOS_LOCAL_SYNC_CATCHUP", raising=False)
    started = []

    async def run_canonical(self, records, job_names, **_kwargs):
        started.append(time.monotonic())
        return {"errors": ["an invented stop before any model"]}
    monkeypatch.setattr("topos.enrichment.orchestrator.EnrichmentOrchestrator.run_canonical", run_canonical)
    yield conn, started
    conn.close()


def embeddings_batch(conn):
    import json
    from topos.ingestion.embeddings_catchup import enqueue_catchup, execute_catchup
    queued = enqueue_catchup(conn, start="local_sync_embeddings")
    body = json.loads(conn.execute("SELECT payload_json FROM pipeline_jobs WHERE job_id=?", (queued[0],)).fetchone()[0])
    return execute_catchup(body)


def test_an_embeddings_batch_starts_only_once_the_owner_frame_has_ended(local_sync):
    conn, started = local_sync
    ended = owner_frame_for(0.3)
    asyncio.run(embeddings_batch(conn))
    assert len(started) == 1 and started[0] >= ended[0]


def test_an_embeddings_batch_waits_for_an_answer_job_with_the_hold_on(local_sync, monkeypatch):
    conn, started = local_sync
    monkeypatch.setenv(HOLDS, "true")
    answer_gate.delta(1)
    ended = []
    threading.Timer(0.3, lambda: (ended.append(time.monotonic()), answer_gate.delta(-1))).start()
    asyncio.run(embeddings_batch(conn))
    assert started[0] >= ended[0]


@pytest.mark.parametrize("how", ["both off", "share evenly"])
def test_an_embeddings_batch_starts_at_once_with_the_rule_off(local_sync, monkeypatch, how):
    conn, started = local_sync
    if how == "share evenly":
        away_config.apply(EVEN)
    else:
        monkeypatch.setenv(YIELDS, "false")
    answer_gate.owner_frame_began()
    answer_gate.delta(1)                                             # HOLDS off: the answers' hold does not apply
    began = time.monotonic()
    asyncio.run(embeddings_batch(conn))
    assert started[0] - began < 0.2


# -- the AI-tool catch-up -------------------------------------------------------------------------------------------------

@pytest.fixture
def coding(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "coding.db"), check_same_thread=False)
    apply_all_migrations(conn)
    conn.execute("CREATE TABLE ai_chat_messages(message_id TEXT PRIMARY KEY,conversation_id TEXT,sender_type TEXT,"
                 "source_id TEXT,content TEXT,share_excluded INTEGER)")
    for rid in ("row-a", "row-b"):
        conn.execute("INSERT INTO ai_chat_messages VALUES (?, 'chat', 'human', 'codex_sessions', 'synthetic text', 0)",
                     (rid,))
    conn.commit()
    monkeypatch.setattr("topos.ingestion.ai_tool_sync.source_state",
                        lambda conn, sid: {"enabled": sid == "codex_sessions", "paused": False})
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    started = []

    async def canonical(self, records, job_names, **kwargs):
        started.append(("batch", time.monotonic()))
        return {"errors": []}

    async def pipeline(**kwargs):
        started.append(("chunk", time.monotonic()))
        return {"signal_derivation": {"errors": []}, "privacy_disclosure_layer": {"errors": []}}
    monkeypatch.setattr("topos.enrichment.orchestrator.EnrichmentOrchestrator.run_canonical", canonical)
    monkeypatch.setattr("topos.ingestion.canonical_pipeline.run_post_canonical_pipeline", pipeline)
    yield conn, started
    conn.close()


def test_each_ai_tool_batch_and_chunk_starts_only_once_the_owner_frame_has_ended(coding):
    from topos.ingestion.ai_tool_catchup import execute_catchup
    conn, started = coding
    ended = owner_frame_for(0.3)
    result = asyncio.run(execute_catchup({"source_id": "codex_sessions",
                                          "missing": {"entities": ["row-a"], "ai_tool_pipeline": ["row-a", "row-b"]}}))
    assert result["status"] == "ok", result
    assert [kind for kind, _at in started] == ["batch", "chunk"] and all(at >= ended[0] for _kind, at in started)


def test_an_ai_tool_chunk_waits_again_for_a_frame_that_came_meanwhile(coding, monkeypatch):
    from topos.ingestion.ai_tool_catchup import execute_catchup
    conn, started = coding

    async def canonical(self, records, job_names, **kwargs):
        started.append(("batch", time.monotonic()))
        owner_frame_for(0.3)                                         # the owner starts chatting during the batch
        return {"errors": []}
    monkeypatch.setattr("topos.enrichment.orchestrator.EnrichmentOrchestrator.run_canonical", canonical)
    asyncio.run(execute_catchup({"source_id": "codex_sessions",
                                 "missing": {"entities": ["row-a"], "ai_tool_pipeline": ["row-a"]}}))
    assert [kind for kind, _at in started] == ["batch", "chunk"] and started[1][1] - started[0][1] >= 0.25


# -- the exemptions -------------------------------------------------------------------------------------------------------

def test_the_owners_own_work_and_a_held_write_gate_never_wait():
    from topos.storage.db.write_gate import db_write_lock
    answer_gate.owner_frame_began()
    with answer_gate.owners_own_work():
        assert asyncio.run(answer_gate.hold_background_batch()) == 0.0
    with db_write_lock():
        assert asyncio.run(answer_gate.hold_background_batch()) == 0.0
    assert 0.2 <= asyncio.run(answer_gate.hold_background_batch(limit=0.2)) < 1.0   # and otherwise, bounded


def test_a_batches_wait_ends_when_the_node_stops(monkeypatch):
    from topos import runtime_shutdown
    run = runtime_shutdown.RuntimeGeneration(20_242)
    monkeypatch.setattr(runtime_shutdown, "current_generation", lambda: run)
    answer_gate.owner_frame_began()
    threading.Timer(0.2, run.request_stop, args=("an invented stop",)).start()
    assert 0.15 <= asyncio.run(answer_gate.hold_background_batch(limit=6.0)) < 1.5


# -- the refresh loop's slices ------------------------------------------------------------------------------------------

def test_a_slice_starts_only_once_the_owner_frame_has_ended_and_the_loop_lock_stays_free(tmp_path):
    from tests.permissions_v2.test_refresh_loop import Clock, daytime
    from tests.permissions_v2.test_refresh_loop_slices import NEWEST, MIDDLE, SliceWorker, aged, windowed
    T = daytime()
    clock, worker = Clock(T), SliceWorker(aged(T, [*NEWEST, *MIDDLE]))
    loop = windowed(tmp_path, clock, worker, window=60 * 86400)
    loop.run_catchup()                                               # the backlog's first slice, no frame in flight
    assert len(worker.runs) == 1
    worker.end()
    ended = owner_frame_for(0.4)
    lock_free = []
    probe = threading.Timer(0.15, lambda: lock_free.append(loop._lock.acquire(timeout=0.1) and (loop._lock.release()
                                                                                                or True)))
    probe.start()
    began = time.monotonic()
    loop.run_catchup()                                               # the next slice: waits for the frame
    probe.join()
    assert len(worker.runs) == 2 and time.monotonic() - began >= 0.35 and ended
    assert lock_free == [True]                                       # observe could take the lock meanwhile


def test_a_tick_that_would_start_nothing_does_not_wait(tmp_path):
    from tests.permissions_v2.test_refresh_loop import Clock, daytime
    from tests.permissions_v2.test_refresh_loop_slices import NEWEST, SliceWorker, aged, windowed
    T = daytime()
    clock, worker = Clock(T), SliceWorker(aged(T, NEWEST))
    loop = windowed(tmp_path, clock, worker, window=20 * 86400)
    loop.run_catchup()                                               # a slice runs
    answer_gate.owner_frame_began()                                  # never ends
    began = time.monotonic()
    assert loop.run_catchup() is None and time.monotonic() - began < 0.2   # its slice still runs: nothing to start
