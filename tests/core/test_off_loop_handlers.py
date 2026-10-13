"""NR166 R3b: a handler marked ``off_loop`` leaves the event loop free while its database work is slow.

Nearly every handler is an ``async def`` that never awaits: its queries ran on the app's event loop, and one slow
table walk stalled every other request the app was serving (the owner's node, 13 Oct: owner-app reads late by the
length of whatever read was in front of them). The dispatcher now runs a marked handler in a worker thread, on
that thread's own connection, together with the inspection floor's Off-limits read.

Pinned here, for every marked type: while its database work is slow the loop keeps running, and the work is not
on the loop's thread. The same measurement on an unmarked handler fails, so it can. A marked handler may not
await (read from its source), and one that does is refused rather than half run.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import sqlite3
import textwrap
import threading
import time

import pytest

import topos.core.handlers as hub
from topos.core import state
from topos.core.handlers import handle_control_plane_request
from topos.core.handlers.registry import HANDLERS, OFF_LOOP_MESSAGE_TYPES, run_whole
from topos.principal import OWNER_APP, RELAY_PRINCIPAL, Principal

OWNER = Principal(cls=OWNER_APP, channel="uds")
SLOW_SECONDS = 0.4
#: What each marked type is asked with: enough for its handler to reach the database.
PAYLOADS = {
    "list_database_tables": {},
    "get_table_count": {"table_name": "messages"},
    "get_table_rows": {"table_name": "messages", "limit": 5},
    "get_table_schema": {"table_name": "messages"},
    "signal_data_health": {},
    "connection_info": {"user_id": "an-invented-account"},
    "enrichment_progress": {"job_id": "an-invented-job"},
    "get_upgrade_status": {},
    "get_user_identity": {"dataset_id": "an-invented-dataset"},
    "put_user_identity": {"dataset_id": "an-invented-dataset", "display_name": "An Invented Name"},
}


@pytest.fixture
def slow_database(tmp_path, monkeypatch):
    """Every connection the handlers resolve takes SLOW_SECONDS to arrive; where it was asked for is recorded."""
    conn = sqlite3.connect(str(tmp_path / "node.sqlite"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE messages (id TEXT PRIMARY KEY, body TEXT)")
    conn.commit()
    asked_on: list[int] = []

    def resolve():
        asked_on.append(threading.get_ident())
        time.sleep(SLOW_SECONDS)
        return conn

    monkeypatch.setattr(hub, "get_db_connection", resolve)
    monkeypatch.setattr(state, "get_db_connection", resolve)
    try:
        yield asked_on
    finally:
        conn.close()


async def longest_gap_during(work) -> tuple[float, object]:
    """The longest the loop went without running while `work` was awaited, and what `work` returned."""
    gaps, done = [0.0], asyncio.Event()

    async def tick():
        last = time.monotonic()
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps[0], last = max(gaps[0], now - last), now

    ticker = asyncio.create_task(tick())
    await asyncio.sleep(0.03)
    try:
        result = await work
    finally:
        done.set()
        await ticker
    return gaps[0], result


def test_the_marked_types_are_the_ones_this_file_asks():
    assert set(PAYLOADS) == OFF_LOOP_MESSAGE_TYPES


@pytest.mark.parametrize("msg_type", sorted(PAYLOADS))
@pytest.mark.asyncio
async def test_the_loop_keeps_running_while_the_handlers_database_work_is_slow(msg_type, slow_database):
    message = {"id": "req-1", "type": msg_type, "payload": PAYLOADS[msg_type]}
    gap, response = await longest_gap_during(handle_control_plane_request(message, principal=OWNER))
    assert isinstance(response, dict) and response.get("id") == "req-1"
    assert slow_database, "the handler never reached the database: this proves nothing"
    assert threading.get_ident() not in slow_database        # none of it on the loop's thread
    assert gap < SLOW_SECONDS / 2, f"the loop stopped for {gap:.2f}s"


@pytest.mark.asyncio
async def test_the_inspection_floors_read_goes_with_the_handler(slow_database):
    """For a relayed caller the floor reads Off-limits state before the handler runs: that read too."""
    message = {"id": "req-2", "type": "list_database_tables", "payload": {}}
    gap, response = await longest_gap_during(handle_control_plane_request(message, principal=RELAY_PRINCIPAL))
    assert isinstance(response, dict) and response.get("id") == "req-2"
    assert len(slow_database) >= 2 and threading.get_ident() not in slow_database   # the floor's, then the handler's
    assert gap < SLOW_SECONDS / 2, f"the loop stopped for {gap:.2f}s"


@pytest.mark.asyncio
async def test_the_same_measurement_fails_for_a_handler_left_on_the_loop(slow_database, monkeypatch):
    async def on_the_loop(message):
        return {"id": message["id"], "status": "ok", "payload": bool(hub.get_db_connection())}

    monkeypatch.setitem(HANDLERS, "nr166_left_on_the_loop", on_the_loop)
    message = {"id": "req-3", "type": "nr166_left_on_the_loop", "payload": {}}
    gap, response = await longest_gap_during(handle_control_plane_request(message, principal=OWNER))
    assert response == {"id": "req-3", "status": "ok", "payload": True}
    assert slow_database == [threading.get_ident()] and gap >= SLOW_SECONDS * 0.9


@pytest.mark.asyncio
async def test_the_principal_reaches_the_worker_thread(monkeypatch):
    from topos.principal import current_principal

    seen = {}

    async def who(message):
        seen["principal"], seen["thread"] = current_principal(), threading.get_ident()
        return {"id": message["id"], "status": "ok"}

    monkeypatch.setitem(HANDLERS, "nr166_who", who)
    monkeypatch.setattr("topos.core.handlers.registry.OFF_LOOP_MESSAGE_TYPES", OFF_LOOP_MESSAGE_TYPES | {"nr166_who"})
    assert await handle_control_plane_request({"id": "req-4", "type": "nr166_who"}, principal=OWNER) == {
        "id": "req-4", "status": "ok"}
    assert seen["principal"] == OWNER and seen["thread"] != threading.get_ident()


def _awaits_in_own_body(handler) -> list[int]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
    function = next(node for node in ast.walk(tree) if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)))
    found = []

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, (ast.Await, ast.AsyncFor, ast.AsyncWith)):
                found.append(child.lineno)
            walk(child)
    walk(function)
    return found


@pytest.mark.parametrize("msg_type", sorted(PAYLOADS))
def test_a_marked_handler_never_awaits(msg_type):
    assert _awaits_in_own_body(HANDLERS[msg_type]) == []


def test_a_handler_that_awaits_is_refused_not_half_run():
    ran = []

    async def awaits(message):
        ran.append("before")
        await asyncio.sleep(0)
        ran.append("after")

    with pytest.raises(RuntimeError, match="cannot be marked off_loop"):
        run_whole(awaits, {"id": "req-5"})
    assert ran == ["before"]


def test_a_handler_that_never_awaits_returns_what_it_returned_and_raises_what_it_raised():
    async def answers(message):
        return {"id": message["id"]}

    async def fails(message):
        raise ValueError("as raised")

    assert run_whole(answers, {"id": "req-6"}) == {"id": "req-6"}
    with pytest.raises(ValueError, match="as raised"):
        run_whole(fails, {"id": "req-7"})


@pytest.mark.asyncio
async def test_disk_status_reads_on_a_worker_thread_with_that_threads_own_connection(slow_database, monkeypatch):
    """`get_disk_status` already ran its work in a worker thread, but on a connection it had resolved on the
    loop and handed across. It now resolves the connection in the worker. (The work itself asks the model host
    for its tag list, so it is a stand-in here: no model host is called.)"""
    from topos.api import disk_space_config

    seen = {}

    def status(conn):
        seen["thread"], seen["conn"] = threading.get_ident(), conn
        return {"free_bytes": 1}

    monkeypatch.setattr(disk_space_config, "_status_sync", status)
    message = {"id": "req-8", "type": "get_disk_status", "payload": {}}
    gap, response = await longest_gap_during(handle_control_plane_request(message, principal=OWNER))
    assert response == {"id": "req-8", "status": "ok", "payload": {"status": "ok", "free_bytes": 1}}
    assert slow_database == [seen["thread"]] and seen["thread"] != threading.get_ident()   # resolved where it ran
    assert seen["conn"] is not None and gap < SLOW_SECONDS / 2
