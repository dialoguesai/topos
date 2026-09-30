from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.permissions_v2.test_direct_message_evidence import setup
from tests.permissions_v2.test_reconciliation_provenance import legacy
from tests.permissions_v2.test_ingest_provenance import ingest_fixture
from topos.api.permissions_search_maintenance import router
from topos.auth import resolve_request_principal
from topos.permissions_v2 import runtime
from topos.principal import OWNER_APP, THIRD_PARTY, Principal
from topos.uds import UDSChannelApp

PATH='/v1/permissions-beta/v2/message-search/message-review'

@pytest.fixture
def api(legacy, monkeypatch):
    resolver, reviews, identity = setup(legacy)
    node = SimpleNamespace(protocol=SimpleNamespace(ledger=SimpleNamespace(identity=resolver.binding)),
        evidence_reviews=lambda **kwargs: SimpleNamespace(resolver=resolver,reviews=reviews))
    monkeypatch.setattr(runtime,'get_runtime',lambda:node)
    monkeypatch.setenv('TOPOS_PERMISSIONS_V2_MESSAGE_SEARCH_ENABLED','false')
    app=FastAPI();app.include_router(router)
    return app, dict(binding=resolver.binding.model_dump(), operation='preview', request={'identity':identity.model_dump()})


def test_verified_owner_socket_preview_returns_only_to_owner(api):
    app,payload=api
    with TestClient(UDSChannelApp(app)) as client:
        result=client.post(PATH,json=payload)
    assert result.status_code==200
    assert result.json()['content']=='I am working on Synthetic message at work.'
    assert result.headers['cache-control']=='no-store'


@pytest.mark.parametrize('principal',[
    Principal(THIRD_PARTY,'cp_relay',acting_user='owner-1'),
    Principal(OWNER_APP,'cp_relay',acting_user='someone-else'),
    Principal(OWNER_APP,'local_http',acting_user='owner-1'),
    Principal(OWNER_APP,'uds',acting_user='someone-else'),
])
def test_grantee_and_forged_owner_cannot_preview_or_record(api,principal):
    app,payload=api
    app.dependency_overrides[resolve_request_principal]=lambda:principal
    with TestClient(app) as client:
        for op in ['preview','record','opt_out','opt_in','queue','queue_page']:
            response=client.post(PATH,json={**payload,'operation':op})
            assert response.status_code==403
            assert 'Synthetic message' not in response.text


def test_tcp_cannot_assert_owner_with_headers(api):
    app,payload=api
    with TestClient(app) as client:
        response=client.post(PATH,json=payload,headers={'X-Transport':'uds'})
    assert response.status_code==401


def test_owner_socket_pages_the_whole_window_and_refuses_a_foreign_binding(api):
    from topos.permissions_v2.fact_eligibility import canonical_utc_microseconds
    app,payload=api
    resolver=runtime.get_runtime().evidence_reviews().resolver
    with resolver._read() as (conn,_):
        event=canonical_utc_microseconds(conn.execute('SELECT event_at FROM conversation_messages').fetchone()[0])//1000000
    request={'after':event-15*86400,'before':event+86400,'limit':5}
    with TestClient(UDSChannelApp(app)) as client:
        every=client.post(PATH,json={**payload,'operation':'queue_page','request':request})
        uncertain=client.post(PATH,json={**payload,'operation':'queue_page',
                                         'request':{**request,'filter':'withheld_uncertain'}})
        foreign=client.post(PATH,json={**payload,'operation':'queue_page','request':request,
                                       'binding':{**payload['binding'],'node_id':'other-node'}})
    assert every.status_code==200 and every.headers['cache-control']=='no-store'
    body=every.json()
    assert [r['snapshot']['message']['identity']['record_id'] for r in body['records']]==['imessage:1']
    assert body['records'][0]['classification_origin']=='owner'      # the fixture's own review
    assert (body['next_cursor'],body['remaining'],body['remaining_exact'],body['order_matched'])==(None,0,True,0)
    assert uncertain.status_code==200 and uncertain.json()['records']==[]  # reviewed already: not withheld
    assert foreign.status_code==403 and 'Synthetic message' not in foreign.text

