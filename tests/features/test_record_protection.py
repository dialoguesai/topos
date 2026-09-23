"""Owner-only records: isolation, positive controls, derived withholding and authority."""
import sqlite3
from types import SimpleNamespace

import pytest

from topos.features.lifecycle.record_protection import RecordProtectionStore, protection_fingerprint
from topos.features.lifecycle.blackhole_guard import BlackholeGuard, CallerClass, guard_from_message
from topos.principal import OWNER_APP, THIRD_PARTY, Principal, set_principal, reset_principal
from topos.storage.db.migrations import apply_all_migrations
from topos.storage.adapters.sqlite.stores import SQLiteCanonicalStore


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "records.db"))
    apply_all_migrations(c)
    c.execute("""CREATE TABLE IF NOT EXISTS conversation_messages (
        message_id TEXT PRIMARY KEY, conversation_id TEXT, sender_id TEXT,
        source_id TEXT, content TEXT, created_at TEXT, event_at TEXT)""")
    for rid, text, date in (("private", "CANARY_OWNER_ONLY", "2026-09-14"), ("public", "CANARY_ALLOWED", "2026-09-13")):
        c.execute("INSERT INTO conversation_messages (message_id, content, created_at) VALUES (?, ?, ?)", (rid, text, date))
    c.commit()
    yield c
    c.close()


def protect(conn):
    return RecordProtectionStore(conn).protect(canonical_table="conversation_messages", record_id="private")


def test_record_selection_is_reversible_without_deleting_content(conn):
    before = protection_fingerprint(conn)
    row = protect(conn)
    assert row["record_id"] == "private"
    assert protection_fingerprint(conn) != before
    assert conn.execute("SELECT content FROM conversation_messages WHERE message_id='private'").fetchone()[0] == "CANARY_OWNER_ONLY"
    assert RecordProtectionStore(conn).unprotect(canonical_table="conversation_messages", record_id="private")
    assert protection_fingerprint(conn) == before


@pytest.mark.parametrize("table,rid", [("invented", "private"), ("conversation_messages", "absent"), ('conversation_messages; DROP TABLE entities', "private")])
def test_selector_validation_does_not_claim_to_protect_missing_records(conn, table, rid):
    with pytest.raises(ValueError):
        RecordProtectionStore(conn).protect(canonical_table=table, record_id=rid)
    assert RecordProtectionStore(conn).list() == []


def test_native_raw_rows_counts_and_page_filter_before_limit(conn):
    protect(conn)
    store = SQLiteCanonicalStore(conn)
    page = store._list_native("conversation_messages", disclosure_tier="default_disclosure", limit=1)
    assert page.total == 1
    assert [r["record_id"] for r in page.items] == ["public"]
    token = set_principal(Principal(OWNER_APP, "uds"))
    try:
        owner = store._list_native("conversation_messages", disclosure_tier="owner_raw", limit=10)
    finally:
        reset_principal(token)
    assert owner.total == 2
    assert any(r["content"] == "CANARY_OWNER_ONLY" for r in owner.items)


def test_guard_filters_nameless_record_with_positive_control(conn):
    protect(conn)
    rows = [{"message_id": "private", "content": "arbitrary paraphrase"}, {"message_id": "public", "content": "allowed"}]
    guard = BlackholeGuard(conn, caller_class=CallerClass.GRANTEE)
    assert guard.active
    assert guard.filter_canonical_rows(rows) == [rows[1]]
    assert guard.filter_name_string_artifacts([{"summary": "untraceable"}], text_keys=("summary",)) == []
    assert BlackholeGuard(conn, caller_class=CallerClass.OWNER_UI).filter_canonical_rows(rows) == rows


@pytest.mark.parametrize("principal", [None, Principal(THIRD_PARTY, "local_http"), Principal("cp_relay", "cp_relay"), Principal("owner_automation", "internal")])
def test_forged_owner_caller_block_does_not_confer_owner_mode(conn, principal):
    protect(conn)
    token = set_principal(principal)
    try:
        guard = guard_from_message(conn, {"caller": {"mcp_source": "topos_home_chat", "requester_is_owner": True, "routine_local_only": True}})
        assert guard.blocks_record_id("private")
    finally:
        reset_principal(token)


def test_verified_owner_channel_retains_selected_information(conn):
    protect(conn)
    token = set_principal(Principal(OWNER_APP, "uds"))
    try:
        assert not guard_from_message(conn, {}).blocks_record_id("private")
    finally:
        reset_principal(token)


@pytest.mark.parametrize("mode", ["summary", "inference"])
def test_derived_query_withholds_before_any_adapter_or_model_read(conn, mode):
    from topos.query.retrieval import DefaultSignalRetrievalAdapter
    from topos.query.types import RetrievalRequest

    protect(conn)
    adapter = DefaultSignalRetrievalAdapter.__new__(DefaultSignalRetrievalAdapter)
    adapter._adapters = SimpleNamespace(signal=SimpleNamespace(_conn=conn))
    adapter.retrieve_call_count = 0
    manifest = SimpleNamespace(scope_id="work_context:read", access_mode_ceiling="raw")
    # Even raw-tier text cannot substitute for channel-verified owner_mode.
    bundle = adapter._retrieve_bundle(RetrievalRequest(manifest=manifest, access_mode=mode, disclosure_tier="owner_raw"))
    assert bundle.stores_touched == []
    assert bundle.context_packet["scores" if mode == "inference" else "summaries"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("stream,scopes", [("conversation", []), ("conversation", ["ai_conversations:read"]), ("ai_chat", ["messages:read"]), ("ai_chat", None)])
async def test_uma_requires_stream_specific_scope_before_db_access(monkeypatch, stream, scopes):
    import topos.core.handlers as hub
    from topos.core.handlers.uma import handle_uma_get_messages

    monkeypatch.setattr(hub, "get_db_connection", lambda: pytest.fail("Unauthorized request reached DB"))
    result = await handle_uma_get_messages({"id": "x", "payload": {"message_stream": stream, "allowed_scopes": scopes}})
    assert result["code"] == 403


@pytest.mark.asyncio
async def test_signal_dispatch_requires_verified_owner_before_read(monkeypatch):
    import topos.core.handlers as hub

    monkeypatch.setitem(hub.HANDLERS, "signal_list_blackholes", lambda _: pytest.fail("Untrusted signal request reached handler"))
    result = await hub.handle_control_plane_request({"id": "x", "type": "signal_list_blackholes", "caller": {"mcp_source": "topos_home_chat"}}, principal=Principal(THIRD_PARTY, "local_http"))
    assert result["code"] == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", ["uds", "cp_relay"])
async def test_verified_owner_signal_dispatch_positive_control(monkeypatch, channel):
    import topos.core.handlers as hub

    async def owner_read(_):
        return {"id": "x", "status": "ok", "payload": {"owner_canary": "visible"}}
    monkeypatch.setitem(hub.HANDLERS, "signal_list_blackholes", owner_read)
    result = await hub.handle_control_plane_request({"id": "x", "type": "signal_list_blackholes"}, principal=Principal(OWNER_APP, channel))
    assert result["payload"]["owner_canary"] == "visible"


@pytest.mark.asyncio
@pytest.mark.parametrize("stream,scope", [("conversation", "messages:read"), ("conversation", "all:read"), ("ai_chat", "ai_conversations:read"), ("ai_chat", "aiChat:read"), ("ai_chat", "aiMessages:read"), ("ai_chat", "all:read")])
async def test_known_message_scopes_reach_the_authorized_reader(monkeypatch, stream, scope):
    import topos.core.handlers as hub
    from topos.core.handlers.uma import handle_uma_get_messages

    visited = []
    monkeypatch.setattr(hub, "get_db_connection", lambda: visited.append(True))
    result = await handle_uma_get_messages({"id": "x", "payload": {"message_stream": stream, "allowed_scopes": [scope], "resource_id": "dataset:owner:dataset:device"}})
    assert visited
    assert result.get("code") != 403


@pytest.mark.asyncio
async def test_shared_oplog_is_denied_before_any_db_access(monkeypatch):
    import topos.core.handlers as hub
    from topos.core.handlers.uma import handle_uma_get_oplog

    monkeypatch.setattr(hub, "get_db_connection", lambda: pytest.fail("Unprojected oplog reached DB"))
    assert (await handle_uma_get_oplog({"id": "x", "payload": {"allowed_scopes": ["all:read"]}}))["code"] == 403


@pytest.mark.parametrize("provider", ["openai", "redpill"])
def test_record_selection_constrains_untraceable_engine_processing_to_local(conn, provider):
    from topos.features.lifecycle.blackhole_llm import evaluate, LOCAL_PROVIDERS

    protect(conn)
    verdict = evaluate(conn, {"prompt": "paraphrase with no record id or entity name"}, provider=provider)
    assert verdict.tainted
    assert verdict.allowed_providers == LOCAL_PROVIDERS
    assert verdict.provider in LOCAL_PROVIDERS


def test_raw_tier_cannot_elevate_third_party_blackhole_summary(conn):
    from topos.query.retrieval import _blackhole_policy_for_summary

    protect(conn)
    token = set_principal(Principal(THIRD_PARTY, "cp_relay"))
    try:
        assert _blackhole_policy_for_summary([{"source_refs": [{"record_id": "private"}], "body": "unnamed paraphrase"}], conn=conn, disclosure_tier="owner_raw") == []
    finally:
        reset_principal(token)


def test_missing_migrated_protection_table_fails_closed(conn):
    protect(conn)
    conn.execute("DROP TABLE owner_only_records")
    with pytest.raises(sqlite3.OperationalError, match="protection schema"):
        RecordProtectionStore(conn).list()


def test_record_capability_names_only_present_native_tables(conn):
    supported = RecordProtectionStore(conn).supported_tables()
    assert "conversation_messages" in supported
    conn.execute("CREATE TABLE custom_private_table(record_id TEXT)")
    assert "custom_private_table" not in RecordProtectionStore(conn).supported_tables()
    # Unsupported schema cannot be advertised merely because its table name is familiar.
    with sqlite3.connect(":memory:") as malformed:
        malformed.execute("CREATE TABLE journal_entries(wrong_id TEXT)")
        assert RecordProtectionStore(malformed).supported_tables() == []


def test_missing_migrated_entity_protection_table_fails_closed(conn):
    from topos.features.lifecycle.blackhole import BlackholeStore

    conn.execute("DROP TABLE entity_blackholes")
    with pytest.raises(sqlite3.OperationalError, match="protection schema"):
        BlackholeStore(conn).blackholed_entity_ids()


def test_third_party_raw_tier_cannot_bypass_native_record_protection(conn):
    protect(conn)
    token = set_principal(Principal(THIRD_PARTY, "local_http"))
    try:
        page = SQLiteCanonicalStore(conn)._list_native("conversation_messages", disclosure_tier="owner_raw")
        assert page.total == 1
        assert [r["record_id"] for r in page.items] == ["public"]
    finally:
        reset_principal(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get_table_rows", "get_table_count", "list_database_tables", "get_analytics", "get_oplog", "get_messages", "read_jsonl_file", "graph_summary"])
async def test_legacy_inspection_cannot_override_record_protection(conn, monkeypatch, operation):
    import topos.core.handlers as hub

    protect(conn)
    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)
    async def reader(_):
        return {"status": "ok", "payload": "CANARY_OWNER_ONLY"}
    monkeypatch.setitem(hub.HANDLERS, operation, reader)
    message = {"id": "x", "type": operation, "payload": {"owner_id": "owner", "requester_id": "owner", "disclosure_tier": "owner_raw"}}
    assert (await hub.handle_control_plane_request(message, principal=Principal(THIRD_PARTY, "cp_relay")))["code"] == 403
    assert (await hub.handle_control_plane_request(message, principal=Principal(OWNER_APP, "uds")))["payload"] == "CANARY_OWNER_ONLY"


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [{"dataset_id": "other"}, {"owner_user_id": "other"}, {"resource_id": "malformed"}])
async def test_uma_resource_binding_checked_before_db_access(monkeypatch, overrides):
    import topos.core.handlers as hub
    from topos.core.handlers.uma import handle_uma_get_messages

    monkeypatch.setattr(hub, "get_db_connection", lambda: pytest.fail("Mismatched resource reached DB"))
    payload = {"resource_id": "dataset:owner:data:device", "allowed_scopes": ["messages:read"], **overrides}
    result = await handle_uma_get_messages({"id": "x", "payload": payload})
    assert result["error"] == "resource_binding_required"


def test_query_exclusion_matches_vector_dimension_alias():
    from topos.query.exclusion import enforce_request_exclusions

    packet = {"semantic_hits": [{"record_id": "finance", "signal_dimension": "resources"}, {"record_id": "book", "signal_dimension": "interests"}]}
    enforce_request_exclusions(packet, query_text="Tell me about my week, except finances")
    assert packet["semantic_hits"] == [{"record_id": "book", "signal_dimension": "interests"}]


@pytest.mark.asyncio
async def test_direct_uma_http_oplog_stays_unavailable(monkeypatch):
    from fastapi import HTTPException
    from topos.api import uma_data

    async def authenticated(*_):
        return {"allowed_scopes": ["all:read"]}
    monkeypatch.setattr(uma_data, "require_uma_rpt", authenticated)
    monkeypatch.setattr(uma_data, "get_db_connection", lambda: pytest.fail("Raw oplog reached DB"))
    with pytest.raises(HTTPException) as exc:
        await uma_data.get_uma_oplog(SimpleNamespace(), "dataset:owner:data:device")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_direct_uma_http_dataset_override_is_rejected(monkeypatch):
    from fastapi import HTTPException
    from topos.api import uma_data

    async def authenticated(*_):
        return {"allowed_scopes": ["messages:read"]}
    monkeypatch.setattr(uma_data, "require_uma_rpt", authenticated)
    monkeypatch.setattr(uma_data, "get_db_connection", lambda: pytest.fail("Other dataset reached DB"))
    with pytest.raises(HTTPException) as exc:
        await uma_data.get_uma_messages(SimpleNamespace(), "dataset:owner:data:device", dataset_id="other")
    assert exc.value.status_code == 403
