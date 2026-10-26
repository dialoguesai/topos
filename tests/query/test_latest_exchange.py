import sqlite3
from types import SimpleNamespace

import pytest

from topos.query.latest_exchange import latest_text_participant, latest_exchange_rows
from topos.query.manifest import ScopeResolutionManifest
from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest

QUERY = 'Search my text messages, not AI conversations, for the most recent exchange with Rowan Testperson. Give a one-sentence summary and include its source.'

@pytest.fixture
def conn():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.executescript('''
    CREATE TABLE contacts(contact_id, dataset_id, display_name, is_self);
    CREATE TABLE contact_identifiers(contact_id, dataset_id, identifier);
    CREATE TABLE conversation_participants(contact_id, conversation_id, dataset_id, source_id);
    CREATE TABLE conversation_messages(message_id, conversation_id, dataset_id, source_id, content, event_at, is_from_self);
    INSERT INTO contacts VALUES ('p','selected','Rowan Testperson',0);
    INSERT INTO conversation_participants VALUES ('p','fixture-thread','selected','imessage');
    INSERT INTO conversation_participants VALUES ('other','mention-thread','selected','imessage');
    INSERT INTO conversation_messages VALUES ('mention','mention-thread','selected','imessage','I invited Rowan Testperson too','2026-10-26',1);
    INSERT INTO conversation_messages VALUES ('old','fixture-thread','selected','imessage','Rowan Testperson','2024-08-15',1);
    INSERT INTO conversation_messages VALUES ('latest','fixture-thread','selected','imessage','See you tomorrow','2026-08-22T22:00:00-05:00',0);
    INSERT INTO conversation_messages VALUES ('lexical_trap','fixture-thread','selected','imessage','Sounds good','2026-08-23T01:00:00Z',1);
    INSERT INTO conversation_messages VALUES ('wrong_dataset','fixture-thread','default','imessage','Other data','2026-10-26',1);
    INSERT INTO conversation_messages VALUES ('wrong_source','fixture-thread','selected','signal','Other source','2026-10-26',1);
    ''')
    return db


def test_query_parser_and_nonexchange_queries():
    assert latest_text_participant(QUERY) == 'Rowan Testperson'
    assert latest_text_participant('latest text messages with Rowan Testperson') == 'Rowan Testperson'
    assert latest_text_participant('Find text messages mentioning Rowan Testperson') is None
    assert latest_text_participant('latest AI conversation with Rowan Testperson') is None


def test_participant_join_then_event_time_order_not_mention_or_lexical_order(conn):
    rows, receipt = latest_exchange_rows(conn, 'Rowan Testperson', source_ids=['imessage','signal'], dataset_id='selected')
    assert [r['message_id'] for r in rows] == ['latest','lexical_trap','old']
    assert receipt['latest_record_id'] == 'latest'
    assert receipt['status'] == 'resolved'
    assert receipt['dataset_id'] == 'selected'


def test_no_identity_does_not_infer_from_message_text(conn):
    conn.execute('UPDATE contacts SET display_name=NULL')
    before = conn.total_changes
    assert latest_exchange_rows(conn, 'Rowan Testperson', source_ids=['imessage'], dataset_id='selected')[1]['status'] == 'identity_unresolved'
    assert latest_exchange_rows(conn, 'Rowan Testperson', source_ids=['imessage'], dataset_id='selected')[0] == []
    assert conn.total_changes == before


def test_ambiguous_names_and_explicit_identifier(conn):
    conn.execute("INSERT INTO contacts VALUES ('p2','selected','Rowan Testperson',0)")
    assert latest_exchange_rows(conn, 'Rowan Testperson', source_ids=['imessage'], dataset_id='selected')[1]['status'] == 'identity_ambiguous'
    conn.execute("DELETE FROM contacts WHERE contact_id='p2'")
    conn.execute("INSERT INTO contact_identifiers VALUES ('p','selected','+12025550101')")
    assert latest_exchange_rows(conn, '+12025550101', source_ids=['imessage'], dataset_id='selected')[1]['status'] == 'resolved'


def test_undated_rows_never_use_ingestion_time(conn):
    conn.execute("UPDATE conversation_messages SET event_at='' WHERE message_id='latest'")
    rows, receipt = latest_exchange_rows(conn, 'Rowan Testperson', source_ids=['imessage'], dataset_id='selected')
    assert rows[0]['message_id'] == 'lexical_trap'
    assert receipt['undated_messages'] == 1


def test_retrieve_does_not_call_vector_or_mix_ai_furniture(conn, monkeypatch):
    import topos.query.retrieval as retrieval
    monkeypatch.setattr(retrieval, '_carried_items', lambda conn: None)
    monkeypatch.setattr(retrieval, '_blackhole_policy_for_summary', lambda items, **kw: items)
    monkeypatch.setattr(retrieval, '_enforce_request_exclusions', lambda *args, **kw: None)
    adapter = DefaultSignalRetrievalAdapter(SimpleNamespace(signal=SimpleNamespace(_conn=conn)))
    manifest = ScopeResolutionManifest(scope_id='messages:read', primary_dimensions=[],
        canonical_tables=['conversation_messages','ai_chat_messages'], default_source_ids=['imessage','chatgpt'], access_mode_ceiling='raw')
    request = RetrievalRequest(manifest=manifest, access_mode='summary', query_text=QUERY, owner_mode=True, dataset_id='selected')
    bundle = adapter.retrieve(request)
    assert [r['record_id'] for r in bundle.context_packet['summaries']] == ['latest','lexical_trap','old']
    assert 'semantic_hits' not in bundle.context_packet
    assert 'topic_clusters' not in bundle.context_packet
    assert bundle.context_packet['latest_exchange']['status'] == 'resolved'
    conn.execute('UPDATE contacts SET display_name=NULL')
    bundle = adapter.retrieve(request)
    assert bundle.context_packet['summaries'] == []
    assert bundle.context_packet['latest_exchange']['status'] == 'identity_unresolved'
    # An AI-only scope cannot widen itself to messages.
    request.manifest = ScopeResolutionManifest(scope_id='ai_conversations:read', primary_dimensions=[],
        canonical_tables=['ai_chat_messages'], default_source_ids=['chatgpt'])
    assert adapter.retrieve(request).context_packet['summaries'] == []

def test_identifier_parser_preserves_email_and_phone():
    assert latest_text_participant('Latest text message with rowan@example.invalid. Include source.') == 'rowan@example.invalid'
    assert latest_text_participant('Latest texts from +12025550101 and include the source') == '+12025550101'


def test_ordering_receipt_survives_public_result(conn, monkeypatch):
    from topos.query.game_layer import DefaultGameLayer
    rows, receipt = latest_exchange_rows(conn, 'Rowan Testperson', source_ids=['imessage'], dataset_id='selected')
    result = DefaultGameLayer().apply(context_packet={'summaries': [], 'latest_exchange': receipt}, access_mode='summary')
    assert result.payload['latest_exchange']['latest_record_id'] == 'latest'


def test_multiple_datasets_require_selection_even_when_name_is_unique(conn):
    rows, receipt = latest_exchange_rows(conn, 'Rowan Testperson', source_ids=['imessage'])
    assert rows == []
    assert receipt['status'] == 'dataset_required'
    conn.execute("UPDATE contacts SET display_name=NULL WHERE dataset_id='selected'")
    conn.execute("INSERT INTO contacts VALUES ('other','default','Rowan Testperson',0)")
    rows, receipt = latest_exchange_rows(conn, 'Rowan Testperson', source_ids=['imessage'], dataset_id='selected')
    assert rows == []
    assert receipt['status'] == 'identity_unresolved'


@pytest.mark.parametrize("access_mode", ["raw", "summary"])
@pytest.mark.parametrize("filter_stage", ["summary_policy", "carried_items"])
def test_policy_removes_latest_row_and_its_ordering_receipt(conn, monkeypatch, access_mode, filter_stage):
    """The fast path must not re-expose a withheld message through its latest-exchange receipt."""
    import topos.query.retrieval as retrieval

    key = "rows" if access_mode == "raw" else "summaries"

    def without_latest(items):
        return [item for item in items if item["record_id"] != "latest"]

    class CarriedRule:
        def withhold_from(self, packet, *, text):
            assert text is False
            return {**packet, key: without_latest(packet[key])}

    monkeypatch.setattr(retrieval, "_blackhole_policy_for_summary",
                        lambda items, **kw: without_latest(items) if filter_stage == "summary_policy" else items)
    monkeypatch.setattr(retrieval, "_carried_items",
                        lambda conn: CarriedRule() if filter_stage == "carried_items" else None)
    monkeypatch.setattr(retrieval, "_enforce_request_exclusions", lambda *args, **kw: None)
    manifest = ScopeResolutionManifest(
        scope_id="messages:read", primary_dimensions=[], canonical_tables=["conversation_messages"],
        default_source_ids=["imessage"], access_mode_ceiling="raw")
    request = RetrievalRequest(manifest=manifest, access_mode=access_mode, query_text=QUERY,
                               owner_mode=True, dataset_id="selected")
    adapter = DefaultSignalRetrievalAdapter(SimpleNamespace(signal=SimpleNamespace(_conn=conn)))

    bundle = adapter.retrieve(request)

    assert [row["record_id"] for row in bundle.context_packet[key]] == ["lexical_trap", "old"]
    assert bundle.record_counts == {key: 2}
    expected_receipt = {"status": "latest_not_visible", "coverage": "stored_messages"}
    assert bundle.context_packet["latest_exchange"] == expected_receipt
    assert bundle.retrieval_metadata["latest_exchange"] == expected_receipt
