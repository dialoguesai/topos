import sqlite3
import pytest
from topos.principal import Principal, set_principal, reset_principal
from topos.core.handlers.conversation_view import conversation_index_sql, enrich_conversation_rows

@pytest.fixture
def db():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
      CREATE TABLE ai_chat_conversations (conversation_id TEXT, title TEXT, latest_title TEXT);
      CREATE TABLE ai_chat_messages (message_id TEXT, conversation_id TEXT, dataset_id TEXT, source_id TEXT, content TEXT, event_at TEXT);
      CREATE TABLE conversation_messages (message_id TEXT, conversation_id TEXT, dataset_id TEXT, source_id TEXT, content TEXT, event_at TEXT, sender_id TEXT, is_from_self INTEGER);
      CREATE TABLE contacts (contact_id TEXT, dataset_id TEXT, display_name TEXT, is_self INTEGER);
      CREATE TABLE contact_identifiers (contact_id TEXT, dataset_id TEXT, source_id TEXT, identifier TEXT);
      CREATE TABLE conversation_participants (conversation_id TEXT, dataset_id TEXT, source_id TEXT, contact_id TEXT);
    ''')
    token = set_principal(Principal('owner_app', 'local_http'))
    yield conn
    reset_principal(token)
    conn.close()

def test_title_comes_from_parent_even_when_message_has_no_metadata(db):
    db.execute("INSERT INTO ai_chat_conversations VALUES ('c','Old title','Actual chat name')")
    db.execute("INSERT INTO ai_chat_messages VALUES ('m','c','selected','chatgpt','hello','2026')")
    rows = [dict(r) for r in db.execute('SELECT * FROM ai_chat_messages')]
    enrich_conversation_rows(db, 'ai_chat_messages', rows)
    assert rows[0]['conversation_title'] == 'Actual chat name'

def test_all_search_finds_older_content_and_title_before_pagination(db):
    db.executemany('INSERT INTO ai_chat_messages VALUES (?,?,?,?,?,?)', [('m1','a','selected','chatgpt','needle','2024'),('m2','a','selected','chatgpt','newer','2026'),('m3','b','selected','chatgpt','newest','2027'),('outside','c','other','chatgpt','needle','2028')])
    db.execute("INSERT INTO ai_chat_conversations VALUES ('a','Unique title',NULL)")
    def search(q):
        sql, params = conversation_index_sql(db, 'ai_chat_messages', ' WHERE dataset_id = ?', ('selected',), {'search':q}, 1, 0)
        return [dict(r) for r in db.execute(sql, params)]
    assert [r['message_id'] for r in search('needle')] == ['m1']
    assert [r['message_id'] for r in search('Unique title')] == ['m2']
    assert [r['message_id'] for r in search('')] == ['m3','m2']
    assert search("%' OR 1=1 --") == []

def test_outgoing_page_gets_contacts_only_from_same_dataset_and_service(db):
    db.executemany('INSERT INTO contacts VALUES (?,?,?,?)', [('a','selected','Alex',0),('b','other','Wrong scope',0),('c','selected','Other service',0),('me','selected','Owner',1)])
    db.executemany('INSERT INTO conversation_participants VALUES (?,?,?,?)', [('thread','selected','imessage','a'),('thread','other','imessage','b'),('thread','selected','signal','c'),('thread','selected','imessage','me')])
    db.execute("INSERT INTO conversation_messages VALUES ('m','thread','selected','imessage','outgoing','2026','self',1)")
    rows = [dict(r) for r in db.execute('SELECT * FROM conversation_messages')]
    enrich_conversation_rows(db,'conversation_messages',rows)
    assert rows[0]['participant_names'] == ['Alex']
    sql, params = conversation_index_sql(db,'conversation_messages',' WHERE dataset_id = ?',('selected',),{'search':'Alex'},10,0)
    assert len(db.execute(sql,params).fetchall()) == 1
    sql, params = conversation_index_sql(db,'conversation_messages',' WHERE dataset_id = ?',('selected',),{'search':'Wrong scope'},10,0)
    assert db.execute(sql,params).fetchall() == []

def test_index_rejects_non_owner(db):
    token = set_principal(Principal('third_party','local_http'))
    try:
        with pytest.raises(ValueError):
            conversation_index_sql(db,'ai_chat_messages','',(),{},10,0)
    finally:
        reset_principal(token)

def test_codex_named_index_overrides_first_prompt_title_and_is_searchable(db, tmp_path, monkeypatch):
    import json
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    (tmp_path / 'session_index.jsonl').write_text('\n'.join(json.dumps(r) for r in [
        {'id':'c','thread_name':'Old name'}, {'id':'c','thread_name':'Actual renamed session'}
    ]))
    db.execute("INSERT INTO ai_chat_conversations VALUES ('codex:c','A very long first prompt',NULL)")
    db.execute("INSERT INTO ai_chat_messages VALUES ('m','codex:c','selected','codex_sessions','reply','2026')")
    rows = [dict(r) for r in db.execute('SELECT * FROM ai_chat_messages')]
    enrich_conversation_rows(db, 'ai_chat_messages', rows)
    assert rows[0]['conversation_title'] == 'Actual renamed session'
    sql, values = conversation_index_sql(db, 'ai_chat_messages', ' WHERE dataset_id = ?', ('selected',), {'search':'renamed session'}, 10, 0)
    assert len(db.execute(sql, values).fetchall()) == 1
    sql, values = conversation_index_sql(db, 'ai_chat_messages', ' WHERE dataset_id = ?', ('other',), {'search':'renamed session'}, 10, 0)
    assert db.execute(sql, values).fetchall() == []


def test_timeline_resolves_all_three_kinds_without_changing_ids_or_pagination(db, tmp_path, monkeypatch):
    from topos.features.facts.reads import list_timeline
    monkeypatch.setenv('CODEX_HOME', str(tmp_path))
    (tmp_path / 'session_index.jsonl').write_text('{"id":"session","thread_name":"Named coding session"}\n')
    db.executescript('''
      CREATE TABLE transcripts (transcript_id TEXT, dataset_id TEXT, title TEXT);
      CREATE TABLE transcript_segments (segment_id TEXT, transcript_id TEXT, dataset_id TEXT, source_id TEXT);
      CREATE TABLE timeline (event_at TEXT, record_id TEXT, source_id TEXT, canonical_table TEXT, record_type TEXT);
      INSERT INTO transcripts VALUES ('t','selected','Design review');
      INSERT INTO transcripts VALUES ('t','other','Wrong transcript');
      INSERT INTO transcript_segments VALUES ('segment','t','selected','recording');
      INSERT INTO ai_chat_conversations VALUES ('codex:session','First prompt',NULL);
      INSERT INTO ai_chat_conversations VALUES ('claude:session','Claude chat name',NULL);
      INSERT INTO ai_chat_messages VALUES ('codex:msg','codex:session','selected','codex_sessions','body','2026');
      INSERT INTO ai_chat_messages VALUES ('claude:msg','claude:session','selected','claude_code_sessions','body','2026');
      INSERT INTO contacts VALUES ('a','selected','Rowan Testperson',0);
      INSERT INTO contacts VALUES ('b','other','Wrong contact',0);
      INSERT INTO contacts VALUES ('me','selected','Owner',1);
      INSERT INTO conversation_participants VALUES ('chat','selected','imessage','a');
      INSERT INTO conversation_participants VALUES ('chat','other','imessage','b');
      INSERT INTO conversation_participants VALUES ('chat','selected','imessage','me');
      INSERT INTO conversation_messages VALUES ('text','chat','selected','imessage','body','2026','self',1);
    ''')
    events = [('codex:msg','codex_sessions','ai_chat_messages'),('claude:msg','claude_code_sessions','ai_chat_messages'),('text','imessage','conversation_messages'),('segment','recording','transcript_segments')]
    db.execute('ALTER TABLE ai_chat_messages DROP COLUMN dataset_id')
    db.executemany('INSERT INTO timeline VALUES (?,?,?,?,NULL)', [('2026', *e) for e in events])
    result = list_timeline(db)
    assert result['total'] == 4
    assert {e['record_id']: e['display_title'] for e in result['items']} == {
        'codex:msg': 'Named coding session', 'claude:msg': 'Claude chat name',
        'text': 'Rowan Testperson', 'segment': 'Design review',
    }
    page = list_timeline(db, limit=1, offset=1)
    assert page['total'] == 4
    assert page['items'] == result['items'][1:2]
    token = set_principal(Principal('third_party', 'local_http'))
    try:
        assert all('display_title' not in e for e in list_timeline(db)['items'])
    finally:
        reset_principal(token)
