"""Real SQLite labels, signed grants, signatures and replay/revocation boundaries."""
import json
import sqlite3
from types import SimpleNamespace
import pytest
from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, owner
from tests.permissions_v2.test_knowledge_search import knowledge_policy
from topos.features.calibration.store import save, catalog, verify, RevisionConflict
from topos.permissions_v2 import truth_release
from topos.permissions_v2.truth_contract import TruthIntent, TruthResult, PACK, VERIFY
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.forwarding import verify_node_result
from topos.permissions_v2.registry import parse_policy, parse_disclosure
from topos.principal import Principal, THIRD_PARTY
from topos.storage.db.migrations import apply_all_migrations

CLAIM = dict(pack_id=PACK, question_id='drink', value='tea')

@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / 'labels.db')
    apply_all_migrations(c)
    yield c
    c.close()

def test_label_revisions_clear_and_restart(conn):
    assert verify(conn, TruthIntent.parse(CLAIM))[0].verdict == 'unknown'
    save(conn, dict(CLAIM, expected_revision=0))
    assert verify(conn, TruthIntent.parse(CLAIM))[0].verdict == 'supports'
    assert verify(conn, TruthIntent.parse(dict(CLAIM, value='coffee')))[0].verdict == 'contradicts'
    with pytest.raises(RevisionConflict):
        save(conn, dict(CLAIM, value='coffee', expected_revision=0))
    save(conn, dict(CLAIM, value='coffee', expected_revision=1))
    assert verify(conn, TruthIntent.parse(CLAIM))[0].verdict == 'contradicts'
    save(conn, dict(CLAIM, value=None, expected_revision=2))
    assert verify(conn, TruthIntent.parse(CLAIM))[0].verdict == 'unknown'
    assert catalog(conn)['questions'][0]['answer'] == {'value': None, 'revision': 3}
    assert conn.execute('SELECT count(*) FROM signal_objects WHERE valid_to IS NOT NULL').fetchone()[0] == 2
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    with sqlite3.connect(path) as reopened:
        assert catalog(reopened) == catalog(conn)

@pytest.mark.parametrize('change', [{'subject':'someone-else'}, {'question_id':'health'}, {'value':'not tea'}, {'query':'I like tea'}])
def test_closed_claim_grammar(change):
    with pytest.raises(PolicyError):
        TruthIntent.parse(dict(CLAIM, **change))

def test_external_fact_revision_withholds_stale_label(conn):
    save(conn, dict(CLAIM, expected_revision=0))
    conn.execute("UPDATE signal_objects SET valid_to='2030-01-01'")
    conn.commit()
    assert verify(conn, TruthIntent.parse(CLAIM))[0].verdict == 'unknown'

def build_node(tmp_path, monkeypatch, actor='actor-1', consent=True, pending_privacy=False):
    corpus = mc.build(tmp_path/'corpus', seed=23, counts={'clean_positive_C': 1})
    with sqlite3.connect(corpus.path) as c:
        apply_all_migrations(c)
        save(c, dict(CLAIM, expected_revision=0))
        if pending_privacy:
            from topos.features.lifecycle.blackhole import BlackholeStore
            BlackholeStore(c).blackhole_entity(entity_ref='Unrelated private name', carried=True)
    policy = knowledge_policy()
    policy['binding']['actor_id'] = actor
    if consent:
        policy['search']['truth_packs'] = [PACK]
    node = Node(corpus, tmp_path/'node', model=None, search_raw=policy)
    monkeypatch.setattr(truth_release, 'time', SimpleNamespace(time=lambda: node.now[0]))
    return node

def signed_call(node, actor, claim=CLAIM, request_id='truth-1', envelope=None):
    envelope = envelope or node._envelope('grant-search', VERIFY, claim, request_id)
    principal = Principal(cls=THIRD_PARTY, channel='cp_relay', acting_user=actor, client_id='client-2')
    result, output, revision = truth_release.dispatch(SimpleNamespace(protocol=node.protocol), principal,
        envelope.model_dump(), claim, request_id)
    verify_node_result(result, envelope=envelope, output=output, now=node.now[0],
                       trusted_keys={'node-key': node.node_key.public_key().public_bytes_raw()})
    return output

@pytest.mark.parametrize('actor', ['actor-1', 'owner-1'])
def test_recipient_and_owner_grants_verify_real_saved_answers(tmp_path, monkeypatch, actor):
    node = build_node(tmp_path, monkeypatch, actor)
    assert signed_call(node, actor)['verdict'] == 'supports'
    assert signed_call(node, actor, dict(CLAIM, value='coffee'), 'truth-2')['verdict'] == 'contradicts'
    with sqlite3.connect(node.corpus.path) as c:
        save(c, dict(CLAIM, value=None, expected_revision=1))
    assert signed_call(node, actor, request_id='truth-3')['verdict'] == 'unknown'
    with pytest.raises(PolicyError, match='request_replay'):
        signed_call(node, actor, request_id='truth-3')

@pytest.mark.parametrize('actor', ['actor-1', 'owner-1'])
def test_no_implicit_truth_permission(tmp_path, monkeypatch, actor):
    node = build_node(tmp_path, monkeypatch, actor, consent=False)
    with pytest.raises(PolicyError, match='permission_denied'):
        signed_call(node, actor)

def test_wrong_actor_tamper_expiry_and_revocation(tmp_path, monkeypatch):
    node = build_node(tmp_path, monkeypatch)
    envelope = node._envelope('grant-search', VERIFY, CLAIM, 'truth-1')
    with pytest.raises(PolicyError):
        signed_call(node, 'owner-1', envelope=envelope)
    with pytest.raises(PolicyError):
        signed_call(node, 'actor-1', dict(CLAIM, value='coffee'), envelope=envelope)
    node.now[0] += 101
    with pytest.raises(PolicyError):
        signed_call(node, 'actor-1', envelope=envelope)
    node.now[0] -= 101
    with owner():
        node.ledger.revoke('grant-search', expected_epoch=node.epoch(), command_id='revoke-truth')
    with pytest.raises(PolicyError):
        signed_call(node, 'actor-1', envelope=envelope)

def test_legacy_policy_bytes_and_search_shape_unchanged():
    raw = knowledge_policy()
    assert 'truth_packs' not in parse_policy(raw).model_dump()['search']
    with pytest.raises(PolicyError):
        parse_disclosure(TruthResult(verdict='unknown', basis='insufficient_evidence').model_dump(),
                         capability='permissions-beta/p2c-v3')


@pytest.mark.asyncio
async def test_owner_proxy_handler_accepts_context_and_clear_but_rejects_grantee(conn, monkeypatch):
    import topos.core.handlers as hub
    from topos.core.handlers.signal_features import handle_calibration
    from topos.principal import OWNER_APP, set_principal, reset_principal
    monkeypatch.setattr(hub, 'get_db_connection', lambda: conn)
    message = dict(id='save', type='calibration_answer', payload=dict(CLAIM, topos_id='topos', expected_revision=0))
    token = set_principal(Principal(cls=THIRD_PARTY, channel='cp_relay', acting_user='owner-1', client_id='game'))
    try:
        assert (await handle_calibration(message))['code'] == 403
    finally:
        reset_principal(token)
    token = set_principal(Principal(cls=OWNER_APP, channel='cp_relay', acting_user='owner-1', client_id='topos_home_chat'))
    try:
        assert (await handle_calibration(message))['status'] == 'ok'
        message['payload'].update(value=None, expected_revision=1)
        assert (await handle_calibration(message))['payload']['questions'][0]['answer']['value'] is None
    finally:
        reset_principal(token)


def test_protected_calibration_fact_is_unknown(conn):
    from topos.features.lifecycle.blackhole_guard import BlackholeGuard, CallerClass
    save(conn, dict(CLAIM, expected_revision=0))
    fact_id = conn.execute('SELECT fact_id FROM calibration_answers').fetchone()[0]
    conn.execute("INSERT INTO owner_only_records(canonical_table,record_id) VALUES('signal_objects',?)", (fact_id,))
    conn.commit()
    assert verify(conn, TruthIntent.parse(CLAIM), guard=BlackholeGuard(conn, caller_class=CallerClass.GRANTEE))[0].verdict == 'unknown'


@pytest.mark.parametrize('actor', ['actor-1', 'owner-1'])
def test_direct_answers_work_for_both_grants_during_pending_privacy_rebuild(tmp_path, monkeypatch, actor):
    from topos.features.lifecycle.blackhole_guard import BlackholeGuard, CallerClass
    node = build_node(tmp_path, monkeypatch, actor, pending_privacy=True)
    with sqlite3.connect(node.corpus.path) as c:
        guard = BlackholeGuard(c, caller_class=CallerClass.GRANTEE)
        assert guard.withhold_pending_rebuild()
        assert guard.filter_name_string_artifacts([{'summary': 'legacy untracked prose'}], text_keys=('summary',)) == []
    assert signed_call(node, actor)['verdict'] == 'supports'
    assert signed_call(node, actor, dict(CLAIM, value='coffee'), 'pending-2')['verdict'] == 'contradicts'
    with sqlite3.connect(node.corpus.path) as c:
        save(c, dict(CLAIM, value='coffee', expected_revision=1))
    assert signed_call(node, actor, request_id='pending-3')['verdict'] == 'contradicts'
    assert signed_call(node, actor, dict(CLAIM, value='coffee'), 'pending-4')['verdict'] == 'supports'
    with sqlite3.connect(node.corpus.path) as c:
        save(c, dict(CLAIM, value=None, expected_revision=2))
        assert BlackholeGuard(c, caller_class=CallerClass.GRANTEE).withhold_pending_rebuild()
    assert signed_call(node, actor, request_id='pending-5')['verdict'] == 'unknown'


def test_unrelated_record_protection_does_not_taint_a_direct_answer(conn):
    from topos.features.lifecycle.blackhole_guard import BlackholeGuard, CallerClass
    from topos.features.facts.store import FactStore
    save(conn, dict(CLAIM, expected_revision=0))
    other = FactStore(conn).assert_fact(subject_entity_id='other', predicate='private_note', object_value='private')
    conn.execute("INSERT INTO owner_only_records(canonical_table,record_id) VALUES('signal_objects',?)", (other['object_id'],))
    conn.commit()
    guard = BlackholeGuard(conn, caller_class=CallerClass.GRANTEE)
    assert guard.withhold_pending_rebuild()
    assert verify(conn, TruthIntent.parse(CLAIM), guard=guard)[0].verdict == 'supports'
    assert guard.filter_name_string_artifacts([{'summary': 'legacy untracked prose'}], text_keys=('summary',)) == []


@pytest.mark.parametrize('refs', [
    'null', 'not json', '{}', '[]', '[null]',
    json.dumps([dict(type='calibration_answer', pack_id=PACK, question_id='drink', revision=0)]),
    json.dumps([dict(type='calibration_answer', pack_id=PACK, question_id='drink', revision=2)]),
    json.dumps([dict(type='calibration_answer', pack_id=PACK, question_id='drink', revision=True)]),
    json.dumps([dict(type='calibration_answer', pack_id='another-pack', question_id='drink', revision=1)]),
    json.dumps([dict(type='calibration_answer', pack_id=PACK, question_id='time', revision=1)]),
    json.dumps([dict(type='message', record_id='private')]),
    json.dumps([dict(type='calibration_answer', pack_id=PACK, question_id='drink', revision=1, record_id='private')]),
    json.dumps([dict(type='calibration_answer', pack_id=PACK, question_id='drink', revision=1), dict(type='message', record_id='private')]),
])
def test_incomplete_mixed_or_unbound_answer_provenance_is_unknown(conn, refs):
    save(conn, dict(CLAIM, expected_revision=0))
    conn.execute('UPDATE signal_objects SET source_refs_json=?', (refs,))
    conn.commit()
    assert verify(conn, TruthIntent.parse(CLAIM))[0].verdict == 'unknown'


def test_same_value_refresh_requires_current_revision_reference(conn):
    save(conn, dict(CLAIM, expected_revision=0))
    save(conn, dict(CLAIM, expected_revision=1))
    assert verify(conn, TruthIntent.parse(CLAIM))[0].verdict == 'supports'
    refs = [dict(type='calibration_answer', pack_id=PACK, question_id='drink', revision=1)]
    conn.execute('UPDATE signal_objects SET source_refs_json=?', (json.dumps(refs),))
    conn.commit()
    assert verify(conn, TruthIntent.parse(CLAIM))[0].verdict == 'unknown'


@pytest.mark.parametrize('column,value', [('object_type','brief'), ('object_key','fact:someone:else'), ('signal_dimension','goals')])
def test_answer_index_cannot_certify_another_artifact_type(conn, column, value):
    save(conn, dict(CLAIM, expected_revision=0))
    conn.execute(f'UPDATE signal_objects SET {column}=?', (value,))
    conn.commit()
    assert verify(conn, TruthIntent.parse(CLAIM))[0].verdict == 'unknown'


@pytest.mark.parametrize('field,value', [('object_entity_id','private-person'), ('asserted_by','assistant'),
    ('disclosure','owner_only'), ('subject_entity_id','other'), ('predicate','other'), ('object_value','coffee')])
def test_generic_fact_edits_cannot_certify_a_direct_answer(conn, field, value):
    save(conn, dict(CLAIM, expected_revision=0))
    payload = json.loads(conn.execute('SELECT payload_json FROM signal_objects').fetchone()[0])
    payload[field] = value
    conn.execute('UPDATE signal_objects SET payload_json=?', (json.dumps(payload),))
    conn.commit()
    assert verify(conn, TruthIntent.parse(CLAIM))[0].verdict == 'unknown'


@pytest.mark.parametrize('payload', ['not json','[]','null'])
def test_malformed_backing_fact_is_unknown(conn, payload):
    save(conn, dict(CLAIM, expected_revision=0))
    conn.execute('UPDATE signal_objects SET payload_json=?', (payload,))
    conn.commit()
    assert verify(conn, TruthIntent.parse(CLAIM))[0].verdict == 'unknown'


@pytest.mark.parametrize('name', ['tea','coffee','drink'])
def test_pending_protected_answer_claim_or_prompt_is_still_unknown(conn, name):
    from topos.features.lifecycle.blackhole import BlackholeStore
    from topos.features.lifecycle.blackhole_guard import BlackholeGuard, CallerClass
    save(conn, dict(CLAIM, expected_revision=0))
    BlackholeStore(conn).blackhole_entity(entity_ref=name)
    guard = BlackholeGuard(conn, caller_class=CallerClass.GRANTEE)
    assert guard.withhold_pending_rebuild()
    assert verify(conn, TruthIntent.parse(dict(CLAIM,value='coffee')), guard=guard)[0].verdict == 'unknown'


def test_protected_self_is_still_unknown(conn):
    from topos.features.lifecycle.blackhole import BlackholeStore
    from topos.features.lifecycle.blackhole_guard import BlackholeGuard, CallerClass
    save(conn, dict(CLAIM, expected_revision=0))
    conn.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name) VALUES('self','person','Owner','owner')")
    conn.commit()
    BlackholeStore(conn).blackhole_entity(entity_ref='self')
    guard = BlackholeGuard(conn, caller_class=CallerClass.GRANTEE)
    assert verify(conn, TruthIntent.parse(CLAIM), guard=guard)[0].verdict == 'unknown'


def test_indexed_read_has_no_recent_fact_cutoff(conn):
    save(conn, dict(CLAIM, expected_revision=0))
    from topos.features.facts.store import FactStore
    from topos.storage.db.write_gate import batched_writes
    with batched_writes(conn):
        for i in range(600):
            FactStore(conn).assert_fact(subject_entity_id='someone-else', predicate=f'noise_{i}', object_value='coffee')
    assert verify(conn, TruthIntent.parse(CLAIM))[0].verdict == 'supports'
    plan = conn.execute('EXPLAIN QUERY PLAN SELECT value FROM calibration_answers WHERE pack_id=? AND question_id=?',
                        (PACK, 'drink')).fetchall()
    assert any('USING INDEX' in row[-1] for row in plan)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['none', 'clear', 'revoke', 'protect_fact', 'mixed_lineage', 'protect_claim'])
async def test_transport_rechecks_before_sending(tmp_path, monkeypatch, change):
    import json
    node = build_node(tmp_path, monkeypatch)
    runtime = SimpleNamespace(protocol=node.protocol)
    principal = Principal(cls=THIRD_PARTY, channel='cp_relay', acting_user='actor-1', client_id='client-2')
    envelope = node._envelope('grant-search', VERIFY, CLAIM, 'transport-1')
    monkeypatch.setattr(truth_release, 'get_runtime', lambda: runtime)
    monkeypatch.setattr(truth_release, 'verify_relay_stamp', lambda message: principal)
    monkeypatch.setenv(truth_release.switches.MESSAGE_SEARCH.name, 'true')
    original = truth_release.dispatch
    def changed(*args):
        result = original(*args)
        if change == 'clear':
            with sqlite3.connect(node.corpus.path) as c:
                save(c, dict(CLAIM, value=None, expected_revision=1))
        if change == 'revoke':
            with owner():
                node.ledger.revoke('grant-search', expected_epoch=node.epoch(), command_id='transport-revoke')
        if change in ('protect_fact', 'mixed_lineage', 'protect_claim'):
            with sqlite3.connect(node.corpus.path) as c:
                if change == 'protect_fact':
                    fact_id = c.execute('SELECT fact_id FROM calibration_answers').fetchone()[0]
                    c.execute("INSERT INTO owner_only_records(canonical_table,record_id) VALUES('signal_objects',?)", (fact_id,))
                elif change == 'mixed_lineage':
                    c.execute("UPDATE signal_objects SET source_refs_json='[]' WHERE object_key='fact:self:calibration_drink'")
                else:
                    from topos.features.lifecycle.blackhole import BlackholeStore
                    BlackholeStore(c).blackhole_entity(entity_ref='tea')
        return result
    monkeypatch.setattr(truth_release, 'dispatch', changed)
    frames = []
    async def send(value): frames.append(json.loads(value))
    await truth_release.dispatch_verify_claim(SimpleNamespace(send=send), dict(id='transport-1',
        type=truth_release.MESSAGE_TYPE, payload={'envelope': envelope.model_dump(), 'intent': CLAIM}))
    assert len(frames) == 1
    assert frames[0]['status'] == ('ok' if change == 'none' else 'error')
    if change != 'none':
        assert frames[0]['error'] == 'permission_denied' and 'payload' not in frames[0]


def test_legacy_truth_door_cannot_read_game_labels(conn, monkeypatch):
    from topos.query.verify import verify_claim
    from topos.query.truth_prompts import suggest_prompts
    monkeypatch.setenv('TOPOS_TRUTH_LLM', 'off')
    save(conn, dict(CLAIM, expected_revision=0))
    result = verify_claim(conn, statement='I like tea', caller_app_id='fun-game')
    assert all(lane['stance'] == 'no_evidence' for lane in result['lanes'].values())
    assert not suggest_prompts(conn, caller_app_id='fun-game')['prompts']
