"""Real signed fact relay, exact send-task checks and no generic fallback."""
import asyncio
import base64
import json
import threading
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from tests.permissions_v2.test_fact_release import fact_setup, issue, timed, projection_service, corpus
from tests.permissions_v2.test_release import release_setup
from tests.permissions_v2.test_release_transport import relay as source_relay, Socket
from topos.permissions_v2 import fact_release_transport, release_transport
from topos.relay_stamp import canonical_signing_payload


@pytest.fixture
def fact_relay(fact_setup,monkeypatch):
    service,_,cp_key,_,now,_,_=fact_setup
    monkeypatch.setenv(fact_release_transport.FLAG,"true")
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY",base64.b64encode(cp_key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode())
    monkeypatch.setattr(fact_release_transport.time,"time",lambda:now[0])
    runtime=SimpleNamespace(protocol=service.protocol,projection_reviews=lambda **kw:service.projections)
    monkeypatch.setattr(fact_release_transport,"get_runtime",lambda:runtime)
    envelope,payload=issue(fact_setup)
    message={"id":"fact-read-1","type":fact_release_transport.MESSAGE_TYPE,"payload":{"envelope":envelope.model_dump(),"intent":payload}}
    stamp={"v":1,"cls":"third_party","client_id":"client-1","acting_user":"actor-1","iat":now[0],"exp":now[0]+100}
    stamp["sig"]=base64.b64encode(cp_key.sign(canonical_signing_payload(stamp,msg_id=message["id"],msg_type=message["type"]))).decode()
    message["principal_stamp"]=stamp
    return fact_setup,message


@pytest.mark.asyncio
async def test_real_fact_socket_emits_only_scalar_and_cannot_replay(fact_relay):
    _,message=fact_relay
    socket=Socket()
    await fact_release_transport.dispatch_fact_message(socket,message)
    assert len(socket.sent)==1 and socket.sent[0]["status"]=="ok"
    assert set(socket.sent[0]["payload"]["output"])=={"family","operation","view_id","subject","predicate","value"}
    assert socket.sent[0]["payload"]["output"]["value"]=="history books"
    await fact_release_transport.dispatch_fact_message(socket,message)
    assert socket.sent[1]["status"]=="error" and "payload" not in socket.sent[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind",["disabled","missing_stamp","owner_stamp","bad_stamp","changed_query","changed_id","extra_payload","source_type"])
async def test_fact_door_rejects_untrusted_or_wrong_request_without_content(fact_relay,monkeypatch,kind):
    _,message=fact_relay
    if kind=="disabled":monkeypatch.delenv(fact_release_transport.FLAG)
    elif kind=="missing_stamp":message.pop("principal_stamp")
    elif kind=="owner_stamp":message["principal_stamp"]["cls"]="owner_app"
    elif kind=="bad_stamp":message["principal_stamp"]["sig"]="wrong"
    elif kind=="changed_query":message["payload"]["intent"]["query"]="fact:other"
    elif kind=="changed_id":message["id"]="other"
    elif kind=="extra_payload":message["payload"]["as_of"]=1
    else:message["type"]=release_transport.MESSAGE_TYPE
    socket=Socket();await fact_release_transport.dispatch_fact_message(socket,message)
    assert socket.sent==[{"id":message["id"],"type":fact_release_transport.MESSAGE_TYPE,"status":"error","code":403,"error":"permission_denied"}]


@pytest.mark.asyncio
async def test_generic_fact_handler_returns_no_disclosure(fact_relay):
    from topos.core.handlers import handle_control_plane_request
    from topos.relay_stamp import verify_relay_stamp
    _,message=fact_relay
    assert await handle_control_plane_request(message,principal=verify_relay_stamp(message))=={
        "id":"fact-read-1","status":"error","code":403,"error":"permission_denied"}


def _mutable_change(setup,monkeypatch,transport,profile,change):
    """The send-time state a revocation can move: the clock past expiry, the door's flag, the CP key, the runtime."""
    flag=fact_release_transport.FLAG if profile=="fact" else "TOPOS_PERMISSIONS_V2_SOURCE_RELEASE_ENABLED"
    if change=="expiry":setup[4][0]+=100  # the envelope's whole lifetime in both fixtures
    elif change=="flag":monkeypatch.setenv(flag,"false")
    elif change=="key":setup[0].protocol.ledger.trusted_keys={}
    elif change=="runtime":monkeypatch.setattr(transport,"get_runtime",lambda:object())


@pytest.mark.asyncio
@pytest.mark.parametrize("profile",["fact","source"])
@pytest.mark.parametrize("change",["none","expiry","flag","key","runtime"])
async def test_mutable_checks_execute_in_actual_send_task(request,monkeypatch,profile,change):
    # The change runs INSIDE the send task, at its wait_for: after the worker handed the send to the loop and
    # before actual_send's check, so this test pins WHERE the check sits (moved into the worker ahead of the
    # hand-off, only this test goes red). It used to be call_soon()ed from there, which preceded the check only
    # while asyncio.wait_for wrapped its coroutine in a new Task. From 3.12 wait_for awaits it inline, so the
    # check and ws.send ran first and the queued change landed after the write: 8 reds on CI's 3.12 (run
    # 35932986329) that were never a window. `none` shows the same harness sends, so a refusal is the change's doing.
    setup,message=request.getfixturevalue(profile+"_relay")
    transport=fact_release_transport if profile=="fact" else release_transport
    dispatch=transport.dispatch_fact_message if profile=="fact" else transport.dispatch_source_message
    original=asyncio.wait_for
    armed=[True]
    async def change_inside_send_task(coro,timeout):
        if armed[0]:
            armed[0]=False
            _mutable_change(setup,monkeypatch,transport,profile,change)
        return await original(coro,timeout)
    monkeypatch.setattr(asyncio,"wait_for",change_inside_send_task)
    socket=Socket();await dispatch(socket,message)
    assert armed[0] is False
    if change=="none":
        assert len(socket.sent)==1 and socket.sent[0]["status"]=="ok"
    else:
        assert socket.sent and all(frame["status"]=="error" and "payload" not in frame for frame in socket.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile",["fact","source"])
@pytest.mark.parametrize("change",["none","expiry","flag","key","runtime"])
async def test_change_between_admission_and_socket_write_refuses_on_any_asyncio(request,monkeypatch,profile,change):
    """A change that has EXECUTED after admission and before the socket write refuses the send.

    Nothing in asyncio is patched. The adapter reaches its send callback only after admission, the checkpoint
    and the post-checkpoint authority re-read; there the worker sets a real asyncio.Event on the socket's loop
    and blocks until the revoker task awaiting it has made the change. So the change is complete before the
    transport path starts, however asyncio.wait_for runs the coroutine it is given, and the only thing left to
    refuse the write is the transport's own send-time check. `none` is the same handshake with nothing changed.
    Deleting that check, or any one of its flag, runtime or signature operands, turns the matching cases red on
    3.10 and 3.12 alike; the test above is the one that pins the check inside the send task.
    """
    setup,message=request.getfixturevalue(profile+"_relay")
    transport=fact_release_transport if profile=="fact" else release_transport
    dispatch=transport.dispatch_fact_message if profile=="fact" else transport.dispatch_source_message
    adapter="FactProjectionRelease" if profile=="fact" else "SourceMessageRelease"
    loop=asyncio.get_running_loop()
    admitted=asyncio.Event();changed=threading.Event()
    seen={};order=[]
    async def revoker():
        await admitted.wait()
        _mutable_change(setup,monkeypatch,transport,profile,change)
        order.append("changed");changed.set()
    class AfterAdmission(getattr(transport,adapter)):
        def dispatch(self,*,send,**kwargs):
            def after_admission(result,output):
                seen["expires_at"]=result["expires_at"]
                loop.call_soon_threadsafe(admitted.set)
                seen["handshake"]=changed.wait(5)
                send(result,output)
            return super().dispatch(send=after_admission,**kwargs)
    monkeypatch.setattr(transport,adapter,AfterAdmission)
    class Recording(Socket):
        async def send(self,value):
            order.append("write");await super().send(value)
    revoking=asyncio.create_task(revoker())
    socket=Recording()
    try:
        await dispatch(socket,message)
    finally:
        revoking.cancel();await asyncio.gather(revoking,return_exceptions=True)
    assert seen.get("handshake") is True and order==["changed","write"]
    if change=="none":
        assert len(socket.sent)==1 and socket.sent[0]["status"]=="ok"
        return
    if change=="expiry":assert setup[4][0]>=seen["expires_at"]
    assert socket.sent==[{"id":message["id"],"type":transport.MESSAGE_TYPE,"status":"error","code":403,"error":"permission_denied"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("profile",["fact","source"])
async def test_dispatch_cancellation_drains_actual_send_with_no_gate_held(request,profile):
    from topos.storage.db.write_gate import db_write_lock
    setup,message=request.getfixturevalue(profile+"_relay")
    transport=fact_release_transport if profile=="fact" else release_transport
    dispatch=transport.dispatch_fact_message if profile=="fact" else transport.dispatch_source_message
    started=asyncio.Event();finish=asyncio.Event()
    class Paused(Socket):
        async def send(self,value):
            started.set();await finish.wait();await super().send(value)
    socket=Paused()
    task=asyncio.create_task(dispatch(socket,message))
    await asyncio.wait_for(started.wait(),2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    # R12: the worker checkpointed and released the node gate before this send.
    acquired=db_write_lock().acquire(blocking=False)
    if acquired:db_write_lock().release()
    assert acquired is True
    finish.set()
    with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,2)
    acquired=db_write_lock().acquire(blocking=False)
    assert acquired is True
    if acquired:db_write_lock().release()
    # Send-start preceded cancellation; committed delivery cannot be recalled.
    assert len(socket.sent)==1 and socket.sent[0]["status"]=="ok"
    replay=Socket();await dispatch(replay,message)
    assert replay.sent[0]["status"]=="error"
