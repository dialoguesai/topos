"""Ingestion doors that write canonical rows or durable jobs answer only the owner.

Every case runs against a scratch database and synthetic rows. Each door is
exercised from three sides:

- a THIRD_PARTY principal arriving over the relay with a verified CP stamp;
- a TCP caller holding the legacy shared key while TOPOS_OWNER_KEY is set;
- the owner: the 0600 socket for HTTP, an owner_app stamp for the relay.

Plus the install-flow invariant: while TOPOS_OWNER_KEY is unset, the legacy key
still reaches every HTTP door exactly as before.
"""
from __future__ import annotations

import base64
import json
import sqlite3
import time
import uuid

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import Depends, FastAPI

from topos.auth import resolve_request_principal
from topos.principal import OWNER_APP, RELAY_PRINCIPAL, THIRD_PARTY
from topos.uds import UDSChannelApp

LEGACY_KEY = "synthetic-engine-key"
OWNER_KEY = "synthetic-owner-key"
OWNER_ACTOR = "owner-synthetic"


@pytest.fixture
def cp_key(monkeypatch):
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(64, 96)))
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", base64.b64encode(key.public_key().public_bytes_raw()).decode())
    return key


@pytest.fixture
def keys(monkeypatch):
    from topos.config.settings import settings

    monkeypatch.setattr(settings, "topos_key", LEGACY_KEY)
    monkeypatch.setattr(settings, "topos_owner_key", OWNER_KEY)
    return settings


@pytest.fixture
def db(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "scratch.db"), check_same_thread=False)
    factory = lambda: conn  # noqa: E731
    import topos.core.handlers as handlers
    import topos.core.state as state
    import topos.ingestion.reprocess as reprocess

    monkeypatch.setattr(state, "get_db_connection", factory)
    monkeypatch.setattr(handlers, "get_db_connection", factory)
    monkeypatch.setattr(reprocess, "get_db_connection", factory)
    monkeypatch.setenv("TOPOS_PIPELINE_WORKER", "off")
    yield conn


def stamped(cp_key, msg_type, payload, *, cls):
    message = {"id": str(uuid.uuid4()), "type": msg_type, "payload": payload}
    from topos.relay_stamp import canonical_signing_payload

    now = time.time()
    stamp = {"v": 1, "cls": cls, "client_id": "topos_home_chat" if cls == OWNER_APP else "claude",
             "acting_user": OWNER_ACTOR, "iat": now, "exp": now + 120}
    signed = canonical_signing_payload(stamp, msg_id=message["id"], msg_type=msg_type)
    stamp["sig"] = base64.b64encode(cp_key.sign(signed)).decode()
    message["principal_stamp"] = stamp
    return message


async def relay(message):
    """The exact verified-principal handoff app._relay_dispatch performs."""
    from topos.core.handlers import handle_control_plane_request
    from topos.relay_stamp import verify_relay_stamp

    return await handle_control_plane_request(message, principal=verify_relay_stamp(message) or RELAY_PRINCIPAL)


def dispatch_app():
    """A principal-aware local HTTP dispatcher, shaped like api/connected_apps._dispatch."""
    from topos.core.handlers import handle_control_plane_request

    app = FastAPI()

    @app.post("/dispatch")
    async def _dispatch(body: dict, principal=Depends(resolve_request_principal)):  # noqa: B008
        return await handle_control_plane_request(body, principal=principal)

    return app


async def post(app, path, *, uds=False, token=None, **kwargs):
    target = UDSChannelApp(app) if uds else app
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=target), base_url="http://127.0.0.1") as client:
        return await client.post(path, headers=headers, **kwargs)


def refused(response):
    return response["status"] == "error" and response.get("error") == "owner_mode_required"


# ---- 1. local sync enqueue ----------------------------------------------------


@pytest.fixture
def sync_spy(monkeypatch):
    calls = []

    async def fake_enqueue(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "job_id": "job-synthetic", "already_running": False}

    import topos.api.ingestion_sources as routes
    import topos.ingestion.local_sync_jobs as jobs

    monkeypatch.setattr(routes, "enqueue_local_sync", fake_enqueue)
    monkeypatch.setattr(jobs, "enqueue_local_sync", fake_enqueue)
    return calls


def sources_app():
    from topos.api.ingestion_sources import router

    app = FastAPI()
    app.include_router(router)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [LEGACY_KEY, OWNER_KEY])
async def test_sync_http_refuses_tcp_bearers_once_the_owner_key_exists(keys, sync_spy, token):
    response = await post(sources_app(), "/sources/imessage/sync", token=token, params={"dataset_id": "ds-a"})
    assert response.status_code == 403
    assert sync_spy == []


@pytest.mark.asyncio
async def test_sync_http_owner_socket_still_enqueues(keys, sync_spy):
    response = await post(sources_app(), "/sources/imessage/sync", uds=True, params={"dataset_id": "ds-a"})
    assert response.status_code == 200 and response.json()["job_id"] == "job-synthetic"
    assert [call["dataset_id"] for call in sync_spy] == ["ds-a"]


@pytest.mark.asyncio
async def test_sync_http_legacy_mode_is_unchanged(keys, sync_spy, monkeypatch):
    monkeypatch.setattr(keys, "topos_owner_key", None)
    response = await post(sources_app(), "/sources/imessage/sync", token=LEGACY_KEY, params={"dataset_id": "ds-a"})
    assert response.status_code == 200 and response.json()["status"] == "ok"
    assert len(sync_spy) == 1
    missing = await post(sources_app(), "/sources/imessage/sync", params={"dataset_id": "ds-a"})
    assert missing.status_code == 401


@pytest.mark.asyncio
async def test_sync_relay_refuses_third_party_unstamped_and_legacy_key(keys, cp_key, sync_spy):
    payload = {"source_id": "imessage", "dataset_id": "ds-a"}
    assert refused(await relay(stamped(cp_key, "source_sync", payload, cls=THIRD_PARTY)))
    assert refused(await relay({"id": "unstamped", "type": "source_sync", "payload": payload}))
    over_tcp = await post(dispatch_app(), "/dispatch", token=LEGACY_KEY,
                          json={"id": "tcp", "type": "source_sync", "payload": payload})
    assert refused(over_tcp.json())
    assert sync_spy == []


@pytest.mark.asyncio
async def test_sync_relay_owner_stamp_still_enqueues(keys, cp_key, sync_spy, db):
    response = await relay(stamped(cp_key, "source_sync", {"source_id": "imessage", "dataset_id": "ds-a"}, cls=OWNER_APP))
    assert response["status"] == "ok" and response["payload"]["job_id"] == "job-synthetic"
    assert len(sync_spy) == 1


# ---- 2. start_ingestion cannot rewrite another job ---------------------------


@pytest.mark.asyncio
async def test_start_ingestion_cannot_rewrite_a_queued_local_sync_job(keys, db, monkeypatch):
    from topos.pipeline.job_store import enqueue_job, get_job
    from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up

    apply_pipeline_jobs_v1_up(db)
    monkeypatch.setattr("topos.pipeline.job_runner.start_pipeline_worker", lambda factory: None)
    job_id = str(uuid.uuid4())
    window = {"mode": "3m"}
    enqueue_job(db, kind="local_sync", job_id=job_id, source_id="imessage",
                payload={"source_id": "imessage", "dataset_id": "ds-owner", "sync_options": window},
                idempotency_key=f"local_sync:{job_id}")
    response = await relay({"id": "cp", "type": "start_ingestion", "payload": {
        "job_id": job_id, "dataset_id": "ds-attacker", "file_path": "/nonexistent/synthetic.jsonl"}})
    job = get_job(db, job_id)
    # Data first, so a regression shows WHAT was rewritten, not just a status.
    assert (job["kind"], job["payload"].get("dataset_id"), job["payload"].get("sync_options")) == (
        "local_sync", "ds-owner", window)
    assert response["status"] == "error"


def test_enqueue_refuses_a_job_id_that_names_another_kind(db):
    from topos.pipeline.job_store import JobIdConflictError, enqueue_job, get_job

    from topos.pipeline import job_secrets

    owner_payload = {"dataset_id": "ds-owner", "sync_options": {"signal_hex_key": "synthetic-owner-secret"}}
    enqueue_job(db, kind="local_sync", job_id="shared-id", payload=owner_payload,
                idempotency_key="local_sync:shared-id")
    held = job_secrets.peek("shared-id")
    stored = get_job(db, "shared-id")["payload"]
    with pytest.raises(JobIdConflictError):
        enqueue_job(db, kind="file_ingestion", job_id="shared-id",
                    payload={"dataset_id": "ds-other", "progress_api_key": "synthetic-caller-secret"},
                    idempotency_key="file_ingestion:shared-id")
    assert get_job(db, "shared-id")["payload"] == stored
    # The refused caller's secret is not held against the job it collided with.
    assert job_secrets.peek("shared-id") == held == {"sync_options.signal_hex_key": "synthetic-owner-secret"}
    assert not db.in_transaction
    # The same kind under a different key is a different job too.
    with pytest.raises(JobIdConflictError):
        enqueue_job(db, kind="local_sync", job_id="shared-id", payload={"dataset_id": "ds-other"},
                    idempotency_key="local_sync:forged")
    assert get_job(db, "shared-id")["payload"] == stored
    job_secrets.release("shared-id")


def test_enqueue_still_resolves_its_own_idempotency_key(db):
    from topos.pipeline.job_store import enqueue_job, get_job

    first = enqueue_job(db, kind="file_ingestion", job_id="cp-job", payload={"attempt": 1},
                        idempotency_key="file_ingestion:cp-job")
    db.execute("UPDATE pipeline_jobs SET status='failed' WHERE job_id='cp-job'")
    db.commit()
    again = enqueue_job(db, kind="file_ingestion", job_id="cp-job", payload={"attempt": 2},
                        idempotency_key="file_ingestion:cp-job")
    assert first == again == "cp-job"
    job = get_job(db, "cp-job")
    assert job["status"] == "queued" and job["payload"] == {"attempt": 2}


# ---- 3. pooled scope backfill ---------------------------------------------------


@pytest.fixture
def unscoped_rows(db):
    db.execute("CREATE TABLE conversation_messages (message_id TEXT PRIMARY KEY, content TEXT, owner_user_id TEXT)")
    db.executemany("INSERT INTO conversation_messages VALUES (?,?,?)",
                   [("m1", "synthetic body one", None), ("m2", "synthetic body two", "")])
    db.commit()
    return db


def owners(conn):
    return [row[0] for row in conn.execute("SELECT owner_user_id FROM conversation_messages ORDER BY message_id")]


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_type,payload", [
    ("pooled_scope_backfill_apply", {"owner_user_id": "attacker", "tables": ["conversation_messages"]}),
    ("pooled_scope_backfill_dry_run", {"tables": ["conversation_messages"]}),
    ("pooled_scope_backfill_rollback", {"migration_id": "synthetic"}),
])
async def test_pooled_scope_backfill_refuses_third_party_and_legacy_key(keys, cp_key, unscoped_rows, msg_type, payload):
    assert refused(await relay(stamped(cp_key, msg_type, payload, cls=THIRD_PARTY)))
    over_tcp = await post(dispatch_app(), "/dispatch", token=LEGACY_KEY, json={"id": "tcp", "type": msg_type, "payload": payload})
    assert refused(over_tcp.json())
    assert owners(unscoped_rows) == [None, ""]
    assert not unscoped_rows.execute(
        "SELECT name FROM sqlite_master WHERE name LIKE 'pooled_scope_%'").fetchall()


@pytest.mark.asyncio
async def test_pooled_scope_backfill_owner_can_apply_and_roll_back(keys, cp_key, unscoped_rows):
    applied = await relay(stamped(cp_key, "pooled_scope_backfill_apply",
                                  {"owner_user_id": OWNER_ACTOR, "tables": ["conversation_messages"]}, cls=OWNER_APP))
    assert applied["status"] == "ok"
    assert owners(unscoped_rows) == [OWNER_ACTOR, OWNER_ACTOR]
    over_socket = await post(dispatch_app(), "/dispatch", uds=True, json={
        "id": "uds", "type": "pooled_scope_backfill_rollback",
        "payload": {"migration_id": applied["payload"]["migration_id"]}})
    assert over_socket.json()["status"] == "ok"
    assert owners(unscoped_rows) == [None, ""]


# ---- 4. ingestion reprocess -----------------------------------------------------


@pytest.fixture
def reprocess_spy(monkeypatch):
    calls = []

    async def fake_reprocess(**kwargs):
        calls.append(kwargs)
        return {"status": "accepted", "source_id": kwargs["source_id"]}

    import topos.api.ingestion_api as api
    import topos.ingestion.reprocess as reprocess

    monkeypatch.setattr(api, "reprocess_source", fake_reprocess)
    monkeypatch.setattr(reprocess, "reprocess_source", fake_reprocess)
    return calls


def ingestion_app():
    from topos.api.ingestion_api import router

    app = FastAPI()
    app.include_router(router)
    return app


REPROCESS = {"source_id": "demo_messenger_file", "dataset_id": "ds-a", "run_enrichment": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [LEGACY_KEY, OWNER_KEY])
async def test_reprocess_http_refuses_tcp_bearers_once_the_owner_key_exists(keys, reprocess_spy, token):
    response = await post(ingestion_app(), "/ingestion/reprocess", token=token, json=REPROCESS)
    assert response.status_code == 403 and reprocess_spy == []


@pytest.mark.asyncio
async def test_reprocess_http_owner_socket_and_legacy_mode_still_run(keys, reprocess_spy, monkeypatch):
    assert (await post(ingestion_app(), "/ingestion/reprocess", uds=True, json=REPROCESS)).status_code == 200
    monkeypatch.setattr(keys, "topos_owner_key", None)
    assert (await post(ingestion_app(), "/ingestion/reprocess", token=LEGACY_KEY, json=REPROCESS)).status_code == 200
    assert len(reprocess_spy) == 2


@pytest.mark.asyncio
async def test_reprocess_relay_refuses_third_party_and_legacy_key_but_serves_owner(keys, cp_key, reprocess_spy):
    assert refused(await relay(stamped(cp_key, "ingestion_reprocess", REPROCESS, cls=THIRD_PARTY)))
    over_tcp = await post(dispatch_app(), "/dispatch", token=LEGACY_KEY,
                          json={"id": "tcp", "type": "ingestion_reprocess", "payload": REPROCESS})
    assert refused(over_tcp.json())
    assert reprocess_spy == []
    owner = await relay(stamped(cp_key, "ingestion_reprocess", REPROCESS, cls=OWNER_APP))
    assert owner["status"] == "ok" and len(reprocess_spy) == 1


@pytest.fixture
def owner_conversation(db):
    """One owner message already canonical under ds-owner, plus raw retention for it and a sibling."""
    from topos.storage.canonical import ConversationsTablesManager
    from topos.storage.raw.raw_tables_manager import RawTablesManager

    ConversationsTablesManager(db).upsert_message_batch(
        [{"message_id": "demo-m1", "dataset_id": "ds-owner", "thread_id": "t1", "ts": "2026-01-01T00:00:00Z",
          "sender_type": "human", "content": "synthetic owner body", "is_from_self": True,
          "owner_user_id": OWNER_ACTOR}],
        "ds-owner", "demo_messenger_file")
    raw = RawTablesManager(db)
    for message_id, content in (("demo-m1", "synthetic replacement body"), ("demo-m2", "synthetic sibling body")):
        raw.write_raw_record(source_id="demo_messenger_file", source_record_id=message_id, payload={
            "message_id": message_id, "conversation_id": "t1", "content": content,
            "event_at": "2026-01-01T00:00:01Z"})
    db.commit()
    return db


def conversation_rows(conn):
    return conn.execute("SELECT message_id, dataset_id, content, is_from_self, owner_user_id "
                        "FROM conversation_messages ORDER BY message_id").fetchall()


@pytest.mark.asyncio
async def test_reprocess_refuses_to_write_raw_rows_into_a_dataset_that_does_not_hold_them(owner_conversation):
    from topos.ingestion.reprocess import reprocess_source

    before = conversation_rows(owner_conversation)
    try:
        await reprocess_source(source_id="demo_messenger_file", dataset_id="ds-other", run_enrichment=False)
        raised = None
    except ValueError as exc:
        raised = exc
    assert conversation_rows(owner_conversation) == before
    assert raised is not None and "dataset" in str(raised)


@pytest.mark.asyncio
async def test_reprocess_under_the_rows_own_dataset_still_remaps(owner_conversation):
    from topos.ingestion.reprocess import reprocess_source

    result = await reprocess_source(source_id="demo_messenger_file", dataset_id="ds-owner", run_enrichment=False)
    assert result["raw_rows_loaded"] == 2
    rows = {row[0]: row for row in conversation_rows(owner_conversation)}
    assert set(rows) == {"demo-m1", "demo-m2"}
    assert {row[1] for row in rows.values()} == {"ds-owner"}
    # The pre-existing owner row keeps its ownership fields.
    assert rows["demo-m1"][3:] == (1, OWNER_ACTOR)


@pytest.mark.asyncio
async def test_reprocess_refuses_when_a_source_spans_datasets(owner_conversation):
    from topos.ingestion.reprocess import reprocess_source
    from topos.storage.canonical import ConversationsTablesManager

    ConversationsTablesManager(owner_conversation).upsert_message_batch(
        [{"message_id": "demo-x", "dataset_id": "ds-second", "thread_id": "t9", "ts": "2026-01-02T00:00:00Z",
          "sender_type": "human", "content": "synthetic second dataset body"}],
        "ds-second", "demo_messenger_file")
    before = conversation_rows(owner_conversation)
    try:
        await reprocess_source(source_id="demo_messenger_file", dataset_id="ds-owner", run_enrichment=False)
        raised = None
    except ValueError as exc:
        raised = exc
    assert conversation_rows(owner_conversation) == before
    assert raised is not None and "dataset" in str(raised)


def test_upgrade_reprocess_step_binds_the_rows_dataset_instead_of_default(owner_conversation):
    from topos.upgrades.runner import _exec_canonical_reprocess

    detail = _exec_canonical_reprocess({"id": "synthetic-step", "params": {
        "from_stage": "raw", "source_ids": ["demo_messenger_file"], "run_enrichment": False}}, owner_conversation)
    assert detail["sources"]["demo_messenger_file"] == "accepted"
    datasets = {row[0] for row in owner_conversation.execute("SELECT DISTINCT dataset_id FROM conversation_messages")}
    assert datasets == {"ds-owner"}


# ---- 5. signal upload -------------------------------------------------------------


@pytest.fixture
def signal_spy(monkeypatch):
    calls = []

    def fake_upload(dataset_id, file_bytes, **kwargs):
        calls.append({"dataset_id": dataset_id, **kwargs})
        return {"status": "ok", "records_processed": 0}

    import topos.api.ingestion_sources as routes
    import topos.ingestion.local_sync as local_sync

    monkeypatch.setattr(routes, "run_signal_upload", fake_upload)
    monkeypatch.setattr(local_sync, "run_signal_upload", fake_upload)
    return calls


SIGNAL_FILE = {"file": ("export.json", json.dumps([]).encode(), "application/json")}


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [LEGACY_KEY, OWNER_KEY])
async def test_signal_upload_http_refuses_tcp_bearers_and_their_owner_claim(keys, signal_spy, token):
    response = await post(sources_app(), "/sources/signal/upload", token=token, files=SIGNAL_FILE,
                          params={"dataset_id": "ds-a", "owner_user_id": "someone-else"})
    assert response.status_code == 403 and signal_spy == []


@pytest.mark.asyncio
async def test_signal_upload_http_owner_socket_and_legacy_mode_still_upload(keys, signal_spy, monkeypatch):
    response = await post(sources_app(), "/sources/signal/upload", uds=True, files=SIGNAL_FILE, params={"dataset_id": "ds-a"})
    assert response.status_code == 200 and response.json()["status"] == "ok"
    monkeypatch.setattr(keys, "topos_owner_key", None)
    legacy = await post(sources_app(), "/sources/signal/upload", token=LEGACY_KEY, files=SIGNAL_FILE, params={"dataset_id": "ds-a"})
    assert legacy.status_code == 200
    assert [call["dataset_id"] for call in signal_spy] == ["ds-a", "ds-a"]


@pytest.mark.asyncio
async def test_signal_upload_relay_is_owner_only_by_declaration_not_by_prefix(keys, cp_key, signal_spy):
    from topos.core.handlers.registry import OWNER_ONLY_MESSAGE_TYPES

    assert "signal_upload" in OWNER_ONLY_MESSAGE_TYPES
    payload = {"dataset_id": "ds-a", "file_base64": base64.b64encode(b"[]").decode(), "owner_user_id": "someone-else"}
    assert refused(await relay(stamped(cp_key, "signal_upload", payload, cls=THIRD_PARTY)))
    over_tcp = await post(dispatch_app(), "/dispatch", token=LEGACY_KEY, json={"id": "tcp", "type": "signal_upload", "payload": payload})
    assert refused(over_tcp.json())
    assert signal_spy == []
    owner = await relay(stamped(cp_key, "signal_upload", dict(payload, owner_user_id=None), cls=OWNER_APP))
    assert owner["status"] == "ok" and len(signal_spy) == 1


def test_every_ingestion_writer_type_is_declared_owner_only():
    from topos.core.handlers.registry import OWNER_ONLY_MESSAGE_TYPES

    writers = {"source_sync", "signal_upload", "ingestion_reprocess", "pooled_scope_backfill_apply",
               "pooled_scope_backfill_dry_run", "pooled_scope_backfill_rollback"}
    assert writers <= OWNER_ONLY_MESSAGE_TYPES
