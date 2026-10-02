"""The node rings the control plane when its protection state moves (owner decision 2, 1 Oct 2026).

  D1  the doorbell rings once at start and once per new revision, never twice for one; a read or send that
      fails rings again later; the frame carries nothing but a unique id and its type
  D2  the revision it reads is the committed one, and it moves when the protection clock does
  D3  the control plane's automatic re-sync is answered a status and nothing else: a mutation under its client
      id is refused before the protocol, a status under it is answered and signed as the owner's Sync is
  D4  it can be switched off, and it sends nothing without a control-plane connection

Every fixture is synthetic.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_node_protocol import mutation, protocol, status_request  # noqa: F401
from tests.permissions_v2.test_protocol_runtime import configured  # noqa: F401
from topos.core.handlers import handle_control_plane_request
from topos.permissions_v2 import protection_doorbell as doorbell
from topos.permissions_v2.canonical import PolicyError
from topos.principal import OWNER_APP, Principal


# -- D1 ------------------------------------------------------------------------------------------------------

def bell(revisions, *, sent=True):
    frames, reads = [], iter(revisions)

    def read():
        value = next(reads)
        if isinstance(value, Exception):
            raise value
        return value

    def send(message):
        if isinstance(sent, Exception):
            raise sent
        frames.append(message)
        return sent
    return doorbell.ProtectionDoorbell(read=read, send=send), frames


def test_D1_it_rings_once_at_start_and_once_per_new_revision():
    ring, frames = bell(["r1", "r1", "r2", "r2", "r1"])
    assert [ring.check() for _ in range(5)] == [True, False, True, False, True]
    assert len(frames) == 3 and len({frame["id"] for frame in frames}) == 3
    assert all(frame["type"] == doorbell.FRAME_TYPE and frame["payload"] == {} and set(frame) == {"id", "type", "payload"}
               for frame in frames)


def test_D1_a_failed_read_rings_nothing_and_a_failed_send_rings_again():
    ring, frames = bell([PolicyError("protection_clock_unavailable"), sqlite3.OperationalError("locked"), "r1", "r1"],
                        sent=False)
    assert [ring.check() for _ in range(4)] == [False, False, False, False]
    assert len(frames) == 2  # nothing marked as rung while the send did not go
    ring, frames = bell(["r1", "r1"], sent=RuntimeError("closed"))
    assert [ring.check(), ring.check()] == [False, False]


# -- D2 ------------------------------------------------------------------------------------------------------

def test_D2_the_revision_is_the_committed_one_and_moves_with_the_clock(protocol):
    node = protocol[0]
    database, owner_id = node.canonical_database, node.ledger.identity.owner_id
    before = doorbell.read_revision(database, owner_id)
    writer = sqlite3.connect(database)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE permissions_v2_protection_state SET generation=generation+1 WHERE singleton=1")
        assert doorbell.read_revision(database, owner_id) == before  # not committed yet
        writer.commit()
    finally:
        writer.close()
    assert doorbell.read_revision(database, owner_id) != before


# -- D3 ------------------------------------------------------------------------------------------------------

AUTO = Principal(cls=OWNER_APP, channel="cp_relay", client_id=doorbell.AUTO_RESYNC_CLIENT, acting_user="owner-1")


@pytest.mark.asyncio
async def test_D3_the_automatic_resync_can_never_mutate(monkeypatch):
    from topos.permissions_v2 import runtime
    monkeypatch.setattr(runtime, "get_runtime", lambda: pytest.fail("refused before the protocol"))
    response = await handle_control_plane_request(
        {"id": "req", "type": "permissions_v2_mutate", "payload": {"envelope": {}}}, principal=AUTO)
    assert (response["code"], response["error"]) == (403, "automation_status_only")


@pytest.mark.asyncio
async def test_D3_the_automatic_resync_is_answered_a_signed_status(configured, monkeypatch):
    from topos.core.handlers import permissions_v2 as handler
    from topos.permissions_v2 import runtime as runtime_module
    from topos.permissions_v2.runtime import load_runtime
    _, path, fixture = configured
    runtime = load_runtime(path, active_database=fixture[0].canonical_database)
    try:
        monkeypatch.setenv("TOPOS_PERMISSIONS_V2_ENABLED", "true")
        monkeypatch.setenv("TOPOS_PERMISSIONS_V2_CONFIG_PATH", str(path))
        monkeypatch.setattr(runtime_module, "_runtime", runtime)
        monkeypatch.setattr(handler, "time", SimpleNamespace(time=lambda: 1100))
        effective = (runtime.protocol, fixture[1], fixture[2], fixture[3])
        owner = Principal(cls=OWNER_APP, channel="cp_relay", acting_user="owner-1")
        applied = await handle_control_plane_request(
            {"id": "m", "type": "permissions_v2_mutate", "payload": {"envelope": mutation(effective).model_dump()}},
            principal=owner)
        assert applied["payload"]["ack"]["outcome"] == "applied"
        request = status_request(effective)
        response = await handle_control_plane_request(
            {"id": "s", "type": "permissions_v2_status", "payload": {"envelope": request.model_dump()}}, principal=AUTO)
        assert response["status"] == "ok" and response["payload"]["ack"]["state"]["grant_state"] == "active"
        wrong = Principal(cls=OWNER_APP, channel="cp_relay", client_id=doorbell.AUTO_RESYNC_CLIENT,
                          acting_user="another-owner")
        refused = await handle_control_plane_request(
            {"id": "w", "type": "permissions_v2_status", "payload": {"envelope": status_request(
                effective, request_id="status-2").model_dump()}}, principal=wrong)
        assert refused["error"] == "owner_binding"
    finally:
        runtime.close()


# -- D4 ------------------------------------------------------------------------------------------------------

def test_D4_it_can_be_switched_off(monkeypatch):
    import threading
    started = []
    monkeypatch.setattr(threading, "Thread", lambda **kw: started.append(kw) or SimpleNamespace(start=lambda: None))
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_AUTO_RESYNC", "off")
    assert doorbell.start_at_startup() is False and started == []
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_AUTO_RESYNC", "on")
    assert doorbell.start_at_startup() is True and started[0]["daemon"] is True


def test_D4_without_a_control_plane_connection_nothing_is_sent(monkeypatch):
    from topos.core import state as engine_state
    monkeypatch.setattr(engine_state, "control_plane_client", None, raising=False)
    assert doorbell.send_to_control_plane(doorbell.frame()) is False
    queued = []
    monkeypatch.setattr(engine_state, "control_plane_client",
                        SimpleNamespace(enqueue_unsolicited_message_threadsafe=queued.append), raising=False)
    message = doorbell.frame()
    assert doorbell.send_to_control_plane(message) is True and queued == [message]
