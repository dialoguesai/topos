"""Milestone: source-backed cross-chat recall survives reopen and stops on deletion."""
import sqlite3
from uuid import uuid4
import pytest
from topos.home_chat import store
from topos.home_chat.memory import memory_action


def session(conn, text, *, sid=None, role='user', user='owner', engine='node', revision=1):
    sid = sid or str(uuid4())
    history = {'version':3,'currentId':'m','messages':{'m':{'id':'m','role':role,'done':True,'content':text,'parentId':None}}}
    store.upsert_session(conn,user_id=user,payload={'sessionId':sid,'engineId':engine,'history':history,'revision':revision})
    return sid


def act(conn, action='search', user='owner', engine='node', **payload):
    return memory_action(conn,user_id=user,engine_id=engine,action=action,payload=payload)


def connect(path):
    c=sqlite3.connect(path); c.row_factory=sqlite3.Row; return c


def test_full_milestone_survives_restart_correction_and_delete(tmp_path):
    path=tmp_path/'memory.db'; c=connect(path)
    original=session(c,'Remember: store agent memory in TOPOS.')
    mid=str(uuid4())
    saved=act(c,'remember',id=mid,text='store agent memory in TOPOS.',sourceSessionId=original,sourceMessageId='m')['memory']
    assert saved['sourceSessionId']==original and saved['sourceMessageId']=='m'
    c.close(); c=connect(path)
    session(c,'Where did we decide to store memory?')
    assert act(c,query='memory')['memories']==[saved]
    correction=session(c,'Actually, remember: use the local TOPOS node.')
    current=act(c,'correct',id=mid,revision=1,text='use the local TOPOS node.',sourceSessionId=correction,sourceMessageId='m')['memory']
    assert current['revision']==2 and current['sourceSessionId']==correction
    assert [r['text'] for r in act(c)['memories']]==['use the local TOPOS node.']
    with pytest.raises(ValueError,match='STALE_REVISION'):
        act(c,'forget',id=mid,revision=1)
    assert act(c,'forget',id=mid,revision=2)['forgotten']
    assert act(c)['memories']==[]
    with pytest.raises(ValueError,match='MEMORY_NOT_FOUND'):
        act(c,'remember',id=mid,text='store agent memory in TOPOS.',sourceSessionId=original,sourceMessageId='m')


@pytest.mark.parametrize('delete_origin',[False,True])
def test_deleting_either_source_removes_corrected_memory(tmp_path,delete_origin):
    c=connect(tmp_path/'db'); sid=session(c,'Remember: A'); mid=str(uuid4())
    act(c,'remember',id=mid,text='A',sourceSessionId=sid,sourceMessageId='m')
    new=session(c,'Correct memory: B')
    act(c,'correct',id=mid,revision=1,text='B',sourceSessionId=new,sourceMessageId='m')
    store.delete_session(c,user_id='owner',session_id=sid if delete_origin else new)
    assert act(c)['memories']==[]


def test_source_edit_withdraws_permanently_and_other_scopes_cannot_read_or_write(tmp_path):
    c=connect(tmp_path/'db'); sid=session(c,'Remember: A'); mid=str(uuid4())
    act(c,'remember',id=mid,text='A',sourceSessionId=sid,sourceMessageId='m')
    assert act(c,user='other')['memories']==[]
    assert act(c,engine='other')['memories']==[]
    with pytest.raises(ValueError,match='MEMORY_NOT_FOUND'):
        act(c,'forget',user='other',id=mid,revision=1)
    session(c,'Remember: B',sid=sid,revision=2)
    assert act(c)['memories']==[]
    session(c,'Remember: A',sid=sid,revision=3)
    assert act(c)['memories']==[]


@pytest.mark.parametrize('role,text,user,engine', [('assistant','A','owner','node'),('user','invented','owner','node'),('user','A','intruder','node'),('user','A','owner','other')])
def test_rejects_assistant_fabrication_and_wrong_scope(tmp_path,role,text,user,engine):
    c=connect(tmp_path/'db'); sid=session(c,'A',role=role)
    with pytest.raises(ValueError,match='INVALID_MEMORY_SOURCE'):
        act(c,'remember',id=str(uuid4()),text=text,sourceSessionId=sid,sourceMessageId='m',user=user,engine=engine)


def test_inactive_branch_not_evidence(tmp_path):
    c=connect(tmp_path/'db'); sid=session(c,'A')
    row=store.get_session(c,user_id='owner',session_id=sid)
    row['history']['currentId']=None
    store.upsert_session(c,user_id='owner',payload=row)
    with pytest.raises(ValueError,match='INVALID_MEMORY_SOURCE'):
        act(c,'remember',id=str(uuid4()),text='A',sourceSessionId=sid,sourceMessageId='m')


def test_granted_history_and_withdrawn_blob_cannot_supply_memories(tmp_path):
    from topos.home_chat.memory import source_message
    c=connect(tmp_path/'db'); sid=session(c,'Remember: shared secret')
    row=store.get_session(c,user_id='owner',session_id=sid)
    row['history']['messages']['m']['parentId']='grant'
    row['history']['messages']['grant']={'role':'assistant','sharedGrantAnswer':True,'parentId':None}
    store.upsert_session(c,user_id='owner',payload=row)
    with pytest.raises(ValueError,match='INVALID_MEMORY_SOURCE'):
        act(c,'remember',id=str(uuid4()),text='shared secret',sourceSessionId=sid,sourceMessageId='m')
    assert source_message([], 'm') is None
