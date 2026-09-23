"""Synthetic grant canaries through the actual SQL readers and disclosure policy.

Contact transforms and audit persistence are isolated: this suite tests the
resource/scope/disclosure boundary, without opening a real owner database.
"""
import asyncio
import hashlib
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import topos.core.handlers as hub
from topos.core.handlers import uma
from topos.api import uma_data
from topos.uma_authority import message_stream_granted

OWNER = "owner-a"
DATASET = "owner-a:topos:allowed"
OTHER_DATASET = "owner-a:topos:excluded"
DEVICE = hashlib.sha256(b"synthetic-node-key").hexdigest()[:16]
RESOURCE = f"dataset:{OWNER}:{DATASET}:{DEVICE}"
RAW = "private-canary@example.com"
DISCLOSED = "Contact [EMAIL]"


@pytest.fixture
def conn(monkeypatch):
    from topos.config.settings import settings
    monkeypatch.setattr(settings, "topos_key", "synthetic-node-key")
    monkeypatch.setattr(settings, "topos_database_mode", "local")
    monkeypatch.setattr(settings, "topos_pool_mode", "off")
    monkeypatch.setattr(settings, "hosted_pool_lease_enabled", False)
    for key in ("K_SERVICE", "K_REVISION", "CLOUD_RUN_JOB"):
        monkeypatch.delenv(key, raising=False)
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE engine_config(key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT INTO engine_config VALUES ('user_id', ?)", (OWNER,))
    monkeypatch.setattr(hub, "get_db_connection", lambda: db)
    monkeypatch.setattr(hub, "record_uma_request", lambda *a, **kw: None)
    monkeypatch.setattr(uma_data, "get_db_connection", lambda: db)
    monkeypatch.setattr(uma, "_uma_blackhole_guard", lambda db: None)
    for module in (uma, uma_data):
        monkeypatch.setattr(module, "apply_message_contact_pipeline", lambda rows, **kw: (rows, {}))
    yield db
    db.close()


def make_messages(conn, table="conversation_messages", *, dataset_column=True):
    # Handler projections require the canonical fields, including disclosure.
    columns = {
        "message_id": "TEXT PRIMARY KEY", "conversation_id": "TEXT", "sender_type": "TEXT",
        "sender_id": "TEXT", "event_at": "TEXT", "ts": "TEXT", "content": "TEXT",
        "content_rendered": "TEXT", "content_disclosure": "TEXT", "content_disclosure_hash": "TEXT",
        "content_rendered_disclosure": "TEXT", "content_nsfw": "INTEGER", "content_nsfw_score": "REAL",
        "metadata_json": "TEXT", "source_id": "TEXT", "reply_to_message_id": "TEXT",
        "message_type": "TEXT", "event_type": "TEXT", "is_from_self": "INTEGER",
        "owner_user_id": "TEXT", "sequence": "INTEGER",
    }
    if dataset_column:
        columns["dataset_id"] = "TEXT"
    conn.execute(f'CREATE TABLE "{table}" (' + ','.join(f'"{k}" {v}' for k, v in columns.items()) + ')')


def seed(conn, table, message_id, timestamp, *, dataset=DATASET, owner=OWNER, disclosed=DISCLOSED):
    row = dict(message_id=message_id, conversation_id="same-conversation", sender_type="user",
               event_at=timestamp, ts=timestamp, content=RAW, content_rendered=RAW,
               content_disclosure=disclosed, content_rendered_disclosure=disclosed,
               source_id="same-source", owner_user_id=owner, dataset_id=dataset)
    columns = {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    row = {k: v for k, v in row.items() if k in columns}
    conn.execute(f'INSERT INTO "{table}" (' + ','.join(row) + ') VALUES (' + ','.join('?' for _ in row) + ')', tuple(row.values()))


def call(handler=uma.handle_uma_get_messages, **overrides):
    payload = dict(resource_id=RESOURCE, dataset_id=DATASET, owner_user_id=OWNER,
                   allowed_scopes=["messages:read"], limit=10, offset=0)
    payload.update(overrides)
    return asyncio.run(handler({"id": "test-read", "payload": payload}))


@pytest.mark.parametrize("scopes,stream,allowed", [
    ([], "conversation", False), (["read"], "conversation", False),
    (["messages:read"], "conversation", True), (["messages:read"], "ai_chat", False),
    (["ai_conversations:read"], "ai_chat", True), (["ai_conversations:read"], "conversation", False),
    (["aiChat:read"], "ai_chat", True), (["aiMessages:read"], "ai_chat", True),
    (["all:read"], "conversation", True), (["all:read"], "ai_chat", True),
    (["all:read"], "typo", False), ("messages:read", "conversation", False),
    (["messages:write"], "conversation", False), (["messages:*"], "conversation", False),
])
def test_explicit_message_scope_vocabulary(scopes, stream, allowed):
    assert message_stream_granted(scopes, stream) is allowed


@pytest.mark.parametrize("overrides", [
    {"resource_id": ""}, {"resource_id": "dataset:owner:device"},
    {"resource_id": "dataset::data:device"}, {"resource_id": "dataset:owner:data:"},
    {"dataset_id": OTHER_DATASET}, {"owner_user_id": "other-owner"},
    {"allowed_scopes": []}, {"allowed_scopes": ["read"]},
    {"message_stream": "ai_chat"}, {"message_stream": "typo", "allowed_scopes": ["all:read"]},
])
def test_bad_authority_denied_before_database(monkeypatch, overrides):
    monkeypatch.setattr(hub, "get_db_connection", lambda: pytest.fail("unauthorized DB access"))
    result = call(**overrides)
    assert result["status"] == "error" and result["code"] == 403


@pytest.mark.parametrize("grantee_flag", [None, False, True])
def test_transport_conversation_dataset_before_limit_and_forced_disclosure(conn, grantee_flag):
    make_messages(conn)
    seed(conn, "conversation_messages", "approved", "2026-01-01")
    seed(conn, "conversation_messages", "foreign-dataset", "2026-03-01", dataset=OTHER_DATASET)
    seed(conn, "conversation_messages", "foreign-owner", "2026-04-01", owner="other-owner")
    result = call(limit=1, requesting_user_id=OWNER, is_grantee_request=grantee_flag,
                  disclosure_ceiling="raw", explicit_tier="owner_raw")
    assert result["status"] == "ok", result
    assert [m["message_id"] for m in result["payload"]["messages"]] == ["approved"]
    assert result["payload"]["messages"][0]["content"] == DISCLOSED
    assert RAW not in str(result)


@pytest.mark.parametrize("table", ["conversation_messages", "messages", "ai_chat_messages"])
def test_http_reader_scopes_before_pagination_and_discloses(conn, table):
    make_messages(conn, table)
    seed(conn, table, "approved-1", "2026-01-01")
    seed(conn, table, "approved-2", "2026-02-01")
    seed(conn, table, "excluded", "2026-03-01", dataset=OTHER_DATASET)
    seed(conn, table, "wrong-owner", "2026-04-01", owner="other-owner")
    result = uma_data._get_messages_from_db(conn, DATASET, 1, 1, {table}, OWNER)
    assert [m["message_id"] for m in result] == ["approved-1"]
    assert result[0]["content"] == DISCLOSED
    assert RAW not in str(result[0]["content"])


def test_transport_ai_with_explicit_dataset_provenance_positive(conn):
    make_messages(conn, "ai_chat_messages")
    seed(conn, "ai_chat_messages", "approved", "2026-01-01")
    seed(conn, "ai_chat_messages", "excluded", "2026-02-01", dataset=OTHER_DATASET)
    result = call(message_stream="ai_chat", allowed_scopes=["ai_conversations:read"], limit=1,
                  requesting_user_id=OWNER, is_grantee_request=False, disclosure_ceiling="raw")
    assert result["status"] == "ok", result
    assert [r["message_id"] for r in result["payload"]["messages"]] == ["approved"]
    assert RAW not in str(result)


def test_real_ai_schema_does_not_prove_dataset_from_conversation_owner(conn):
    from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
    manager = CanonicalTablesManager.__new__(CanonicalTablesManager)
    manager.conn = conn
    manager._ensure_tables()  # Actual production DDL, without unrelated migrations.
    assert "dataset_id" not in {r[1] for r in conn.execute('PRAGMA table_info(ai_chat_messages)')}
    conn.execute("INSERT INTO ai_chat_conversations VALUES ('c', ?, 'title', 'source', 'now', 'now')", (OWNER,))
    conn.execute("INSERT INTO ai_chat_messages(message_id, conversation_id, sender_type, event_at, content, source_id) VALUES ('private', 'c', 'user', 'now', ?, 'source')", (RAW,))
    result = call(message_stream="ai_chat", allowed_scopes=["ai_conversations:read"])
    assert result["error"] == "dataset_scope_unavailable"
    with pytest.raises(ValueError, match="dataset_scope_unavailable"):
        uma_data._get_messages_from_db(conn, DATASET, 10, 0, {"ai_chat_messages"}, OWNER)


def test_ambiguous_jsonl_fallback_is_not_a_granted_reader(conn, monkeypatch):
    import topos.analytics.raw_queries as raw_queries
    monkeypatch.setattr(raw_queries, "load_raw_messages", lambda **kw: pytest.fail("unbound JSONL read"))
    result = call(message_stream="ai_chat", allowed_scopes=["aiChat:read"])
    assert result["error"] == "dataset_scope_unavailable"


@pytest.mark.parametrize("allowed_tables", [None, [], {}, "conversation_messages", ["other_table"]])
def test_generic_rows_requires_explicit_table_membership(monkeypatch, allowed_tables):
    monkeypatch.setattr(hub, "get_db_connection", lambda: pytest.fail("unauthorized DB access"))
    result = call(uma.handle_uma_get_rows, table_name="conversation_messages", allowed_tables=allowed_tables)
    assert result["status"] == "error" and "table not allowed" in result["error"]


def test_generic_rows_dataset_and_has_more_are_bound(conn):
    make_messages(conn)
    seed(conn, "conversation_messages", "approved", "2026-01-01")
    seed(conn, "conversation_messages", "foreign", "2026-02-01", dataset=OTHER_DATASET)
    result = call(uma.handle_uma_get_rows, table_name="conversation_messages", allowed_tables=["conversation_messages"], limit=1)
    assert result["status"] == "ok", result
    payload = result["payload"]
    assert [r["message_id"] for r in payload["rows"]] == ["approved"]
    assert payload["has_more"] is False and payload["next_offset"] is None
    assert payload["rows"][0]["content"] == DISCLOSED


def test_owner_or_tenant_is_not_a_dataset_predicate(conn):
    conn.execute("CREATE TABLE legacy (id TEXT, owner_user_id TEXT, tenant_id TEXT, secret TEXT)")
    conn.execute("INSERT INTO legacy VALUES ('x', ?, 'tenant', ?)", (OWNER, RAW))
    result = call(uma.handle_uma_get_rows, table_name="legacy", allowed_tables=["legacy"], tenant_id="tenant")
    assert result["status"] == "error" and "dataset scoped" in result["error"]
    assert RAW not in str(result)


def client(monkeypatch, scopes, filters=None):
    async def fake_rpt(request, resource_id):
        payload = {"allowed_scopes": scopes, "filters": filters or {}}
        request.state.uma_introspection = payload
        return payload
    monkeypatch.setattr(uma_data, "require_uma_rpt", fake_rpt)
    app = FastAPI()
    app.include_router(uma_data.router)
    return TestClient(app)


def test_http_endpoint_real_query_count_and_disclosure(conn, monkeypatch):
    make_messages(conn)
    seed(conn, "conversation_messages", "approved", "2026-01-01")
    seed(conn, "conversation_messages", "excluded", "2026-02-01", dataset=OTHER_DATASET)
    response = client(monkeypatch, ["messages:read"]).get(f"/v1/uma/resources/{RESOURCE}/data/messages?limit=1")
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 1
    assert [r["message_id"] for r in response.json()["messages"]] == ["approved"]
    assert response.json()["messages"][0]["content"] == DISCLOSED


@pytest.mark.parametrize("scopes,query", [([], ""), (["read"], ""), (["messages:read"], f"?dataset_id={OTHER_DATASET}")])
def test_http_scope_or_resource_override_denied_before_db(monkeypatch, scopes, query):
    monkeypatch.setattr(uma_data, "get_db_connection", lambda: pytest.fail("unauthorized DB access"))
    response = client(monkeypatch, scopes).get(f"/v1/uma/resources/{RESOURCE}/data/messages{query}")
    assert response.status_code == 403


def test_shared_oplog_denied_on_both_transports_before_db(monkeypatch):
    monkeypatch.setattr(hub, "get_db_connection", lambda: pytest.fail("oplog DB access"))
    monkeypatch.setattr(uma_data, "get_db_connection", lambda: pytest.fail("oplog DB access"))
    result = call(uma.handle_uma_get_oplog, allowed_scopes=["all:read"])
    assert result["error"] == "shared_oplog_unavailable" and result["code"] == 403
    response = client(monkeypatch, ["all:read"]).get(f"/v1/uma/resources/{RESOURCE}/data/oplog")
    assert response.status_code == 403 and response.json()["detail"] == "shared_oplog_unavailable"


@pytest.fixture
def local_resource(conn, monkeypatch):
    import hashlib
    from topos.config.settings import settings
    for name, value in {"topos_key": "synthetic-node-key", "topos_database_mode": "local",
                        "topos_pool_mode": "off", "hosted_pool_lease_enabled": False}.items():
        monkeypatch.setattr(settings, name, value)
    for key in ("K_SERVICE", "K_REVISION", "CLOUD_RUN_JOB"):
        monkeypatch.delenv(key, raising=False)
    device = hashlib.sha256(b"synthetic-node-key").hexdigest()[:16]
    return f"dataset:{OWNER}:{OWNER}:default:{device}:{device}"


def relay_call(handler=uma.handle_uma_get_messages, **payload):
    from topos.principal import RELAY_PRINCIPAL, set_principal, reset_principal
    token = set_principal(RELAY_PRINCIPAL)
    try:
        return call(handler, **payload)
    finally:
        reset_principal(token)


def real_ai_schema(conn):
    from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
    from topos.storage.db.migrations.canonical_disclosure_v1 import apply_canonical_disclosure_v1_up
    from topos.storage.db.migrations.canonical_nsfw_v1 import apply_canonical_nsfw_v1_up
    manager = CanonicalTablesManager.__new__(CanonicalTablesManager)
    manager.conn = conn
    manager._ensure_tables()
    apply_canonical_disclosure_v1_up(conn)
    apply_canonical_nsfw_v1_up(conn)


def test_verified_local_engine_resource_preserves_real_ai_reader(conn, local_resource, monkeypatch):
    real_ai_schema(conn)
    seed(conn, "ai_chat_messages", "local-ai", "2026-01-01")
    result = relay_call(resource_id=local_resource, dataset_id=None, message_stream="ai_chat",
                        allowed_scopes=["ai_conversations:read"], is_grantee_request=False,
                        requesting_user_id=OWNER, disclosure_ceiling="raw")
    assert result["status"] == "ok", result
    assert [r["message_id"] for r in result["payload"]["messages"]] == ["local-ai"]
    assert RAW not in str(result)
    response = client(monkeypatch, ["aiChat:read"]).get(f"/v1/uma/resources/{local_resource}/data/messages")
    assert response.status_code == 200, response.text
    assert response.json()["count"] == 1 and response.json()["messages"][0]["content"] == DISCLOSED
    assert RAW not in response.text


def test_verified_local_engine_resource_covers_ingest_datasets(conn, local_resource):
    make_messages(conn)
    # Actual local ingestion uses :topos: IDs, while automatic UMA registration
    # is a physical-node :default:keyhash resource. Message owner IDs can differ.
    seed(conn, "conversation_messages", "local-1", "2026-01-01", owner="source-account")
    seed(conn, "conversation_messages", "local-2", "2026-02-01", dataset=OTHER_DATASET)
    result = relay_call(resource_id=local_resource, dataset_id=None)
    assert result["status"] == "ok", result
    assert {r["message_id"] for r in result["payload"]["messages"]} == {"local-1", "local-2"}
    assert RAW not in str(result)


@pytest.mark.parametrize("defect", ["missing-owner", "wrong-owner", "wrong-key", "wrong-device",
                                    "custom-resource", "pool", "lease", "cloud", "postgres", "unverified-channel"])
def test_local_engine_proof_cannot_be_claimed_by_payload(conn, local_resource, monkeypatch, defect):
    from topos.config.settings import settings
    real_ai_schema(conn)
    seed(conn, "ai_chat_messages", "private", "2026-01-01")
    resource = local_resource
    if defect == "missing-owner":
        conn.execute("DELETE FROM engine_config")
    elif defect == "wrong-owner":
        conn.execute("UPDATE engine_config SET value = 'other-owner'")
    elif defect == "wrong-key":
        monkeypatch.setattr(settings, "topos_key", "different-node")
    elif defect == "wrong-device":
        resource = resource.rsplit(":", 1)[0] + ":different-device"
    elif defect == "custom-resource":
        resource = RESOURCE
    elif defect == "pool":
        monkeypatch.setattr(settings, "topos_pool_mode", "pooled")
    elif defect == "lease":
        monkeypatch.setattr(settings, "hosted_pool_lease_enabled", True)
    elif defect == "cloud":
        monkeypatch.setenv("K_SERVICE", "hosted")
    elif defect == "postgres":
        monkeypatch.setattr(settings, "topos_database_mode", "postgres")
    invoke = call if defect == "unverified-channel" else relay_call
    result = invoke(resource_id=resource, dataset_id=None, message_stream="ai_chat",
                    allowed_scopes=["ai_conversations:read"], whole_engine_scope=True,
                    local_node_resource=True, rpt_validated=True,
                    caller={"channel": "cp_relay", "cls": "owner_app"})
    assert result["error"] == "dataset_scope_unavailable", result
    assert RAW not in str(result)


def test_local_engine_generic_rows_still_require_table_allowlist(conn, local_resource):
    conn.execute("CREATE TABLE plain (id TEXT, secret TEXT)")
    conn.execute("INSERT INTO plain VALUES ('approved', 'synthetic')")
    denied = relay_call(uma.handle_uma_get_rows, resource_id=local_resource, dataset_id=None,
                        table_name="plain", allowed_tables=[])
    assert denied["status"] == "error"
    allowed = relay_call(uma.handle_uma_get_rows, resource_id=local_resource, dataset_id=None,
                         table_name="plain", allowed_tables=["plain"])
    assert allowed["status"] == "error" and "dataset scoped" in allowed["error"]


def test_local_http_whole_node_scope_still_requires_rpt(conn, local_resource, monkeypatch):
    from fastapi import HTTPException
    app = FastAPI()
    app.include_router(uma_data.router)
    async def reject_rpt(request, resource_id):
        raise HTTPException(status_code=401, detail="invalid RPT")
    monkeypatch.setattr(uma_data, "require_uma_rpt", reject_rpt)
    monkeypatch.setattr(uma_data, "get_db_connection", lambda: pytest.fail("DB before RPT validation"))
    response = TestClient(app).get(f"/v1/uma/resources/{local_resource}/data/messages")
    assert response.status_code == 401


@pytest.mark.parametrize("stored_owner", [None, "another-owner"])
def test_local_custom_http_resource_requires_persisted_owner(conn, monkeypatch, stored_owner):
    make_messages(conn)
    seed(conn, "conversation_messages", "private", "2026-01-01")
    conn.execute("DELETE FROM engine_config")
    if stored_owner:
        conn.execute("INSERT INTO engine_config VALUES ('user_id', ?)", (stored_owner,))
    queries = []
    conn.set_trace_callback(queries.append)
    response = client(monkeypatch, ["messages:read"]).get(f"/v1/uma/resources/{RESOURCE}/data/messages")
    assert response.status_code == 403 and response.json()["detail"] == "resource_owner_mismatch"
    assert not any('FROM "conversation_messages"' in q for q in queries)
    assert RAW not in response.text


def test_custom_http_cannot_claim_foreign_owner_even_for_ownerless_table(conn, monkeypatch):
    conn.execute("CREATE TABLE messages(message_id TEXT, dataset_id TEXT, ts TEXT, content TEXT, content_disclosure TEXT)")
    conn.execute("INSERT INTO messages VALUES ('private', ?, '2026-01-01', ?, ?)", (DATASET, RAW, DISCLOSED))
    foreign_resource = f"dataset:other-owner:{DATASET}:device-a"
    response = client(monkeypatch, ["messages:read"]).get(f"/v1/uma/resources/{foreign_resource}/data/messages")
    assert response.status_code == 403 and response.json()["detail"] == "resource_owner_mismatch"
    positive = client(monkeypatch, ["messages:read"]).get(f"/v1/uma/resources/{RESOURCE}/data/messages")
    assert positive.status_code == 200 and positive.json()["count"] == 1
    assert positive.json()["messages"][0]["content"] == DISCLOSED


def test_custom_http_resource_from_another_node_cannot_replay(conn, monkeypatch):
    make_messages(conn)
    seed(conn, "conversation_messages", "private", "2026-01-01")
    wrong_node_resource = RESOURCE.rsplit(":", 1)[0] + ":another-node"
    response = client(monkeypatch, ["messages:read"]).get(f"/v1/uma/resources/{wrong_node_resource}/data/messages")
    assert response.status_code == 403 and response.json()["detail"] == "resource_device_mismatch"
    assert RAW not in response.text


@pytest.mark.parametrize("grantee_flag", [None, False])
def test_equal_owner_ids_never_lift_uma_disclosure(conn, grantee_flag):
    make_messages(conn)
    seed(conn, "conversation_messages", "approved", "2026-01-01")
    result = call(requesting_user_id=OWNER, is_grantee_request=grantee_flag, disclosure_ceiling="raw")
    assert result["status"] == "ok", result
    assert [m["message_id"] for m in result["payload"]["messages"]] == ["approved"]
    assert result["payload"]["messages"][0]["content"] == DISCLOSED
    assert RAW not in str(result)


def test_direct_http_oplog_with_real_canary_is_unavailable(conn, monkeypatch):
    conn.execute("CREATE TABLE oplog(hlc_ts TEXT, raw_change TEXT)")
    conn.execute("INSERT INTO oplog VALUES ('2026-01-01', ?)", (RAW,))
    response = client(monkeypatch, ["all:read"]).get(f"/v1/uma/resources/{RESOURCE}/data/oplog")
    assert response.status_code == 403, response.text
    assert RAW not in response.text


@pytest.mark.parametrize("fid,key", [("source_filter", "source_ids"), ("column_allowlist", "fields")])
@pytest.mark.parametrize("generic", [False, True])
def test_saved_empty_filters_never_release_rows_or_pagination(conn, fid, key, generic):
    make_messages(conn)
    seed(conn, "conversation_messages", "approved-1", "2026-01-01")
    seed(conn, "conversation_messages", "approved-2", "2026-02-01")
    filters = {"filter_manifest": {"filters": [{"filter_id": fid, "params": {key: []}}]}}
    handler = uma.handle_uma_get_rows if generic else uma.handle_uma_get_messages
    result = call(handler, filters=filters, limit=1, table_name="conversation_messages", allowed_tables=["conversation_messages"])
    assert result["status"] == "ok", result
    assert result["payload"]["rows" if generic else "messages"] == []
    assert not result["payload"].get("has_more")
    assert not result["payload"].get("next_offset")


def test_generic_source_filter_scopes_before_limit_and_has_more(conn):
    make_messages(conn)
    seed(conn, "conversation_messages", "approved", "2026-01-01")
    seed(conn, "conversation_messages", "excluded", "2026-02-01")
    conn.execute("UPDATE conversation_messages SET source_id='excluded-source' WHERE message_id='excluded'")
    filters = {"filter_manifest": {"filters": [{"filter_id": "source_filter", "params": {"source_ids": ["same-source"]}}]}}
    result = call(uma.handle_uma_get_rows, filters=filters, limit=1, table_name="conversation_messages", allowed_tables=["conversation_messages"])
    assert result["status"] == "ok", result
    assert [row["message_id"] for row in result["payload"]["rows"]] == ["approved"]
    assert result["payload"]["has_more"] is False


@pytest.mark.parametrize("projection,allowed", [
    ({"scope_table_allowlist": {"messages:read": []}}, False),
    ({"scope_table_allowlist": {"messages:read": ["ai_chat_messages"]}}, False),
    ({"scope_table_allowlist": {"messages:read": ["conversation_messages"]}}, True),
    ({"access_mode_ceiling": "summary"}, False),
    ({"access_mode_ceiling": "inference"}, False),
    ({"access_mode_ceiling": "raw"}, True),
])
@pytest.mark.parametrize("transport", ["http", "relay", "generic"])
def test_raw_readers_enforce_saved_table_and_view_projection(conn, monkeypatch, projection, allowed, transport):
    make_messages(conn)
    seed(conn, "conversation_messages", "approved", "2026-01-01")
    filters = {"filter_manifest": {"filters": [], **projection}}
    queries = []
    conn.set_trace_callback(queries.append)
    if transport == "http":
        response = client(monkeypatch, ["messages:read"], filters).get(f"/v1/uma/resources/{RESOURCE}/data/messages")
        assert response.status_code == (200 if allowed else 403), response.text
        payload = response.json()
    else:
        result = call(uma.handle_uma_get_rows if transport == "generic" else uma.handle_uma_get_messages,
                      filters=filters, table_name="conversation_messages", allowed_tables=["conversation_messages"])
        assert result["status"] == ("ok" if allowed else "error"), result
        payload = result.get("payload", result)
    if allowed:
        assert "approved" in str(payload) and DISCLOSED in str(payload)
    else:
        assert not any('FROM "conversation_messages"' in q or 'FROM conversation_messages m' in q for q in queries)
        assert "approved" not in str(payload) and DISCLOSED not in str(payload)


def test_other_scope_cannot_erase_empty_message_table_selection(conn, monkeypatch):
    for table in ("conversation_messages", "ai_chat_messages"):
        make_messages(conn, table)
        seed(conn, table, table, "2026-01-01")
    filters = {"filter_manifest": {"filters": [], "scope_table_allowlist": {"messages:read": []}}}
    response = client(monkeypatch, ["messages:read", "ai_conversations:read"], filters).get(f"/v1/uma/resources/{RESOURCE}/data/messages")
    assert response.status_code == 200, response.text
    assert [row["message_id"] for row in response.json()["messages"]] == ["ai_chat_messages"]


@pytest.mark.parametrize("fid,key", [("source_filter", "source_ids"), ("column_allowlist", "fields")])
def test_empty_message_selection_never_builds_contact_sidecars(conn, monkeypatch, fid, key):
    make_messages(conn)
    seed(conn, "conversation_messages", "private", "2026-01-01")
    for module in (uma, uma_data):
        monkeypatch.setattr(module, "apply_message_contact_pipeline", lambda *a, **kw: pytest.fail("empty selection reached contact enrichment"))
    filters = {"filter_manifest": {"filters": [{"filter_id": fid, "params": {key: []}}]}}
    response = client(monkeypatch, ["messages:read"], filters).get(f"/v1/uma/resources/{RESOURCE}/data/messages")
    assert response.status_code == 200, response.text
    assert response.json() == {"messages": [], "count": 0, "message_owner": {}}
    assert call(filters=filters)["payload"] == {"messages": []}
