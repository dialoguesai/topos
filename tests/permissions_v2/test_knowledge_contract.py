from copy import deepcopy

import pytest

from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.knowledge_contract import KnowledgeSearchResult
from topos.permissions_v2.registry import parse_disclosure


def result(kind='message'):
    extra = {'fact':{'assertion':'owner_stated'}, 'goal':{'status':'stated_intention'},
        'relationship':{'subject':'Owner','relation':'works_on','object':'Atlas'}}.get(kind,{})
    return dict(family='canonical_record',operation='search',view_id='canonical.knowledge_search.v1',records=[dict(
        kind=kind,record_id='r.'+'1'*64,content='I work on Atlas.',source_ids=['imessage'],
        citations=[dict(record_id='r.'+'2'*64,source_id='imessage',content='I work on Atlas.')],**extra)])


@pytest.mark.parametrize('kind',['message','fact','goal','relationship'])
def test_four_closed_projected_types(kind):
    assert KnowledgeSearchResult.parse(result(kind)).records[0].kind == kind


@pytest.mark.parametrize('mutation',['raw_id','hidden_source','extra_metadata','unknown_family','no_evidence','too_many','duplicate'])
def test_disclosure_rejects_unsupported_or_unbound_fields(mutation):
    value = result()
    record = value['records'][0]
    if mutation == 'raw_id': record['record_id'] = 'imessage:1234'
    elif mutation == 'hidden_source': record['source_ids'].append('hidden-source')
    elif mutation == 'extra_metadata': record['neighbors'] = ['PRIVATE_CANARY']
    elif mutation == 'unknown_family': record['kind'] = 'contacts'
    elif mutation == 'no_evidence': record['citations'] = []
    elif mutation == 'too_many': value['records'] *= 11
    else: value['records'].append(deepcopy(record))
    with pytest.raises(PolicyError):
        KnowledgeSearchResult.parse(value)


def test_old_capability_cannot_parse_new_projection():
    for capability in ('permissions-beta/p2c-v1','permissions-beta/p2c-v2'):
        with pytest.raises(PolicyError):
            parse_disclosure(result('fact'),capability=capability)
