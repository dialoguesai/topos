"""A rebuild maintains existing authority; transport alone proves local ownership."""
from types import SimpleNamespace
from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from topos.api.permissions_search_maintenance import router
from topos.auth import resolve_request_principal
from topos.permissions_v2 import runtime
from topos.permissions_v2.search_index import SearchIndexService
from topos.principal import OWNER_APP, THIRD_PARTY, Principal
from topos.uds import UDSChannelApp


@pytest.fixture
def maintenance(monkeypatch):
    calls = []
    def rebuild():
        # Exercise the real downstream authority check as well as the HTTP
        # handler. The local socket has no actor string until the handler
        # binds its verified owner transport to this node's paired identity.
        SearchIndexService._require_owner(SimpleNamespace(owner_id="owner-1"))
        calls.append("rebuild")
        return {"private-grant-a":"ready","private-grant-b":"removed"}
    index = SimpleNamespace(sweep=lambda: calls.append("sweep"),
        rebuild_all=rebuild)
    node = SimpleNamespace(protocol=SimpleNamespace(ledger=SimpleNamespace(identity=SimpleNamespace(owner_id="owner-1"))),
        message_search_index=lambda: index)
    monkeypatch.setattr(runtime,"get_runtime",lambda: node)
    app = FastAPI()
    app.include_router(router)
    return app,calls


def test_verified_owner_socket_can_rebuild_without_a_bearer_or_actor_string(maintenance):
    app,calls = maintenance
    with TestClient(UDSChannelApp(app)) as client:
        result = client.post("/v1/sharing/message-search/rebuild")
    assert result.status_code == 200
    assert result.json() == {"grants":2,"ready":1}
    assert result.headers["cache-control"] == "no-store"
    assert "private-grant" not in result.text
    assert calls == ["sweep","rebuild"]


def test_tcp_cannot_claim_the_owner_socket_in_headers_or_payload(maintenance):
    app,calls = maintenance
    with TestClient(app) as client:
        result = client.post("/v1/sharing/message-search/rebuild",
            headers={"X-Topos-Client":"topos_home_chat","X-Transport":"uds"},
            json={"principal":{"cls":"owner_app","channel":"uds","acting_user":"owner-1"}})
    assert result.status_code == 401 and calls == []


def test_rebuild_work_runs_outside_the_node_writer_gate(maintenance,monkeypatch):
    app,calls = maintenance
    from topos.storage.db import write_gate
    entered = []
    @contextmanager
    def gate(*a,**k):
        entered.append(True)
        try:
            yield
        finally:
            entered.pop()
    monkeypatch.setattr(write_gate,"with_db_write",gate)
    node = runtime.get_runtime()
    index = node.message_search_index()
    def rebuild():
        assert not entered
        SearchIndexService._require_owner(SimpleNamespace(owner_id="owner-1"))
        calls.append("rebuild")
        return {"private-grant-a":"ready"}
    index.rebuild_all = rebuild
    with TestClient(UDSChannelApp(app)) as client:
        result = client.post("/v1/sharing/message-search/rebuild")
    assert result.status_code == 200 and calls == ["sweep","rebuild"]


@pytest.mark.parametrize("principal", [Principal(THIRD_PARTY,"local_http"), Principal(THIRD_PARTY,"uds"),
    Principal(OWNER_APP,"uds",acting_user="another-owner"),
    Principal(OWNER_APP,"cp_relay",acting_user="another-owner"), Principal(OWNER_APP,"local_http",acting_user="owner-1")])
def test_other_principals_cannot_rebuild(maintenance,principal):
    app,calls = maintenance
    app.dependency_overrides[resolve_request_principal] = lambda: principal
    with TestClient(app) as client:
        result = client.post("/v1/sharing/message-search/rebuild")
    assert result.status_code == 403 and calls == []


def test_matching_verified_owner_relay_retains_downstream_authority(maintenance):
    app,calls = maintenance
    app.dependency_overrides[resolve_request_principal] = lambda: Principal(OWNER_APP,"cp_relay",acting_user="owner-1")
    with TestClient(app) as client:
        result = client.post("/v1/sharing/message-search/rebuild")
    assert result.status_code == 200 and calls == ["sweep","rebuild"]
