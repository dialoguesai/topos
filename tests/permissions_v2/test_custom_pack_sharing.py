from copy import deepcopy
import pytest
import yaml
from topos.permissions_v2 import selective_facts as sf


def pack_yaml():
    return yaml.safe_dump({'pack':'custom.gardening','version':'1.0.0','title':'My gardening',
        'sensitivity_class':'personal','role_policy':'authored_only','net_subject':'deny',
        'disclosure_default':'owner_only','sharing_domains':['hobbies'],
        'eval':{'gold':[{'text':'I grow tomatoes','role':'authored','expect':[]}],
                'negative_controls':[{'text':'A gardening movie','role':'ambient','expect':[]}]},'consumers':['facts'],
        'predicates':[{'name':'custom.plant','value_type':'structured','cardinality':'multi',
        'temporal':'interval','altitude':'stated','value_schema':{'name':'string'}}]})


def install(tmp_path,monkeypatch):
    from topos.features.derivation import custom_packs
    monkeypatch.setattr(custom_packs,'custom_pack_dir',lambda:tmp_path/'packs')
    from topos.features.derivation.custom_packs import install_pack
    source=tmp_path/'garden.yaml';source.write_text(pack_yaml())
    return install_pack(source)


def test_installed_custom_pack_is_runtime_and_grant_catalog_member(tmp_path,monkeypatch):
    install(tmp_path,monkeypatch)
    from topos.features.derivation.packs import load_packs
    from topos.features.derivation.registry import bundled_pack_dir
    assert 'custom.gardening' in load_packs(bundled_pack_dir())
    catalog=sf.catalog()
    assert catalog['version']=='selective-facts/v3'
    assert any(p['ontology_id']=='custom.gardening' for p in catalog['ontologies'])
    from topos.permissions_v2.custom_facts import selection
    raw=selection(catalog,['custom.gardening'],assertions=['inferred'])
    assert sf.parse_selection(raw).ontologies[0].ontology_id=='custom.gardening'


def test_install_never_widens_existing_consent(tmp_path,monkeypatch):
    old=sf.selection_v2(['work.career'],assertions=['inferred'])
    install(tmp_path,monkeypatch)
    assert sf.parse_selection(old).model_dump()==old

from topos.permissions_v2.canonical import PolicyError
from tests.permissions_v2.test_reconciliation_provenance import legacy
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner
from tests.permissions_v2.test_all_fact_grants import pack_fact, expanded_node
from tests.permissions_v2.test_knowledge_search import switch_ontologies
def selection(*args,**kwargs):
    from topos.permissions_v2.custom_facts import selection as select
    return select(*args,**kwargs)
import json


def custom_node(legacy,tmp_path,monkeypatch):
    install(tmp_path,monkeypatch)
    node=expanded_node(legacy,tmp_path,monkeypatch,'work.career')
    fid=pack_fact(legacy,'work.career')
    conn=legacy[1]
    payload=json.loads(conn.execute('SELECT payload_json FROM signal_objects WHERE object_id=?',(fid,)).fetchone()[0])
    payload.update(pack='custom.gardening',predicate='custom.plant',altitude='stated',
                   extractor={'pack_version':'1.0.0','definition_digest':sf.catalog()['custom_packs']['custom.gardening']['definition_digest']},value_struct={'name':'tomatoes'})
    conn.execute('UPDATE signal_objects SET ontology_id=?,ontology_version=?,altitude=?,payload_json=? WHERE object_id=?',
                 ('custom.gardening','1.0.0','stated',json.dumps(payload),fid));conn.commit()
    raw=deepcopy(node.search_raw);raw['policy_version_id']='custom-policy'
    raw['search']['selective_facts']=selection(sf.catalog(),['custom.gardening'],assertions=['inferred'])
    node.activate(raw,generation=3);node.search_raw=raw
    from topos.features.derivation.registry import seed_pack_registry,bundled_pack_dir
    seed_pack_registry(conn,bundled_pack_dir())
    switch_ontologies(conn,['custom.gardening'])
    return node,fid


def test_custom_fact_passes_signed_grant_and_preview(legacy,tmp_path,monkeypatch):
    node,fid=custom_node(legacy,tmp_path,monkeypatch)
    node.rebuild()
    result,error=node.search_request('owner',k=10)
    assert error is None and len(result['records'])==1,result
    fact=result['records'][0]
    assert fact['ontology_id']=='custom.gardening' and 'tomatoes' in fact['content']
    assert fact['assertion']=='inferred'
    assert all(c['content']==fact['content'] for c in fact['citations'])
    assert fid not in json.dumps(result)
    from topos.permissions_v2.share_counts import count
    from topos.permissions_v2.registry import parse_policy
    with owner():
        preview=count(node.corpus.resolver,node.corpus.reviews,parse_policy(node.search_raw),now=node.now[0])
    [item]=preview['fact_preview']['items']
    assert item['fact_id']==fid and item['eligible'] and 'tomatoes' in item['statement']


@pytest.mark.parametrize('restriction',['disabled','excluded','source_protected','owner_mark','stated_only','unselected','removed','changed','other_owner','third_party_subject','unknown_field','unstamped','wrong_definition','reinstalled'])
def test_custom_grant_boundaries_rechecked_after_build(legacy,tmp_path,monkeypatch,restriction):
    node,fid=custom_node(legacy,tmp_path,monkeypatch);node.rebuild();conn=legacy[1]
    if restriction=='disabled':switch_ontologies(conn,['custom.gardening'],on=False)
    elif restriction in ('owner_mark','source_protected'):
        conn.execute('INSERT INTO owner_only_records(canonical_table,record_id) VALUES(?,?)',
            ('signal_objects',fid) if restriction=='owner_mark' else ('conversation_messages','imessage:1'));conn.commit()
    elif restriction in ('excluded','stated_only','unselected'):
        raw=deepcopy(node.search_raw);raw['policy_version_id']='changed-consent';sel=raw['search']['selective_facts']
        if restriction=='excluded':sel['excluded_fact_ids']=[fid]
        if restriction=='stated_only':sel['assertions']=['owner_stated']
        if restriction=='unselected':raw['search']['selective_facts']=selection(sf.catalog(),['work.career'],assertions=['inferred'])
        node.activate(raw,generation=4)
    elif restriction=='reinstalled':
        from topos.features.derivation.custom_packs import remove_pack,install_pack
        remove_pack('custom.gardening');install_pack(tmp_path/'garden.yaml')
    elif restriction=='other_owner':
        from topos.features.derivation import custom_packs
        monkeypatch.setattr(custom_packs,'custom_pack_dir',lambda:tmp_path/'other-owner')
    elif restriction in ('removed','changed'):
        p=tmp_path/'packs/custom.gardening.yaml'
        if restriction=='removed':p.unlink()
        else:
            d=yaml.safe_load(p.read_text());d['title']='Changed semantics';p.write_text(yaml.safe_dump(d))
    else:
        payload=json.loads(conn.execute('SELECT payload_json FROM signal_objects WHERE object_id=?',(fid,)).fetchone()[0])
        if restriction=='unstamped':payload['extractor'].pop('definition_digest')
        elif restriction=='wrong_definition':payload['extractor']['definition_digest']='0'*64
        elif restriction=='unknown_field':payload['value_struct']['secret']='must not leave'
        else:
            conn.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name) VALUES('other','person','Another Person','another person')")
            payload['subject_entity_id']='other'
        conn.execute('UPDATE signal_objects SET payload_json=? WHERE object_id=?',(json.dumps(payload),fid));conn.commit()
    result,error=node.search_request('owner',k=10)
    assert error is not None or not result['records']




def test_old_packs_need_no_extra_domain_metadata_and_scale_fields_are_portable(tmp_path, monkeypatch):
    install(tmp_path, monkeypatch)
    path=tmp_path/'packs/custom.gardening.yaml'
    raw=yaml.safe_load(path.read_text());raw.pop('sharing_domains')
    from topos.features.derivation.packs import load_scales
    from topos.features.derivation.registry import bundled_pack_dir
    name,values=next(iter(load_scales(bundled_pack_dir()).items()))
    raw['predicates'][0]['value_schema']={'name':' string ','strength':{'scale':name}}
    path.write_text(yaml.safe_dump(raw))
    spec=sf.catalog()['custom_packs']['custom.gardening']
    assert spec['domains']==[]
    assert spec['predicates']['custom.plant']['value_schema']=={'name':'string','strength':{'enum':values}}


def test_adding_an_unrelated_pack_does_not_revoke_a_grant(legacy,tmp_path,monkeypatch):
    node,fid=custom_node(legacy,tmp_path,monkeypatch);node.rebuild()
    from topos.features.derivation.custom_packs import install_pack
    raw=yaml.safe_load(pack_yaml());raw['pack']='custom.books';raw['predicates'][0]['name']='custom.book'
    path=tmp_path/'books.yaml';path.write_text(yaml.safe_dump(raw));install_pack(path)
    result,error=node.search_request('owner',k=10)
    assert error is None and len(result['records'])==1


def test_registry_definition_is_checked_before_reseeding(legacy,tmp_path,monkeypatch):
    node,fid=custom_node(legacy,tmp_path,monkeypatch)
    assert 'custom.gardening' in sf.enabled_ontologies(legacy[1])
    path=tmp_path/'packs/custom.gardening.yaml';raw=yaml.safe_load(path.read_text())
    raw['title']='New definition';path.write_text(yaml.safe_dump(raw))
    assert 'custom.gardening' not in sf.enabled_ontologies(legacy[1])


def test_bad_custom_file_does_not_break_bundled_or_other_catalogs(tmp_path,monkeypatch):
    install(tmp_path,monkeypatch)
    before=sf.catalog()
    (tmp_path/'packs/custom.bad.yaml').write_text('pack: [unclosed')
    assert sf.catalog()==before


def test_real_writer_fact_reaches_signed_recipient_output(legacy,tmp_path,monkeypatch):
    node,fid=custom_node(legacy,tmp_path,monkeypatch)
    conn=legacy[1]
    conn.execute('DELETE FROM signal_objects WHERE object_id=?',(fid,));conn.commit()
    from topos.features.derivation.custom_packs import load_custom_packs
    from topos.features.derivation.writer import DerivationWriter
    from topos.storage.db.migrations.signal_objects_period_v1 import apply_signal_objects_period_v1_up
    apply_signal_objects_period_v1_up(conn)
    stamp=conn.execute('SELECT event_at FROM conversation_messages').fetchone()[0]
    result=DerivationWriter(conn,'synthetic-model').assert_pack_fact(
        pack=load_custom_packs()['custom.gardening'],predicate='custom.plant',subject_entity_id='self',
        value={'name':'tomatoes'},actor_role='authored',confidence=1,
        source_refs=[{'table':'conversation_messages','record_id':'imessage:1'}],event_date=stamp)
    assert result['outcome']=='written'
    node.rebuild()
    output,error=node.search_request('owner',k=10)
    assert error is None and len(output['records'])==1
    assert 'tomatoes' in output['records'][0]['content']
