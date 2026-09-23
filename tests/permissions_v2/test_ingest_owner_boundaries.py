"""Actual HTTP principal resolution and signed relay dispatch, scratch DB only."""
import base64
import asyncio
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import threading
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI

from topos.api.permissions_ingestion import router
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.ingest_protocol import (
    IngestCommandBody, OWNER_ATTESTATION, sign_ingest_command, verify_ingest_ack,
)
from topos.permissions_v2 import ingest_protocol
from topos.permissions_v2.ledger import NodeIdentity
from topos.principal import OWNER_APP, RELAY_PRINCIPAL, current_principal
from topos.uds import UDSChannelApp
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401
from tests.ingestion.test_owner_snapshot import enrolled_snapshot  # noqa: F401


class ScratchService:
    def __init__(self, owner):
        self.owner = owner
        self.calls = []

    def consume_command(self, conn, *, command_id, command_hash, allow_install=False):
        if allow_install:
            conn.execute("CREATE TABLE IF NOT EXISTS consumed (id TEXT PRIMARY KEY, hash TEXT)")
        try:
            conn.execute("INSERT INTO consumed VALUES (?,?)", (command_id, command_hash))
            conn.commit()
        except sqlite3.IntegrityError:
            raise PolicyError("ingest_command_replayed") from None

    def enroll(self, conn, **request):
        principal = current_principal()
        assert principal.cls == OWNER_APP and principal.acting_user == self.owner
        assert conn.execute("SELECT value FROM engine_config WHERE key='user_id'").fetchone()[0] == self.owner
        self.calls.append((principal, request))
        return {"enrollment_id": "enrollment-1", "dataset_id": request["dataset_id"], "source_id": "imessage",
                "revision": 1, "state": "active", "ownership_basis": "owner_attested_snapshot"}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from topos.permissions_v2 import runtime as module
    from topos.config.settings import settings
    tmp_path = tmp_path / "boundary"
    tmp_path.mkdir()
    cp_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    node_key = Ed25519PrivateKey.from_private_bytes(bytes(range(32, 64)))
    identity = NodeIdentity(environment_id="permissions-beta-test", node_id="node-a", resource_id="resource-a", owner_id="owner-a")
    canonical = tmp_path / "canonical.db"
    with sqlite3.connect(canonical) as conn:
        conn.execute("CREATE TABLE engine_config(key TEXT PRIMARY KEY,value TEXT)")
        conn.execute("INSERT INTO engine_config VALUES('user_id','owner-a')")
    root = tmp_path / "permissions-v2" / "ingest-snapshots"
    root.mkdir(parents=True, mode=0o700)
    protocol = SimpleNamespace(ledger=SimpleNamespace(identity=identity), canonical_database=canonical,
        cp_issuer_id="cp-a", frontend_client_id="permissions-beta-web", trusted_cp_keys={"cp-key": cp_key.public_key().public_bytes_raw()},
        node_signing_kid="node-key", node_signing_key=node_key)
    runtime = module.Runtime(protocol, None, tmp_path / "config.json")
    service = ScratchService(identity.owner_id)
    runtime._ingestion_service = service
    monkeypatch.setattr(module, "get_runtime", lambda: runtime)
    for key, value in {"TOPOS_PERMISSIONS_V2_ENABLED": "true", "TOPOS_PERMISSIONS_V2_INGEST_SNAPSHOTS_ENABLED": "true",
                       "TOPOS_PERMISSIONS_V2_INGEST_SNAPSHOT_ROOT": str(root),
                       "TOPOS_CP_STAMP_PUBKEY": base64.b64encode(cp_key.public_key().public_bytes_raw()).decode()}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(settings, "topos_key", "synthetic-engine-key")
    monkeypatch.setattr(settings, "topos_owner_key", "synthetic-owner-key")
    # Pinned key loader has no global cache; this is a deterministic local pin.
    app = FastAPI()
    app.include_router(router)
    now = int(time.time())
    request = {"operation": "enroll", "source_id": "imessage", "snapshot_id": "snapshot-a",
        "snapshot_sha256": "1" * 64, "dataset_id": "dataset-unrelated-to-owner", "owner_attestation": OWNER_ATTESTATION}
    command = sign_ingest_command(IngestCommandBody.parse({"version": "topos-owner-ingest-command/v2", "kid": "cp-key",
        "issuer_id": "cp-a", "audience_id": identity.node_id, "command_id": "command-a", "binding": identity.model_dump(),
        "owner_authorization": {"actor_id": identity.owner_id, "client_id": "permissions-beta-web"},
        "request": request, "issued_at": now, "expires_at": now + 120}), cp_key)
    return SimpleNamespace(app=app, command=command, runtime=runtime, service=service, cp_key=cp_key, node_key=node_key)


async def http_call(setup, *, uds=False, token="synthetic-owner-key", raw=None):
    app = UDSChannelApp(setup.app) if uds else setup.app
    headers = {"X-Topos-Client": "topos-home-chat/1", "X-Topos-Transport": "uds"}
    if token:
        headers["Authorization"] = "Bearer " + token
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        return await client.post("/v1/permissions-beta/v2/ingestion/command", headers=headers,
                                 json={"envelope": raw or setup.command.model_dump()})


def checked_ack(setup, raw):
    return verify_ingest_ack(raw, trusted_keys={"node-key": setup.node_key.public_key().public_bytes_raw()},
        issuer_id="node-a", audience_id="cp-a", request=setup.command, now=int(time.time()))


@pytest.mark.asyncio
async def test_actual_uds_http_binds_empty_local_principal_to_signed_persisted_owner(setup):
    response = await http_call(setup, uds=True, token=None)
    assert response.status_code == 200
    ack = checked_ack(setup, response.json()["ack"])
    assert ack.error_code is None and ack.result.dataset_id == "dataset-unrelated-to-owner"
    assert setup.service.calls[0][0].channel == "uds"
    assert current_principal() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["synthetic-owner-key", "synthetic-engine-key", "garbage", None])
async def test_tcp_headers_and_keys_cannot_mint_owner_even_with_valid_signed_command(setup, token):
    response = await http_call(setup, token=token)
    assert response.status_code in {401, 403}
    assert setup.service.calls == []


@pytest.mark.asyncio
async def test_legacy_owner_key_unconfigured_still_cannot_mint_owner(setup, monkeypatch):
    from topos.config.settings import settings
    monkeypatch.setattr(settings, "topos_owner_key", None)
    response = await http_call(setup, token="synthetic-engine-key")
    assert response.status_code == 403 and not setup.service.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("dataset_id", "other"), ("source_id", "signal"),
    ("snapshot_id", "other"), ("snapshot_sha256", "2" * 64), ("owner_user_id", "owner-a"),
    ("chat_db_path", "/private/elsewhere.db"), ("owner_attestation", "yes")])
async def test_signed_request_payload_cannot_be_retargeted_or_extended(setup, field, value):
    raw = setup.command.model_dump()
    raw["request"][field] = value
    response = await http_call(setup, uds=True, raw=raw)
    assert response.status_code == 403 and not setup.service.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "client", "node", "environment", "resource", "expired", "future", "kid"])
async def test_even_correct_signature_requires_current_exact_pairing(setup, change):
    raw = setup.command.model_dump(exclude={"signature"})
    if change == "owner":
        raw["binding"]["owner_id"] = raw["owner_authorization"]["actor_id"] = "other-owner"
    elif change == "client":
        raw["owner_authorization"]["client_id"] = "claude"
    elif change in {"node", "environment", "resource"}:
        raw["binding"][change + "_id"] = "other"
        if change == "node":
            raw["audience_id"] = "other"
    elif change == "expired":
        raw["issued_at"] -= 121
        raw["expires_at"] -= 121
    elif change == "future":
        raw["issued_at"] += 30
        raw["expires_at"] += 30
    else:
        raw["kid"] = "unknown"
    signed = sign_ingest_command(IngestCommandBody.parse(raw), setup.cp_key)
    response = await http_call(setup, uds=True, raw=signed.model_dump())
    assert response.status_code == 403 and not setup.service.calls


@pytest.mark.asyncio
async def test_mutation_replay_burn_is_durable_before_second_dispatch(setup):
    assert (await http_call(setup, uds=True)).status_code == 200
    response = await http_call(setup, uds=True)
    assert checked_ack(setup, response.json()["ack"]).error_code == "ingest_command_replayed"
    assert len(setup.service.calls) == 1


async def relay_call(setup, *, cls="owner_app", actor="owner-a", client="permissions-beta-web", stamp=True, mutate=None):
    from topos.core.handlers import handle_control_plane_request
    from topos.relay_stamp import canonical_signing_payload, verify_relay_stamp
    message = {"id": "relay-a", "type": "permissions_v2_ingest_snapshot", "payload": {"envelope": setup.command.model_dump()}}
    now = time.time()
    if stamp:
        proof = {"v": 1, "cls": cls, "client_id": client, "acting_user": actor, "iat": now, "exp": now + 120}
        proof["sig"] = base64.b64encode(setup.cp_key.sign(canonical_signing_payload(proof, msg_id=message["id"], msg_type=message["type"]))).decode()
        message["principal_stamp"] = proof
    if mutate:
        mutate(message)
    # This is the exact verified-principal handoff in app._relay_dispatch.
    return await handle_control_plane_request(message, principal=verify_relay_stamp(message) or RELAY_PRINCIPAL)


@pytest.mark.asyncio
async def test_actual_signature_to_registry_relay_positive(setup):
    response = await relay_call(setup)
    assert response["status"] == "ok" and checked_ack(setup, response["payload"]["ack"]).error_code is None
    assert setup.service.calls[0][0].channel == "cp_relay"


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{"stamp": False}, {"cls": "third_party"}, {"actor": "wrong-owner"},
    {"client": "claude"}, {"client": "topos_home_chat"}, {"mutate": lambda msg: msg["payload"]["envelope"]["request"].update(dataset_id="other")}])
async def test_unstamped_thirdparty_wrong_owner_or_tampered_relay_denied(setup, args):
    response = await relay_call(setup, **args)
    assert response["status"] == "error" and not setup.service.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("variable,value", [("TOPOS_PERMISSIONS_V2_INGEST_SNAPSHOTS_ENABLED", "false"),
    ("TOPOS_PERMISSIONS_V2_ENABLED", "false"), ("TOPOS_PERMISSIONS_V2_INGEST_SNAPSHOT_ROOT", "/tmp/other")])
async def test_runtime_disabled_or_retargeted_never_dispatches(setup, monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    response = await http_call(setup, uds=True)
    assert response.status_code in {200, 403, 404} and not setup.service.calls
    if response.status_code == 200:
        assert checked_ack(setup, response.json()["ack"]).error_code is not None


@pytest.mark.asyncio
async def test_waiting_for_writer_gate_cannot_extend_signed_command_lifetime(setup, monkeypatch):
    from topos.permissions_v2 import ingest_dispatch
    from topos.storage.db import write_gate
    original_gate = write_gate.with_db_write
    clock = [setup.command.issued_at]
    monkeypatch.setattr(ingest_dispatch.time, "time", lambda: clock[0])
    held, release, waiting = threading.Event(), threading.Event(), threading.Event()
    def blocker():
        with original_gate():
            held.set()
            release.wait(5)
    @contextmanager
    def observed_gate():
        waiting.set()
        with original_gate():
            yield
    worker = threading.Thread(target=blocker)
    worker.start()
    assert held.wait(2)
    monkeypatch.setattr(write_gate, "with_db_write", observed_gate)
    try:
        task = asyncio.create_task(http_call(setup, uds=True))
        assert await asyncio.to_thread(waiting.wait, 2)
        clock[0] = setup.command.expires_at
        release.set()
        response = await asyncio.wait_for(task, 3)
        assert checked_ack(setup, response.json()["ack"]).error_code == "envelope_time"
        assert not setup.service.calls
    finally:
        release.set()
        worker.join(2)


def use_real_service(setup, service, path, monkeypatch):
    setup.runtime.protocol.canonical_database = path
    setup.runtime.protocol.ledger.identity = NodeIdentity.parse(service.binding.model_dump())
    setup.runtime._ingestion_service = service
    setup.runtime._ingestion_snapshot_root = None
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_INGEST_SNAPSHOT_ROOT", str(service.root))


def next_command(setup, request, serial):
    raw = setup.command.model_dump(exclude={"signature"})
    raw.update(command_id=f"command-{serial}", binding=setup.runtime.protocol.ledger.identity.model_dump(),
               audience_id=setup.runtime.protocol.ledger.identity.node_id, request=request)
    raw["owner_authorization"]["actor_id"] = raw["binding"]["owner_id"]
    setup.command = sign_ingest_command(IngestCommandBody.parse(raw), setup.cp_key)


def real_ack(setup, response):
    assert response.status_code == 200, response.text
    return verify_ingest_ack(response.json()["ack"], trusted_keys={"node-key": setup.node_key.public_key().public_bytes_raw()},
        issuer_id=setup.runtime.protocol.ledger.identity.node_id, audience_id="cp-a", request=setup.command, now=int(time.time()))


@pytest.mark.asyncio
async def test_actual_http_service_enroll_queue_restart_status_revoke_and_replay(setup, ingest_fixture, monkeypatch):
    from topos.permissions_v2.ingest_provenance import IngestProvenanceService
    service, conn, snapshot = ingest_fixture
    use_real_service(setup, service, snapshot.parent.parent.parent / "canonical.db", monkeypatch)
    next_command(setup, {"operation": "enroll", "snapshot_id": "canary", "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "dataset_id": "not-an-owner-label", "owner_attestation": OWNER_ATTESTATION}, 1)
    enrollment = real_ack(setup, await http_call(setup, uds=True)).result
    assert enrollment.state == "active" and enrollment.dataset_id == "not-an-owner-label"
    assert real_ack(setup, await http_call(setup, uds=True)).error_code == "ingest_command_replayed"
    next_command(setup, {"operation": "enqueue", "enrollment_id": enrollment.enrollment_id}, 2)
    job = real_ack(setup, await http_call(setup, uds=True)).result
    assert job.status == "queued"
    # Reopen the durable service; no in-memory proof substitutes for its ledger.
    setup.runtime._ingestion_service = IngestProvenanceService(canonical_database=setup.runtime.protocol.canonical_database,
        binding=service.binding, snapshot_root=service.root)
    next_command(setup, {"operation": "status", "job_id": job.job_id}, 3)
    assert real_ack(setup, await http_call(setup, uds=True)).result == job
    next_command(setup, {"operation": "revoke", "enrollment_id": enrollment.enrollment_id}, 4)
    assert real_ack(setup, await http_call(setup, uds=True)).result.state == "revoked"
    next_command(setup, {"operation": "enqueue", "enrollment_id": enrollment.enrollment_id}, 5)
    assert real_ack(setup, await http_call(setup, uds=True)).error_code == "ingest_enrollment_stale"
    assert conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_actual_signed_http_run_writes_only_native_snapshot_and_returns_metadata(setup, enrolled_snapshot, monkeypatch):
    service, job_id, _factory, path, snapshot = enrolled_snapshot
    use_real_service(setup, service, path, monkeypatch)
    next_command(setup, {"operation": "run", "job_id": job_id}, "run")
    before = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    ack = real_ack(setup, await http_call(setup, uds=True))
    assert ack.error_code is None and ack.result.status == "ok"
    assert ack.result.messages_created == 2 and ack.result.messages_processed == 2
    assert "Synthetic message" not in str(ack.model_dump())
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ingest_provenance_records").fetchone()[0] == 2
        assert conn.execute("SELECT DISTINCT owner_user_id FROM conversation_messages").fetchall() == [("owner-synthetic",)]
        assert conn.execute("SELECT status FROM ingest_provenance_jobs WHERE job_id=?", (job_id,)).fetchone()[0] == "done"
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == before
    assert real_ack(setup, await http_call(setup, uds=True)).error_code == "ingest_command_replayed"


def test_portable_ingest_schema_and_golden_are_current():
    directory = Path(__file__).resolve().parents[2] / "fixtures/permissions_v2/ingestion"
    for model in (ingest_protocol.SignedIngestCommand, ingest_protocol.SignedIngestAck):
        assert model.model_json_schema() == json.loads((directory / (model.__name__ + ".schema.json")).read_text())
    fixture = json.loads((directory / "golden-v1.json").read_text())
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(fixture["seed_hex"]))
    command = ingest_protocol.SignedIngestCommand.parse(fixture["command"])
    assert sign_ingest_command(IngestCommandBody.parse(command.model_dump(exclude={"signature"})), key) == command
    assert ingest_protocol.verify_ingest_ack(fixture["ack"], trusted_keys={"test-key": key.public_key().public_bytes_raw()},
        issuer_id="node-a", audience_id="cp-a", request=command, now=1001).result.state == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["revoke", "enqueue"])
async def test_inplace_canonical_rollback_cannot_restore_authority_or_unburn_command(setup, ingest_fixture, monkeypatch, mutation):
    from topos.permissions_v2.ingest_provenance import IngestProvenanceService
    service, conn, snapshot = ingest_fixture
    path = snapshot.parent.parent.parent / "canonical.db"
    use_real_service(setup, service, path, monkeypatch)
    next_command(setup, {"operation": "enroll", "snapshot_id": "canary", "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "dataset_id": "rollback-canary", "owner_attestation": OWNER_ATTESTATION}, 1)
    enrollment = real_ack(setup, await http_call(setup, uds=True)).result
    assert not conn.in_transaction and conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    before = path.read_bytes()
    inode = path.stat().st_ino
    next_command(setup, {"operation": mutation, "enrollment_id": enrollment.enrollment_id}, 2)
    assert real_ack(setup, await http_call(setup, uds=True)).error_code is None
    # This scratch-only restore intentionally preserves inode to exercise the
    # external authority marker, not merely the existing file-replacement pin.
    path.write_bytes(before)
    assert path.stat().st_ino == inode
    setup.runtime._ingestion_service = IngestProvenanceService(canonical_database=path, binding=service.binding, snapshot_root=service.root)
    if mutation == "revoke":
        next_command(setup, {"operation": "enqueue", "enrollment_id": enrollment.enrollment_id}, 3)
    # The enqueue variant retries the exact previously consumed signed command.
    assert real_ack(setup, await http_call(setup, uds=True)).error_code is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_point,stored_state", [("after_pending", "active"), ("before_active", "revoked")])
async def test_marker_crash_order_never_reopens_or_replays_uncertain_mutation(setup, ingest_fixture, monkeypatch, failure_point, stored_state):
    from topos.permissions_v2.ingest_provenance import IngestProvenanceService
    service, conn, snapshot = ingest_fixture
    path = snapshot.parent.parent.parent / "canonical.db"
    use_real_service(setup, service, path, monkeypatch)
    next_command(setup, {"operation": "enroll", "snapshot_id": "canary", "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "dataset_id": "crash-canary", "owner_attestation": OWNER_ATTESTATION}, 1)
    enrollment = real_ack(setup, await http_call(setup, uds=True)).result
    next_command(setup, {"operation": "revoke", "enrollment_id": enrollment.enrollment_id}, 2)
    publish = service._publish_marker
    calls = []
    def interrupted_publish(marker):
        calls.append(marker["state"])
        # First two writes durably consume the signed command. The third and
        # fourth are the actual revocation's pending/active marker writes.
        if failure_point == "before_active" and len(calls) == 4:
            raise OSError("synthetic crash after SQL commit")
        publish(marker)
        if failure_point == "after_pending" and len(calls) == 3:
            raise OSError("synthetic crash before SQL commit")
    monkeypatch.setattr(service, "_publish_marker", interrupted_publish)
    response = real_ack(setup, await http_call(setup, uds=True))
    assert response.error_code == "ingest_unavailable"
    assert conn.execute("SELECT state FROM ingest_provenance_enrollments WHERE enrollment_id=?", (enrollment.enrollment_id,)).fetchone()[0] == stored_state
    assert conn.execute("SELECT COUNT(*) FROM ingest_provenance_commands WHERE command_id=?", (setup.command.command_id,)).fetchone()[0] == 1
    assert json.loads(service.marker.read_text())["state"] == "pending"
    setup.runtime._ingestion_service = IngestProvenanceService(canonical_database=path, binding=service.binding, snapshot_root=service.root)
    # Same signed request, after a process-equivalent service restart, remains
    # blocked. It cannot turn a torn marker into a new enrollment or new burn.
    assert real_ack(setup, await http_call(setup, uds=True)).error_code is not None
    assert json.loads(service.marker.read_text())["state"] == "pending"
    assert conn.execute("SELECT state FROM ingest_provenance_enrollments WHERE enrollment_id=?", (enrollment.enrollment_id,)).fetchone()[0] == stored_state


@pytest.mark.asyncio
async def test_lost_enrollment_ack_can_be_recovered_with_new_exact_request_without_resurrection(setup, ingest_fixture, monkeypatch):
    service, conn, snapshot = ingest_fixture
    path = snapshot.parent.parent.parent / "canonical.db"
    use_real_service(setup, service, path, monkeypatch)
    request = {"operation": "enroll", "snapshot_id": "canary", "snapshot_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "dataset_id": "recovery-canary", "owner_attestation": OWNER_ATTESTATION}
    next_command(setup, request, 1)
    first = real_ack(setup, await http_call(setup, uds=True)).result
    # Simulate the caller losing the first response. A new signed command may
    # recover the exact immutable tuple; replaying its old command is forbidden.
    next_command(setup, request, 2)
    recovered = real_ack(setup, await http_call(setup, uds=True)).result
    assert recovered == first
    next_command(setup, {"operation": "revoke", "enrollment_id": first.enrollment_id}, 3)
    revoked = real_ack(setup, await http_call(setup, uds=True)).result
    assert revoked.state == "revoked"
    next_command(setup, request, 4)
    assert real_ack(setup, await http_call(setup, uds=True)).result == revoked
    assert conn.execute("SELECT COUNT(*) FROM ingest_provenance_enrollments").fetchone()[0] == 1
