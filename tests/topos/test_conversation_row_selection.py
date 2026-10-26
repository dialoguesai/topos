"""Conversation narrowing is additive to tenant scope, parameterized and paged in SQL."""
import sqlite3
import pytest
from topos.core.handlers.database_explorer import _conversation_row_selection

@pytest.mark.parametrize("table,thread,key", [
    ("ai_chat_messages", "conversation_id", "message_id"),
    ("conversation_messages", "conversation_id", "message_id"),
    ("transcript_segments", "transcript_id", "segment_id"),
])
def test_thread_filter_keeps_scope_and_pages(table, thread, key):
    conn = sqlite3.connect(":memory:")
    conn.execute(f'CREATE TABLE "{table}" ("{key}" TEXT, "{thread}" TEXT, dataset_id TEXT)')
    conn.executemany(f'INSERT INTO "{table}" VALUES (?, ?, ?)', [("a","thread","mine"),("b","thread","mine"),("c","thread","theirs"),("d","other","mine")])
    where, params = _conversation_row_selection(table,{key,thread,"dataset_id"},"dataset_id","mine",{"filter_column":thread,"filter_value":"thread"})
    rows = conn.execute(f'SELECT "{key}" FROM "{table}"{where} ORDER BY "{key}" LIMIT ? OFFSET ?', params+(1,1)).fetchall()
    assert rows == [("b",)]
    conn.close()

@pytest.mark.parametrize("column,value", [("content","secret"),("conversation_id", ""),("conversation_id",None),("conversation_id\" OR 1=1 --","x")])
def test_invalid_filters_fail_closed(column,value):
    with pytest.raises(ValueError):
        _conversation_row_selection("ai_chat_messages",{"conversation_id"},None,None,{"filter_column":column,"filter_value":value})

def test_filter_value_is_bound_and_postgres_uses_same_scope():
    value = "' OR 1=1 --"
    where, params = _conversation_row_selection("ai_chat_messages",{"conversation_id"},"dataset_id","mine",{"filter_column":"conversation_id","filter_value":value},"%s")
    assert where == ' WHERE "dataset_id" = %s AND "conversation_id" = %s'
    assert params == ("mine",value)
    assert value not in where

@pytest.mark.parametrize("table,thread,key", [("conversation_messages","conversation_id","message_id"),("ai_chat_messages","conversation_id","message_id"),("transcript_segments","transcript_id","segment_id")])
def test_anchor_window_is_ranked_inside_dataset_and_thread(table, thread, key):
    from topos.core.handlers.database_explorer import _conversation_anchor_offset
    conn = sqlite3.connect(':memory:')
    conn.execute(f'CREATE TABLE {table} ({key} TEXT, {thread} TEXT, dataset_id TEXT, sequence INTEGER)')
    conn.executemany(f'INSERT INTO {table} VALUES (?,?,?,?)', [(f'm{i}','same','mine',i) for i in range(25)] + [('outside','same','other',5)])
    payload = {'filter_column':thread,'filter_value':'same','anchor_id':'m16'}
    where, values = _conversation_row_selection(table,{thread,key,'dataset_id'},'dataset_id','mine',payload)
    assert _conversation_anchor_offset(conn,table,where,values,'sequence ASC',payload,6,0) == 13
    payload['anchor_id'] = 'outside'
    assert _conversation_anchor_offset(conn,table,where,values,'sequence ASC',payload,6,0) == 0
    conn.close()
