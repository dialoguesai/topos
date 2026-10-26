from unittest.mock import AsyncMock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from topos.api import database_explorer as api
from topos.auth import resolve_request_principal
from topos.principal import Principal

@pytest.mark.parametrize('kind,status', [('owner_app',200),('third_party',403),('cp_relay',403)])
def test_rows_auth_and_forwarding(monkeypatch, kind, status):
    app = FastAPI()
    app.include_router(api.router)
    principal = Principal(kind, 'local_http')
    app.dependency_overrides[resolve_request_principal] = lambda: principal
    dispatch = AsyncMock(return_value={'status':'ok','payload':{'rows':[], 'row_filter':{'column':'conversation_id','value':'thread'}}})
    monkeypatch.setattr(api, 'handle_control_plane_request', dispatch)
    response = TestClient(app).get('/v1/database/tables/ai_chat_messages/rows', params={'dataset_id':'dataset','filter_column':'conversation_id','filter_value':'thread','limit':100,'order_by':'sequence','order_dir':'asc'})
    assert response.status_code == status
    if status == 200:
        assert response.json()['row_filter']['value'] == 'thread'
        call = dispatch.call_args
        assert call.kwargs['principal'] is principal
        assert call.args[0]['payload'] == {'table_name':'ai_chat_messages','dataset_id':'dataset','filter_column':'conversation_id','filter_value':'thread','limit':100,'offset':0,'order_by':'sequence','order_dir':'asc'}
    else:
        dispatch.assert_not_called()

@pytest.mark.parametrize('table,key,thread,order', [
    ('ai_chat_messages','message_id','conversation_id','sequence'),
    ('conversation_messages','message_id','conversation_id','event_at'),
    ('transcript_segments','segment_id','transcript_id','start_sec'),
])
def test_http_thread_filter_with_real_sql(monkeypatch, table, key, thread, order):
    import sqlite3
    from topos.core.handlers import database_explorer as handler
    from topos.principal import set_principal, reset_principal
    conn = sqlite3.connect(':memory:', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f'CREATE TABLE "{table}" ("{key}" TEXT, "{thread}" TEXT, "{order}" INTEGER, content TEXT)')
    conn.executemany(f'INSERT INTO "{table}" VALUES (?,?,?,?)', [('a','wanted',1,'before'),('b','wanted',2,'selected'),('c','other',3,'not in this thread')])
    monkeypatch.setattr(handler.hub, 'get_db_connection', lambda: conn)
    monkeypatch.setattr(handler.settings, 'topos_database_mode', 'sqlite')
    monkeypatch.setattr(handler, '_pooled_read_enforcement_enabled', lambda: False)
    async def dispatch(message, principal):
        token = set_principal(principal)
        try:
            return await handler.handle_get_table_rows(message)
        finally:
            reset_principal(token)
    monkeypatch.setattr(api, 'handle_control_plane_request', dispatch)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[resolve_request_principal] = lambda: Principal('owner_app','local_http')
    result = TestClient(app).get(f'/v1/database/tables/{table}/rows', params={'filter_column':thread,'filter_value':'wanted','order_by':order,'order_dir':'asc','offset':1,'limit':1})
    assert result.status_code == 200, result.text
    assert [row[key] for row in result.json()['rows']] == ['b']
    assert result.json()['row_filter'] == {'column':thread,'value':'wanted'}
    conn.close()

@pytest.mark.parametrize('principal_kind', ['owner_app', 'third_party'])
def test_sender_names_stay_with_row_dataset_and_source(principal_kind):
    import sqlite3
    from topos.core.handlers.database_explorer import _conversation_sender_names
    from topos.principal import set_principal, reset_principal
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE contacts (contact_id TEXT, dataset_id TEXT, display_name TEXT)')
    conn.execute('CREATE TABLE contact_identifiers (dataset_id TEXT, source_id TEXT, identifier TEXT, contact_id TEXT)')
    conn.executemany('INSERT INTO contacts VALUES (?,?,?)', [('a','selected','Alex'),('b','other','Wrong name'),('c','selected','Other service')])
    conn.executemany('INSERT INTO contact_identifiers VALUES (?,?,?,?)', [('selected','imessage','person','a'),('other','imessage','person','b'),('selected','signal','person','c')])
    rows = [{'dataset_id':'selected','source_id':'imessage','sender_id':'person'}]
    token = set_principal(Principal(principal_kind, 'local_http'))
    try:
        _conversation_sender_names(conn, rows)
    finally:
        reset_principal(token)
        conn.close()
    assert rows[0].get('sender_name') == ('Alex' if principal_kind == 'owner_app' else None)
