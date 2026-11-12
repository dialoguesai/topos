"""New local source flow against a disposable no-account node database."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.public
from fastapi import HTTPException

from topos.api import owner_sources
from topos.principal import OWNER_APP, OWNER_AGENT
from topos.sources import install_service
from topos.sources.registry import REGISTRY
from topos.storage.db.migrations import apply_all_migrations


def body():
    return {"source_definition_json": {
        "source_id": "personal_rows", "display_name": "Personal rows", "source_type": "file",
        "schema_id": "personal.rows.v1", "parser_id": "personal.rows.v1", "delivery": "owner_upload",
        "enrichment_trigger": "manual", "posture": "mixed", "pipeline_include_data_table": True,
        "tables": [{"table_id": "private_personal_rows_data", "display_name": "Rows", "columns": [
            {"name": "record_id", "type": "text", "primary_key": True}, {"name": "content", "type": "text"}]}],
        "file_ingest_shape": {"format": "json", "parser_extract_map": {"record_id": "id", "content": "content"}},
    }, "sample": {"file_name": "rows.json", "content": '[{"id":"r1","content":"Synthetic row"}]'}}


OWNER = SimpleNamespace(cls=OWNER_APP, channel="uds")
AGENT = SimpleNamespace(cls=OWNER_AGENT, channel="uds")


@pytest.fixture
def node(tmp_path, monkeypatch):
    from topos.local_identity import ensure_local_identity
    from topos.ingestion.parsers import PARSER_REGISTRY
    from topos.canonicalization.mappers import MAPPER_REGISTRY
    from topos.storage.canonical.ai_chat.mapper import _MAPPER_REGISTRY
    from topos.storage.raw.file_store import RawFileStore
    from topos.ingestion import ingest_helpers

    conn = sqlite3.connect(str(tmp_path / "node.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    apply_all_migrations(conn)
    ensure_local_identity(conn)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    monkeypatch.setattr(install_service, "get_db_connection", lambda: conn)
    monkeypatch.setattr(owner_sources, "_confirmed_owner_socket", lambda: True)
    monkeypatch.setattr(ingest_helpers, "RawFileStore", lambda: RawFileStore(base_path=tmp_path / "raw"))
    snapshots = [(r, dict(r)) for r in (REGISTRY, PARSER_REGISTRY, MAPPER_REGISTRY, _MAPPER_REGISTRY, install_service._ACTIVE_HANDLES)]
    yield conn
    for r, snapshot in snapshots:
        r.clear()
        r.update(snapshot)
    conn.close()


async def plan(payload):
    response = await owner_sources.prepare_private_source(payload, principal=AGENT)
    data = json.loads(response.body)
    assert data["status"] == "ready", data
    return data


@pytest.mark.asyncio
async def test_prepare_no_writes_then_approved_install_import_and_rehydrate(node):
    data = body()
    before = node.total_changes
    tables = node.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    missing = json.loads((await owner_sources.private_source_status("personal_rows", principal=AGENT)).body)
    assert not missing["installed"]
    assert node.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == tables
    prepared = await plan(data)
    assert node.total_changes == before
    assert prepared["target"]["user_id"].startswith("local:")
    response = await owner_sources.install_private_source({**data, "plan_sha256": prepared["plan_sha256"]}, principal=OWNER)
    receipt = json.loads(response.body)
    assert receipt["status"] == "ingested", receipt
    assert receipt["registered"] is False
    assert receipt["sample_import"]["records_processed"] == 1
    row = node.execute('SELECT content FROM private_personal_rows_data').fetchone()
    assert row[0] == "Synthetic row"
    status = json.loads((await owner_sources.private_source_status("personal_rows", principal=AGENT)).body)
    assert status["installed"] and status["installs"][0]["source_definition_json"]["source_id"] == "personal_rows"
    REGISTRY.pop("personal_rows")
    install_service._ACTIVE_HANDLES.clear()
    install_service.rehydrate_active_installs_runtime()
    assert REGISTRY["personal_rows"].display_name == "Personal rows"


@pytest.mark.asyncio
async def test_repeated_definition_is_idempotent_and_changed_definition_refused(node):
    data = body()
    data.pop("sample")
    prepared = await plan(data)
    one = json.loads((await owner_sources.install_private_source({**data, "plan_sha256": prepared["plan_sha256"]}, principal=OWNER)).body)
    two = json.loads((await owner_sources.install_private_source({**data, "plan_sha256": prepared["plan_sha256"]}, principal=OWNER)).body)
    assert one["install_id"] == two["install_id"]
    assert node.execute("SELECT count(*) FROM source_runtime_installs").fetchone()[0] == 1
    data["source_definition_json"]["display_name"] = "Different"
    prepared = await plan(data)
    with pytest.raises(HTTPException, match="already exists"):
        await owner_sources.install_private_source({**data, "plan_sha256": prepared["plan_sha256"]}, principal=OWNER)


@pytest.mark.asyncio
async def test_agent_tcp_and_unconfirmed_owner_cannot_install(node, monkeypatch):
    for principal in (AGENT, SimpleNamespace(cls=OWNER_APP, channel="tcp")):
        with pytest.raises(HTTPException) as exc:
            await owner_sources.install_private_source(body(), principal=principal)
        assert exc.value.status_code == 403
    monkeypatch.setattr(owner_sources, "_confirmed_owner_socket", lambda: False)
    with pytest.raises(HTTPException) as exc:
        await owner_sources.install_private_source(body(), principal=OWNER)
    assert exc.value.status_code == 403
    assert "personal_rows" not in REGISTRY


@pytest.mark.asyncio
async def test_modified_sample_or_switched_target_refuses_before_install(node, monkeypatch):
    data = body()
    prepared = await plan(data)
    changed = body()
    changed["sample"]["content"] = changed["sample"]["content"].replace("Synthetic", "Changed")
    with pytest.raises(HTTPException, match="Plan changed"):
        await owner_sources.install_private_source({**changed, "plan_sha256": prepared["plan_sha256"]}, principal=OWNER)
    from topos.core.state import set_engine_config_value
    set_engine_config_value(node, "topos_id", "different-topos")
    with pytest.raises(HTTPException, match="Plan changed"):
        await owner_sources.install_private_source({**data, "plan_sha256": prepared["plan_sha256"]}, principal=OWNER)
    assert "personal_rows" not in REGISTRY


@pytest.mark.asyncio
async def test_import_failure_reports_persisted_definition(node, monkeypatch):
    async def fail(**kwargs):
        raise RuntimeError("synthetic failure with sensitive details that must not be echoed")
    monkeypatch.setattr("topos.ingestion.ingest_helpers.ingest_file_payload", fail)
    data = body()
    prepared = await plan(data)
    result = json.loads((await owner_sources.install_private_source({**data, "plan_sha256": prepared["plan_sha256"]}, principal=OWNER)).body)
    assert result["status"] == "import_failed" and result["installed"] is True
    assert "sensitive" not in json.dumps(result)
    assert "personal_rows" in REGISTRY


@pytest.mark.asyncio
async def test_custom_conversation_mapping_reaches_canonical_storage(node):
    data = {"source_definition_json": {
        "source_id": "personal_conversations", "display_name": "Personal conversations",
        "source_type": "file", "delivery": "owner_upload",
        "schema_id": "personal.conversations.v1", "parser_id": "personal.conversations.v1",
        "canonical_mapper_id": "personal_conversations", "canonical_group_id": "conversations",
        "default_scope_id": "messages", "allowed_scope_ids": ["messages:read"],
        "posture": "mixed", "enrichment_trigger": "manual",
        "file_ingest_shape": {"format": "json", "parser_extract_map": {
            "message_id": "id", "conversation_id": "thread", "sender_type": "role",
            "content": "text", "event_at": "date"}},
    }, "sample": {"file_name": "messages.json", "content": json.dumps([
        {"id": "synthetic-message-1", "thread": "synthetic-thread", "role": "user",
         "text": "Synthetic conversation record", "date": "2026-01-01T12:00:00Z"}])}}
    prepared = await plan(data)
    receipt = json.loads((await owner_sources.install_private_source(
        {**data, "plan_sha256": prepared["plan_sha256"]}, principal=OWNER)).body)
    assert receipt["status"] == "ingested", receipt
    row = node.execute("SELECT content, writer_class FROM conversation_messages WHERE message_id = ?", ("synthetic-message-1",)).fetchone()
    assert row and row[0] == "Synthetic conversation record"
    assert row[1] == "owner_import"
