import json
from types import SimpleNamespace
import pytest

from tests.permissions_v2.test_reconciliation_provenance import legacy, publish
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner
from topos.permissions_v2.reconciliation_facts import (classification, validated_classification,
    grounded_facts, prepare_facts, derive_prepared)
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.evidence import EvidenceReviewStore


def test_strict_literal_grounding_does_not_accept_inferred_facts_or_payload_overrides():
    text = 'I work at Northwind.'
    assert grounded_facts('[{"predicate":"works_at","object":"Northwind"}]', text)
    assert grounded_facts('[{"works_at":"Northwind"}]', text) == [{'predicate':'works_at','object':'Northwind'}]
    for value in [
        [{'predicate':'works_at','object':'Fabrikam'}],
        [{'predicate':'works_at','object':'Northwind','subject':'someone else'}],
        [{'predicate':'unknown','object':'Northwind'}],
        {'predicate':'works_at','object':'Northwind'},
        [{'works_at':'Northwind','subject':'someone else'}],
        [{'works_at':['Northwind']}],
        [{'unknown':'Northwind'}],
    ]:
        assert grounded_facts(json.dumps(value), text) == []


@pytest.mark.parametrize('bad', [None, {}, {'domains': ['unknown'], 'sensitivity': 'none'},
    {'domains': [], 'sensitivity': 'none'}, {'domains':['work','work'],'sensitivity':'none'}])
def test_unknown_source_classification_is_not_a_permit(bad):
    with pytest.raises(PolicyError):
        classification(bad)


def test_changed_model_or_rubric_cannot_retain_classification():
    labels = classification({'domains':['work','health'], 'sensitivity':'special'})
    assert validated_classification(labels) == labels
    for key in ('version','model_revision','rubric_revision'):
        with pytest.raises(PolicyError):
            validated_classification(labels | {key:'changed'})


def fact_from_proof(legacy, labels, contract=None):
    service, conn, _, _ = legacy
    row = dict(zip((c[1] for c in conn.execute('PRAGMA table_info(conversation_messages)')),
                   conn.execute('SELECT * FROM conversation_messages').fetchone()))
    # Synthetic message is the fixture's exact native text, not a live record.
    item = {'content':row['content'], 'classification':labels,
            'facts':[{'predicate':'works_on','object':'Synthetic message'}]}
    publish(legacy, classifications={'imessage:1':labels} if labels else {},
        derive=lambda db, rows: derive_prepared(db, rows, {'imessage:1':item}) if labels else None)
    if not labels:
        from topos.features.facts.store import FactStore
        FactStore(conn).assert_fact(subject_entity_id='owner-entity',predicate='works_on',object_value='Synthetic message',
            source_refs=[{'table':'conversation_messages','record_id':'imessage:1','source_id':'imessage','dataset_id':'native-dataset'}])
    fact_id = conn.execute("SELECT object_id FROM signal_objects WHERE object_type='fact'").fetchone()[0]
    with owner():
        reviews = EvidenceReviewStore(service.root.parent / 'reviews.db', resolver=service.resolver)
    return service.resolver.qualify(fact_id, reviews=reviews, **({'contract':contract} if contract else {})), reviews, fact_id


def test_real_native_proof_and_derived_fact_qualify_with_whole_message_ceiling(legacy):
    result, _, _ = fact_from_proof(legacy, classification({'domains':['work','health'],'sensitivity':'special'}))
    assert result.verdict == 'qualified', result.reason_code
    leaves = [c for c in result.evidence.classifications if c.evidence.identity.table == 'conversation_messages']
    assert len(leaves) == 1 and leaves[0].domains == ['health','work'] and leaves[0].sensitivity == 'special'
    roots = [c for c in result.evidence.classifications if c.evidence.identity.table == 'signal_objects']
    assert roots[0].sensitivity == 'none'


def test_proof_without_whole_message_classification_does_not_release_later_facts(legacy):
    result, _, _ = fact_from_proof(legacy, None)
    assert result.verdict == 'withheld' and result.reason_code == 'native_classification_unknown'


def test_owner_fact_deselection_still_dominates_recovered_evidence(legacy):
    result, reviews, fact_id = fact_from_proof(legacy, classification({'domains':['work'],'sensitivity':'none'}))
    assert result.verdict == 'qualified', result.reason_code
    with owner():
        reviews.opt_out(fact_id, now=1789430400)
    assert legacy[0].resolver.qualify(fact_id, reviews=reviews).reason_code == 'owner_opted_out'


@pytest.mark.asyncio
async def test_oversize_quoted_and_nonfirstperson_messages_never_reach_model():
    class Transport:
        async def verify(self): pass
        async def label(self, text): raise AssertionError('must not call model')
    prepared, stats = await prepare_facts([
        {'content': 'I work at ' + 'X'*8000},
        {'content': 'Someone else works at Northwind.'},
        {'content': 'I said “someone else works at Northwind”.'},
    ], transport=Transport())
    assert prepared == {} and stats == {'not_candidate':3}


@pytest.mark.parametrize('domains,sensitivity,expected', [
    (['work'], 'none', 'permit'),
    (['work','health'], 'special', 'deny'),
    (['work','family'], 'personal', 'deny'),
])
def test_actual_policy_withholds_whole_mixed_message(legacy, domains, sensitivity, expected):
    from tests.permissions_v2.message_search_corpus import p2a_v2_policy
    from topos.permissions_v2.registry import parse_policy
    from topos.permissions_v2.release import source_message_decision
    from topos.permissions_v2.identity import ATTESTED_CONTRACT
    result, reviews, fact_id = fact_from_proof(legacy, classification({'domains':domains,'sensitivity':sensitivity}))
    evidence = legacy[0].resolver.qualify(fact_id, reviews=reviews, contract=ATTESTED_CONTRACT).evidence
    raw = p2a_v2_policy()
    raw['source_universe']['source_ids'] = ['imessage']
    for rule in raw['rules']:
        rule['evidence_use']['sources'] = {'kind':'only','values':['imessage']}
    assert source_message_decision(parse_policy(raw), evidence).verdict == expected


@pytest.mark.asyncio
async def test_preparation_is_bounded_and_cannot_accept_model_invented_values():
    from topos.permissions_v2.shadow_labeler_local import MODEL
    from topos.permissions_v2.reconciliation_facts import MAX_CANDIDATES
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {'model':MODEL, 'done':True, 'message':{'content':'[{"predicate":"works_at","object":"Fabrikam"}]'}}
    class Transport:
        def __init__(self):
            self.calls = 0
            self.client = self
            self.base_url = 'http://synthetic'
        async def verify(self): pass
        async def label(self, text):
            self.calls += 1
            return {'domains':['work'], 'sensitivity':'none'}
        async def post(self, *args, **kwargs): return Response()
    transport = Transport()
    prepared, stats = await prepare_facts([
        {'message_id':str(i), 'content':'I work at Northwind.'} for i in range(MAX_CANDIDATES+5)
    ], transport=transport)
    assert prepared == {} and transport.calls == MAX_CANDIDATES
    assert stats['batch_limit'] == 5 and stats['no_grounded_fact'] == MAX_CANDIDATES


def test_native_derivation_uses_literal_owner_without_attesting_graph_aliases(legacy):
    from topos.permissions_v2.reconciliation_facts import owner_subject
    from tests.permissions_v2.test_owner_identity_binding import add_entity
    from topos.permissions_v2.identity import entries
    service, conn, _, _ = legacy
    before = entries(conn)
    add_entity(conn, 'second-self')
    assert owner_subject(conn) == 'self'
    assert entries(conn) == before
    conn.rollback()


def test_reserved_self_entity_shadow_still_blocks_native_derivation(legacy):
    from topos.permissions_v2.reconciliation_facts import owner_subject
    from tests.permissions_v2.test_owner_identity_binding import add_entity
    add_entity(legacy[1], 'self')
    with pytest.raises(PolicyError, match='owner_subject_ambiguous'):
        owner_subject(legacy[1])


@pytest.mark.parametrize('legacy', ['unattested'], indirect=True)
def test_four_unattested_aliases_cannot_be_borrowed_but_native_literal_owner_qualifies(legacy):
    from topos.permissions_v2.identity import ATTESTED_CONTRACT, attested_subjects
    assert attested_subjects(legacy[1]) == set()
    result, reviews, fact_id = fact_from_proof(legacy,
        classification({'domains':['work'],'sensitivity':'none'}), contract=ATTESTED_CONTRACT)
    assert result.verdict == 'qualified', result.reason_code
    payload = json.loads(legacy[1].execute('SELECT payload_json FROM signal_objects WHERE object_id=?', (fact_id,)).fetchone()[0])
    assert payload['subject_entity_id'] == 'self'
    assert attested_subjects(legacy[1]) == set()
    assert legacy[0].resolver.qualify(fact_id, reviews=reviews).reason_code == 'owner_subject_ambiguous'
