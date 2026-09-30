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


# --- OD-54: an AI-chat prompt's neighbours are the owner's own turns -----------------------------

CAPTURE_SOURCE, CAPTURE_APP = 'chatgpt_ui_conversation', 'chatgpt-shadow-extension'
CHAT_START = 1_758_000_000
REPLY = 'Here is a synthetic reply about the plan. ' * 220   # 9,240 chars: two exceed MAX_CONTEXT_CHARS
TURNS = [('u0', 'user', 'I am planning the synthetic sprint at work.'), ('a0', 'assistant', REPLY),
         ('u1', 'user', 'I am listing the synthetic tasks.'), ('a1', 'assistant', REPLY),
         ('u2', 'user', 'I am drafting the synthetic plan for work.'), ('a2', 'assistant', REPLY),
         ('u3', 'user', 'I am sending the synthetic plan on Friday.'), ('a3', 'assistant', REPLY),
         ('u4', 'human', 'I am closing the synthetic sprint.')]


def iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def add_turn(conn, message_id, sender, text, epoch):
    # Owner turns carry the capture app's stamp (OD-39), so each passes the native source
    # checks as a node's own capture prompt does; replies are unstamped, as the extension writes them.
    stamped = sender in ('human', 'user')
    conn.execute('INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, event_at, content, '
                 'source_id, writer_class, writer_app_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                 (message_id, 'chat-1', sender, iso(epoch), text, CAPTURE_SOURCE,
                  'owner_app' if stamped else None, CAPTURE_APP if stamped else None))
    conn.commit()


def capture_conversation(conn, owner_id, turns, *, start=CHAT_START):
    """The owner's capture conversation holding `turns` [(message_id, sender_type, text)], a minute apart."""
    from topos.storage.canonical.ai_chat import CanonicalTablesManager
    from topos.storage.db.migrations.actor_role_v1 import apply_actor_role_v1_up
    conn.execute('DROP TABLE ai_chat_messages')  # the legacy fixture's two-column stand-in
    CanonicalTablesManager(conn)
    apply_actor_role_v1_up(conn)
    conn.execute('INSERT INTO ai_chat_conversations (conversation_id, owner_user_id, title, source_id, created_at, '
                 'updated_at) VALUES (?, ?, NULL, ?, ?, ?)', ('chat-1', owner_id, CAPTURE_SOURCE, iso(start), iso(start)))
    for minute, (message_id, sender, text) in enumerate(turns):
        add_turn(conn, message_id, sender, text, start + 60 * minute)


def chat_setup(fixture, monkeypatch, turns=TURNS):
    monkeypatch.delenv('TOPOS_OWNER_CAPTURE_APP_IDS', raising=False)  # the OD-39 default capture list
    resolver, reviews, _, _ = setup(fixture)
    capture_conversation(fixture[1], resolver.binding.owner_id, turns)
    return resolver, reviews, lambda message_id: resolver._identity('ai_chat_messages', message_id, CAPTURE_SOURCE)


def pre_od54_context(conn, identity, row, terms):
    """context_for as engine main 6ed0bab4 had it, before OD-54, without its cap: every speaker, version v2."""
    scope, args = 'conversation_id=? AND source_id=?', [row['conversation_id'], identity.source_id]
    if identity.table == 'conversation_messages':
        scope += ' AND dataset_id=?'
        args.append(identity.dataset_id)
    before = conn.execute(f'SELECT message_id,content,event_at FROM {identity.table} WHERE {scope} '
        'AND (event_at,message_id)<(?,?) ORDER BY event_at DESC,message_id DESC LIMIT 2',
        (*args, row['event_at'], identity.record_id)).fetchall()
    after = conn.execute(f'SELECT message_id,content,event_at FROM {identity.table} WHERE {scope} '
        'AND (event_at,message_id)>(?,?) ORDER BY event_at,message_id LIMIT 2',
        (*args, row['event_at'], identity.record_id)).fetchall()
    context = [list(r) for r in [*reversed(before), *after]]
    return (digest({'version': 'message-classifier-context/v2', 'context': context, 'protected_terms': terms}),
            {'before': [r[1] for r in reversed(before)], 'after': [r[1] for r in after], 'protected_terms': terms})


def test_long_assistant_replies_no_longer_block_assessing_an_ai_chat_prompt(legacy, monkeypatch):
    """OD-54: the reviewer reads the owner's own turns around a prompt; the replies between them are not context."""
    from topos.permissions_v2.automatic_message_review import MAX_CONTEXT_CHARS
    from topos.permissions_v2.message_evidence import qualify_automatic_message
    resolver, reviews, identity_of = chat_setup(legacy, monkeypatch)
    text = {message_id: body for message_id, _, body in TURNS}
    identity = identity_of('u2')
    with resolver._read() as (conn, _floor):
        _, old = pre_od54_context(conn, identity, resolver._load(conn, identity), [])
    # The old window was u1, a1 | a2, u3: two replies over the cap together, so no pass could assess this prompt.
    assert old['before'] == [text['u1'], REPLY] and old['after'] == [REPLY, text['u3']]
    assert sum(map(len, old['before'] + old['after'])) > MAX_CONTEXT_CHARS
    with owner():
        prepared = prepare(resolver, reviews, identity)
    assert prepared['input']['before'] == [text['u0'], text['u1']]
    assert prepared['input']['after'] == [text['u3'], text['u4']]   # 'human' is the owner too
    with owner():
        result = publish(resolver, reviews, prepared, answer(prepared), now=1)
        assert is_current(result, prepare(resolver, reviews, identity))
    with resolver._read() as (conn, floor), reviews._db() as db:
        qualified, _ = qualify_automatic_message(resolver, conn, floor, identity, reviews, db)
    assert qualified.review_id == result.review_id


def test_an_ai_chat_prompts_context_moves_with_owner_turns_and_not_with_replies(legacy, monkeypatch):
    resolver, reviews, identity_of = chat_setup(legacy, monkeypatch)
    identity, conn = identity_of('u2'), legacy[1]
    with owner():
        prepared = prepare(resolver, reviews, identity)
        result = publish(resolver, reviews, prepared, answer(prepared), now=1)
    add_turn(conn, 'a1b', 'assistant', REPLY, CHAT_START + 210)   # a reply nearer the prompt than u1
    add_turn(conn, 'a2b', 'assistant', 'A short synthetic reply.', CHAT_START + 250)
    with owner():
        again = prepare(resolver, reviews, identity)
    assert again['context_revision'] == prepared['context_revision'] and is_current(result, again)
    add_turn(conn, 'u1b', 'user', 'I am adding one synthetic task.', CHAT_START + 225)   # a nearer owner turn
    with owner():
        moved = prepare(resolver, reviews, identity)
    assert moved['input']['before'] == ['I am listing the synthetic tasks.', 'I am adding one synthetic task.']
    assert not is_current(result, moved)
    with owner(), pytest.raises(PolicyError, match='machine_review_conflict'):
        publish(resolver, reviews, prepared, answer(prepared), now=2)


def test_an_ai_chat_assessment_under_the_old_rule_reruns_even_where_both_rules_pick_the_same_turns(legacy, monkeypatch):
    """The v3 version string, not only a different window, retires every AI-chat assessment made before OD-54."""
    turns = [(f'u{i}', 'user', f'I am writing synthetic plan step {i} for work.') for i in range(5)]
    resolver, reviews, identity_of = chat_setup(legacy, monkeypatch, turns)
    identity = identity_of('u2')
    with owner():
        prepared = prepare(resolver, reviews, identity)
        result = publish(resolver, reviews, prepared, answer(prepared), now=1)
    with resolver._read() as (conn, _floor):
        old_revision, old = pre_od54_context(conn, identity, resolver._load(conn, identity),
                                             prepared['input']['protected_terms'])
    assert old == {key: prepared['input'][key] for key in ('before', 'after', 'protected_terms')}
    assert len(old['before']) == len(old['after']) == 2
    assert old_revision != prepared['context_revision']
    assert not is_current(result.model_copy(update={'context_revision': old_revision}), prepared)
    assert is_current(result, prepared)


def test_imessage_context_and_revision_are_unchanged_by_od54(legacy):
    """conversation_messages keeps its rule byte for byte: both speakers' nearest rows, version v2."""
    from topos.permissions_v2.automatic_message_review import context_for
    from topos.permissions_v2.fact_eligibility import canonical_utc_microseconds
    resolver, reviews, identity, _ = setup(legacy)
    conn = legacy[1]
    columns = [r[1] for r in conn.execute('PRAGMA table_info(conversation_messages)')]
    target = dict(zip(columns, conn.execute('SELECT * FROM conversation_messages').fetchone()))
    stamp = canonical_utc_microseconds(target['event_at']) // 1_000_000
    neighbours = [('imessage:b3', -180, 0, 'Too far back for the synthetic window.'),
                  ('imessage:b2', -120, 1, 'I am setting up the synthetic demo.'),
                  ('imessage:b1', -60, 0, 'Can you send the synthetic demo?'),
                  ('imessage:a1', 60, 0, 'Thanks for the synthetic demo.'),
                  ('imessage:a2', 120, 1, 'I will send the synthetic notes.'),
                  ('imessage:a3', 180, 0, 'Too late for the synthetic window.')]
    for message_id, offset, from_self, text in neighbours:
        row = {**target, 'message_id': message_id, 'event_at': iso(stamp + offset), 'is_from_self': from_self,
               'content': text}
        conn.execute('INSERT INTO conversation_messages VALUES(' + ','.join('?' * len(columns)) + ')',
                     [row.get(c) for c in columns])
    conn.commit()
    with owner():
        prepared = prepare(resolver, reviews, identity)
        result = publish(resolver, reviews, prepared, answer(prepared), now=1)
    with resolver._read() as (read, _floor):
        row = resolver._load(read, identity)
        frozen = pre_od54_context(read, identity, row, prepared['input']['protected_terms'])
        assert context_for(read, identity, row, boundary=resolver.entity_boundary(read)) == frozen
    # Not vacuous: the window holds another person's messages, so a speaker filter here would change it.
    assert frozen[1]['before'] == ['I am setting up the synthetic demo.', 'Can you send the synthetic demo?']
    assert frozen[1]['after'] == ['Thanks for the synthetic demo.', 'I will send the synthetic notes.']
    assert prepared['context_revision'] == result.context_revision == frozen[0]


def test_the_owner_turn_filter_keeps_the_context_index_and_needs_no_sort(legacy, monkeypatch):
    from topos.permissions_v2.automatic_message_review import context_for
    resolver, _, identity_of = chat_setup(legacy, monkeypatch)
    identity = identity_of('u2')

    class Recording:
        def __init__(self, conn):
            self.conn, self.queries = conn, []

        def execute(self, sql, params=()):
            self.queries.append((sql, params))
            return self.conn.execute(sql, params)

    with resolver._read() as (conn, _floor):
        recording = Recording(conn)
        context_for(recording, identity, resolver._load(conn, identity), boundary=resolver.entity_boundary(conn))
        plans = [' '.join(str(r[3]) for r in conn.execute('EXPLAIN QUERY PLAN ' + sql, params))
                 for sql, params in recording.queries]
    assert len(plans) == 2 and all("sender_type IN ('human','user')" in sql for sql, _ in recording.queries)
    assert all('idx_ai_chat_messages_permission_context' in plan and 'TEMP B-TREE' not in plan for plan in plans)
