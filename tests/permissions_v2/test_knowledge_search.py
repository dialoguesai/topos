from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_automatic_message_review import setup, answer
from tests.permissions_v2.test_reconciliation_provenance import legacy
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner
from tests.permissions_v2.message_search_harness import Node
from tests.permissions_v2 import message_search_corpus as mc
from topos.permissions_v2.automatic_message_review import publish
from topos.permissions_v2.fact_eligibility import canonical_utc_microseconds
from topos.permissions_v2.canonical import PolicyError


def knowledge_policy():
    raw=mc.search_policy(max_k=10)
    raw['versions']['capability']='permissions-beta/p2c-v3'
    raw['versions']['subject_binding']=dict(contract='permissioned_knowledge_v1',
        authorship='native_provenance_required',classification='machine_review_with_owner_corrections/v1',
        lineage='complete_permitted_support/v1',exclusions='item_and_dependencies')
    raw['search'].update(view_id='canonical.knowledge_search.v1',result_types=['message','fact','goal','relationship'],
                         time_semantics='underlying_evidence_time/v1')
    for rule in raw['rules']:
        if rule['effect']=='permit':
            rule['evidence_use']['predicate']['terms'][0]['values']=['work','plans']
            rule['release']['predicate']['terms'][0]['values']=['work','plans']
        for form in rule['release']['forms']:
            form['view_id']='canonical.knowledge_search.v1'
    raw['evaluator']['version']='hard-rules/p2c-v3'
    return raw


def node_for(legacy,tmp_path,monkeypatch,*,labels=None):
    resolver,reviews,identity,prepared=setup(legacy)
    classification=answer(prepared)
    if labels: classification=classification.model_copy(update=labels)
    with owner(): publish(resolver,reviews,prepared,classification,now=1)
    stamp=legacy[1].execute('SELECT event_at FROM conversation_messages').fetchone()[0]
    now=canonical_utc_microseconds(stamp)//1000000+60
    monkeypatch.setattr(mc,'NOW',now)
    node=Node(SimpleNamespace(resolver=resolver,reviews=reviews,path=resolver.path),tmp_path/'node',
              model=None,search_raw=knowledge_policy(),now=now)
    return node,identity


def test_signed_knowledge_search_releases_machine_checked_message(legacy,tmp_path,monkeypatch):
    node,_=node_for(legacy,tmp_path,monkeypatch)
    with owner():
        assert node.index.rebuild('grant-search',now=node.now[0])['state']=='ready'
    output,refused=node.search_request('Synthetic message',k=10)
    assert refused is None
    assert output['view_id']=='canonical.knowledge_search.v1'
    assert len(output['records'])==1
    record=output['records'][0]
    assert record['kind']=='message'
    assert record['content']=='I am working on Synthetic message at work.'
    assert record['citations'][0]['content']==record['content']
    assert record['record_id'].startswith('r.')


@pytest.mark.parametrize('labels',[{'domains':['work','health']},{'protected_content':'unknown'},
                                  {'speech':'third_party_quote'},{'sensitivity':'special'}])
def test_automatic_labels_never_bypass_grant_or_privacy_floors(legacy,tmp_path,monkeypatch,labels):
    node,_=node_for(legacy,tmp_path,monkeypatch,labels=labels)
    node.rebuild()
    output,refused=node.search_request('Synthetic message',k=10)
    assert refused is not None or output['records']==[]


def add_fact(legacy,*,predicate='works_on',value='Synthetic message',refs=None):
    from topos.features.facts.store import FactStore
    conn=legacy[1]
    FactStore(conn).assert_fact(subject_entity_id='self',predicate=predicate,object_value=value,confidence=1,
        source_refs=refs or [{'table':'conversation_messages','record_id':'imessage:1'}],
        disclosure='owner_only',asserted_by='owner')
    conn.commit()
    return conn.execute("SELECT object_id FROM signal_objects WHERE object_type='fact'").fetchone()[0]


def test_stored_fact_with_unique_legacy_reference_gets_its_own_cited_result(legacy,tmp_path,monkeypatch):
    fact=add_fact(legacy)
    node,_=node_for(legacy,tmp_path,monkeypatch)
    with owner(): node.index.rebuild('grant-search',now=node.now[0])
    output,refused=node.search_request('Synthetic message',k=10)
    assert refused is None
    facts=[r for r in output['records'] if r['kind']=='fact']
    assert len(facts)==1
    assert facts[0]['content']=='Owner works on Synthetic message.'
    assert facts[0]['assertion']=='owner_stated'
    assert fact not in str(output)


@pytest.mark.parametrize('legacy',['visit'],indirect=True)
def test_visit_does_not_release_a_residence_fact(legacy,tmp_path,monkeypatch):
    add_fact(legacy,predicate='lives_in',value='Example Place')
    node,_=node_for(legacy,tmp_path,monkeypatch)
    node.rebuild()
    output,refused=node.search_request('Example Place',k=10)
    assert refused is None
    assert all(r['kind']!='fact' for r in output['records'])


def test_changed_fact_after_indexing_cannot_release_cached_claim(legacy,tmp_path,monkeypatch):
    fact=add_fact(legacy)
    node,_=node_for(legacy,tmp_path,monkeypatch)
    node.rebuild()
    import json
    conn=legacy[1]
    row=json.loads(conn.execute('SELECT payload_json FROM signal_objects WHERE object_id=?',(fact,)).fetchone()[0])
    row['object_value']='Private unrelated project'
    conn.execute('UPDATE signal_objects SET payload_json=? WHERE object_id=?',(json.dumps(row),fact));conn.commit()
    output,refused=node.search_request('Synthetic message',k=10)
    assert refused is not None or not output['records']


def add_goal_graph(legacy):
    import json
    from tests.permissions_v2.test_owner_identity_binding import add_entity
    from topos.storage.db.migrations.entity_edges_validity_v1 import apply_entity_edges_validity_v1_up
    import sqlite3
    conn=sqlite3.connect(legacy[0].resolver.path)
    apply_entity_edges_validity_v1_up(conn)
    conn.execute('CREATE TABLE user_goals(goal_id TEXT PRIMARY KEY,record_id TEXT,source_id TEXT,goal_text TEXT,payload_json TEXT)')
    text='finish the compiler at work by Friday'
    conn.execute('INSERT INTO user_goals VALUES(?,?,?,?,?)',('goal-1','imessage:1','imessage',text,'{}'))
    add_entity(conn,'goal-node',is_self=0,entity_type='goal')
    conn.execute('UPDATE entities SET canonical_name=?,normalized_name=? WHERE entity_id=?',(text,text,'goal-node'))
    conn.execute('INSERT INTO entity_edges(edge_id,src_entity_id,dst_entity_id,edge_type,metadata_json) VALUES(?,?,?,?,?)',
                 ('edge-1','owner-entity','goal-node','pursues',json.dumps({'source_object_id':'goal-1','actor_role':'authored'})))
    conn.commit()
    conn.close()


@pytest.mark.parametrize('legacy',['goal'],indirect=True)
def test_goal_and_graph_edge_release_with_evidence_without_completed_status(legacy,tmp_path,monkeypatch):
    add_goal_graph(legacy)
    node,_=node_for(legacy,tmp_path,monkeypatch,labels={'domains':['work','plans']})
    with owner(): node.index.rebuild('grant-search',now=node.now[0])
    output,refused=node.search_request('compiler Friday',k=10)
    assert refused is None
    assert {r['kind'] for r in output['records']}=={'message','goal','relationship'}
    goal=next(r for r in output['records'] if r['kind']=='goal')
    assert goal['status']=='stated_intention'
    edge=next(r for r in output['records'] if r['kind']=='relationship')
    assert edge['relation']=='pursues' and edge['subject']=='Owner'
    assert 'goal-node' not in str(output)


@pytest.mark.parametrize('legacy',['goal'],indirect=True)
def test_unproven_goal_cannot_become_a_goal_or_graph_result(legacy,tmp_path,monkeypatch):
    add_goal_graph(legacy)
    legacy[1].execute("UPDATE user_goals SET goal_text='sell private medical records'")
    legacy[1].commit()
    node,_=node_for(legacy,tmp_path,monkeypatch,labels={'domains':['work','plans']})
    with owner(): node.index.rebuild('grant-search',now=node.now[0])
    output,refused=node.search_request('compiler Friday',k=10)
    assert refused is None
    assert {r['kind'] for r in output['records']}=={'message'}


@pytest.mark.parametrize('legacy',['goal'],indirect=True)
@pytest.mark.parametrize('restricted', ['goal', 'endpoint', 'evidence'])
def test_excluding_a_dependency_removes_all_dependent_results(legacy,tmp_path,monkeypatch,restricted):
    add_goal_graph(legacy)
    node,identity=node_for(legacy,tmp_path,monkeypatch,labels={'domains':['work','plans']})
    node.rebuild()
    with owner():
        if restricted == 'evidence':
            from topos.permissions_v2.message_evidence import message_key
            node.corpus.reviews.opt_out(message_key(identity), now=node.now[0])
        else:
            table,record = ('user_goals','goal-1') if restricted == 'goal' else ('entities','goal-node')
            conn=legacy[1]
            columns=[r[1] for r in conn.execute('PRAGMA table_info(owner_only_records)')]
            values={'canonical_table':table,'record_id':record,'created_at':1,'reason':'synthetic restriction'}
            conn.execute('INSERT INTO owner_only_records('+','.join(k for k in columns if k in values)+') VALUES('+','.join('?' for k in columns if k in values)+')', [values[k] for k in columns if k in values]);conn.commit()
    node.rebuild()
    output,refused=node.search_request('compiler Friday',k=10)
    kinds={r['kind'] for r in output['records']} if output else set()
    assert 'relationship' not in kinds
    if restricted != 'endpoint': assert 'goal' not in kinds
    if restricted == 'evidence': assert not kinds


def test_fact_with_an_unresolved_second_source_does_not_release(legacy,tmp_path,monkeypatch):
    add_fact(legacy,refs=[{'table':'conversation_messages','record_id':'imessage:1'},
                          {'table':'conversation_messages','record_id':'missing-private-evidence'}])
    node,_=node_for(legacy,tmp_path,monkeypatch)
    node.rebuild()
    output,refused=node.search_request('Synthetic message',k=10)
    assert refused is None
    assert all(r['kind']!='fact' for r in output['records'])


def test_knowledge_model_revision_change_invalidates_index(legacy,tmp_path,monkeypatch):
    node,_=node_for(legacy,tmp_path,monkeypatch)
    node.rebuild()
    from topos.permissions_v2 import automatic_message_review
    monkeypatch.setattr(automatic_message_review,'MODEL_REVISION','f'*64)
    output,refused=node.search_request('Synthetic message',k=10)
    assert refused is not None and output is None
