"""Subprocess entity-graph rebuild (GIL-starvation fix, 2026-08-08).

A forced rebuild starved the node's event loop for ~103s with ZERO write-gate
warnings: the in-process compute (goal embeddings, role map, Louvain) held the
GIL, so the loop thread never ran — lock discipline can't fix scheduling.
``run_graph_rebuild`` therefore ships file-backed rebuilds to a child process;
these tests pin the dispatch rules, the child's verdict protocol, the advisory
single-flight lock, and — the point of it all — that the event loop keeps
serving while a rebuild runs.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading

import pytest

from topos.features.entities import rebuild_subprocess
from topos.features.entities.rebuild_subprocess import (
    GraphRebuildSubprocessError,
    _database_file,
    _parse_verdict,
    rebuild_in_subprocess,
    run_graph_rebuild,
)
from topos.features.entities.resolver import EntityResolver
from topos.storage.db.migrations import apply_all_migrations
from topos.storage.db.write_gate import WriteGateDeferred


def _seed(conn: sqlite3.Connection) -> None:
    """Two entities co-mentioned on one record — enough for one edge."""
    r = EntityResolver(conn)
    a = r._create_entity("Ada", "person")
    b = r._create_entity("Bram", "person")
    for entity_id in (a, b):
        conn.execute(
            """
            INSERT INTO entity_mentions
                (mention_id, entity_id, record_id, source_id, canonical_table,
                 surface_text, confidence, event_at, created_at)
            VALUES (?, ?, 'rec1', 'imessage', 'conversation_messages',
                    'x', 0.9, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')
            """,
            (f"m_{entity_id}", entity_id),
        )
    conn.commit()


@pytest.fixture()
def file_db(tmp_path):
    path = str(tmp_path / "graph.db")
    # check_same_thread=False matches production connections
    # (core.state.get_db_connection) and lets the async test use the conn from
    # an asyncio.to_thread worker.
    conn = sqlite3.connect(path, check_same_thread=False)
    apply_all_migrations(conn)
    _seed(conn)
    yield path, conn
    conn.close()


def test_database_file_resolution(file_db):
    path, conn = file_db
    assert _database_file(conn) == path
    mem = sqlite3.connect(":memory:")
    assert _database_file(mem) is None


def test_dispatch_uses_subprocess_for_file_db(file_db, monkeypatch):
    path, conn = file_db
    calls = []
    monkeypatch.setattr(
        rebuild_subprocess,
        "rebuild_in_subprocess",
        lambda db_path, **kw: calls.append(db_path) or {"edges_after": 1},
    )
    report = run_graph_rebuild(conn)
    assert calls == [path]
    assert report == {"edges_after": 1}


def test_dispatch_runs_in_process_for_memory_db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    _seed(conn)
    monkeypatch.setattr(
        rebuild_subprocess,
        "rebuild_in_subprocess",
        lambda *a, **kw: pytest.fail("in-memory database must rebuild in-process"),
    )
    report = run_graph_rebuild(conn)
    assert report["co_occurrence"] >= 1


def test_dispatch_kill_switch_forces_in_process(file_db, monkeypatch):
    _path, conn = file_db
    monkeypatch.setenv("TOPOS_GRAPH_REBUILD_SUBPROCESS", "off")
    monkeypatch.setattr(
        rebuild_subprocess,
        "rebuild_in_subprocess",
        lambda *a, **kw: pytest.fail("kill-switch must keep the rebuild in-process"),
    )
    report = run_graph_rebuild(conn)
    assert report["co_occurrence"] >= 1


def test_parse_verdict_ignores_stray_stdout():
    out = "loading model...\n{'not': 'json'}\n" + json.dumps(
        {"status": "ok", "report": {"edges_after": 3}}
    )
    assert _parse_verdict(out) == {"status": "ok", "report": {"edges_after": 3}}
    assert _parse_verdict("no verdict here") is None
    assert _parse_verdict(None) is None


def test_subprocess_rebuild_end_to_end(file_db):
    """Real child process: rebuilds the graph on its own connection and the
    parent sees the result through SQLite, not shared memory."""
    path, conn = file_db
    conn.execute("DELETE FROM entity_edges")
    conn.commit()

    report = rebuild_in_subprocess(path, timeout_s=180.0)

    assert report["co_occurrence"] >= 1
    assert report["edges_after"] >= 1
    active = conn.execute(
        "SELECT COUNT(*) FROM entity_edges WHERE edge_type='co_occurrence' AND valid_to IS NULL"
    ).fetchone()[0]
    assert active >= 1, "parent connection must see the child's rebuilt edges"


def test_subprocess_rebuild_defers_when_lock_held(file_db):
    """Second rebuild steps aside (WriteGateDeferred) instead of interleaving."""
    fcntl = pytest.importorskip("fcntl")
    path, _conn = file_db
    with open(path + ".rebuild.lock", "a+") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(WriteGateDeferred):
            rebuild_in_subprocess(path, timeout_s=180.0)


def test_subprocess_missing_database_is_an_error(tmp_path):
    with pytest.raises(GraphRebuildSubprocessError):
        rebuild_in_subprocess(str(tmp_path / "nope.db"), timeout_s=180.0)


@pytest.mark.asyncio
async def test_event_loop_stays_responsive_during_subprocess_rebuild(file_db):
    """The regression this whole module exists for: on 2026-08-08 a rebuild in
    asyncio.to_thread served ZERO loop iterations for ~103s (queued
    healthchecks flushed in one burst when it finished). With the compute in a
    child process the parent loop must keep ticking — sampled here the same
    way the live healthcheck poll caught the outage."""
    path, conn = file_db
    done = asyncio.Event()
    gaps = []

    async def tick():
        loop = asyncio.get_running_loop()
        last = loop.time()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = loop.time()
            gaps.append(now - last)
            last = now

    sampler = asyncio.create_task(tick())
    worker_thread = []

    def _rebuild():
        worker_thread.append(threading.current_thread())
        return run_graph_rebuild(conn)

    try:
        report = await asyncio.to_thread(_rebuild)
    finally:
        done.set()
        await sampler

    assert report["edges_after"] >= 1
    assert worker_thread and worker_thread[0] is not threading.main_thread()
    # The 2026-08-08 failure was a ~103s gap; a healthy loop ticks every ~10ms.
    # 0.5s leaves room for CI scheduling jitter while still catching any
    # return of the convoy by two orders of magnitude.
    assert max(gaps) < 0.5, f"event loop starved during rebuild: worst gap {max(gaps):.2f}s"
