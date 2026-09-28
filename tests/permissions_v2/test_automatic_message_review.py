"""Machine review never manufactures owner review or bypasses source authority."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_reconciliation_provenance import legacy, publish as publish_native
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner
from topos.permissions_v2.evidence import EvidenceReviewStore
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.message_evidence import qualify_message, message_key, record_message_review
from topos.permissions_v2.automatic_message_review import (
    prepare, publish, parse_assessment, is_current, machine_key, assess, MODEL,
    apply_floors,
)


def setup(fixture):
    publish_native(fixture)
    service, conn, _, _ = fixture
    resolver = service.resolver
    identity = resolver._identity('conversation_messages', 'imessage:1', 'imessage', 'native-dataset')
    with owner():
        reviews = EvidenceReviewStore(service.root.parent / 'reviews.db', resolver=resolver)
        prepared = prepare(resolver, reviews, identity)
    return resolver, reviews, identity, prepared


def answer(prepared, **updates):
    return parse_assessment(dict(domains=['work'], sensitivity='none', speech='original_message',
        protected_content='none', **updates), prepared['snapshot'].message)


def test_machine_record_is_separate_and_cannot_activate_old_capability(legacy):
    resolver, reviews, identity, prepared = setup(legacy)
    with owner():
        result = publish(resolver, reviews, prepared, answer(prepared), now=1)
        assert is_current(result, prepare(resolver, reviews, identity))
    with reviews._db() as db:
        assert reviews._current_in(db, message_key(identity)) is None
        assert reviews._current_in(db, machine_key(identity)) == result
        frozen = reviews.freeze(db)
        assert frozen.reviews[machine_key(identity)] == result
    with resolver._read() as (conn, floor), reviews._db() as db:
        with pytest.raises(PolicyError, match='message_review_required'):
            qualify_message(resolver, conn, floor, identity, reviews, db)


def test_owner_correction_during_model_call_prevents_publication(legacy):
    resolver, reviews, identity, prepared = setup(legacy)
    with owner():
        record_message_review(resolver, reviews, review_id='human-correction',
            expected_snapshot=prepared['snapshot'].model_dump(), classification=answer(prepared).model_dump(),
            expected_current_review_revision=None, reviewed_at=2)
        with pytest.raises(PolicyError, match='machine_review_conflict'):
            publish(resolver, reviews, prepared, answer(prepared), now=3)


def test_other_machine_worker_cannot_overwrite_a_newer_assessment(legacy):
    resolver, reviews, identity, prepared = setup(legacy)
    with owner():
        publish(resolver, reviews, prepared, answer(prepared), now=1)
        with pytest.raises(PolicyError, match='machine_review_conflict'):
            publish(resolver, reviews, prepared, answer(prepared), now=2)


def test_optout_during_model_call_prevents_publication(legacy):
    resolver, reviews, identity, prepared = setup(legacy)
    with owner():
        reviews.opt_out(message_key(identity), now=2)
        with pytest.raises(PolicyError, match='owner_opted_out'):
            publish(resolver, reviews, prepared, answer(prepared), now=3)


def test_context_change_prevents_publication(legacy):
    resolver, reviews, identity, prepared = setup(legacy)
    conn = legacy[1]
    columns = [r[1] for r in conn.execute('PRAGMA table_info(conversation_messages)')]
    original = dict(zip(columns, conn.execute('SELECT * FROM conversation_messages').fetchone()))
    original.update(message_id='imessage:2', content='That means my private medical appointment.', is_from_self=0)
    conn.execute('INSERT INTO conversation_messages VALUES(' + ','.join('?' for _ in columns) + ')',
                 [original.get(c) for c in columns])
    conn.commit()
    with owner(), pytest.raises(PolicyError, match='machine_review_conflict'):
        publish(resolver, reviews, prepared, answer(prepared), now=3)


def test_lost_native_proof_during_model_call_prevents_publication(legacy):
    resolver, reviews, identity, prepared = setup(legacy)
    service, conn, _, enrollment = legacy
    with owner():
        service.revoke(conn, enrollment_id=enrollment)
        with pytest.raises(PolicyError):
            publish(resolver, reviews, prepared, answer(prepared), now=3)


@pytest.mark.parametrize('raw', [None, {}, {'domains':['work']},
    '{"domains":["work"],"domains":["health"],"sensitivity":"none","speech":"original_message","protected_content":"none"}',
    {'domains':['invented'],'sensitivity':'none','speech':'original_message','protected_content':'none'},
    {'domains':['work','work'],'sensitivity':'none','speech':'original_message','protected_content':'none'},
    {'domains':['work'],'sensitivity':'none','speech':'original_message','protected_content':'none','permit':True},
])
def test_invalid_model_answers_never_become_assessments(legacy, raw):
    *_, prepared = setup(legacy)
    with pytest.raises(PolicyError):
        parse_assessment(raw, prepared['snapshot'].message)


def test_model_and_rubric_changes_make_assessment_stale(legacy):
    resolver, reviews, identity, prepared = setup(legacy)
    with owner():
        result = publish(resolver, reviews, prepared, answer(prepared), now=1)
    assert not is_current(result.model_copy(update={'model_revision':'f'*64}), prepared)
    assert not is_current(result.model_copy(update={'rubric_revision':'f'*64}), prepared)


def test_model_call_has_no_database_write_lock_and_never_truncates(legacy):
    resolver, reviews, identity, prepared = setup(legacy)
    from topos.storage.db.write_gate import db_write_lock
    observed = []
    class Transport:
        base_url = 'http://127.0.0.1:11434'
        client = None
        async def verify(self):
            observed.append('verified')
        async def post(self, url, **kwargs):
            assert not db_write_lock()._is_owned()
            request = kwargs['json']
            assert json.loads(request['messages'][1]['content']) == prepared['input']
            assert request['model'] == MODEL
            return SimpleNamespace(raise_for_status=lambda:None, json=lambda:{'model':MODEL,'done':True,
                'message':{'content':json.dumps({'domains':['work'],'sensitivity':'none',
                    'speech':'original_message','protected_content':'none'})}})
    transport = Transport()
    transport.client = transport
    result = asyncio.run(assess(prepared, transport=transport))
    assert result.domains == ['work'] and observed == ['verified']


@pytest.mark.parametrize('actor', ['other', None])
def test_nonowner_cannot_prepare_or_publish(legacy, actor):
    resolver, reviews, identity, prepared = setup(legacy)
    with owner(actor=actor), pytest.raises(PolicyError):
        prepare(resolver, reviews, identity)


@pytest.mark.parametrize('target,term,expected', [
    ('I am fixing the signal processor.', 'Al', 'none'),
    ('Al sent a project update.', 'Al', 'present'),
    ('M.E. sent a project update.', 'M.E.', 'present'),
    ('I am sending a message.', 'M.E.', 'none'),
    ('M\u200b.E. sent a project update.', 'M.E.', 'present'),
])
def test_short_protected_aliases_match_complete_tokens(legacy, target, term, expected):
    _, _, _, prepared = setup(legacy)
    result = apply_floors(answer(prepared), {
        'target': target, 'before': [], 'after': [], 'protected_terms': [term]})
    assert result.protected_content == expected


def test_protected_neighbor_blocks_reference_but_not_unrelated_work(legacy):
    *_, prepared = setup(legacy)
    base = answer(prepared)
    context = {'before':['Dr Sparrow sent the test results.'],'after':[], 'protected_terms':['dr sparrow']}
    blocked = apply_floors(base, {**context,'target':'Her results arrived yesterday.'})
    assert blocked.protected_content == 'unknown'
    unrelated = apply_floors(base, {**context,'target':'I am building a compiler at work.'})
    assert unrelated.protected_content == 'none'


def test_health_domain_cannot_have_a_lower_sensitivity(legacy):
    *_, prepared = setup(legacy)
    labels = answer(prepared).model_copy(update={'domains':['health'], 'sensitivity':'personal'})
    result = apply_floors(labels,prepared['input'])
    assert result.sensitivity == 'special'


def test_owner_preview_shows_current_automatic_labels_and_correction_precedence(legacy):
    from topos.permissions_v2.message_evidence import preview_message
    resolver,reviews,identity,prepared=setup(legacy)
    with owner():
        publish(resolver,reviews,prepared,answer(prepared),now=1)
        preview=preview_message(resolver,reviews,identity)
        assert preview['classification_origin']=='automatic'
        assert preview['classification']['domains']==['work']
        assert preview['current_review_revision'] is None
        corrected=answer(prepared).model_copy(update={'domains':['work','plans']})
        record_message_review(resolver,reviews,review_id='correction',expected_snapshot=prepared['snapshot'].model_dump(),
            classification=corrected.model_dump(),expected_current_review_revision=None,reviewed_at=2)
        preview=preview_message(resolver,reviews,identity)
        assert preview['classification_origin']=='owner'
        assert preview['classification']['domains']==['work','plans']


def test_owner_can_see_exclusion_after_reload_without_releasing_excluded_message(legacy):
    from topos.permissions_v2.message_evidence import preview_message, qualify_automatic_message
    resolver,reviews,identity,prepared=setup(legacy)
    with owner():
        publish(resolver,reviews,prepared,answer(prepared),now=1)
        reviews.opt_out(message_key(identity),now=2)
        assert preview_message(resolver,reviews,identity)['opted_out'] is True
    with resolver._read() as (conn,floor),reviews._db() as db,pytest.raises(PolicyError,match='owner_opted_out'):
        qualify_automatic_message(resolver,conn,floor,identity,reviews,db)


def test_automatic_review_cannot_send_protected_context_to_configured_remote_model(legacy,monkeypatch):
    from topos.permissions_v2 import automatic_message_review as module
    *_,prepared=setup(legacy)
    calls=[]
    def capture(*,base_url):
        calls.append(base_url)
        raise RuntimeError('synthetic transport stop')
    monkeypatch.setattr(module,'open_transport',capture)
    monkeypatch.setenv('ENGINE_OLLAMA_BASE_URL','https://external.example.test')
    with pytest.raises(RuntimeError,match='synthetic transport stop'):
        asyncio.run(module.assess(prepared))
    assert calls==['http://127.0.0.1:11434']
