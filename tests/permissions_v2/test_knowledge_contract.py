from copy import deepcopy

import pytest

from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.knowledge_contract import KNOWLEDGE_MAX_K, KnowledgeSearchResult, KnowledgeSetDecision
from topos.permissions_v2.registry import parse_disclosure, parse_policy
from tests.permissions_v2.test_direct_message_search import direct_policy
from tests.permissions_v2.test_knowledge_search import knowledge_policy


def result(kind='message'):
    extra = {'fact':{'assertion':'owner_stated'}, 'goal':{'status':'stated_intention'},
        'relationship':{'subject':'Owner','relation':'works_on','object':'Atlas'}}.get(kind,{})
    return dict(family='canonical_record',operation='search',view_id='canonical.knowledge_search.v1',records=[dict(
        kind=kind,record_id='r.'+'1'*64,content='I work on Atlas.',source_ids=['imessage'],
        citations=[dict(record_id='r.'+'2'*64,source_id='imessage',content='I work on Atlas.')],**extra)])



def distinct(count):
    """`count` results that differ in every id, so only their number can refuse them."""
    records = []
    for n in range(count):
        record = deepcopy(result()['records'][0])
        record['record_id'] = 'r.' + format(n, '064x')
        record['citations'][0]['record_id'] = 'r.' + format(10_000 + n, '064x')
        records.append(record)
    return dict(result(), records=records)


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
    elif mutation == 'too_many': value = distinct(KNOWLEDGE_MAX_K + 1)
    else: value['records'].append(deepcopy(record))
    with pytest.raises(PolicyError):
        KnowledgeSearchResult.parse(value)


def test_old_capability_cannot_parse_new_projection():
    for capability in ('permissions-beta/p2c-v1','permissions-beta/p2c-v2'):
        with pytest.raises(PolicyError):
            parse_disclosure(result('fact'),capability=capability)


def test_a_result_holds_up_to_the_ceiling_a_grant_may_sign():
    assert KNOWLEDGE_MAX_K == 20
    assert len(KnowledgeSearchResult.parse(distinct(KNOWLEDGE_MAX_K)).records) == KNOWLEDGE_MAX_K


def set_decision(members):
    return dict(stage='output_release', verdict='permit', policy_hash='a' * 64, candidate_revision='b' * 64,
                evaluator_version='hard-rules/p2c-v3', matched_allow_clause_ids=['permit-content'],
                matched_deny_clause_ids=[], reason_code='rule_permit',
                required_projection_id='canonical.knowledge_search.v1', member_count=members, missing_context_codes=[])


@pytest.mark.parametrize('members', [0, 10, 20])
def test_the_set_decision_names_up_to_twenty_members(members):
    assert KnowledgeSetDecision.parse(set_decision(members)).member_count == members


def test_the_set_decision_refuses_a_twenty_first_member():
    with pytest.raises(PolicyError):
        KnowledgeSetDecision.parse(set_decision(KNOWLEDGE_MAX_K + 1))


@pytest.mark.parametrize('max_k', [1, 10, 20])
def test_a_knowledge_grant_may_sign_max_k_up_to_twenty(max_k):
    assert parse_policy(knowledge_policy(max_k)).search.max_k == max_k


@pytest.mark.parametrize('max_k', [0, 21, 25])
def test_a_knowledge_grant_signing_outside_one_to_twenty_is_refused_at_parse(max_k):
    """25 is the request grammar's ceiling (MAX_K_CEILING), not a grant's: a p2c-v3 grant stops at 20."""
    with pytest.raises(PolicyError):
        parse_policy(knowledge_policy(max_k))


def test_the_independent_message_grant_keeps_its_ten():
    """C2 widened p2c-v3 only: a p2c-v2 grant still signs at most 10 (search_contract.DirectSearchDeclaration)."""
    raw = direct_policy()
    assert parse_policy(raw).search.max_k == 10
    raw['search']['max_k'] = 11
    with pytest.raises(PolicyError):
        parse_policy(raw)
