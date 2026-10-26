import json
import sqlite3
from datetime import datetime, timezone
from topos.principal import Principal, set_principal, reset_principal
from topos.query.retrieval import _annotate_owner_chat_urls

THREAD='12345678-1234-1234-1234-123456789abc'
OTHER='abcdefab-1234-1234-1234-123456789abc'
NOW=datetime(2026,10,12,tzinfo=timezone.utc)

def test_observed_url_uses_message_origin_thread_not_branch_and_respects_clock():
    conn=sqlite3.connect(':memory:')
    conn.execute('create table ai_chat_messages(message_id, conversation_id, metadata_json, source_id)')
    conn.execute('create table browser_visits(url, visited_at)')
    conn.execute('insert into ai_chat_messages values (?,?,?,?)',('m','chatgpt:'+OTHER,json.dumps({'thread_id':THREAD}),'chatgpt_ui_conversation'))
    original='https://chatgpt.com/g/project/c/'+THREAD
    conn.executemany('insert into browser_visits values (?,?)',[(original,'2026-10-09'),('https://chatgpt.com/c/'+OTHER,'2026-10-10'),('https://evil.example/c/'+THREAD,'2026-10-11'),('https://chatgpt.com/c/'+THREAD+'suffix','2026-10-11'),('https://chatgpt.com/g/future/c/'+THREAD,'2026-10-13')])
    for principal in ['third_party','cp_relay','owner_app']:
        token=set_principal(Principal(principal,'internal'))
        try:
            items=_annotate_owner_chat_urls([{'record_id':'m'},{'record_id':'missing'}],conn,now=NOW)
            assert items[1]=={'record_id':'missing'}
            if principal=='owner_app': assert items[0]['source_url']==original
            else: assert items[0]=={'record_id':'m'}
        finally:reset_principal(token)

def test_missing_url_tables_leave_evidence_intact():
    conn=sqlite3.connect(':memory:')
    token=set_principal(Principal('owner_app','internal'))
    try: assert _annotate_owner_chat_urls([{'record_id':'m'}],conn)==[{'record_id':'m'}]
    finally:reset_principal(token)


def test_conversation_origins_cover_all_families_without_exposing_them_to_grantees():
    from topos.query.retrieval import _annotate_owner_conversation_origins
    conn = sqlite3.connect(':memory:')
    for table, key, thread in [('ai_chat_messages', 'message_id', 'conversation_id'),
                               ('conversation_messages', 'message_id', 'conversation_id'),
                               ('transcript_segments', 'segment_id', 'transcript_id')]:
        conn.execute(f'create table {table} ({key}, {thread}, source_id)')
        conn.execute(f'insert into {table} values (?, ?, ?)', (table, 'thread', 'origin'))
    for cls in ['owner_app', 'owner_automation', 'third_party', 'cp_relay']:
        token = set_principal(Principal(cls, 'internal'))
        try:
            items = [{'record_id': table} for table in ['ai_chat_messages', 'conversation_messages', 'transcript_segments']]
            items.append({'record_id': 'absent'})
            result = _annotate_owner_conversation_origins(items, conn)
            assert result[-1] == {'record_id': 'absent'}
            for item in result[:-1]:
                if cls.startswith('owner_'):
                    assert item['origin_table'] == item['record_id']
                    assert item['source_id'] == 'origin'
                    assert item.get('conversation_id', item.get('transcript_id')) == 'thread'
                else:
                    assert set(item) == {'record_id'}
        finally:
            reset_principal(token)
