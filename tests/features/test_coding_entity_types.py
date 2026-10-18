import json
import sqlite3

from topos.features.entities.coding_types import capture_product_type, repair_capture_product_type
from topos.features.entities.resolver import map_ner_type


def test_capture_application_uses_software_context_without_global_name_override():
    assert capture_product_type('Codex', 'codex_sessions', 'Install the Codex plugin for chat history') == 'PRODUCT'
    assert capture_product_type('Codex', 'imessage', 'Install Codex software') is None
    assert capture_product_type('Codex', 'codex_sessions', 'Discuss an ancient codex with the chat agent') is None
    assert capture_product_type('Cursor', 'codex_sessions', 'Move the text cursor') is None
    assert capture_product_type('Cursor', 'cursor_sessions', 'Install the Cursor coding agent') == 'PRODUCT'


def test_repair_preserves_identity_and_records_why():
    c = sqlite3.connect(':memory:')
    c.execute('CREATE TABLE entities(entity_id TEXT, entity_type TEXT, metadata_json TEXT,contact_id TEXT,updated_at TEXT)')
    c.execute("INSERT INTO entities VALUES('same-id','work_of_art','{}',NULL,NULL)")
    assert repair_capture_product_type(c, 'same-id', surface='Codex', source_id='codex_sessions', text='Install Codex plugin')
    row = c.execute('SELECT entity_id,entity_type,metadata_json FROM entities').fetchone()
    assert row[:2] == ('same-id', 'product')
    assert json.loads(row[2])['type_correction']['from'] == 'work_of_art'
    assert not repair_capture_product_type(c, 'same-id', surface='Codex', source_id='codex_sessions', text='Install Codex plugin')


def test_schema_driven_model_labels_remain_specific():
    assert map_ner_type('software product') == 'product'
    assert map_ner_type('repository') == 'project'
    assert map_ner_type('organization') == 'org'
    assert map_ner_type('date') is None
