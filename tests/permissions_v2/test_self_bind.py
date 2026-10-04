"""The node binds itself for sharing (any-to-any N2; contract A2A-1 §4.2, tests of §8 "Node (N2)").

Every node here is in-process and fresh: a served database in a temporary folder with no sharing folder, a
temporary home whose only Topos file is the pinned relay stamp key (what the node pins at first boot), and an
environment with no sharing switch in it. The test acts as the control plane: it signs the bind with the relay
stamp key, stamps each frame as the owner's app, and later signs the mutation, the status request and the
recipient envelope with the control-plane key the bind made the node trust. Every key is derived from a label at
run time; every person, Topos and id is invented.

No model is ever called: the embedding model resolves to none (a lexical index), and the refresh loop's rounds are
replaced by a recorder, since its catch-up would call the local model.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import as_principal
from tests.permissions_v2.test_message_search_refusals import Socket
from topos.core.handlers import dispatch_relay_message, handle_control_plane_request
from topos.permissions_v2 import bind_protocol as bp
from topos.permissions_v2 import protection_doorbell, refresh_loop, search_transport, self_bind, switches
from topos.permissions_v2 import runtime as runtime_module
from topos.permissions_v2.canonical import digest
from topos.permissions_v2.protection_clock import clock_state
from topos.principal import OWNER_APP, Principal
from topos.relay_stamp import canonical_signing_payload
from topos.storage.db import paths

OWNER = mc.OWNER_ID
TOPOS = "topos_" + "5e" * 16
ENVIRONMENT = "permissions-beta-n2-test"
CP_ISSUER = "permissions-beta-n2-cp"
FRONTEND = "topos-app-n2"
CP_KID = "ck_n2"
OWNER_CLIENT = "topos_home_chat"
ACTOR, RECIPIENT_CLIENT = "actor-1", "client-2"


def derived(label: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(label.encode("ascii")).digest())


def public(key: Ed25519PrivateKey) -> bytes:
    return key.public_key().public_bytes_raw()


STAMP = derived("N2 test: relay stamp key")
CP = derived("N2 test: control plane signing key")
STRANGER = derived("N2 test: a key nobody pinned")


# --- the fresh node --------------------------------------------------------------------------------------------

def fresh_database(path: Path, *, now: int, messages: int = 6) -> None:
    """A served database as a migrated node has it, with a few of the owner's own work messages, each with an
    owner-asserted fact. No protection clock, no attestation, no review: nothing of sharing yet."""
    from topos.features.facts.store import FactStore
    from topos.storage.db.migrations import (journal_entries_duration_v1, journal_entries_ends_at_v1,
                                             journal_entries_people_v1, journal_entries_place_name_v1,
                                             journal_entries_starts_at_v1, signal_dimension_harness)
    with closing(sqlite3.connect(path)) as conn:
        mc._schema(conn)
        signal_dimension_harness.apply_signal_dimension_harness_up(conn)
        for migration in (journal_entries_people_v1.apply_journal_entries_people_v1_up,
                          journal_entries_place_name_v1.apply_journal_entries_place_name_v1_up,
                          journal_entries_duration_v1.apply_journal_entries_duration_v1_up,
                          journal_entries_ends_at_v1.apply_journal_entries_ends_at_v1_up,
                          journal_entries_starts_at_v1.apply_journal_entries_starts_at_v1_up):
            migration(conn)
        # The owner's disk floor: none, so the bind's room check does not depend on this machine's free space.
        conn.execute("INSERT INTO engine_config VALUES('min_free_disk_bytes','0')")
        facts = FactStore(conn)
        words = ("roadmap", "deploy", "launch", "review", "budget", "vendor")
        for number in range(messages):
            message_id = f"imessage:{70000 + number}"
            mc.insert_message(conn, message_id=message_id, source_id=mc.SOURCE,
                              content=f"{words[number % len(words)]} roadmap deploy notes team {number}",
                              event_at=mc._iso(now - (number + 1) * 3_600))
            facts.assert_fact(subject_entity_id=mc.OWNER_ENTITY, predicate="works_on",
                              object_value=f"project {number}", disclosure="scoped", asserted_by="owner",
                              source_refs=[{"table": "conversation_messages", "dataset_id": mc.DATASET,
                                            "source_id": mc.SOURCE, "record_id": message_id}])
        conn.commit()


class FreshNode:
    def __init__(self, root: Path, canonical: Path, home: Path):
        self.root, self.canonical, self.home = root, canonical, home
        self.durable = canonical.parent / "permissions-v2"
        self.backups = root / "backups"
        self.pin = home / ".topos" / "cp_stamp_key.pub"

    # what the control plane sends
    def bind_body(self, **changes) -> dict:
        now = int(time.time())
        raw = {"version": "topos-node-bind/v1", "request_id": "n2-bind-" + secrets.token_hex(6),
               "environment_id": ENVIRONMENT, "resource_id": TOPOS, "owner_id": OWNER, "node_id": None,
               "new_key_allowed": True, "cp_issuer_id": CP_ISSUER, "trusted_cp_keys": {CP_KID: public(CP).hex()},
               "frontend_client_id": FRONTEND, "nonce": secrets.token_hex(32), "issued_at": now,
               "expires_at": now + 120}
        raw.update(changes)
        return raw

    @staticmethod
    def stamped(message: dict, *, cls="owner_app", client=OWNER_CLIENT, acting=OWNER, key=STAMP) -> dict:
        now = int(time.time())
        stamp = {"v": 1, "cls": cls, "client_id": client, "acting_user": acting, "iat": now, "exp": now + 100}
        stamp["sig"] = base64.b64encode(key.sign(canonical_signing_payload(
            stamp, msg_id=message["id"], msg_type=message["type"]))).decode("ascii")
        return {**message, "principal_stamp": stamp}

    def frame(self, raw: dict | None = None, *, bind_key=STAMP, stamp_key=STAMP, **stamp) -> dict:
        raw = raw or self.bind_body()
        bind = bp.sign_bind(bp.BindBody.parse(raw), bind_key).model_dump()
        return self.stamped({"id": raw["request_id"], "type": "permissions_v2_bind", "payload": {"bind": bind}},
                            key=stamp_key, **stamp)

    @staticmethod
    async def send(message: dict) -> dict:
        return await dispatch_relay_message(message)

    async def bind(self, **changes):
        message = self.frame(self.bind_body(**changes))
        reply = await self.send(message)
        assert reply["status"] == "ok", reply
        sent = bp.SignedBind.parse(message["payload"]["bind"])
        return bp.verify_bind_proof(reply["payload"]["proof"], bind=sent, now=int(time.time())), reply

    # what is on disk
    def config(self) -> dict:
        return json.loads((self.durable / "config.json").read_text())

    def snapshot(self):
        """The sharing folder, the backups folder and the clock tables: everything a bind may write."""
        files = {}
        for top in (self.durable, self.backups):
            if not os.path.lexists(top):
                continue
            for path in [top, *sorted(top.rglob("*"))]:
                info = os.lstat(path)
                body = hashlib.sha256(path.read_bytes()).hexdigest() if stat.S_ISREG(info.st_mode) else None
                files[str(path)] = (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_mtime_ns, body)
        with closing(sqlite3.connect(self.canonical.as_uri() + "?mode=ro", uri=True)) as conn:
            schema = sorted(conn.execute("SELECT type, name, sql FROM sqlite_master WHERE name LIKE 'permissions_v2_%' "
                                         "OR name = 'entity_merge_tombstones'").fetchall())
            state = (conn.execute("SELECT * FROM permissions_v2_protection_state").fetchall()
                     if any(row[1] == "permissions_v2_protection_state" for row in schema) else None)
        return files, schema, state


def capabilities() -> dict:
    from topos.engine import registration
    return registration.build_engine_capabilities()


@pytest.fixture
def node(tmp_path, monkeypatch):
    from topos.engine import registration
    from topos.engine.backends import huggingface
    from topos.permissions_v2 import search_index, search_release

    home = tmp_path / "home"
    (home / ".topos").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("TOPOS_CP_STAMP_PUBKEY", raising=False)
    for name in switches.BY_NAME:
        monkeypatch.delenv(name, raising=False)
    canonical = tmp_path / "node" / "database.db"
    canonical.parent.mkdir()
    # Building the fixture runs migration helpers that take their own pre-migration backup; it goes elsewhere,
    # so the folder a bind backs up into starts empty.
    monkeypatch.setenv("TOPOS_BACKUP_DIR", str(tmp_path / "setup-backups"))
    fresh_database(canonical, now=int(time.time()))
    monkeypatch.setenv("TOPOS_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(paths, "resolve_active_database", lambda *a, **k: SimpleNamespace(path=canonical))
    fresh = FreshNode(tmp_path, canonical, home)
    from topos import relay_stamp
    monkeypatch.setattr(relay_stamp, "_PINNED_KEY_PATH", str(fresh.pin))   # this test's home, not the session's
    fresh.pin.write_text(base64.b64encode(public(STAMP)).decode("ascii") + "\n")   # pinned at first boot
    # No model: a lexical index, and the refresh loop's rounds recorded instead of run.
    monkeypatch.setattr(huggingface, "active_embedding_model", lambda: None)
    monkeypatch.setattr(search_index, "local_passage_embedder", lambda *a: pytest.fail("embedded a passage"))
    monkeypatch.setattr(search_release, "default_embedder", lambda *a: pytest.fail("embedded a query"))
    monkeypatch.setattr(registration, "ollama_is_reachable", lambda: False)
    fresh.rounds = []
    monkeypatch.setattr(refresh_loop.RefreshLoop, "step", lambda loop: fresh.rounds.append(loop))
    monkeypatch.setattr(runtime_module, "_runtime", None)
    monkeypatch.setattr(self_bind, "_TAKEN", {})
    protection_doorbell.stop()
    switches.forget_bound()
    yield fresh
    restart(fresh)


def quiesce() -> None:
    """Let every start a bind began finish starting, so that stopping it leaves nothing running behind."""
    assert wait_for(lambda: not threads_named("p2c-search-refresh-start"))
    assert wait_for(lambda: all(thread is protection_doorbell._watcher
                                for thread in threads_named("permissions-v2-protection-doorbell")))


def restart(node) -> None:
    """What a restart leaves of this process: nothing loaded, nothing running, nothing remembered."""
    quiesce()
    protection_doorbell.stop()
    loaded = runtime_module._runtime
    if loaded is not None:
        loaded.close()
        if loaded._refresh is not None and loaded._refresh._thread is not None:
            loaded._refresh._thread.join(5)
    runtime_module._runtime = None
    switches.forget_bound()


def threads_named(name: str) -> list:
    import threading
    return [thread for thread in threading.enumerate() if thread.name == name and thread.is_alive()]


def loop_running(runtime) -> bool:
    loop = runtime._refresh
    return loop is not None and loop._thread is not None and loop._thread.is_alive()


def wait_for(predicate, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# --- 1. a fresh node binds -------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_fresh_node_binds_with_one_call_and_no_hand_step(node):
    environment = dict(os.environ)
    assert not switches.is_bound() and not node.durable.exists() and not node.backups.exists()
    assert capabilities()["permissions_v2_bind_version"] == 1
    assert capabilities()["permissions_v2_node_key_id"] is None

    proof, reply = await node.bind()

    # The answer: the contract's frame, a proof that verifies with the key it names, made by this bind.
    assert set(reply) == {"id", "type", "status", "payload"} and reply["type"] == "permissions_v2_bind"
    assert proof.outcome == "bound" and proof.node_id.startswith("node_") and proof.engine_version
    assert proof.kid == bp.node_key_id(bytes.fromhex(proof.node_public_key))
    # No environment change, no env file, no switch.
    assert dict(os.environ) == environment
    assert not any(name in os.environ for name in switches.BY_NAME)
    assert not [path for path in node.root.rglob("*") if path.name == ".env"]
    # The config, private, where a bound node keeps it; the key, private, the one the proof names.
    assert stat.S_IMODE(node.durable.stat().st_mode) == 0o700
    for name in ("config.json", "node-signing.key"):
        assert stat.S_IMODE((node.durable / name).stat().st_mode) == 0o600
    config = node.config()
    assert config["identity"] == {"environment_id": ENVIRONMENT, "node_id": proof.node_id, "resource_id": TOPOS,
                                  "owner_id": OWNER}
    assert (config["cp_issuer_id"], config["frontend_client_id"], config["trusted_cp_keys"]) == (
        CP_ISSUER, FRONTEND, {CP_KID: public(CP).hex()})
    assert config["node_signing_kid"] == proof.kid
    assert config["canonical_database_path"] == str(node.canonical.resolve())
    assert config["ledger_path"] == str(node.durable.resolve() / "ledger.db")
    assert "evidence_review_store_path" not in config and "projection_review_store_path" not in config
    seed = bytes.fromhex((node.durable / "node-signing.key").read_text().strip())
    assert public(Ed25519PrivateKey.from_private_bytes(seed)).hex() == proof.node_public_key
    assert not (node.durable / "bind-pending.json").exists()
    # Bound, loaded with no variable set, ledger pinned to the identity, clock installed.
    assert switches.is_bound()
    runtime = runtime_module.get_runtime()
    assert runtime is runtime_module._runtime and runtime.config_path == (node.durable / "config.json").resolve()
    assert runtime.protocol.ledger.identity.model_dump() == config["identity"]
    with runtime.protocol.ledger._transaction() as conn:
        assert runtime.protocol.ledger._node(conn)["epoch"] == 0           # a new ledger, at epoch 0
    with closing(sqlite3.connect(node.canonical.as_uri() + "?mode=ro", uri=True)) as conn:
        clock_state(conn)
    # The doorbell and the refresh loop run now, with no restart.
    assert protection_doorbell.running()
    assert wait_for(lambda: loop_running(runtime)) and wait_for(lambda: node.rounds)
    # The heartbeat says so.
    assert capabilities()["permissions_v2_node_key_id"] == proof.kid
    # One backup, private, of the whole database, under a name migration retention never matches.
    [backup] = list(node.backups.iterdir())
    assert backup.name.startswith("database-pre-sharing-bind-") and backup.suffix == ".db"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    with closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)) as conn:
        assert conn.execute("SELECT value FROM engine_config WHERE key='user_id'").fetchone() == (OWNER,)
        assert conn.execute("SELECT count(*) FROM conversation_messages").fetchone()[0] == 6
    from topos.storage.db.migrations.backup import condemned_backups, untracked_snapshots
    assert condemned_backups(node.backups) == [] and backup in untracked_snapshots(node.backups)


# --- the bound node then shares: a signed change, then a search through the doors -----------------------------

def attest_owner(node) -> None:
    """The owner's "this is me", as the identity command records it (the runtime's floor is not involved)."""
    from tests.permissions_v2.test_owner_identity_binding import do_attest
    with closing(sqlite3.connect(node.canonical)) as conn:
        do_attest(conn, mc.OWNER_ENTITY)
        conn.commit()


async def owner_command(node, message_type: str, envelope: dict, request_id: str) -> dict:
    return await node.send(node.stamped({"id": request_id, "type": message_type,
                                         "payload": {"envelope": envelope}}))


def search_grant(node_id: str, monkeypatch) -> dict:
    """A p2c-v1 search share to one recipient, bound to the node the bind made, as its policy model dumps it."""
    from topos.permissions_v2.registry import parse_policy
    monkeypatch.setattr(mc, "NOW", int(time.time()))     # the grant is valid from now, its window ends now
    policy = mc.search_policy(grant="n2-grant", actor=ACTOR, client=RECIPIENT_CLIENT)
    policy["binding"].update(environment_id=ENVIRONMENT, node_id=node_id, resource_id=TOPOS, owner_id=OWNER)
    return parse_policy(policy).model_dump()


async def share_with_a_recipient(node, proof, monkeypatch) -> dict:
    """What the control plane does at a bound node's first share: its first signed status, then the signed
    activation of one search share. Each acknowledgement is verified with the key the bind's proof named."""
    from topos.permissions_v2.protocol import (MutationBody, StatusRequestBody, sign_mutation, sign_status_request,
                                               verify_ack)
    node_keys = {proof.kid: bytes.fromhex(proof.node_public_key)}
    attest_owner(node)
    policy = search_grant(proof.node_id, monkeypatch)
    now = int(time.time())

    # The control plane's first signed status: the node's epoch and protection revision.
    status = sign_status_request(StatusRequestBody.parse({
        "version": "topos-policy-status-request/v2", "kid": CP_KID, "issuer_id": CP_ISSUER,
        "audience_id": proof.node_id, "request_id": "n2-status-1", "binding": policy["binding"], "command_id": None,
        "command_hash": None, "issued_at": now, "expires_at": now + 120}), CP)
    reply = await owner_command(node, "permissions_v2_status", status.model_dump(), "n2-status-1")
    assert reply["status"] == "ok", reply
    ack = verify_ack(reply["payload"]["ack"], trusted_keys=node_keys, issuer_id=proof.node_id, audience_id=CP_ISSUER,
                     request=status, now=int(time.time()))
    state = ack.state
    assert state.grant_state == "absent"

    # The signed change: activate the share.
    authority = {**policy["binding"], "grant_generation": 1, "assignment_generation": 1,
                 "policy_version_id": policy["policy_version_id"], "policy_hash": digest(policy),
                 "capability_version": policy["versions"]["capability"],
                 "protection_revision": state.protection_revision, "node_epoch": state.node_epoch + 1}
    change = sign_mutation(MutationBody.parse({
        "version": "topos-policy-mutation/v2", "kid": CP_KID, "issuer_id": CP_ISSUER, "audience_id": proof.node_id,
        "command_id": "n2-activate-1", "operation": "activate", "expected_epoch": state.node_epoch,
        "authority": authority, "policy": policy, "owner_authorization": {"actor_id": OWNER, "client_id": FRONTEND},
        "issued_at": now, "expires_at": now + 120}), CP)
    reply = await owner_command(node, "permissions_v2_mutate", change.model_dump(), "n2-activate-1")
    assert reply["status"] == "ok", reply
    applied = verify_ack(reply["payload"]["ack"], trusted_keys=node_keys, issuer_id=proof.node_id,
                         audience_id=CP_ISSUER, request=change, now=int(time.time()))
    assert applied.outcome == "applied" and applied.state.grant_state == "active"
    # N3: the acknowledgement leaves before the share's index is built; the runtime's rebuild queue builds it after.
    assert runtime_module.get_runtime().index_rebuilds().wait_idle(30)
    return policy


@pytest.mark.asyncio
async def test_a_bound_node_accepts_a_signed_change_and_serves_a_search_for_it(node, monkeypatch):
    from topos.permissions_v2.forwarding import verify_node_result
    from topos.permissions_v2.signing import parse_envelope, request_digest, sign_envelope

    proof, _ = await node.bind()
    node_keys = {proof.kid: bytes.fromhex(proof.node_public_key)}
    policy = await share_with_a_recipient(node, proof, monkeypatch)
    now = int(time.time())

    # A recipient's search through the search door, answered and signed by the new key.
    runtime = runtime_module.get_runtime()

    def authority():          # off the event loop, as every ledger write on the node is
        with runtime.protocol.ledger._transaction() as conn:
            runtime.protocol._sync_protection(conn)
        with as_principal(cls=OWNER_APP, channel="uds", acting_user=OWNER):
            return runtime.protocol.ledger.authority_snapshot(policy["binding"]["grant_id"], now=now)
    snapshot = await asyncio.to_thread(authority)
    intent = {"query": "roadmap deploy", "k": 5}
    envelope = sign_envelope(parse_envelope({
        **snapshot.model_dump(), "version": "topos-grantee-envelope/v2", "kid": CP_KID, "request_id": "n2-search-1",
        "request_type": "permissions.v2.search", "request_hash": request_digest("permissions.v2.search", intent),
        "issued_at": now, "expires_at": now + 100}, signed=False), CP)
    message = node.stamped({"id": "n2-search-1", "type": search_transport.MESSAGE_TYPE,
                            "payload": {"envelope": envelope.model_dump(), "intent": intent}},
                           cls="third_party", client=RECIPIENT_CLIENT, acting=ACTOR)
    socket = Socket()
    await search_transport.dispatch_message_search(socket, message)
    [frame] = [json.loads(value) for value in socket.sent]
    assert frame["status"] == "ok", frame
    output = frame["payload"]["output"]
    assert output["records"] and all("roadmap" in record["content"] for record in output["records"])
    verify_node_result(frame["payload"]["result"], trusted_keys=node_keys, envelope=envelope, output=output,
                       now=int(time.time()))


# --- 2. a second bind is safe ------------------------------------------------------------------------------------

def settled(node) -> None:
    """Let what the bind started reach its first quiet moment before files are compared."""
    runtime = runtime_module._runtime
    assert wait_for(lambda: loop_running(runtime)) and wait_for(lambda: node.rounds)
    quiesce()


@pytest.mark.asyncio
@pytest.mark.parametrize("restarted", [False, True], ids=["same_process", "after_a_restart"])
async def test_an_identical_second_bind_answers_already_bound_and_rewrites_nothing(node, restarted):
    first, _ = await node.bind()
    settled(node)
    if restarted:
        restart(node)
    before = node.snapshot()
    again, reply = await node.bind()
    assert again.outcome == "already_bound"
    assert (again.node_id, again.kid, again.node_public_key) == (first.node_id, first.kid, first.node_public_key)
    # The control plane's later binds name the node id its registry holds; the answer is the same.
    named, _ = await node.bind(node_id=first.node_id, new_key_allowed=False)
    assert (named.outcome, named.node_id, named.node_public_key) == ("already_bound", first.node_id,
                                                                      first.node_public_key)
    assert node.snapshot() == before                    # no file rewritten: same bytes, same mtimes, same clock
    assert len(list(node.backups.iterdir())) == 1       # an already-bound answer takes no second backup


# --- 3 and 5. every refusal before the backup writes nothing ----------------------------------------------------

def _without_stamp(message):
    message = dict(message)
    del message["principal_stamp"]
    return message


async def _answer_directly(message):
    """What the handler does once the dispatcher let a verified owner stamp through."""
    principal = Principal(cls=OWNER_APP, channel="cp_relay", client_id=OWNER_CLIENT, acting_user=OWNER)
    return await asyncio.to_thread(self_bind.answer, message, principal)


def refusal_owner_socket(node, monkeypatch):
    message = _without_stamp(node.frame())
    return handle_control_plane_request(message, principal=Principal(cls=OWNER_APP, channel="uds",
                                                                      acting_user=OWNER)), 403, "bind_channel"


def refusal_automatic_resync_client(node, monkeypatch):
    return node.send(node.frame(client=protection_doorbell.AUTO_RESYNC_CLIENT)), 403, "bind_channel"


def refusal_recipient_stamp(node, monkeypatch):
    return node.send(node.frame(cls="third_party", client=RECIPIENT_CLIENT, acting=ACTOR)), 403, "owner_mode_required"


def refusal_no_stamp(node, monkeypatch):
    return node.send(_without_stamp(node.frame())), 403, "owner_mode_required"


def refusal_stamp_by_another_key(node, monkeypatch):
    return node.send(node.frame(stamp_key=STRANGER)), 403, "owner_mode_required"


def refusal_unpinned_stamp_key(node, monkeypatch):
    node.pin.unlink()
    return node.send(node.frame()), 403, "owner_mode_required"


def refusal_stamp_key_gone_before_the_bind_is_checked(node, monkeypatch):
    message = node.frame()
    node.pin.unlink()
    return _answer_directly(message), 403, "stamp_key_unavailable"


def refusal_payload_with_another_key(node, monkeypatch):
    message = node.frame()
    message["payload"]["mode"] = "owner"
    return node.send(message), 400, "bind_payload_invalid"


def refusal_bind_that_is_not_an_object(node, monkeypatch):
    message = node.frame()
    message["payload"]["bind"] = json.dumps(message["payload"]["bind"])
    return node.send(message), 400, "bind_payload_invalid"


def refusal_missing_signature(node, monkeypatch):
    message = node.frame()
    del message["payload"]["bind"]["signature"]
    return node.send(message), 400, "bind_payload_invalid"


def refusal_not_a_beta_environment(node, monkeypatch):
    raw = node.bind_body(environment_id="production-n2")
    message = node.stamped({"id": raw["request_id"], "type": "permissions_v2_bind",
                            "payload": {"bind": {**raw, "signature": "A" * 86}}})
    return node.send(message), 400, "bind_payload_invalid"


def refusal_frame_id_differs(node, monkeypatch):
    raw = node.bind_body()
    bind = bp.sign_bind(bp.BindBody.parse(raw), STAMP).model_dump()
    message = node.stamped({"id": raw["request_id"] + "-other", "type": "permissions_v2_bind",
                            "payload": {"bind": bind}})
    return node.send(message), 400, "bind_frame_mismatch"


def refusal_expired(node, monkeypatch):
    now = int(time.time())
    return node.send(node.frame(node.bind_body(issued_at=now - 300, expires_at=now - 180))), 403, "bind_expired"


def refusal_signed_by_the_control_plane_key(node, monkeypatch):
    return node.send(node.frame(bind_key=CP)), 403, "bind_signature_invalid"


def refusal_changed_after_signing(node, monkeypatch):
    message = node.frame()
    message["payload"]["bind"]["resource_id"] = "topos_" + "7b" * 16
    return node.send(message), 403, "bind_signature_invalid"


def refusal_stamp_names_another_owner(node, monkeypatch):
    return node.send(node.frame(acting="owner-2")), 403, "bind_owner_mismatch"


def refusal_another_owner_than_the_nodes(node, monkeypatch):
    return node.send(node.frame(node.bind_body(owner_id="owner-2"), acting="owner-2")), 403, "owner_mismatch"


def refusal_a_source_installed_for_another_topos(node, monkeypatch):
    with closing(sqlite3.connect(node.canonical)) as conn:
        conn.execute("CREATE TABLE source_runtime_installs(install_id TEXT PRIMARY KEY, scope_key TEXT)")
        conn.execute("INSERT INTO source_runtime_installs VALUES('install-1', ?)", (json.dumps(
            {"user_id": OWNER, "device_id": "*", "topos_id": "topos_" + "9d" * 16, "dataset_id": "dataset-1"}),))
        conn.commit()
    return node.send(node.frame()), 409, "topos_mismatch"


def refusal_no_new_key_on_an_unbound_node(node, monkeypatch):
    raw = node.bind_body(node_id="node_" + "6a" * 16, new_key_allowed=False)
    return node.send(node.frame(raw)), 409, "not_bound"


def refusal_another_bind_running(node, monkeypatch):
    async def busy():
        assert self_bind._LOCK.acquire(blocking=False)
        try:
            return await node.send(node.frame())
        finally:
            self_bind._LOCK.release()
    return busy(), 409, "bind_busy"


def refusal_disk_low(node, monkeypatch):
    monkeypatch.setattr(self_bind, "_free_bytes", lambda directory: 1)
    return node.send(node.frame()), 409, "disk_low"


def refusal_sharing_switched_off(node, monkeypatch):
    monkeypatch.setenv(switches.ENABLED.name, "false")
    return node.send(node.frame()), 503, "bind_failed"


def refusal_config_named_elsewhere(node, monkeypatch):
    monkeypatch.setenv(switches.CONFIG_PATH.name, str(node.root / "elsewhere" / "config.json"))
    return node.send(node.frame()), 503, "bind_failed"


def _hand_made_config(node, *, database: Path, text: str | None = None) -> Path:
    node.durable.mkdir(mode=0o700)
    config = {"version": "topos-policy-node-config/v1",
              "identity": {"environment_id": ENVIRONMENT, "node_id": "node_" + "2f" * 16,
                           "resource_id": "topos_" + "3e" * 16, "owner_id": OWNER},
              "cp_issuer_id": CP_ISSUER, "frontend_client_id": FRONTEND, "trusted_cp_keys": {CP_KID: public(CP).hex()},
              "node_signing_kid": "nk_" + "4d" * 16, "node_signing_key_path": str(node.durable / "node-signing.key"),
              "canonical_database_path": str(database), "ledger_path": str(node.durable / "ledger.db")}
    path = node.durable / "config.json"
    path.write_text(text if text is not None else json.dumps(config))
    path.chmod(0o600)
    return path


def refusal_a_damaged_config_is_there(node, monkeypatch):
    _hand_made_config(node, database=node.canonical, text="{")
    return node.send(node.frame()), 503, "bind_failed"


def refusal_another_databases_config_is_there(node, monkeypatch):
    other = node.root / "other-topos" / "database.db"
    other.parent.mkdir()
    other.write_bytes(b"")
    _hand_made_config(node, database=other)                  # a folder another Topos left beside this database
    return node.send(node.frame()), 409, "bound_elsewhere"


REFUSALS = {name[len("refusal_"):]: case for name, case in globals().items() if name.startswith("refusal_")}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(REFUSALS))
async def test_each_refusal_writes_nothing(node, monkeypatch, case):
    sending, status, code = REFUSALS[case](node, monkeypatch)
    before = node.snapshot()
    reply = await sending
    assert (reply["status"], reply.get("code"), reply["error"]) == ("error", status, code), reply
    assert set(reply) <= {"id", "type", "status", "code", "error"}
    assert node.snapshot() == before
    switches.forget_bound()
    assert not switches.is_bound() and runtime_module._runtime is None and not protection_doorbell.running()


# --- a replayed bind is spent -------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_replayed_bind_is_refused_and_writes_nothing(node):
    message = node.frame()
    first = await node.send(message)
    assert first["status"] == "ok" and first["payload"]["proof"]["outcome"] == "bound"
    settled(node)
    before = node.snapshot()
    again = await node.send(json.loads(json.dumps(message)))      # the same frame, byte for byte, still in time
    assert (again["status"], again["code"], again["error"]) == ("error", 403, "bind_expired")
    assert node.snapshot() == before


@pytest.mark.asyncio
async def test_a_replay_of_a_refused_bind_is_spent_too(node):
    message = node.frame(node.bind_body(node_id="node_" + "6a" * 16, new_key_allowed=False))
    assert (await node.send(message))["error"] == "not_bound"
    before = node.snapshot()
    assert (await node.send(json.loads(json.dumps(message))))["error"] == "bind_expired"
    assert node.snapshot() == before
    proof, _ = await node.bind()                                    # a fresh bind is not affected
    assert proof.outcome == "bound"


# --- 6. a bound node refuses another identity and other control-plane keys --------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("change,code", [
    ({"resource_id": "topos_" + "7b" * 16}, "bound_elsewhere"),
    ({"environment_id": "permissions-beta-n2-other"}, "bound_elsewhere"),
    ({"node_id": "node_" + "8c" * 16, "new_key_allowed": False}, "bound_elsewhere"),
    ({"trusted_cp_keys": {"ck_other": public(STRANGER).hex()}}, "bind_conflict"),
    ({"cp_issuer_id": "permissions-beta-n2-other-cp"}, "bind_conflict"),
    ({"frontend_client_id": "topos-app-other"}, "bind_conflict"),
], ids=["topos", "environment", "node_id", "cp_keys", "cp_issuer", "frontend_client"])
async def test_a_bound_node_refuses_another_identity_and_other_control_plane_keys(node, change, code):
    await node.bind()
    settled(node)
    before = node.snapshot()
    reply = await node.send(node.frame(node.bind_body(**change)))
    assert (reply["status"], reply["code"], reply["error"]) == ("error", 409, code)
    assert node.snapshot() == before


# --- 4. a crash before and after the commit --------------------------------------------------------------------

class Crash(BaseException):
    """The process dying: nothing after it runs, no cleanup, no answer."""


def crash(*args, **kwargs):
    raise Crash()


@pytest.mark.asyncio
async def test_a_crash_before_the_commit_leaves_an_unbound_node_the_next_bind_completes(node, monkeypatch):
    with monkeypatch.context() as during:
        during.setattr(self_bind, "_commit_config", crash)
        with pytest.raises(Crash):
            await node.send(node.frame())
    restart(node)
    assert not switches.is_bound() and not (node.durable / "config.json").exists()
    pending = json.loads((node.durable / "bind-pending.json").read_text())
    seed = bytes.fromhex((node.durable / "node-signing.key").read_text().strip())
    # The control plane's pending bind expired; the owner's next setup sends a new first bind.
    proof, _ = await node.bind()
    assert proof.outcome == "bound" and proof.node_id == pending["node_id"] and proof.kid == pending["kid"]
    assert proof.node_public_key == public(Ed25519PrivateKey.from_private_bytes(seed)).hex()
    assert not (node.durable / "stale").exists() and not (node.durable / "bind-pending.json").exists()


@pytest.mark.asyncio
async def test_leftovers_of_another_identity_are_set_aside_never_used(node, monkeypatch):
    with monkeypatch.context() as during:
        during.setattr(self_bind, "_commit_config", crash)
        with pytest.raises(Crash):
            await node.send(node.frame())
    restart(node)
    left = json.loads((node.durable / "bind-pending.json").read_text())
    proof, _ = await node.bind(environment_id="permissions-beta-n2-second")
    assert proof.node_id != left["node_id"] and proof.kid != left["kid"]
    [aside] = list((node.durable / "stale").iterdir())
    assert sorted(path.name for path in aside.iterdir()) == ["bind-pending.json", "node-signing.key"]
    assert json.loads((aside / "bind-pending.json").read_text()) == left
    assert stat.S_IMODE(aside.stat().st_mode) == 0o700


@pytest.mark.asyncio
async def test_a_crash_after_the_commit_leaves_a_bound_node_the_next_bind_answers(node, monkeypatch):
    with monkeypatch.context() as during:
        during.setattr(self_bind, "_drop_pending", crash)       # after the rename and the load: no proof left
        with pytest.raises(Crash):
            await node.send(node.frame())
    restart(node)
    assert switches.is_bound()
    config = node.config()
    # The control plane never heard: its next setup sends a first bind again, with no confirmation.
    proof, _ = await node.bind()
    assert proof.outcome == "already_bound" and proof.node_id == config["identity"]["node_id"]
    assert proof.kid == config["node_signing_kid"]
    assert not (node.durable / "bind-pending.json").exists()       # no pending record outlives its commit


def committed_key(node) -> SimpleNamespace:
    """The committed config's node id, key id and public key: what share_with_a_recipient needs of a proof."""
    config = node.config()
    seed = bytes.fromhex(Path(config["node_signing_key_path"]).read_text().strip())
    return SimpleNamespace(node_id=config["identity"]["node_id"], kid=config["node_signing_kid"],
                           node_public_key=public(Ed25519PrivateKey.from_private_bytes(seed)).hex())


@pytest.mark.asyncio
@pytest.mark.parametrize("found_by", ["the_next_bind", "the_first_load", "neither"])
async def test_a_pending_record_never_outlives_its_commit(node, monkeypatch, found_by):
    """Review N2 finding 1, walked through its crash point. A bind dies after its config is committed and before its
    pending record is removed: the record's key is now the node's key, the one the control plane may hold. The node
    removes the record the first time it finds itself bound (the next bind's already_bound answer, or the runtime's
    first load); and should the record survive both, a bind never takes up a pending key beside a ledger. So when
    the folder later loses its config, the confirmed rebind makes a NEW key, which the control plane resets for,
    instead of presenting the registered key over a reset ledger (every share stuck)."""
    with monkeypatch.context() as during:
        during.setattr(runtime_module, "load_runtime", crash)      # dead just after the rename, before the load
        with pytest.raises(Crash):
            await node.send(node.frame())
    restart(node)
    pending = node.durable / "bind-pending.json"
    key = committed_key(node)
    assert switches.is_bound() and json.loads(pending.read_text())["kid"] == key.kid   # the window leaves one
    if found_by == "neither":
        monkeypatch.setattr(self_bind, "forget_committed_pending", lambda durable, kid: False)
    if found_by == "the_next_bind":
        # The bind loads the runtime to check it can serve; with the load's own removal off (as when its unlink
        # failed), the record is removed by the already_bound answer itself.
        monkeypatch.setattr(runtime_module, "_forget_committed_pending", lambda runtime: None)
        answered, _ = await node.bind()
        assert (answered.outcome, answered.kid) == ("already_bound", key.kid)
    else:
        runtime_module.get_runtime()
    assert pending.exists() is (found_by == "neither")
    # The owner shares; then the folder loses only its config, and the owner confirms a new key.
    await share_with_a_recipient(node, key, monkeypatch)
    restart(node)
    (node.durable / "config.json").unlink()
    again, _ = await node.bind(node_id=key.node_id, new_key_allowed=True)
    assert again.outcome == "bound" and again.node_id == key.node_id
    assert again.kid != key.kid and again.node_public_key != key.node_public_key      # a new key: the CP resets
    assert ledger_state(node.durable / "ledger.db") == (0, 0, key.node_id)


# --- after a failure at the backup, the identity, the clock, the commit and the load ----------------------------

@pytest.mark.asyncio
async def test_a_failed_backup_leaves_nothing(node, monkeypatch):
    before = node.snapshot()
    monkeypatch.setattr(self_bind, "_fsync_file", lambda path: (_ for _ in ()).throw(OSError("full")))
    reply = await node.send(node.frame())
    assert (reply["code"], reply["error"]) == (503, "backup_failed")
    assert node.snapshot() == before and not node.backups.exists()


@pytest.mark.asyncio
async def test_a_failed_identity_leaves_an_unbound_node_and_harmless_leftovers(node, monkeypatch):
    real = self_bind._write_private

    def no_key(path, data):
        if path.name == "node-signing.key":
            raise OSError("no room")
        return real(path, data)
    with monkeypatch.context() as during:
        during.setattr(self_bind, "_write_private", no_key)
        reply = await node.send(node.frame())
    assert (reply["code"], reply["error"]) == (503, "bind_failed")
    assert not switches.is_bound() and (node.durable / "bind-pending.json").exists()
    assert not (node.durable / "node-signing.key").exists()
    proof, _ = await node.bind()                     # a pending record without its key is set aside
    assert proof.outcome == "bound"
    [aside] = list((node.durable / "stale").iterdir())
    assert [path.name for path in aside.iterdir()] == ["bind-pending.json"]


@pytest.mark.asyncio
async def test_a_clock_that_cannot_be_installed_leaves_the_node_unbound(node):
    with closing(sqlite3.connect(node.canonical)) as conn:
        conn.execute("DROP TABLE intelligence_exclusions")
        conn.commit()
    _files, schema, state = node.snapshot()
    reply = await node.send(node.frame())
    assert (reply["code"], reply["error"]) == (409, "protection_unavailable")
    assert not switches.is_bound() and not (node.durable / "config.json").exists()
    assert node.snapshot()[1:] == (schema, state) and state is None      # no clock, and nothing half-installed


@pytest.mark.asyncio
async def test_a_failed_commit_leaves_the_node_unbound_with_no_partial_config(node, monkeypatch):
    real = self_bind._write_private

    def no_config(path, data):
        if path.name == "config.json.tmp":
            raise OSError("no room")
        return real(path, data)
    monkeypatch.setattr(self_bind, "_write_private", no_config)
    reply = await node.send(node.frame())
    assert (reply["code"], reply["error"]) == (503, "bind_failed")
    assert not switches.is_bound()
    assert not [path.name for path in node.durable.iterdir() if path.name.startswith("config.json")]


@pytest.mark.asyncio
async def test_a_config_that_does_not_load_is_set_aside_and_the_node_is_unbound_again(node, monkeypatch):
    from topos.permissions_v2.canonical import PolicyError

    def refuse(*args, **kwargs):
        raise PolicyError("ledger_identity")
    with monkeypatch.context() as during:
        during.setattr(runtime_module, "load_runtime", refuse)
        reply = await node.send(node.frame())
    assert (reply["code"], reply["error"]) == (503, "bind_load_failed")
    switches.forget_bound()
    assert not switches.is_bound() and runtime_module._runtime is None and not protection_doorbell.running()
    [failed] = [path for path in node.durable.iterdir() if path.name.startswith("config.json")]
    assert failed.name.startswith("config.json.failed-")
    proof, _ = await node.bind()                     # the next bind of the same identity completes
    assert proof.outcome == "bound" and proof.node_id == json.loads(failed.read_text())["identity"]["node_id"]


# --- a rebind keeps the node id ---------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_node_that_lost_its_sharing_folder_binds_again_with_its_node_id_and_a_new_key(node):
    first, _ = await node.bind()
    settled(node)
    restart(node)
    node.durable.rename(node.root / "lost-sharing-folder")      # a new machine, a fresh install, a deleted folder
    assert capabilities()["permissions_v2_node_key_id"] is None
    # Without the owner's confirmation the node makes no key.
    reply = await node.send(node.frame(node.bind_body(node_id=first.node_id, new_key_allowed=False)))
    assert (reply["code"], reply["error"]) == (409, "not_bound") and not node.durable.exists()
    # With it, the node adopts the registry's node id and makes a new key.
    again, _ = await node.bind(node_id=first.node_id, new_key_allowed=True)
    assert again.outcome == "bound" and again.node_id == first.node_id
    assert again.node_public_key != first.node_public_key and again.kid != first.kid
    assert node.config()["identity"]["node_id"] == first.node_id
    assert capabilities()["permissions_v2_node_key_id"] == again.kid


def ledger_state(path: Path) -> tuple:
    """A ledger's epoch, its number of shares and the node id it is pinned to, read without changing it."""
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        identity, epoch = conn.execute("SELECT identity_json, epoch FROM p2a_node WHERE singleton=1").fetchone()
        shares = conn.execute("SELECT count(*) FROM p2a_grants").fetchone()[0]
    return epoch, shares, json.loads(identity)["node_id"]


def tree(root: Path) -> dict:
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


@pytest.mark.asyncio
async def test_a_new_key_sets_the_previous_ledger_and_share_indexes_aside_and_starts_a_fresh_ledger(node,
                                                                                                    monkeypatch):
    """A2A-1 amendment 5.4, the lost-config case. The folder kept its ledger (a share applied, its index built)
    and lost only its config; the owner confirms a new key. Before the new config is written, the ledger and the
    share indexes are moved aside whole and kept byte for byte; nothing else in the folder changes; the new ledger
    starts at epoch 0 with no share, which is what the control plane's store reset expects."""
    first, _ = await node.bind()
    await share_with_a_recipient(node, first, monkeypatch)
    settled(node)
    restart(node)
    ledger, indexes = node.durable / "ledger.db", node.durable / "message-search"
    epoch, shares, node_id = ledger_state(ledger)
    assert epoch >= 1 and shares == 1 and node_id == first.node_id          # the old ledger served a share
    kept_ledger, kept_indexes = hashlib.sha256(ledger.read_bytes()).hexdigest(), tree(indexes)
    assert any(name.startswith("grant-") for name in kept_indexes)          # and its index was built
    (node.durable / "config.json").unlink()        # lost; the ledger, the indexes, the key and the reviews stay
    before, schema, state = node.snapshot()

    # Stop the bind at the moment it would write the new config: by then the ledger and indexes are aside.
    real = self_bind._write_private

    def stop_at_the_config(path, data):
        if path.name == "config.json.tmp":
            raise Crash()
        return real(path, data)
    with monkeypatch.context() as during:
        during.setattr(self_bind, "_write_private", stop_at_the_config)
        with pytest.raises(Crash):
            await node.send(node.frame(node.bind_body(node_id=first.node_id, new_key_allowed=True)))
    [aside] = [folder for folder in (node.durable / "stale").iterdir() if folder.name.endswith("-previous-ledger")]
    assert stat.S_IMODE(aside.stat().st_mode) == 0o700
    assert hashlib.sha256((aside / "ledger.db").read_bytes()).hexdigest() == kept_ledger
    assert tree(aside / "message-search") == kept_indexes
    assert not ledger.exists() and not indexes.exists() and not (node.durable / "config.json").exists()
    # Nothing else changed: only the bind's own files (its pending record and new key, the old key set aside),
    # the moved ledger and indexes, and the backup. The owner's reviews and the clock are as they were.
    after, after_schema, after_state = node.snapshot()
    own = {str(node.durable), str(node.durable / "bind-pending.json"), str(node.durable / "node-signing.key")}
    moved = (str(ledger), str(indexes), str(node.durable / "stale"), str(node.backups))
    changed = {path for path in set(before) | set(after) if before.get(path) != after.get(path)}
    assert {path for path in changed if path not in own and not path.startswith(moved)} == set()
    assert any(Path(path).name.startswith("evidence-reviews") for path in before)    # they were there to compare
    assert (after_schema, after_state) == (schema, state)

    # The same bind to its end: the node keeps its id, has a new key, and serves from a new ledger at epoch 0
    # with no share. The old one stays where it was put, unread.
    restart(node)
    again, _ = await node.bind(node_id=first.node_id, new_key_allowed=True)
    assert again.outcome == "bound" and again.node_id == first.node_id
    assert again.node_public_key != first.node_public_key
    runtime = runtime_module._runtime
    assert Path(runtime.protocol.ledger.path).resolve() == ledger.resolve()
    assert ledger_state(ledger) == (0, 0, first.node_id)
    assert hashlib.sha256((aside / "ledger.db").read_bytes()).hexdigest() == kept_ledger
    assert tree(aside / "message-search") == kept_indexes


@pytest.mark.asyncio
async def test_an_owner_node_bound_by_hand_answers_with_its_configured_key_id(node):
    """D29: the owner's existing node keeps its configured key id, which no bind derived."""
    await node.bind()
    restart(node)
    config = node.config()
    config["node_signing_kid"] = "beta-policy-node-1"
    (node.durable / "config.json").write_text(json.dumps(config))
    switches.forget_bound()
    assert capabilities()["permissions_v2_node_key_id"] == "beta-policy-node-1"
    proof, _ = await node.bind(node_id=config["identity"]["node_id"], new_key_allowed=False)
    assert (proof.outcome, proof.kid) == ("already_bound", "beta-policy-node-1")


@pytest.mark.asyncio
async def test_a_loaded_runtime_whose_config_went_away_needs_a_restart(node):
    await node.bind()
    settled(node)
    runtime_module._runtime._sweeper_stop.set()
    (node.durable / "config.json").rename(node.root / "config-moved-away.json")
    switches.forget_bound()
    before = node.snapshot()
    reply = await node.send(node.frame())
    assert (reply["code"], reply["error"]) == (409, "restart_required")
    assert node.snapshot() == before


# --- the loops: once per process --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_loops_start_after_a_bind_once_and_the_startup_path_starts_no_second_copy(node):
    # Start-up on a node that is not bound yet: the doorbell finds nothing to watch, the refresh loop stays off.
    assert protection_doorbell.start_at_startup(delay=0) is True
    assert refresh_loop.start_at_startup(delay=0) is False
    assert wait_for(lambda: not threads_named("permissions-v2-protection-doorbell"))
    assert not protection_doorbell.running()
    loops_before = len(threads_named("p2c-search-refresh"))
    await node.bind()
    settled(node)
    runtime = runtime_module._runtime
    loop = runtime._refresh
    assert len(threads_named("p2c-search-refresh")) == loops_before + 1
    assert protection_doorbell.running() and len(threads_named("permissions-v2-protection-doorbell")) == 1
    # The start-up path again (its 60 s wait is over): it finds both running and starts nothing.
    assert protection_doorbell.start_at_startup(delay=0) is True
    assert refresh_loop.start_at_startup(delay=0) is True
    assert protection_doorbell.start_after_bind() is False
    assert wait_for(lambda: not threads_named("p2c-search-refresh-start"))
    assert wait_for(lambda: len(threads_named("permissions-v2-protection-doorbell")) == 1)
    assert runtime._refresh is loop and runtime.refresh_loop() is loop
    assert len(threads_named("p2c-search-refresh")) == loops_before + 1


# --- 7. a profile switch carries the sharing folder -------------------------------------------------------------

def test_a_profile_switch_moves_the_sharing_folder_both_ways(tmp_path, monkeypatch):
    from topos import profiles
    base = tmp_path / "machine" / ".topos"
    base.mkdir(parents=True)
    (base / ".env").write_text("TOPOS_KEY=KEYAAA\n")
    (base / "database.db").write_bytes(b"first-topos")
    durable = base / "permissions-v2"
    durable.mkdir(mode=0o700)
    (durable / "node-signing.key").write_text(secrets.token_hex(32) + "\n")
    config = {"version": "topos-policy-node-config/v1",
              "identity": {"environment_id": ENVIRONMENT, "node_id": "node_" + "1c" * 16, "resource_id": TOPOS,
                           "owner_id": OWNER},
              "cp_issuer_id": CP_ISSUER, "frontend_client_id": FRONTEND, "trusted_cp_keys": {CP_KID: public(CP).hex()},
              "node_signing_kid": "nk_" + "1c" * 16, "node_signing_key_path": str(durable / "node-signing.key"),
              "canonical_database_path": str(base / "database.db"), "ledger_path": str(durable / "ledger.db")}
    (durable / "config.json").write_text(json.dumps(config))
    (durable / "config.json").chmod(0o600)
    second = base / "profiles" / "second"
    second.mkdir(parents=True)
    (second / ".env").write_text("TOPOS_KEY=KEYBBB\n")
    (second / "database.db").write_bytes(b"second-topos")
    (second / "profile.json").write_text(json.dumps({"profile_id": "second", "topos_name": "Second"}))
    monkeypatch.setattr(profiles, "node_is_running", lambda port=None: False)
    monkeypatch.setattr(paths, "resolve_active_database", lambda *a, **k: SimpleNamespace(path=base / "database.db"))
    for name in switches.BY_NAME:
        monkeypatch.delenv(name, raising=False)
    switches.forget_bound()
    assert switches.is_bound()

    away = profiles.switch_profile("second", base)
    archived = base / "profiles" / away["archived_as"] / "permissions-v2"
    assert json.loads((archived / "config.json").read_text())["identity"]["resource_id"] == TOPOS
    assert not durable.exists()                     # nothing of the first Topos's sharing stays beside the second's
    switches.forget_bound()
    assert not switches.is_bound()                  # the second Topos has no folder: it is not bound

    profiles.switch_profile(away["archived_as"], base)
    assert (durable / "config.json").exists() and stat.S_IMODE(durable.stat().st_mode) == 0o700
    switches.forget_bound()
    assert switches.is_bound()                      # back at the top, the folder's absolute paths are valid again


# --- 8. the snapshot -------------------------------------------------------------------------------------------

def test_the_handled_types_snapshot_lists_the_bind_as_owner_only():
    from topos.core.handlers import HANDLERS, OWNER_ONLY_MESSAGE_TYPES
    snapshot = json.loads((Path(__file__).resolve().parents[2] / "topos" / "protocol" /
                           "handled_message_types.json").read_text())
    assert bp.MESSAGE_TYPE == "permissions_v2_bind"
    assert bp.MESSAGE_TYPE in snapshot["handled_message_types"] and bp.MESSAGE_TYPE in HANDLERS
    assert bp.MESSAGE_TYPE in snapshot["owner_only_message_types"] and bp.MESSAGE_TYPE in OWNER_ONLY_MESSAGE_TYPES


# --- the heartbeat's two fields ----------------------------------------------------------------------------------

def test_the_heartbeat_names_the_key_id_of_the_config_where_the_node_keeps_it(tmp_path, monkeypatch):
    from topos.engine import registration
    monkeypatch.setattr(registration, "ollama_is_reachable", lambda: False)
    for name in switches.BY_NAME:
        monkeypatch.delenv(name, raising=False)
    canonical = tmp_path / "node" / "database.db"
    canonical.parent.mkdir()
    canonical.write_bytes(b"")
    monkeypatch.setattr(paths, "resolve_active_database", lambda *a, **k: SimpleNamespace(path=canonical))
    switches.forget_bound()
    assert (capabilities()[bp.CAPABILITY_FIELD], capabilities()[bp.KEY_ID_FIELD]) == (bp.CAPABILITY_VERSION, None)
    # The owner's existing node: its config where its own line names it, with its configured key id (D29).
    elsewhere = tmp_path / "hand-set" / "config.json"
    elsewhere.parent.mkdir()
    config = {"version": "topos-policy-node-config/v1",
              "identity": {"environment_id": ENVIRONMENT, "node_id": "node-1", "resource_id": TOPOS, "owner_id": OWNER},
              "cp_issuer_id": CP_ISSUER, "frontend_client_id": FRONTEND, "trusted_cp_keys": {CP_KID: public(CP).hex()},
              "node_signing_kid": "beta-policy-node-1", "node_signing_key_path": str(elsewhere.parent / "k"),
              "canonical_database_path": str(canonical), "ledger_path": str(elsewhere.parent / "ledger.db")}
    elsewhere.write_text(json.dumps(config))
    elsewhere.chmod(0o600)
    monkeypatch.setenv(switches.CONFIG_PATH.name, str(elsewhere))
    assert capabilities()[bp.KEY_ID_FIELD] == "beta-policy-node-1"
    elsewhere.chmod(0o644)                                          # not private: no hint
    assert capabilities()[bp.KEY_ID_FIELD] is None
    elsewhere.chmod(0o600)
    elsewhere.write_text("{")                                       # does not parse: no hint
    assert capabilities()[bp.KEY_ID_FIELD] is None


# --- the coverage repair runs by itself (inventory E1, surprise 16) ---------------------------------------------

@pytest.mark.asyncio
async def test_a_node_whose_clock_predates_its_people_tables_binds_and_watches_them(node):
    """The clock was installed (an earlier binding, its folder since lost) when the node had no mentions table; a
    migration created it later. The bind repairs the coverage by itself and the node serves."""
    from topos.permissions_v2.protection_clock import ensure_protection_clock, identity_coverage
    from topos.storage.db.migrations.wiki_entities_v1 import apply_wiki_entities_v1_up
    with closing(sqlite3.connect(node.canonical)) as conn:
        conn.execute("DROP TABLE entity_mentions")
        conn.commit()
    ensure_protection_clock(node.canonical, owner_id=OWNER)
    with closing(sqlite3.connect(node.canonical)) as conn:
        assert identity_coverage(conn) == ("entities", "signal_objects")
        before = clock_state(conn)
        apply_wiki_entities_v1_up(conn)                              # the people tables arrive
        conn.commit()
    proof, _ = await node.bind()
    assert proof.outcome == "bound"
    with closing(sqlite3.connect(node.canonical.as_uri() + "?mode=ro", uri=True)) as conn:
        assert clock_state(conn) == (before[0], before[1] + 1)       # watched now, and every old authority stale
        assert identity_coverage(conn) == ("entities", "entity_mentions", "signal_objects")


# --- review N2: its findings, and the planted faults no test caught ---------------------------------------------

@pytest.mark.asyncio
async def test_a_served_database_that_is_a_link_binds_beside_the_file_it_links_to(node, monkeypatch):
    """Finding 2. The bind writes the config beside the file the database links to; where the node looks for it
    (switches) and where it loads it from (the runtime) are the same folder, so it binds once and stays bound."""
    link = node.root / "linked" / "database.db"
    link.parent.mkdir()
    link.symlink_to(node.canonical)
    monkeypatch.setattr(paths, "resolve_active_database", lambda *a, **k: SimpleNamespace(path=link))
    switches.forget_bound()
    proof, _ = await node.bind()
    assert proof.outcome == "bound" and switches.is_bound()
    assert switches.default_config_path() == (node.durable / "config.json").resolve()
    assert not (link.parent / "permissions-v2").exists()
    assert capabilities()[bp.KEY_ID_FIELD] == proof.kid
    again, _ = await node.bind()
    assert again.outcome == "already_bound" and len(list(node.backups.iterdir())) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("loaded", [False, True], ids=["after_a_restart", "while_loaded"])
async def test_a_bound_node_that_cannot_serve_is_not_vouched_for_and_nothing_is_written(node, loaded):
    """Finding 4. A bound node whose protection clock lost a trigger refuses every read. already_bound would have told
    the control plane it serves; it answers bind_failed with the cause instead, and writes nothing."""
    await node.bind()
    settled(node)
    restart(node)
    if loaded:
        runtime_module.get_runtime()
    with closing(sqlite3.connect(node.canonical)) as conn:
        conn.execute("DROP TRIGGER permissions_v2_owner_only_records_insert")
        conn.commit()
    before = node.snapshot()
    message = node.frame()
    reply = await node.send(message)
    assert reply == {"id": message["id"], "type": "permissions_v2_bind", "status": "error", "code": 503,
                     "error": "bind_failed", "cause": "protection_clock_unavailable"}
    assert node.snapshot() == before


@pytest.mark.asyncio
async def test_a_partial_backup_a_death_left_is_counted_as_room_and_removed(node, monkeypatch):
    """Finding 5. A death during the backup strands a full-size partial copy that nothing lists. The next bind counts
    it as room, removes it before it writes, and touches no other backup."""
    node.backups.mkdir()
    stranded = node.backups / "database-pre-sharing-bind-20261003T120000Z.db.partial"
    stranded.write_bytes(bytes(4096))
    stranded.chmod(0o600)
    migration = node.backups / "database-pre-v1.4.4-20261003T120000Z.db"     # not the bind's: never touched
    migration.write_bytes(b"backup")
    needed = 2 * self_bind._database_bytes(node.canonical)                    # the fixture's floor is 0
    monkeypatch.setattr(self_bind, "_free_bytes", lambda directory: needed - 4096)   # room only with the partial
    proof, _ = await node.bind()
    assert proof.outcome == "bound" and not stranded.exists() and migration.read_bytes() == b"backup"
    names = sorted(path.name for path in node.backups.iterdir())
    assert not [name for name in names if name.endswith(".partial")]
    assert len([name for name in names if name.startswith("database-pre-sharing-bind-")]) == 1


@pytest.mark.asyncio
async def test_the_heartbeat_offers_no_bind_and_no_key_while_the_kill_switch_is_on(node, monkeypatch):
    """Finding 7. With sharing switched off every bind is refused, so the heartbeat offers neither."""
    proof, _ = await node.bind()
    assert (capabilities()[bp.CAPABILITY_FIELD], capabilities()[bp.KEY_ID_FIELD]) == (1, proof.kid)
    monkeypatch.setenv(switches.ENABLED.name, "false")
    assert (capabilities()[bp.CAPABILITY_FIELD], capabilities()[bp.KEY_ID_FIELD]) == (0, None)
    monkeypatch.setenv(switches.ENABLED.name, "true")
    assert (capabilities()[bp.CAPABILITY_FIELD], capabilities()[bp.KEY_ID_FIELD]) == (1, proof.kid)
