"""BL-32 (T4 F5): after a restart, a node's shares serve again within seconds, not after the refresh loop's minute.

What the rig saw: a node restarted, its shares were refused for about a minute, and the log said ``message search
index stale (basis)``. The field that moved is the protection clock's generation, which every index basis binds: the
graph reconcile that runs at every start inserted the owner's self entity the first time it ran there (the rig turns
the graph refresh off, so the graph was first built at a restart), and an insert of a self entity advances the clock.
That is a protection event, and the index must not survive it; what was wrong is that nothing rebuilt it until the
refresh loop started, 60 s after the node (``refresh_loop.start_at_startup``). The start-up restore now rebuilds every
share whose index is missing or stale from the start, until the loop takes over.

Here the protection write is an owner-only mark on a record no share releases, which advances the same clock; the
node is N2's fresh in-process node, bound through the real bind, its share made through the real signed activation,
and read through the real search door. The app's own start-up call runs with its default delay of 60 s.

protects: the first read after a restart serves within seconds, whether the protection write landed while the node
was down or during its start; an index still current is not rebuilt; the restore switch still turns it off.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import closing

import pytest

from tests.permissions_v2.message_search_harness import as_principal
from tests.permissions_v2.test_message_search_refusals import Socket
from tests.permissions_v2.test_self_bind import (ACTOR, CP, CP_KID, FRONTEND, OWNER, node,  # noqa: F401
                                                 restart, share_with_a_recipient)
from topos.permissions_v2 import refresh_loop, search_transport
from topos.permissions_v2 import runtime as runtime_module
from topos.permissions_v2.protection_clock import clock_state
from topos.principal import OWNER_APP

#: "Within a few seconds" (BL-32's test), with room for a loaded machine. Measured well under it (see the report).
WITHIN = 8.0
RECIPIENT_CLIENT = FRONTEND


async def read(node, policy, request_id: str) -> dict:
    """One recipient search, its envelope signed as the control plane signs it after re-syncing the share."""
    from topos.permissions_v2.signing import parse_envelope, request_digest, sign_envelope
    now = int(time.time())
    runtime = runtime_module.get_runtime()

    def authority():
        with runtime.protocol.ledger._transaction() as conn:
            runtime.protocol._sync_protection(conn)
        with as_principal(cls=OWNER_APP, channel="uds", acting_user=OWNER):
            return runtime.protocol.ledger.authority_snapshot(policy["binding"]["grant_id"], now=now)
    snapshot = await asyncio.to_thread(authority)
    intent = {"query": "roadmap deploy", "k": 5}
    envelope = sign_envelope(parse_envelope({
        **snapshot.model_dump(), "version": "topos-grantee-envelope/v2", "kid": CP_KID, "request_id": request_id,
        "request_type": "permissions.v2.search", "request_hash": request_digest("permissions.v2.search", intent),
        "issued_at": now, "expires_at": now + 100}, signed=False), CP)
    message = node.stamped({"id": request_id, "type": search_transport.MESSAGE_TYPE,
                            "payload": {"envelope": envelope.model_dump(), "intent": intent}},
                           cls="third_party", client=RECIPIENT_CLIENT, acting=ACTOR)
    socket = Socket()
    await search_transport.dispatch_message_search(socket, message)
    [frame] = [json.loads(value) for value in socket.sent]
    return frame


def protection_write(node) -> None:
    """An owner-only mark on a record no share releases: it advances the protection clock every basis binds."""
    with closing(sqlite3.connect(node.canonical)) as conn:
        generation = clock_state(conn)[1]
        conn.execute("INSERT INTO owner_only_records (canonical_table, record_id, created_at, updated_at) "
                     "VALUES ('conversation_messages', 'imessage:99999', 't', 't')")
        conn.commit()
        assert clock_state(conn)[1] == generation + 1


async def first_served(node, policy, started: float, *, seconds: float = 20.0) -> float | None:
    attempt = 0
    while time.monotonic() - started < seconds:
        attempt += 1
        frame = await read(node, policy, f"bl32-read-{attempt}-{time.monotonic_ns()}")
        if frame["status"] == "ok":
            return time.monotonic() - started
        await asyncio.sleep(0.1)
    return None


@pytest.fixture()
def served_share(node, monkeypatch):
    async def make():
        from tests.permissions_v2 import test_self_bind
        monkeypatch.setattr(test_self_bind, "RECIPIENT_CLIENT", FRONTEND)
        proof, _ = await node.bind()
        policy = await share_with_a_recipient(node, proof, monkeypatch)
        assert (await read(node, policy, "bl32-before"))["status"] == "ok"
        restart(node)                             # the process stops: nothing loaded, nothing running
        return policy
    yield make
    refresh_loop.stop_startup()


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["while_the_node_was_down", "during_its_start"])
async def test_the_first_read_after_a_restart_serves_within_seconds(node, served_share, when):
    policy = await served_share()
    if when == "while_the_node_was_down":
        protection_write(node)
    started = time.monotonic()
    assert refresh_loop.start_at_startup()           # the app's own call, with its default 60 s delay
    if when == "during_its_start":
        await asyncio.sleep(0.5)                     # the start-up graph reconcile lands after the node started
        protection_write(node)
    served = await first_served(node, policy, started)
    print(f"BL-32 {when}: first read served {served:.2f} s after the start" if served is not None else "never")
    assert served is not None and served < WITHIN, served


@pytest.mark.asyncio
async def test_an_index_still_current_is_not_rebuilt_at_start(node, served_share):
    policy = await served_share()
    assert refresh_loop.start_at_startup()
    served = await first_served(node, policy, time.monotonic())
    assert served is not None and served < WITHIN
    await asyncio.sleep(refresh_loop.STARTUP_TICK + 0.5)    # a full round after the read
    assert list(runtime_module.get_runtime().index_rebuilds().results) == []


@pytest.mark.asyncio
async def test_the_restore_switch_still_turns_the_start_up_restore_off(node, served_share, monkeypatch):
    policy = await served_share()
    protection_write(node)
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_INDEX_RESTORE_ENABLED", "false")
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_ASSESSMENT_CATCHUP_ENABLED", "false")
    assert not refresh_loop.start_at_startup()
    assert (await read(node, policy, "bl32-off"))["status"] == "error"
    await asyncio.sleep(refresh_loop.STARTUP_TICK + 0.5)
    assert (await read(node, policy, "bl32-off-again"))["status"] == "error"
