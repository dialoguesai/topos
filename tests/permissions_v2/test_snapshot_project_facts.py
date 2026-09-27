"""Explicit project statements in proven rows; no expansion of legacy extraction."""
import json

import pytest

from tests.ingestion.test_chatgpt_owner_snapshot import export, owner_chat
from tests.permissions_v2 import test_chatgpt_owner_snapshot_canary as chat
from tests.permissions_v2.test_ingest_snapshot_work_canary import (  # noqa: F401
    canonical, corpus, facts, ingest, lane, paired_runtime, projection_runtime,
)
from topos.features.facts.extract import extract_message_facts
from topos.permissions_v2.snapshot_message_facts import extract_snapshot_message_facts


def row(text, **extra):
    return {'content': text, 'is_from_self': 1, 'sender_id': 'self', 'sender_type': 'human',
            'event_at': '2026-09-01T00:00:00+00:00', **extra}


@pytest.mark.parametrize('text', [
    'My current work project is Northwind.', 'My work project is Northwind.',
    "At work, I'm working on Northwind.", 'At work, I am working on Northwind',
    'I’m working on Northwind at work.', 'I am working on Northwind at work.',
])
def test_explicit_work_project_is_a_single_dated_fact(text):
    [fact] = extract_snapshot_message_facts(row(text), table='conversation_messages')
    assert (fact['predicate'], fact['object_value'], fact['dimension'], fact['valid_from']) == (
        'works_on', 'Northwind', 'work', '2026-09-01T00:00:00+00:00')
    assert extract_message_facts(row(text), table='conversation_messages') == []


@pytest.mark.parametrize('text', [
    'My current work project is Northwind?',
    'My current work project is Northwind and I am unwell.',
    'My current work project is Northwind. I live in Elsewhere.',
    'My current work project is Northwind\nI am unwell.',
    'My current work project is not Northwind.',
    'If my current work project is Northwind, I will leave.',
    'My next work project might be Northwind.',
    'Their current work project is Northwind.',
    '“My current work project is Northwind.”',
    '> My current work project is Northwind.',
    'My work project is the thing I mentioned.',
    'I am working on Northwind.',
    'My work project is Northwind or Fabrikam.',
    'My work project is Northwind & a long sentence.',
])
def test_other_prose_does_not_become_a_work_project_fact(text):
    assert all(fact['predicate'] != 'works_on' for fact in
        extract_snapshot_message_facts(row(text), table='conversation_messages'))


@pytest.mark.parametrize('extra', [
    {'is_from_self': 0, 'sender_id': 'correspondent'},
    {'metadata_json': json.dumps({'associated_message_guid': 'quoted-message'})},
    {'reply_to_message_id': 'other-message', 'metadata_json': json.dumps({'associated_message_type': 2000})},
])
def test_received_or_native_quote_rows_do_not_produce_project_facts(extra):
    assert extract_snapshot_message_facts(row('My work project is Northwind.', **extra), table='conversation_messages') == []


@pytest.mark.asyncio
@pytest.mark.parametrize('domain', ['work', 'food'])
async def test_signed_snapshot_releases_a_project_statement_only_under_matching_grant(lane, monkeypatch, domain):
    conversation = owner_chat()
    text = 'My current work project is Northwind.'
    conversation['mapping']['user-1']['message']['content']['parts'] = [text]
    conversation['mapping']['assistant-1']['message']['content']['parts'] = ['My work project is Fabrikam.']
    run = await chat.run_chatgpt_lane(lane, export(conversation))
    assert run.result.status == 'ok'
    [fact] = facts(lane, active_only=True)
    assert fact.payload['predicate'] == 'works_on' and fact.payload['object_value'] == 'Northwind'
    assert fact.refs == [{'table': 'ai_chat_messages', 'record_id': chat.PROMPT_ID, 'source_id': chat.SOURCE}]
    await chat.owner_message('evidence', 'preview', {'fact_id': fact.object_id})
    assert chat.qualify(lane, fact.object_id).verdict == 'qualified'
    monkeypatch.setattr(chat, 'DOMAIN', domain)
    authority = await chat.signed_chatgpt_grant(lane)
    outputs, error = chat.source_adapter_read(lane, authority, fact.object_id, request_id='derived-project-read')
    if domain == 'work':
        assert error is None and chat.released_text(outputs) == [(chat.PROMPT_ID, 'ai_chat_messages', text)]
    else:
        assert outputs == [] and error is not None
    assert 'Fabrikam' not in json.dumps(outputs)
    await ingest(lane, {'operation': 'revoke', 'source_id': chat.SOURCE, 'enrollment_id': run.enrollment.enrollment_id})
    assert chat.qualify(lane, fact.object_id).verdict == 'withheld'
