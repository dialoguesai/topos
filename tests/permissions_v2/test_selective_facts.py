"""Ontology consent through ordinary search, including the real snapshot producer."""
from copy import deepcopy
from types import SimpleNamespace
import json
import time
import pytest
from tests.permissions_v2.test_knowledge_search import add_fact, knowledge_policy, node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner
from tests.permissions_v2.test_ingest_snapshot_work_canary import (
    canonical, corpus, facts, ingest, lane, paired_runtime, projection_runtime, run_lane, attest_selves, SELF_ENTITY, owner_message,
)
from tests.permissions_v2.message_search_harness import Node
from tests.permissions_v2 import message_search_corpus as mc
from topos.permissions_v2.automatic_message_review import prepare, publish, parse_assessment
from topos.permissions_v2.selective_facts import SelectiveFacts, catalog, selection, ontology_of, require_active_output
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.registry import parse_policy


def explicit_node(legacy,tmp_path,monkeypatch,*,excluded=(),labels=None):
    monkeypatch.setenv('TOPOS_PERMISSIONS_V2_SELECTIVE_FACTS_ENABLED','true')
    return node_for(legacy,tmp_path,monkeypatch,labels=labels,
                    selective_facts=selection(['work.career'],excluded))[0]


def test_positive_and_grant_exclusion_are_independent_of_global_review(legacy,tmp_path,monkeypatch):
    fid=add_fact(legacy,disclosure='scoped');node=explicit_node(legacy,tmp_path,monkeypatch);node.rebuild()
    result,error=node.search_request('Synthetic message',k=10);assert error is None
    [record]=[r for r in result['records'] if r['kind']=='fact']
    assert record['ontology_id']=='work.career' and record['predicate']=='works_on'
    assert record['assertion']=='owner_stated' and all(c['content']==record['content'] for c in record['citations'])
    assert fid not in json.dumps(result)
    raw=deepcopy(node.search_raw);raw['policy_version_id']='excluded-version'
    raw['binding']['grant_id']='excluded-grant';raw['binding']['assignment_id']='excluded-assignment'
    raw['search']['selective_facts']=selection(['work.career'],[fid]);node.activate(raw)
    node.rebuild()
    excluded=node
    result,error=excluded.search_request('Synthetic message',k=10,grant_id='excluded-grant')
    assert error is None and all(r['kind']!='fact' for r in result['records'])
    result,error=node.search_request('Synthetic message',k=10)
    assert error is None and any(r['kind']=='fact' for r in result['records'])


@pytest.mark.parametrize('labels',[dict(domains=['health','work']),dict(sensitivity='special'),
                                   dict(protected_content='unknown'),dict(speech='third_party_quote')])
def test_selective_facts_still_apply_whole_source_privacy_floors(legacy,tmp_path,monkeypatch,labels):
    add_fact(legacy,disclosure='scoped');node=explicit_node(legacy,tmp_path,monkeypatch,labels=labels);node.rebuild()
    result,error=node.search_request('Synthetic message',k=10);assert error is not None or result['records']==[]


def test_owner_only_is_never_converted_by_selection(legacy,tmp_path,monkeypatch):
    add_fact(legacy,disclosure='owner_only');node=explicit_node(legacy,tmp_path,monkeypatch);node.rebuild()
    result,error=node.search_request('Synthetic message',k=10)
    assert error is None and all(r['kind']!='fact' for r in result['records'])


def test_switch_change_invalidates_existing_index(legacy,tmp_path,monkeypatch):
    add_fact(legacy,disclosure='scoped');node=explicit_node(legacy,tmp_path,monkeypatch);node.rebuild()
    result,error=node.search_request('Synthetic message',k=10)
    assert error is None and any(r['kind']=='fact' for r in result['records'])
    monkeypatch.setenv('TOPOS_PERMISSIONS_V2_SELECTIVE_FACTS_ENABLED','false')
    result,error=node.search_request('Synthetic message',k=10)
    assert error is not None or all(r['kind']!='fact' for r in result['records'])
    node.rebuild();result,error=node.search_request('Synthetic message',k=10)
    assert error is None and all(r['kind']!='fact' for r in result['records'])


@pytest.mark.parametrize('change',[lambda x:x.update(extra=True),lambda x:x.update(catalog_digest='f'*64),
    lambda x:x.update(ontologies=[]),lambda x:x['ontologies'][0].update(revision='future/v2'),
    lambda x:x['ontologies'][0]['predicates'].append('future.predicate'),
    lambda x:x.update(excluded_fact_ids=['same','same']),lambda x:x['ontologies'].append(deepcopy(x['ontologies'][0]))])
def test_closed_selection_rejects_unknown_stale_widened_and_duplicate_consent(change):
    raw=selection(['work.career']);change(raw)
    with pytest.raises((ValueError,PolicyError)):SelectiveFacts.parse(raw)


def test_absent_selection_preserves_old_policy_bytes_and_null_is_not_absent():
    raw=knowledge_policy();raw['search']['release_event_time']='none'
    assert parse_policy(raw).model_dump()==raw
    raw['search']['selective_facts']=None
    with pytest.raises((ValueError,PolicyError)):parse_policy(raw)


def test_unknown_ontology_and_malformed_producer_fail_closed():
    for extractor in ('forged',[],True):
        with pytest.raises(PolicyError):
            ontology_of(dict(ontology_id='work.career',ontology_version='0.2.3',altitude='stated'),
                dict(predicate='work.project',pack='work.career',altitude='stated',extractor=extractor))
    with pytest.raises(PolicyError):ontology_of(dict(extractor_version='future'),dict(predicate='works_on'))
    for field,value in [('ontology_id','health.physical'),('ontology_version','future')]:
        with pytest.raises(PolicyError):
            ontology_of(dict(ontology_id='work.career',ontology_version='0.2.3',altitude='stated'),
                dict(predicate='work.project',pack='work.career',altitude='stated',
                     extractor={'pack_version':'0.2.3'},**{field:value}))


def test_catalog_has_no_counts_or_default_selected_ontology():
    data=catalog();assert len(data['ontologies'])==28
    assert [o['ontology_id'] for o in data['ontologies'] if o['available']]==['work.career']
    assert 'selected' not in json.dumps(data) and 'count' not in json.dumps(data)


@pytest.mark.parametrize('predicate,statement', [
    ('works_at', 'I work at Northwind.'), ('worked_at', 'I worked at Northwind.'),
    ('works_on', 'My current work project is Northwind.'), ('work.project', 'I am working on Northwind.')])
def test_all_admitted_predicates_require_complete_atomic_owner_statement(predicate,statement):
    from topos.permissions_v2.native_claim_grounding import explicitly_states_claim
    assert explicitly_states_claim(statement,predicate,'Northwind')
    for text in [f'"{statement}"',f'Maybe {statement}',f'{statement} My health changed.',statement.replace('Northwind','Northwind or Atlas')]:
        assert not explicitly_states_claim(text,predicate,'Northwind')


def test_final_send_boundary_observes_switch_for_fact_records_and_answer_only():
    for output,selective in [(dict(records=[dict(kind='fact')]),False),(dict(outcome='answered',answer='work fact'),True)]:
        require_active_output(output,enabled=True,selective_answer=selective)
        with pytest.raises(PolicyError,match='permission_denied'):
            require_active_output(output,enabled=False,selective_answer=selective)
    require_active_output(dict(records=[dict(kind='message')]),enabled=False)


@pytest.mark.asyncio
async def test_signed_snapshot_production_fact_reaches_ordinary_facts_only_search(lane,tmp_path,monkeypatch):
    monkeypatch.setenv('TOPOS_PERMISSIONS_V2_SELECTIVE_FACTS_ENABLED','true')
    await attest_selves(lane,[SELF_ENTITY])
    run=await run_lane(lane,[(1,'My current work project is Northwind.'),(0,'A private correspondent reply.')])
    assert run.result.status=='ok'
    [fact]=facts(lane,active_only=True)
    assert fact.payload['disclosure']=='scoped' and fact.payload['predicate']=='works_on'
    await owner_message('evidence','preview',{'fact_id':fact.object_id})
    with owner():
        service=lane.runtime.evidence_reviews();resolver,reviews=service.resolver,service.reviews
        ref=fact.refs[0]
        identity=resolver._identity(ref['table'],ref['record_id'],ref['source_id'],ref.get('dataset_id'))
        prepared=prepare(resolver,reviews,identity)
        assessment=parse_assessment(dict(domains=['work'],sensitivity='none',speech='original_message',
                                         protected_content='none'),prepared['snapshot'].message)
        publish(resolver,reviews,prepared,assessment,now=int(time.time()))
    now=int(time.time());monkeypatch.setattr(mc,'NOW',now)
    raw=knowledge_policy();raw['search']['result_types']=['fact'];raw['search']['selective_facts']=selection(['work.career'])
    node=Node(SimpleNamespace(resolver=resolver,reviews=reviews,path=resolver.path),tmp_path/'ordinary-node',
              model=None,search_raw=raw,now=now)
    node.rebuild();result,error=node.search_request('Northwind',k=10);assert error is None
    [record]=result['records']
    assert record['kind']=='fact' and record['content']=='Owner works on Northwind.'
    assert record['citations'][0]['content']==record['content']
    assert 'My current work project' not in json.dumps(result) and 'private correspondent' not in json.dumps(result)
    await ingest(lane,dict(operation='revoke',enrollment_id=run.enrollment.enrollment_id))
    result,error=node.search_request('Northwind',k=10);assert error is not None or result['records']==[]


@pytest.mark.parametrize('field,value', [('altitude','inferred'),('altitude','predicted'),('actor_role','addressed'),
    ('asserted_by','third_party'),('dependencies',['other-fact']),('lineage',{'fact_id':'other-fact'}),
    ('ontology_id','health.physical')])
def test_unsupported_fact_semantics_never_enter_search(legacy,tmp_path,monkeypatch,field,value):
    fid=add_fact(legacy,disclosure='scoped')
    conn=legacy[1];payload=json.loads(conn.execute('SELECT payload_json FROM signal_objects WHERE object_id=?',(fid,)).fetchone()[0])
    payload[field]=value
    conn.execute('UPDATE signal_objects SET payload_json=? WHERE object_id=?',(json.dumps(payload),fid));conn.commit()
    node=explicit_node(legacy,tmp_path,monkeypatch);node.rebuild()
    result,error=node.search_request('Synthetic message',k=10)
    assert error is None and all(r['kind']!='fact' for r in result['records'])


def test_batch_and_owner_preview_use_same_selection_and_exclusion(legacy,tmp_path,monkeypatch):
    from topos.permissions_v2.share_counts import count
    fid=add_fact(legacy,disclosure='scoped');node=explicit_node(legacy,tmp_path,monkeypatch);node.rebuild()
    outputs,error=node.search_batch_request(['Synthetic message','Synthetic'],k=10)
    assert error is None and all(any(r['kind']=='fact' for r in output['records']) for output in outputs)
    with owner():
        policy=parse_policy(node.search_raw)
        preview=count(node.corpus.resolver,node.corpus.reviews,policy,now=node.now[0])
    assert preview['kinds']['facts']['can_share']==1
    [item]=preview['fact_preview']['items']
    assert item['fact_id']==fid and item['eligible'] and item['sources']
    raw=deepcopy(node.search_raw);raw['search']['selective_facts']=selection(['work.career'],[fid])
    with owner():preview=count(node.corpus.resolver,node.corpus.reviews,parse_policy(raw),now=node.now[0])
    [item]=preview['fact_preview']['items']
    assert not item['eligible'] and item['reason']=='fact_excluded_from_grant'
    assert preview['kinds']['facts']['held_back']['you_held_back']==1
    with owner():preview=count(node.corpus.resolver,node.corpus.reviews,policy,now=node.now[0],fact_preview_offset=100)
    assert preview['fact_preview']==dict(offset=100,total=1,items=[])


@pytest.mark.parametrize('mode',['only','with_sources'])
def test_answer_context_and_cached_fetch_respect_fact_switch(legacy,tmp_path,monkeypatch,mode):
    from tests.permissions_v2.test_answer_release import _service
    from tests.permissions_v2.message_search_harness import recipient
    from topos.permissions_v2.answer_protocol import ASK,FETCH,verify_node_answer
    add_fact(legacy,disclosure='scoped');monkeypatch.setenv('TOPOS_PERMISSIONS_V2_SELECTIVE_FACTS_ENABLED','true')
    node,_=node_for(legacy,tmp_path,monkeypatch,answers=mode,result_types=['fact'],selective_facts=selection(['work.career']))
    node.rebuild();service=_service(node,'The owner is working on a synthetic task [1].');prompts=[]
    async def generate(prompt,*,deadline):
        prompts.append(str(prompt));return 'The owner is working on a synthetic task [1].'
    service.generate=generate
    def call(kind,payload):
        signed=node._envelope('grant-search',kind,payload,node.next_id('selective-answer'))
        with recipient():result,output=(service.submit if kind==ASK else service.fetch)(envelope=signed.model_dump(),payload=payload,request_id=signed.request_id)
        verify_node_answer(result,trusted_keys={'node-key':node.node_key.public_key().public_bytes_raw()},envelope=signed,output=output,mode=mode,now=node.now[0])
        return output
    try:
        job=call(ASK,dict(question='Synthetic message'))['answer_id']
        for _ in range(100):
            body=call(FETCH,dict(answer_id=job))
            if 'outcome' in body:break
            time.sleep(.01)
        assert body['outcome']=='answered' and len(prompts)==1
        assert 'I am working on Synthetic message at work' not in prompts[0]
        if mode=='with_sources':assert all(r['kind']=='fact' for r in body['records'])
        monkeypatch.setenv('TOPOS_PERMISSIONS_V2_SELECTIVE_FACTS_ENABLED','false')
        try:
            body=call(FETCH,dict(answer_id=job))
            assert body.get('outcome')!='answered' and 'answer' not in body
        except PolicyError:pass
    finally:service.close()
