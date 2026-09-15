"""Real signed fact relay, exact send-task checks and no generic fallback."""
import asyncio
import base64
import json
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


@pytest.mark.asyncio
@pytest.mark.parametrize("profile",["fact","source"])
@pytest.mark.parametrize("change",["expiry","flag","key","runtime"])
async def test_mutable_checks_execute_in_actual_send_task(request,monkeypatch,profile,change):
    setup,message=request.getfixturevalue(profile+"_relay")
    transport=fact_release_transport if profile=="fact" else release_transport
    dispatch=transport.dispatch_fact_message if profile=="fact" else transport.dispatch_source_message
    flag=fact_release_transport.FLAG if profile=="fact" else "TOPOS_PERMISSIONS_V2_SOURCE_RELEASE_ENABLED"
    original=asyncio.wait_for
    armed=[True]
    def mutate():
        if change=="expiry":setup[4][0]+=100
        elif change=="flag":monkeypatch.setenv(flag,"false")
        elif change=="key":setup[0].protocol.ledger.trusted_keys={}
        else:monkeypatch.setattr(transport,"get_runtime",lambda:object())
    async def schedule_between_check_and_task(coro,timeout):
        if armed[0]:
            armed[0]=False
            asyncio.get_running_loop().call_soon(mutate)
        return await original(coro,timeout)
    monkeypatch.setattr(asyncio,"wait_for",schedule_between_check_and_task)
    socket=Socket();await dispatch(socket,message)
    assert socket.sent and all(frame["status"]=="error" and "payload" not in frame for frame in socket.sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("profile",["fact","source"])
async def test_dispatch_cancellation_drains_actual_send_before_gates_release(request,profile):
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
    # Event-loop thread cannot own the worker's process gate.
    acquired=db_write_lock().acquire(blocking=False)
    if acquired:db_write_lock().release()
    assert acquired is False
    finish.set()
    with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,2)
    acquired=db_write_lock().acquire(blocking=False)
    assert acquired is True
    if acquired:db_write_lock().release()
    # Send-start preceded cancellation; committed delivery cannot be recalled.
    assert len(socket.sent)==1 and socket.sent[0]["status"]=="ok"
    replay=Socket();await dispatch(replay,message)
    assert replay.sent[0]["status"]=="error"
