import json

import pytest

from tests.permissions_v2.test_reconciliation_provenance import legacy, publish
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner
from topos.permissions_v2.native_claim_grounding import explicitly_states_claim
from topos.permissions_v2.reconciliation_facts import classification, grounded_facts
from topos.permissions_v2.evidence import EvidenceReviewStore
from topos.permissions_v2.identity import ATTESTED_CONTRACT


@pytest.mark.parametrize('text', [
    'I will be at Example Place!', 'I am in Example Place.',
    'I am visiting Example Place.', 'I moved past Example Place.',
    'I do not live in Example Place.', 'Do I live in Example Place?',
    'If I live in Example Place, I could walk.',
    'My friend says I live in Example Place.', '“I live in Example Place.”',
    'I live in Example Place. That was a lie.',
    'I used to live in Example Place.', 'I plan to live in Example Place.',
])
def test_object_mention_does_not_establish_residence(text):
    assert not explicitly_states_claim(text, 'lives_in', 'Example Place')
    assert grounded_facts('[{"lives_in":"Example Place"}]', text) == []


@pytest.mark.parametrize('predicate,text,value', [
    ('works_at', 'I work at Northwind.', 'Northwind'),
    ('works_on', 'My work project is Example Project.', 'Example Project'),
    ('works_on', 'I am working on Example Project at work.', 'Example Project'),
    ('prefers', 'I prefer Coffee.', 'Coffee'),
    ('lives_in', 'I currently live in Example City.', 'Example City'),
    ('practices', 'I practice Yoga.', 'Yoga'),
    ('member_of', 'I am a member of Example Club.', 'Example Club'),
])
def test_direct_claims_preserve_the_exact_relation(predicate, text, value):
    assert grounded_facts(json.dumps([{'predicate':predicate, 'object':value}]), text)
    wrong = 'lives_in' if predicate != 'lives_in' else 'works_at'
    assert grounded_facts(json.dumps([{'predicate':wrong, 'object':value}]), text) == []


@pytest.mark.parametrize('legacy', ['visit'], indirect=True)
def test_already_published_visit_fact_cannot_authorize_source_release(legacy):
    from topos.features.facts.store import FactStore
    service, conn, _, _ = legacy
    labels = classification({'domains':['home'], 'sensitivity':'personal'})
    # Reproduce the old writer, deliberately bypassing the newly fixed producer.
    def old_writer(db, rows):
        FactStore(db).assert_fact(subject_entity_id='self', predicate='lives_in',
            object_value='Example Place', confidence=0.55,
            source_refs=[{'table':'conversation_messages', 'record_id':'imessage:1',
                          'source_id':'imessage', 'dataset_id':'native-dataset'}],
            disclosure='owner_only', asserted_by='owner')
    publish(legacy, classifications={'imessage:1':labels}, derive=old_writer)
    fact = conn.execute("SELECT object_id FROM signal_objects WHERE object_type='fact'").fetchone()[0]
    with owner():
        reviews = EvidenceReviewStore(service.root.parent / 'reviews.db', resolver=service.resolver)
    result = service.resolver.qualify(fact, reviews=reviews, contract=ATTESTED_CONTRACT)
    assert result.verdict == 'withheld'
    assert result.reason_code == 'native_fact_relation_unproven'
    # Refusing the derived claim must not mutate the owner's original message.
    assert conn.execute('SELECT content FROM conversation_messages').fetchone()[0] == 'I will be at Example Place!'
