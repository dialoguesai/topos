"""ChatGPT lane below the doors: wire contract, durable links, the evidence proof and the store guards.

Scratch canonical databases built by the node's own schema writers
(test_ingest_snapshot_work_canary._lane_database) and synthetic exports only.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import sqlite3
import time
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.ingestion.test_chatgpt_owner_snapshot import CONVERSATION, export, group_chat, owner_chat
from tests.permissions_v2.test_ingest_snapshot_work_canary import BINDING, OWNER_ID, _lane_database
from topos.ingestion.chatgpt_owner_snapshot import SOURCE_ID, run_chatgpt_snapshot_job, write_trusted_ai_chat_batch
from topos.ingestion.owner_snapshot import run_snapshot_job
from topos.permissions_v2 import ingest_protocol as protocol
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.evidence import EvidenceResolver
from topos.permissions_v2.ingest_provenance import IngestProvenanceService
from topos.principal import OWNER_APP, Principal, reset_principal, set_principal

CONVERSATION_ID = f"{SOURCE_ID}:{CONVERSATION}"
PROMPT_ID = f"{CONVERSATION_ID}:user-1"
REPLY_ID = f"{CONVERSATION_ID}:assistant-1"
DATASET = "dataset-chatgpt-lane"


@contextmanager
def owner():
    token = set_principal(Principal(cls=OWNER_APP, channel="uds", acting_user=OWNER_ID))
    try:
        yield
    finally:
        reset_principal(token)


# --- the wire ----------------------------------------------------------------

CHATGPT_ENROLL = {"operation": "enroll", "source_id": SOURCE_ID, "snapshot_id": "export-a", "snapshot_sha256": "1" * 64,
                  "dataset_id": "dataset-a", "owner_attestation": protocol.CHATGPT_OWNER_ATTESTATION,
                  "reader_contract": protocol.CHATGPT_READER_CONTRACT}


def test_the_imessage_default_never_travels_and_the_chatgpt_contract_always_does():
    for model, raw in ((protocol.DescribeSnapshot, {"snapshot_id": "snapshot-a"}),
                       (protocol.EnrollSnapshot, {"snapshot_id": "snapshot-a", "snapshot_sha256": "1" * 64,
                                                  "dataset_id": "dataset-a", "owner_attestation": protocol.OWNER_ATTESTATION})):
        dumped = model.parse(raw).model_dump()
        assert "reader_contract" not in dumped and dumped["source_id"] == "imessage"
        # An explicit default is refused, not normalized: both ends sign model_dump().
        with pytest.raises(PolicyError, match="schema_invalid"):
            model.parse({**raw, "reader_contract": protocol.IMESSAGE_READER_CONTRACT})
    assert protocol.EnrollSnapshot.parse(CHATGPT_ENROLL).model_dump() == CHATGPT_ENROLL
    described = protocol.DescribeSnapshot.parse({"source_id": SOURCE_ID, "snapshot_id": "export-a",
                                                 "reader_contract": protocol.CHATGPT_READER_CONTRACT}).model_dump()
    assert described["reader_contract"] == protocol.CHATGPT_READER_CONTRACT


@pytest.mark.parametrize("change", [
    {"source_id": "imessage"}, {"reader_contract": None}, {"owner_attestation": protocol.OWNER_ATTESTATION},
    {"reader_contract": "chatgpt-owner-snapshot/v2"}, {"source_id": "chatgpt"},
])
def test_a_reader_contract_source_and_attestation_must_name_one_lane(change):
    raw = {key: value for key, value in {**CHATGPT_ENROLL, **change}.items() if value is not None}
    with pytest.raises(PolicyError, match="schema_invalid"):
        protocol.EnrollSnapshot.parse(raw)


def test_a_signed_chatgpt_command_verifies_and_its_ack_must_name_the_same_lane():
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    identity = {"environment_id": "permissions-beta-test", "node_id": "node-a", "resource_id": "resource-a", "owner_id": "owner-a"}
    now = int(time.time())
    command = protocol.sign_ingest_command(protocol.IngestCommandBody.parse({
        "version": "topos-owner-ingest-command/v2", "kid": "k", "issuer_id": "cp-a", "audience_id": "node-a",
        "command_id": "command-a", "binding": identity,
        "owner_authorization": {"actor_id": "owner-a", "client_id": "permissions-beta-web"},
        "request": CHATGPT_ENROLL, "issued_at": now, "expires_at": now + 60}), key)
    keys = {"k": key.public_key().public_bytes_raw()}
    verified = protocol.verify_ingest_command(command.model_dump(), trusted_keys=keys, issuer_id="cp-a",
        identity=protocol.NodeIdentity.parse(identity), frontend_client_id="permissions-beta-web", now=now)
    assert verified.request.reader_contract == protocol.CHATGPT_READER_CONTRACT

    def ack(source_id):
        return protocol.sign_ingest_ack(protocol.IngestAckBody.parse({
            "version": "topos-owner-ingest-ack/v2", "kid": "k", "issuer_id": "node-a", "audience_id": "cp-a",
            "command_id": "command-a", "response_to": digest(command.model_dump()), "binding": identity,
            "operation": "enroll", "result": {"enrollment_id": "enrollment-a", "dataset_id": "dataset-a",
                "source_id": source_id, "revision": 1, "state": "active", "ownership_basis": "owner_attested_snapshot"},
            "error_code": None, "issued_at": now, "expires_at": now + 60}), key).model_dump()

    assert protocol.verify_ingest_ack(ack(SOURCE_ID), trusted_keys=keys, issuer_id="node-a", audience_id="cp-a",
                                      request=command, now=now).result.source_id == SOURCE_ID
    with pytest.raises(PolicyError, match="ingest_ack_target"):
        protocol.verify_ingest_ack(ack("imessage"), trusted_keys=keys, issuer_id="node-a", audience_id="cp-a",
                                   request=command, now=now)


# --- the durable lane --------------------------------------------------------

@pytest.fixture
def chatgpt_lane(tmp_path):
    path = tmp_path / "canonical.db"
    path.touch(mode=0o600)
    path = path.resolve(strict=True)
    _lane_database(path)
    root = path.parent / "permissions-v2" / "ingest-snapshots"
    root.parent.mkdir(mode=0o700)
    root.mkdir(mode=0o700)
    snapshot = root / "export-a.json"
    snapshot.write_bytes(export(owner_chat(), group_chat()))
    snapshot.chmod(0o400)
    service = IngestProvenanceService(canonical_database=path, binding=BINDING, snapshot_root=root)

    def connect():
        conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    with owner(), connect() as conn:
        described = service.describe_snapshot(conn, snapshot_id="export-a", reader_contract=protocol.CHATGPT_READER_CONTRACT)
        enrollment = service.enroll(conn, snapshot_id="export-a", dataset_id=DATASET,
                                    snapshot_sha256=described["snapshot_sha256"],
                                    owner_attestation=protocol.CHATGPT_OWNER_ATTESTATION,
                                    reader_contract=protocol.CHATGPT_READER_CONTRACT)
        job = service.enqueue(conn, enrollment_id=enrollment["enrollment_id"], source_id=SOURCE_ID)
    return SimpleNamespace(path=path, root=root, snapshot=snapshot, service=service, connect=connect,
                           described=described, enrollment=enrollment, job=job)


def count(lane, table):
    with lane.connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def origin(lane, message_id):
    with lane.connect() as conn:
        return json.loads(conn.execute("SELECT metadata_json FROM ai_chat_messages WHERE message_id=?",
                                       (message_id,)).fetchone()[0])["topos_owner_ingest"]


def test_enrollment_pins_the_json_export_and_its_own_attestation(chatgpt_lane):
    lane = chatgpt_lane
    assert lane.described == {"snapshot_id": "export-a", "snapshot_sha256": hashlib.sha256(lane.snapshot.read_bytes()).hexdigest(),
        "snapshot_bytes": lane.snapshot.stat().st_size, "reader_contract": protocol.CHATGPT_READER_CONTRACT,
        "ownership_basis": "owner_attested_snapshot"}
    assert (lane.enrollment["source_id"], lane.enrollment["state"]) == (SOURCE_ID, "active")
    with owner(), lane.connect() as conn:
        # The iMessage reader looks for export-a.db, never the owner's JSON.
        with pytest.raises(PolicyError, match="ingest_snapshot_invalid|ingest_snapshot_unavailable"):
            lane.service.describe_snapshot(conn, snapshot_id="export-a")
        for attestation in (protocol.OWNER_ATTESTATION, "yes"):
            with pytest.raises(PolicyError, match="ingest_owner_attestation_required"):
                lane.service.enroll(conn, snapshot_id="export-a", dataset_id="dataset-other",
                                    snapshot_sha256=lane.described["snapshot_sha256"], owner_attestation=attestation,
                                    reader_contract=protocol.CHATGPT_READER_CONTRACT)
        with pytest.raises(PolicyError, match="ingest_reader_unsupported"):
            lane.service.describe_snapshot(conn, snapshot_id="export-a", reader_contract="chatgpt-owner-snapshot/v2")
        with pytest.raises(PolicyError, match="ingest_enrollment_unknown"):
            lane.service.revoke(conn, enrollment_id=lane.enrollment["enrollment_id"])  # the iMessage lane's revoke


@pytest.mark.asyncio
async def test_a_run_links_every_row_and_only_the_ai_chat_table(chatgpt_lane):
    lane = chatgpt_lane
    result = await run_chatgpt_snapshot_job(lane.service, lane.connect, lane.job["job_id"])
    assert result == {"status": "ok", "messages_processed": 4, "messages_created": 4, "conversations_created": 1,
                      "historical_skipped": 0}
    assert (count(lane, "ai_chat_messages"), count(lane, "ingest_provenance_records"), count(lane, "conversation_messages")) == (4, 4, 0)
    with lane.connect() as conn:
        for message_id in (PROMPT_ID, REPLY_ID):
            lane.service.validate_record_origin(conn, message_id=message_id, origin=origin(lane, message_id),
                                                table="ai_chat_messages")
            with pytest.raises(PolicyError, match="ingest_origin_invalid"):
                lane.service.validate_record_origin(conn, message_id=message_id, origin=origin(lane, message_id))


@pytest.mark.asyncio
@pytest.mark.parametrize("statement", [
    "UPDATE ai_chat_messages SET content='I prefer Fabrikam espresso.' WHERE message_id=:id",
    "UPDATE ai_chat_messages SET sender_type='assistant' WHERE message_id=:id",
    "UPDATE ai_chat_messages SET actor_role=NULL WHERE message_id=:id",
    "UPDATE ai_chat_messages SET conversation_id='chatgpt:other' WHERE message_id=:id",
    "UPDATE ai_chat_messages SET source_id='chatgpt_ui_conversation' WHERE message_id=:id",
    "UPDATE ai_chat_messages SET sequence=9 WHERE message_id=:id",
    "UPDATE ai_chat_conversations SET owner_user_id='other-owner'",
    "UPDATE ai_chat_conversations SET source_id='chatgpt_ui_conversation'",
    "DELETE FROM ingest_provenance_records WHERE message_id=:id",
])
async def test_the_link_pins_content_role_conversation_and_the_parent_owner(chatgpt_lane, statement):
    lane = chatgpt_lane
    assert (await run_chatgpt_snapshot_job(lane.service, lane.connect, lane.job["job_id"]))["status"] == "ok"
    marker = origin(lane, PROMPT_ID)
    with lane.connect() as conn:
        lane.service.validate_record_origin(conn, message_id=PROMPT_ID, origin=marker, table="ai_chat_messages")
        conn.execute(statement, {"id": PROMPT_ID})
    with lane.connect() as conn, pytest.raises(PolicyError):
        lane.service.validate_record_origin(conn, message_id=PROMPT_ID, origin=marker, table="ai_chat_messages")


@pytest.mark.asyncio
async def test_revocation_withholds_existing_links(chatgpt_lane):
    lane = chatgpt_lane
    assert (await run_chatgpt_snapshot_job(lane.service, lane.connect, lane.job["job_id"]))["status"] == "ok"
    with owner(), lane.connect() as conn:
        assert lane.service.revoke(conn, enrollment_id=lane.enrollment["enrollment_id"], source_id=SOURCE_ID)["state"] == "revoked"
    with lane.connect() as conn, pytest.raises(PolicyError, match="ingest_enrollment_stale"):
        lane.service.validate_record_origin(conn, message_id=PROMPT_ID, origin=origin(lane, PROMPT_ID), table="ai_chat_messages")


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [
    ("ai_chat_messages", {"message_id": PROMPT_ID, "conversation_id": "chatgpt:elsewhere", "sender_type": "human",
                          "event_at": "2025-01-01T00:00:00+00:00", "content": "planted", "source_id": "chatgpt_ui_conversation"}),
    ("ai_chat_conversations", {"conversation_id": CONVERSATION_ID, "owner_user_id": OWNER_ID, "source_id": SOURCE_ID,
                               "created_at": "2025-01-01T00:00:00+00:00", "updated_at": "2025-01-01T00:00:00+00:00"}),
])
async def test_any_existing_row_with_a_lane_id_refuses_the_whole_batch(chatgpt_lane, existing):
    lane = chatgpt_lane
    table, row = existing
    with lane.connect() as conn:
        conn.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' for _ in row)})", list(row.values()))
    result = await run_chatgpt_snapshot_job(lane.service, lane.connect, lane.job["job_id"])
    assert result == {"status": "error", "reason_code": "snapshot_job_unavailable"}
    assert (count(lane, "ai_chat_messages"), count(lane, "ingest_provenance_records")) == (int(table == "ai_chat_messages"), 0)
    with owner(), lane.connect() as conn:
        assert lane.service.status(conn, job_id=lane.job["job_id"], source_id=SOURCE_ID)["status"] == "failed"


@pytest.mark.asyncio
async def test_a_failure_at_completion_rolls_back_rows_links_and_conversations(chatgpt_lane, monkeypatch):
    lane = chatgpt_lane
    inside = []

    def failing_finish(self, conn, context, result):
        inside.append((conn.execute("SELECT COUNT(*) FROM ai_chat_messages").fetchone()[0],
                       conn.execute("SELECT COUNT(*) FROM ingest_provenance_records").fetchone()[0]))
        raise RuntimeError("SYNTHETIC_FAILURE")

    monkeypatch.setattr(IngestProvenanceService, "finish", failing_finish)
    result = await run_chatgpt_snapshot_job(lane.service, lane.connect, lane.job["job_id"])
    assert inside == [(4, 4)] and result == {"status": "error", "reason_code": "snapshot_job_unavailable"}
    assert (count(lane, "ai_chat_messages"), count(lane, "ai_chat_conversations"), count(lane, "ingest_provenance_records")) == (0, 0, 0)


@pytest.mark.asyncio
async def test_the_imessage_runner_cannot_claim_a_chatgpt_job(chatgpt_lane):
    lane = chatgpt_lane
    assert (await run_snapshot_job(lane.service, lane.connect, lane.job["job_id"]))["status"] == "error"
    with owner(), lane.connect() as conn:
        assert lane.service.status(conn, job_id=lane.job["job_id"], source_id=SOURCE_ID)["status"] == "queued"
        with pytest.raises(PolicyError, match="ingest_job_unknown"):
            lane.service.status(conn, job_id=lane.job["job_id"])
    assert count(lane, "ai_chat_messages") == 0


def test_the_trusted_writer_needs_the_live_claimed_batch(chatgpt_lane):
    lane = chatgpt_lane
    parsed = {"conversations": [], "messages": []}
    with lane.connect() as conn:
        with pytest.raises(PolicyError, match="ingest_canonical_context_required"):
            write_trusted_ai_chat_batch(conn, parsed, trusted_context=SimpleNamespace(**vars(lane)))
        context = lane.service.claim(conn, lane.job["job_id"], source_id=SOURCE_ID)
        with pytest.raises(PolicyError, match="ingest_transaction_required"):
            write_trusted_ai_chat_batch(conn, parsed, trusted_context=context)


# --- the evidence proof ------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("change,proven", [
    ({}, True),
    ({"sender_type": "assistant"}, False),
    ({"sender_type": "user"}, True),
    ({"source_id": "chatgpt_ui_conversation"}, False),
    ({"identity_source": "chatgpt_ui_conversation"}, False),
    ({"conversation_id": "chatgpt:elsewhere"}, False),
])
async def test_the_ai_chat_proof_reads_role_source_and_parent_as_well_as_the_link(chatgpt_lane, change, proven):
    """Each clause alone: the real row's link is intact, only the in-memory row or identity differs."""
    lane = chatgpt_lane
    assert (await run_chatgpt_snapshot_job(lane.service, lane.connect, lane.job["job_id"]))["status"] == "ok"
    resolver = EvidenceResolver(lane.path, binding=BINDING)
    with resolver._read() as (conn, _floor):
        row = dict(conn.execute("SELECT * FROM ai_chat_messages WHERE message_id=?", (PROMPT_ID,)).fetchone())
        identity = resolver._identity("ai_chat_messages", PROMPT_ID, change.pop("identity_source", SOURCE_ID))
        assert resolver._ai_chat_owner_proven(conn, identity, {**row, **change}) is proven


# --- the store guards --------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    from topos.storage.canonical.canonical_store import SQLiteCanonicalStore
    path = tmp_path / "store.db"
    path.touch(mode=0o600)
    _lane_database(path)
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ingest_provenance_records (message_id TEXT PRIMARY KEY, enrollment_id TEXT NOT NULL, "
                 "enrollment_revision INTEGER NOT NULL, job_id TEXT NOT NULL, row_identity TEXT NOT NULL)")
    conn.execute("INSERT INTO ai_chat_conversations(conversation_id,owner_user_id,source_id,created_at,updated_at) "
                 "VALUES ('conversation-1', ?, 'chatgpt_ui_conversation', 'c', 'u')", (OWNER_ID,))
    for message_id in ("linked-1", "legacy-1"):
        conn.execute("INSERT INTO ai_chat_messages(message_id,conversation_id,sender_type,event_at,content,metadata_json,"
                     "source_id) VALUES (?, 'conversation-1', 'human', '2025-09-04T00:00:00+00:00', 'original', "
                     "'{\"topos_owner_ingest\": {}}', ?)", (message_id, SOURCE_ID))
    conn.execute("INSERT INTO ingest_provenance_records VALUES ('linked-1','enrollment-1',1,'job-1','identity')")
    conn.commit()
    yield SimpleNamespace(conn=conn, store=SQLiteCanonicalStore(conn))
    conn.close()


def rewrite(store, message_id):
    return store.store.upsert("ai_chat_messages", {
        "message_id": message_id, "conversation_id": "conversation-2", "sender_type": "assistant",
        "event_at": "2025-09-05T00:00:00+00:00", "content": "rewritten", "metadata_json": {"injected": True},
        "source_id": "chatgpt_ui_conversation"}, sync_batch_id="resync-1")


def message(store, message_id):
    return store.conn.execute("SELECT content,sender_type,conversation_id,source_id,metadata_json,sync_batch_id "
                              "FROM ai_chat_messages WHERE message_id=?", (message_id,)).fetchone()


def test_a_linked_ai_chat_row_keeps_its_body_role_conversation_source_and_marker(store):
    ref = rewrite(store, "linked-1")
    assert ref.created is False
    assert message(store, "linked-1") == ("original", "human", "conversation-1", SOURCE_ID, '{"topos_owner_ingest": {}}', "resync-1")


def test_an_unlinked_legacy_row_upserts_exactly_as_before(store):
    rewrite(store, "legacy-1")
    # Unchanged legacy semantics: body, metadata and source replace; role and conversation never did.
    assert message(store, "legacy-1") == ("rewritten", "human", "conversation-1", "chatgpt_ui_conversation",
                                          '{"injected": true}', "resync-1")


def test_a_conversation_owner_is_never_rebound_and_a_same_owner_write_still_updates(store):
    def upsert(owner_user_id, updated_at):
        store.store.upsert("ai_chat_conversations", {"conversation_id": "conversation-1", "owner_user_id": owner_user_id,
            "source_id": "chatgpt_ui_conversation", "created_at": "c", "updated_at": updated_at}, sync_batch_id=updated_at)

    def stored():
        return store.conn.execute("SELECT owner_user_id,updated_at,sync_batch_id FROM ai_chat_conversations").fetchone()

    upsert("other-owner", "u-other")
    assert stored() == (OWNER_ID, "u", None)
    upsert(OWNER_ID, "u-same")
    assert stored() == (OWNER_ID, "u-same", "u-same")
    upsert("", "u-unnamed")
    assert stored() == (OWNER_ID, "u-unnamed", "u-unnamed")
