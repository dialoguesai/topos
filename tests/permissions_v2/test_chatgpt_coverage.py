import json
import sqlite3
from datetime import datetime, timezone

import pytest

from topos.permissions_v2.chatgpt_coverage import receipt_comparison, coverage
from topos.permissions_v2.canonical import PolicyError


def sample():
    row = {'source_id':'chatgpt_ui_conversation', 'conversation_id':'chatgpt:thread',
           'message_id':'turn', 'source_record_id':'turn', 'sender_type':'human', 'actor_role':None,
           'content':'Synthetic private message', 'event_at':'2026-09-01T12:00:00+00:00',
           'metadata_json':json.dumps({'thread_id':'thread', 'original_source':'chatgpt'})}
    parent = {'source_id':row['source_id'], 'conversation_id':row['conversation_id'], 'owner_user_id':'owner'}
    raw = {'id':'turn', 'thread_id':'thread', 'role':'user', 'content':row['content'],
           'created_at':datetime(2026,9,1,12,tzinfo=timezone.utc).timestamp()}
    return row, parent, raw


def test_matching_receipt_never_proves_release_authority():
    row,parent,raw = sample()
    assert receipt_comparison(row,parent,json.dumps(raw),owner_id='owner') == 'matching_receipt_needs_native_proof'
    raw['created_at'] += 1
    assert receipt_comparison(row,parent,json.dumps(raw),owner_id='owner') == 'matching_text_different_clock'


@pytest.mark.parametrize('target,field,value,reason', [
    ('row','source_id','other','owner_or_source_mismatch'),
    ('parent','owner_user_id','other','owner_or_source_mismatch'),
    ('row','sender_type','assistant','not_owner_role'),
    ('row','actor_role','addressed','not_owner_role'),
    ('raw','role','assistant','not_owner_role'),
    ('raw','id','other','identity_mismatch'),
    ('raw','thread_id','other','identity_mismatch'),
    ('raw','content','replaced','content_mismatch'),
    ('raw','created_at',True,'receipt_time_invalid'),
    ('row','metadata_json','{"quoted_text":"private"}','unsupported_message_metadata'),
])
def test_failures_are_codes_without_private_text(target,field,value,reason):
    row,parent,raw = sample()
    {'row':row,'parent':parent,'raw':raw}[target][field] = value
    assert receipt_comparison(row,parent,json.dumps(raw),owner_id='owner') == reason


def test_duplicate_keys_cannot_override_receipt_role():
    row,parent,raw = sample()
    raw = json.dumps(raw).replace('"role": "user"', '"role":"assistant","role":"user"')
    assert receipt_comparison(row,parent,raw,owner_id='owner') == 'malformed_receipt_or_metadata'


def test_read_only_windowed_census_preserves_every_cell_and_returns_counts_only():
    row,parent,raw = sample()
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE ai_chat_messages (' + ','.join(k+' TEXT' for k in row) + ')')
    db.execute('CREATE TABLE ai_chat_conversations (' + ','.join(k+' TEXT' for k in parent) + ')')
    db.execute('CREATE TABLE raw_chat_messages_chatgpt(source_system TEXT,source_record_id TEXT,payload_json TEXT)')
    db.execute('INSERT INTO ai_chat_messages VALUES ('+','.join('?' for _ in row)+')',list(row.values()))
    db.execute('INSERT INTO ai_chat_conversations VALUES ('+','.join('?' for _ in parent)+')',list(parent.values()))
    db.execute('INSERT INTO raw_chat_messages_chatgpt VALUES (?,?,?)',(row['source_id'],'turn',json.dumps(raw)))
    db.commit()
    before = list(db.iterdump())
    db.execute('PRAGMA query_only=ON')
    db.execute('BEGIN')
    args = dict(owner_id='owner',source_id=row['source_id'],starts_at='2026-09-01T00:00:00+00:00',
                ends_at='2026-09-02T00:00:00+00:00',now=datetime(2026,9,2,tzinfo=timezone.utc))
    result = coverage(db,**args)
    assert result['counts'] == {'matching_receipt_needs_native_proof':1}
    assert result['authority_created'] is False
    assert 'Synthetic private message' not in json.dumps(result)
    assert list(db.iterdump()) == before
    with pytest.raises(PolicyError, match='coverage_window_invalid'):
        coverage(db,**(args | {'starts_at':'2020-01-01T00:00:00+00:00'}))
    db.close()
