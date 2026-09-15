import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from topos.core.handlers import handle_control_plane_request
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.runtime import load_runtime
from topos.principal import OWNER_APP, THIRD_PARTY, Principal
from tests.permissions_v2.test_node_protocol import mutation, protocol  # noqa: F401


@pytest.fixture
def configured(protocol, monkeypatch):
    node, policy, cp_key, node_key = protocol
    durable = node.canonical_database.parent / "permissions-v2"
    durable.mkdir(mode=0o700)
    key_path = durable / "node-signing.key"
    key_path.write_text(bytes(range(32, 64)).hex())
    key_path.chmod(0o600)
    policy["binding"]["environment_id"] = "permissions-beta-test"
    config = {"version": "topos-policy-node-config/v1", "identity": {key: policy["binding"][key] for key in type(node.ledger.identity).model_fields}, "cp_issuer_id": "beta-cp", "frontend_client_id": "permissions-beta-web", "trusted_cp_keys": {kid: value.hex() for kid, value in node.trusted_cp_keys.items()}, "node_signing_kid": "node-key", "node_signing_key_path": str(key_path), "canonical_database_path": str(node.canonical_database), "ledger_path": str(durable / "ledger.db")}
    config_path = durable / "config.json"
    config_path.write_text(json.dumps(config))
    config_path.chmod(0o600)
    return config, config_path, protocol


def test_runtime_private_config_active_db_binding_and_single_instance(configured):
    config, path, fixture = configured
    runtime = load_runtime(path, active_database=fixture[0].canonical_database)
    try:
        assert runtime.protocol.ledger.identity.environment_id == "permissions-beta-test"
        with pytest.raises(PolicyError, match="single_process_required"):
            load_runtime(path, active_database=fixture[0].canonical_database)
    finally:
        runtime.close()
    reopened = load_runtime(path, active_database=fixture[0].canonical_database)
    reopened.close()


def test_runtime_restart_rejects_lost_clock_instead_of_reinstalling(configured):
    import sqlite3
    from topos.permissions_v2.protection_clock import TABLE, TRIGGERS
    _, path, fixture = configured
    canonical = fixture[0].canonical_database
    runtime = load_runtime(path, active_database=canonical)
    runtime.close()
    with sqlite3.connect(canonical) as conn:
        for name in TRIGGERS:
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute(f"DROP TABLE {TABLE}")
    with pytest.raises(PolicyError, match="protection_clock_unavailable"):
        load_runtime(path, active_database=canonical)
    with sqlite3.connect(canonical) as conn:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name=?", (TABLE,)).fetchone() is None


@pytest.mark.asyncio
async def test_writer_wait_does_not_extend_signed_mutation_expiry(configured, monkeypatch):
    import asyncio
    import threading
    from contextlib import contextmanager
    from topos.permissions_v2 import runtime as runtime_module
    from topos.core.handlers import permissions_v2 as handler
    from topos.storage.db import write_gate
    _, path, fixture = configured
    runtime = load_runtime(path, active_database=fixture[0].canonical_database)
    held, release, waiting = threading.Event(), threading.Event(), threading.Event()
    original_gate = write_gate.with_db_write
    clock = [1100]
    def writer():
        with original_gate():
            held.set()
            release.wait(5)
    @contextmanager
    def observed_gate():
        waiting.set()
        with original_gate():
            yield
    blocker = threading.Thread(target=writer)
    try:
        monkeypatch.setenv("TOPOS_PERMISSIONS_V2_ENABLED", "true")
        monkeypatch.setenv("TOPOS_PERMISSIONS_V2_CONFIG_PATH", str(path))
        monkeypatch.setattr(runtime_module, "_runtime", runtime)
        monkeypatch.setattr(handler, "time", SimpleNamespace(time=lambda: clock[0]))
        command = mutation((runtime.protocol, fixture[1], fixture[2], fixture[3]))
        blocker.start()
        assert held.wait(2)
        monkeypatch.setattr(write_gate, "with_db_write", observed_gate)
        pending = asyncio.create_task(handle_control_plane_request({"id": "queued", "type": "permissions_v2_mutate", "payload": {"envelope": command.model_dump()}}, principal=Principal(cls=OWNER_APP, channel="cp_relay", acting_user="owner-1")))
        assert await asyncio.to_thread(waiting.wait, 2)
        clock[0] = 1300
        release.set()
        response = await pending
        assert response["status"] == "error" and response["error"] == "envelope_time"
    finally:
        release.set()
        if blocker.ident is not None:
            blocker.join(2)
        runtime.close()


@pytest.mark.parametrize("change", ["production", "owner", "db", "directory", "permissions"])
def test_runtime_wrong_config_rejected(configured, change):
    config, path, fixture = configured
    if change == "production":
        config["identity"]["environment_id"] = "production"
    elif change == "owner":
        config["identity"]["owner_id"] = "different-owner"
    elif change == "db":
        other = fixture[0].canonical_database.parent / "other.db"
        other.touch()
        config["canonical_database_path"] = str(other)
    elif change == "directory":
        config["ledger_path"] = str(path.parent.parent / "outside.db")
    path.write_text(json.dumps(config))
    if change == "permissions":
        path.chmod(0o644)
    with pytest.raises(PolicyError):
        load_runtime(path, active_database=fixture[0].canonical_database)


def test_runtime_rejects_multiworker_configuration(configured, monkeypatch):
    _, path, fixture = configured
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(PolicyError, match="single_process_required"):
        load_runtime(path, active_database=fixture[0].canonical_database)


@pytest.mark.asyncio
async def test_disabled_handler_rejects_without_loading_configuration(monkeypatch):
    from topos.permissions_v2 import runtime
    monkeypatch.delenv("TOPOS_PERMISSIONS_V2_ENABLED", raising=False)
    monkeypatch.setattr(runtime, "load_runtime", lambda *args, **kwargs: pytest.fail("disabled feature loaded runtime"))
    response = await handle_control_plane_request({"id": "req", "type": "permissions_v2_mutate", "payload": {"envelope": {}}}, principal=Principal(cls=OWNER_APP, channel="uds"))
    assert response["status"] == "error" and response["error"] == "permissions_v2_disabled"


@pytest.mark.asyncio
@pytest.mark.parametrize("principal", [None, Principal(cls=THIRD_PARTY, channel="local_http", acting_user="owner-1"), Principal(cls=OWNER_APP, channel="local_http", acting_user="owner-1")])
async def test_real_dispatcher_rejects_nonowner_channels_before_protocol(principal):
    response = await handle_control_plane_request({"id": "req", "type": "permissions_v2_mutate", "payload": {"envelope": {}}}, principal=principal)
    assert response["code"] == 403


@pytest.mark.asyncio
async def test_real_dispatcher_valid_owner_relay_still_requires_signed_v2_and_returns_ack(configured, monkeypatch):
    from topos.permissions_v2 import runtime as runtime_module
    from topos.core.handlers import permissions_v2 as handler
    _, path, fixture = configured
    runtime = load_runtime(path, active_database=fixture[0].canonical_database)
    try:
        monkeypatch.setenv("TOPOS_PERMISSIONS_V2_ENABLED", "true")
        monkeypatch.setenv("TOPOS_PERMISSIONS_V2_CONFIG_PATH", str(path))
        monkeypatch.setattr(runtime_module, "_runtime", runtime)
        monkeypatch.setattr(handler, "time", SimpleNamespace(time=lambda: 1100))
        effective = (runtime.protocol, fixture[1], fixture[2], fixture[3])
        signed = mutation(effective)
        principal = Principal(cls=OWNER_APP, channel="cp_relay", acting_user="owner-1")
        message = {"id": "req", "type": "permissions_v2_mutate", "payload": {"envelope": signed.model_dump()}}
        response = await handle_control_plane_request(message, principal=principal)
        assert response["status"] == "ok" and response["payload"]["ack"]["outcome"] == "applied"
        tampered = signed.model_dump()
        tampered["command_id"] = "changed"
        message["payload"]["envelope"] = tampered
        response = await handle_control_plane_request(message, principal=principal)
        assert response["error"] == "signature_invalid"
        response = await handle_control_plane_request(message, principal=Principal(cls=OWNER_APP, channel="cp_relay", acting_user="wrong-owner"))
        assert response["error"] == "owner_binding"
    finally:
        runtime.close()
