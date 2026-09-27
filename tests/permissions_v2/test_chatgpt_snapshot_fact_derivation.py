"""Actual attested AI ingest must mint usable facts without a seeded locator."""
import json
from types import SimpleNamespace

import pytest

from tests.ingestion.test_chatgpt_owner_snapshot import T0, export, owner_chat
from tests.permissions_v2 import test_chatgpt_owner_snapshot_canary as chat
from tests.permissions_v2.test_ingest_snapshot_work_canary import (  # noqa: F401
    canonical, corpus, facts, ingest, lane, paired_runtime, projection_runtime,
)


def work_export():
    conversation = owner_chat()
    conversation['mapping']['user-1']['message']['content']['parts'] = ['I work at Northwind.']
    conversation['mapping']['assistant-1']['message']['content']['parts'] = ['I work at Fabrikam.']
    return export(conversation)


async def derived(lane):
    run = await chat.run_chatgpt_lane(lane, work_export())
    assert run.result.status == 'ok'
    found = facts(lane, active_only=True)
    assert len(found) == 1, 'the real import must derive one owner fact, without a seeded locator'
    [fact] = found
    assert fact.payload['predicate'] == 'works_at'
    assert fact.payload['object_value'] == 'Northwind'
    assert fact.refs == [{'table': 'ai_chat_messages', 'record_id': chat.PROMPT_ID, 'source_id': chat.SOURCE}]
    await chat.owner_message('evidence', 'preview', {'fact_id': fact.object_id})
    assert chat.qualify(lane, fact.object_id).verdict == 'qualified'
    return run, fact


async def second_work_export(lane, monkeypatch, *, shift, value='Contoso'):
    conversation = owner_chat()
    other_id = 'c0ffee00-0000-4000-8000-00000000000c'
    conversation['conversation_id'] = conversation['id'] = other_id
    conversation['mapping']['user-1']['message']['content']['parts'] = [f'I work at {value}.']
    for node in conversation['mapping'].values():
        if node['message'] and isinstance(node['message']['create_time'], (int, float)):
            node['message']['create_time'] += shift
    conversation['create_time'] += shift
    conversation['update_time'] += shift
    monkeypatch.setattr(chat, 'SNAPSHOT_ID', 'second-export')
    monkeypatch.setattr(chat, 'DATASET', 'second-dataset')
    run = await chat.run_chatgpt_lane(lane, export(conversation), attest_identity=False)
    assert run.result.status == 'ok'
    return run


@pytest.mark.asyncio
async def test_proved_prompt_derives_qualified_fact_and_releases_exact_owner_text(lane, monkeypatch):
    _run, fact = await derived(lane)
    # The existing signed grant helper is reused; only the grant's reviewed
    # domain changes from food to work. No canonical or review row is seeded.
    monkeypatch.setattr(chat, 'DOMAIN', 'work')
    authority = await chat.signed_chatgpt_grant(lane)
    outputs, error = chat.source_adapter_read(lane, authority, fact.object_id, request_id='derived-ai-read')
    assert error is None
    assert chat.released_text(outputs) == [(chat.PROMPT_ID, 'ai_chat_messages', 'I work at Northwind.')]
    assert 'Fabrikam' not in json.dumps(outputs)


@pytest.mark.asyncio
async def test_revoke_withholds_the_automatically_derived_fact(lane):
    run, fact = await derived(lane)
    await ingest(lane, {'operation': 'revoke', 'source_id': chat.SOURCE, 'enrollment_id': run.enrollment.enrollment_id})
    assert chat.qualify(lane, fact.object_id).verdict == 'withheld'


@pytest.mark.asyncio
async def test_changed_prompt_cannot_retain_the_derivations_proof(lane):
    _run, fact = await derived(lane)
    with canonical(lane) as conn:
        conn.execute('UPDATE ai_chat_messages SET content=? WHERE message_id=?', ('I work at Contoso.', chat.PROMPT_ID))
        conn.commit()
    assert chat.qualify(lane, fact.object_id).verdict == 'withheld'


@pytest.mark.asyncio
async def test_older_prompt_imported_later_cannot_restore_old_work(lane, monkeypatch):
    _run, newer = await derived(lane)
    await second_work_export(lane, monkeypatch, shift=-1000)
    active = facts(lane, active_only=True)
    assert [(f.object_id,f.payload['object_value']) for f in active] == [(newer.object_id,'Northwind')]
    old = [f for f in facts(lane) if f.payload['object_value'] == 'Contoso']
    assert len(old) == 1 and old[0].valid_to is not None


@pytest.mark.asyncio
async def test_newer_prompt_imported_later_still_updates_work(lane, monkeypatch):
    await derived(lane)
    await second_work_export(lane, monkeypatch, shift=1000)
    assert [f.payload['object_value'] for f in facts(lane, active_only=True)] == ['Contoso']


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['revoked', 'changed', 'after_attestation'])
async def test_an_invalid_newer_proof_cannot_hold_back_an_older_statement(lane, monkeypatch, case):
    if case == 'after_attestation':
        from topos.permissions_v2 import ingest_provenance
        with pytest.MonkeyPatch.context() as clock:
            clock.setattr(ingest_provenance, 'time', SimpleNamespace(time=lambda: T0 - 1))
            await derived(lane)
    else:
        run, _fact = await derived(lane)
        if case == 'revoked':
            await ingest(lane, {'operation': 'revoke', 'source_id': chat.SOURCE,
                               'enrollment_id': run.enrollment.enrollment_id})
        else:
            with canonical(lane) as conn:
                conn.execute('UPDATE ai_chat_messages SET content=? WHERE message_id=?', ('changed', chat.PROMPT_ID))
                conn.commit()
    await second_work_export(lane, monkeypatch, shift=-1000)
    assert [f.payload['object_value'] for f in facts(lane, active_only=True)] == ['Contoso']


@pytest.mark.asyncio
async def test_unattested_self_produces_no_owner_fact(lane):
    run = await chat.run_chatgpt_lane(lane, work_export(), attest_identity=False)
    assert run.result.status == 'ok'
    assert facts(lane) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('marker', ['members', 'future_roster', 'gizmo_id', 'conversation_template_id'])
async def test_unknown_conversation_context_produces_no_owner_evidence_or_fact(lane, marker):
    conversation = json.loads(work_export())[0]
    conversation[marker] = ['synthetic-other-person'] if 'roster' in marker or marker == 'members' else 'synthetic-gpt'
    run = await chat.run_chatgpt_lane(lane, export(conversation))
    assert run.result.status == 'ok'
    assert run.result.messages_created == 0
    assert facts(lane) == []


@pytest.mark.asyncio
async def test_failure_after_derivation_rolls_back_canonical_links_and_facts(lane, monkeypatch):
    from topos.permissions_v2.ingest_provenance import IngestProvenanceService
    def fail_finish(self, conn, context, result):
        assert conn.execute("SELECT count(*) FROM signal_objects WHERE object_type='fact'").fetchone()[0] == 1
        raise RuntimeError('synthetic completion failure')
    monkeypatch.setattr(IngestProvenanceService, 'finish', fail_finish)
    run = await chat.run_chatgpt_lane(lane, work_export())
    assert run.result.status == 'error' and run.status.status == 'failed'
    with canonical(lane) as conn:
        assert all(conn.execute('SELECT count(*) FROM '+table).fetchone()[0] == 0 for table in
                   ('ai_chat_messages','ai_chat_conversations','ingest_provenance_records','signal_objects'))


@pytest.mark.asyncio
async def test_protected_prompt_withholds_while_unrelated_prompt_qualifies(lane):
    from topos.features.lifecycle.blackhole import BlackholeStore
    _run, fact = await derived(lane)
    with canonical(lane) as conn:
        BlackholeStore(conn).blackhole_entity(entity_ref='Mara Example')
    # A protected person elsewhere no longer stops unrelated qualifying data.
    assert chat.qualify(lane, fact.object_id).verdict == 'qualified'
    with canonical(lane) as conn:
        BlackholeStore(conn).blackhole_entity(entity_ref='Northwind')
    assert chat.qualify(lane, fact.object_id).verdict == 'withheld'
