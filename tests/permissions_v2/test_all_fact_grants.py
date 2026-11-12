"""All bundled fact families need explicit, versioned grant consent."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
import yaml
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2 import selective_facts as sf


def test_all_bundled_fact_types_are_offered_and_selectable_together():
    packs = [yaml.safe_load(p.read_text()) for p in (Path(__file__).parents[2] / 'topos/features/derivation/bundled_packs').glob('*.yaml')]
    expected = {p['pack'] for p in packs if 'pack' in p}
    data = sf.catalog()
    assert {o['ontology_id'] for o in data['ontologies'] if o['available']} == expected
    declaration = sf.parse_selection(sf.selection_v2(sorted(expected), assertions=['owner_stated', 'inferred']))
    assert len(declaration.ontologies) == 30


def test_old_work_consent_round_trips_without_growth():
    raw = sf.SelectiveFacts.parse(sf.selection(['work.career'])).model_dump()
    assert sf.SelectiveFacts.parse(raw).model_dump() == raw
    parsed = sf.parse_selection(raw)
    assert parsed.model_dump() == raw
    assert set(parsed.ontologies[0].predicates) == set(sf.WORK_PREDICATES)


@pytest.mark.parametrize('change', [
    lambda d: d.update(catalog_digest='f'*64),
    lambda d: d.update(assertions=[]),
    lambda d: d.update(assertions=['inferred','inferred']),
    lambda d: d.update(assertions=['predicted']),
    lambda d: d['ontologies'][0]['predicates'].append('future.predicate'),
    lambda d: d['ontologies'][0].update(revision='future/v1'),
])
def test_new_consent_is_closed(change):
    raw = sf.selection_v2(['health.physical'], assertions=['inferred'])
    change(raw)
    with pytest.raises(PolicyError): sf.parse_selection(raw)


def test_v1_cannot_parse_v2_or_be_widened_by_changing_digest():
    raw = sf.selection_v2(['work.career'], assertions=['inferred'])
    with pytest.raises(PolicyError): sf.SelectiveFacts.parse(raw)
    raw.update(version=sf.VERSION,vocabulary=sf.VOCABULARY,catalog_digest=sf.CATALOG_DIGEST)
    with pytest.raises(PolicyError): sf.parse_selection(raw)

from tests.permissions_v2.test_knowledge_search import add_fact, node_for, switch_ontologies
from tests.permissions_v2.test_reconciliation_provenance import legacy
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner
from topos.permissions_v2.expanded_facts import PACKS


def sample(schema):
    if isinstance(schema,list): return schema[0]
    if isinstance(schema,dict):
        if 'enum' in schema:return schema['enum'][0]
        if set(schema)=={'scale'}:return 'moderate'
        return {k:sample(v) for k,v in schema.items()}
    return 'Synthetic message'


def pack_fact(legacy,oid):
    fid=add_fact(legacy,disclosure='owner_only')
    conn=legacy[1]
    from topos.storage.db.migrations.derivation_provenance_v1 import apply_derivation_provenance_v1_up
    apply_derivation_provenance_v1_up(conn)
    predicate,spec=next(iter(PACKS[oid]['predicates'].items()))
    payload=json.loads(conn.execute('SELECT payload_json FROM signal_objects WHERE object_id=?',(fid,)).fetchone()[0])
    payload.update(predicate=predicate,pack=oid,altitude=spec['altitude'],actor_role='authored',
                   sensitivity=spec['sensitivity'],extractor={'pack_version':PACKS[oid]['pack_versions'][0]})
    if spec['value_type']=='structured': payload['value_struct']=sample(spec['value_schema'])
    elif spec['value_type']=='enum':payload['object_value']=spec['values'][0]
    stamp=conn.execute('SELECT event_at FROM conversation_messages').fetchone()[0]
    conn.execute('UPDATE signal_objects SET ontology_id=?,ontology_version=?,altitude=?,payload_json=?,valid_from=? WHERE object_id=?',
        (oid,PACKS[oid]['pack_versions'][0],spec['altitude'],json.dumps(payload),stamp,fid))
    conn.commit()
    return fid


def expanded_node(legacy,tmp_path,monkeypatch,oid,*,assertions=('owner_stated','inferred'),excluded=(),labels=None):
    monkeypatch.setenv('TOPOS_PERMISSIONS_V2_SELECTIVE_FACTS_ENABLED','true')
    node,_=node_for(legacy,tmp_path,monkeypatch,selective_facts=sf.selection_v2([oid],excluded,assertions=assertions),labels=labels)
    raw=deepcopy(node.search_raw)
    raw['rules']=[r for r in raw['rules'] if r['effect']=='permit']
    domains=['work','plans','hobbies','health','family','finance','relationships','home']
    for rule in raw['rules']:
        for part in ('evidence_use','release'):
            rule[part]['predicate']={'kind':'all_of','terms':[
                {'kind':'atom','attribute':'domain','operator':'intersects','values':domains},
                {'kind':'atom','attribute':'sensitivity','operator':'intersects','values':['none','personal','special']}]}
    raw['search']['result_types']=['fact']
    raw['policy_version_id']='expanded-policy'
    node.activate(raw,generation=2)
    node.search_raw=raw
    return node


@pytest.mark.parametrize('oid',sorted(PACKS))
def test_each_pack_delivers_a_fact_through_signed_grant(legacy,tmp_path,monkeypatch,oid):
    fid=pack_fact(legacy,oid)
    node=expanded_node(legacy,tmp_path,monkeypatch,oid)
    node.rebuild()
    result,error=node.search_request('owner',k=10)
    assert error is None
    assert len(result['records'])==1,result
    fact=result['records'][0]
    assert fact['ontology_id']==oid
    assert fact['assertion']=='inferred'
    assert 'Extracted or inferred' in fact['content']
    assert all(c['content']==fact['content'] for c in fact['citations'])
    assert fid not in json.dumps(result)
    switch_ontologies(legacy[1],[oid],on=False)
    result,error=node.search_request('owner',k=10)
    assert error is not None or not result['records']


@pytest.mark.parametrize('restriction',['excluded','stated_only','owner_mark','opt_out','disabled','source_excluded','unselected','special_unselected'])
def test_new_fact_families_keep_each_permission_boundary(legacy,tmp_path,monkeypatch,restriction):
    oid='health.physical';fid=pack_fact(legacy,oid)
    node=expanded_node(legacy,tmp_path,monkeypatch,oid,
        assertions=['owner_stated'] if restriction=='stated_only' else ['owner_stated','inferred'],
        excluded=[fid] if restriction=='excluded' else [])
    conn=legacy[1]
    if restriction=='owner_mark':
        conn.execute("INSERT INTO owner_only_records(canonical_table,record_id) VALUES('signal_objects',?)",(fid,));conn.commit()
    elif restriction=='opt_out':
        with owner():node.corpus.reviews.opt_out(fid,now=node.now[0])
    elif restriction=='disabled':monkeypatch.setenv('TOPOS_PERMISSIONS_V2_SELECTIVE_FACTS_ENABLED','false')
    elif restriction=='source_excluded':
        conn.execute("INSERT INTO owner_only_records(canonical_table,record_id) VALUES('conversation_messages','imessage:1')");conn.commit()
    elif restriction in ('unselected','special_unselected'):
        raw=deepcopy(node.search_raw)
        raw['policy_version_id']='narrowed'
        if restriction=='unselected':raw['search']['selective_facts']=sf.selection_v2(['work.career'],assertions=['inferred'])
        else:
            for rule in raw['rules']:
                for part in ('evidence_use','release'):
                    for term in rule[part]['predicate'].get('terms',[]):
                        if term.get('attribute')=='sensitivity':term['values']=['none','personal']
        node.activate(raw,generation=3)
    node.rebuild()
    result,error=node.search_request('owner',k=10)
    assert error is not None or not result['records']


def test_every_registered_predicate_has_a_closed_projection():
    from topos.permissions_v2.expanded_fact_projection import stored_value
    from topos.permissions_v2.expanded_facts import ontology_of
    for oid,pack in PACKS.items():
        for predicate,spec in pack['predicates'].items():
            payload=dict(predicate=predicate,pack=oid,altitude=spec['altitude'],extractor={'pack_version':pack['pack_versions'][0]})
            if spec['value_type']=='structured':payload['value_struct']=sample(spec['value_schema'])
            else:payload['object_value']=spec.get('values',['Synthetic value'])[0]
            row=dict(ontology_id=oid,ontology_version=pack['pack_versions'][0],altitude=spec['altitude'])
            assert ontology_of(row,payload)==oid
            assert stored_value(oid,payload) is not None
            if spec['value_type']=='structured':
                payload['value_struct']['private_unregistered_field']='secret'
                with pytest.raises(PolicyError):stored_value(oid,payload)
            row['ontology_version']='future'
            with pytest.raises(PolicyError):ontology_of(row,payload)


def clone_derived(legacy,fid):
    conn=legacy[1];cur=conn.execute('SELECT * FROM signal_objects WHERE object_id=?',(fid,))
    row=dict(zip((column[0] for column in cur.description),cur.fetchone()))
    row.update(object_id='derived-test-fact',object_key='derived-test-key',source_refs_json=json.dumps([{'table':'signal_objects','record_id':fid}]))
    payload=json.loads(row['payload_json']);payload.update(actor_role='synthesis',asserted_by='extracted:synthesis')
    row['payload_json']=json.dumps(payload)
    columns=','.join(row);values=','.join('?' for _ in row)
    conn.execute(f'INSERT INTO signal_objects({columns}) VALUES({values})',tuple(row.values()));conn.commit()
    return row['object_id']


def test_derived_fact_requires_every_dependency_and_rechecks_stale_rows(legacy,tmp_path,monkeypatch):
    oid='communication.style';fid=pack_fact(legacy,oid);derived=clone_derived(legacy,fid)
    node=expanded_node(legacy,tmp_path,monkeypatch,oid);node.rebuild()
    result,error=node.search_request('owner',k=10)
    assert error is None and len(result['records'])==2
    conn=legacy[1]
    # Withhold the contributor after the index was built: both disappear at the
    # release boundary, even before any rebuild.
    conn.execute("INSERT INTO owner_only_records(canonical_table,record_id) VALUES('signal_objects',?)",(fid,));conn.commit()
    result,error=node.search_request('owner',k=10)
    assert error is not None or not result['records']


def test_dependency_cycle_is_never_admitted(legacy,tmp_path,monkeypatch):
    oid='communication.style';fid=pack_fact(legacy,oid);derived=clone_derived(legacy,fid)
    conn=legacy[1]
    refs=json.loads(conn.execute('SELECT source_refs_json FROM signal_objects WHERE object_id=?',(fid,)).fetchone()[0])
    refs.append({'table':'signal_objects','record_id':derived})
    conn.execute('UPDATE signal_objects SET source_refs_json=? WHERE object_id=?',(json.dumps(refs),fid));conn.commit()
    node=expanded_node(legacy,tmp_path,monkeypatch,oid);node.rebuild()
    result,error=node.search_request('owner',k=10)
    assert error is not None or not result['records']


def test_facts_about_another_person_are_named_and_keep_entity_protections(legacy,tmp_path,monkeypatch):
    oid='net.character';fid=pack_fact(legacy,oid);conn=legacy[1]
    conn.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name) VALUES('test-person','person','River Example','river example')")
    payload=json.loads(conn.execute('SELECT payload_json FROM signal_objects WHERE object_id=?',(fid,)).fetchone()[0])
    payload['subject_entity_id']='test-person'
    conn.execute('UPDATE signal_objects SET payload_json=? WHERE object_id=?',(json.dumps(payload),fid));conn.commit()
    node=expanded_node(legacy,tmp_path,monkeypatch,oid);node.rebuild()
    result,error=node.search_request('River',k=10)
    assert error is None and len(result['records'])==1
    assert 'about River Example' in result['records'][0]['content']
    assert 'test-person' not in json.dumps(result)
    conn.execute("INSERT INTO owner_only_records(canonical_table,record_id) VALUES('entities','test-person')");conn.commit()
    result,error=node.search_request('River',k=10)
    assert error is not None or not result['records']


def test_real_pack_writer_person_bindings_are_projected_without_internal_ids(legacy,tmp_path,monkeypatch):
    from topos.features.derivation.packs import load_packs
    from topos.features.derivation.writer import DerivationWriter
    oid='net.capability';placeholder=pack_fact(legacy,oid);conn=legacy[1]
    from topos.storage.db.migrations.signal_objects_period_v1 import apply_signal_objects_period_v1_up
    apply_signal_objects_period_v1_up(conn)
    conn.execute('DELETE FROM signal_objects WHERE object_id=?',(placeholder,))
    conn.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name) VALUES('test-person','person','River Example','river example')")
    pack=load_packs(Path(__file__).parents[2]/'topos/features/derivation/bundled_packs',only=[oid])[oid]
    stamp=conn.execute('SELECT event_at FROM conversation_messages').fetchone()[0]
    result=DerivationWriter(conn,'synthetic-model').assert_pack_fact(pack=pack,predicate='net.demonstrated_skill',
        subject_entity_id='self',value={'person':'River Example','skill':'Synthetic message','basis':'reviewed_work'},
        actor_role='authored',source_refs=[{'table':'conversation_messages','record_id':'imessage:1'}],event_date=stamp)
    conn.commit()
    assert result['outcome']=='written',result
    stored=json.loads(conn.execute("SELECT payload_json FROM signal_objects WHERE object_type='fact'").fetchone()[0])
    assert stored['value_struct']['person_entity_id']=='test-person'
    node=expanded_node(legacy,tmp_path,monkeypatch,oid)
    from topos.permissions_v2.share_counts import count
    from topos.permissions_v2.registry import parse_policy
    with owner():preview=count(node.corpus.resolver,node.corpus.reviews,parse_policy(node.search_raw),now=node.now[0])
    assert any(item['eligible'] for item in preview['fact_preview']['items']),[(item['reason'], item['statement']) for item in preview['fact_preview']['items']]
    node.rebuild()
    output,error=node.search_request('River',k=10)
    assert error is None and len(output['records'])==1
    assert 'River Example' in output['records'][0]['content']
    assert 'test-person' not in json.dumps(output) and 'person_entity_id' not in json.dumps(output)
    conn.execute("INSERT INTO owner_only_records(canonical_table,record_id) VALUES('entities','test-person')");conn.commit()
    output,error=node.search_request('River',k=10)
    assert error is not None or not output['records']


@pytest.mark.parametrize('change',[
    {'predicate':[]}, {'subject_entity_id':[]}, {'sensitivity':[]}, {'object_value':[]},
    {'lineage':['untrusted']}, {'asserted_by':'extracted:addressed','actor_role':'addressed'},
    {'ontology_version':'future'},
])
def test_malformed_or_unqualified_fact_cannot_enter_the_grant(legacy,tmp_path,monkeypatch,change):
    oid='health.physical';fid=pack_fact(legacy,oid);conn=legacy[1]
    node=expanded_node(legacy,tmp_path,monkeypatch,oid)
    payload=json.loads(conn.execute('SELECT payload_json FROM signal_objects WHERE object_id=?',(fid,)).fetchone()[0]);payload.update(change)
    conn.execute('UPDATE signal_objects SET payload_json=? WHERE object_id=?',(json.dumps(payload),fid));conn.commit()
    node.rebuild()
    result,error=node.search_request('owner',k=10)
    assert error is not None or not result['records']


def test_future_fact_day_cannot_borrow_a_current_sources_permission(legacy,tmp_path,monkeypatch):
    oid='health.physical';fid=pack_fact(legacy,oid);conn=legacy[1]
    conn.execute("UPDATE signal_objects SET valid_from='2099-12-31' WHERE object_id=?",(fid,));conn.commit()
    node=expanded_node(legacy,tmp_path,monkeypatch,oid);node.rebuild()
    result,error=node.search_request('owner',k=10)
    assert error is not None or not result['records']


def test_frozen_catalog_matches_every_bundled_predicate_and_version():
    directory=Path(__file__).parents[2]/'topos/features/derivation/bundled_packs'
    for path in directory.glob('*.yaml'):
        pack=yaml.safe_load(path.read_text())
        if 'pack' not in pack:continue
        frozen=PACKS[pack['pack']]
        assert frozen['pack_versions']==[pack['version']]
        assert set(frozen['predicates'])=={p['name'] for p in pack['predicates']}
        for predicate in pack['predicates']:
            spec=frozen['predicates'][predicate['name']]
            for key in ('value_type','value_schema','values','altitude'):
                assert spec.get(key)==predicate.get(key),(pack['pack'],predicate['name'],key)


def test_structured_fact_preview_matches_explicit_inference_consent(legacy,tmp_path,monkeypatch):
    from topos.permissions_v2.share_counts import count
    from topos.permissions_v2.registry import parse_policy
    oid='net.capability';fid=pack_fact(legacy,oid)
    node=expanded_node(legacy,tmp_path,monkeypatch,oid)
    with owner():
        preview=count(node.corpus.resolver,node.corpus.reviews,parse_policy(node.search_raw),now=node.now[0])
    [item]=preview['fact_preview']['items']
    assert item['fact_id']==fid and item['eligible'] and item['assertion']=='inferred'
    assert item['statement'].startswith('Extracted or inferred fact') and item['sources']
    assert preview['kinds']['facts']['can_share']==1
    raw=deepcopy(node.search_raw)
    raw['search']['selective_facts']['assertions']=['owner_stated']
    with owner():
        preview=count(node.corpus.resolver,node.corpus.reviews,parse_policy(raw),now=node.now[0])
    [item]=preview['fact_preview']['items']
    assert not item['eligible'] and item['reason']=='fact_assertion_unselected'
    assert item['assertion']=='inferred' and item['statement']!='Unsupported structured fact'
    assert preview['kinds']['facts']['can_share']==0
