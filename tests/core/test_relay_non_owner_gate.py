"""Who a relay frame may reach, decided on the node (review S4, findings H1 and M1).

H1  A frame whose stamp verifies as ``third_party`` for a user who is not this node's owner reaches only the types
    on ``NON_OWNER_RELAY_TYPES``. Every other type, handled or not, gets the one refusal the dispatcher already
    gives a caller for a type it may not use. Until this rule the only thing in the way was that the control plane
    did not forward such a frame.
M1  A frame whose stamp is present but does not verify (expired, signed by another key, of a class this node does
    not know, malformed, or arriving at a node that pinned no key) is not "no stamp": it reaches nothing a verified
    third-party frame for a non-owner could not.

The reviewer's probe, kept: every registered handler is replaced by a recorder, so these tests measure the
dispatcher's own gates and nothing else. Keys are made at run time; every id here is invented.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import time
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import topos.core.handlers as hub
from topos.core.handlers.registry import HANDLERS, OWNER_ONLY_MESSAGE_TYPES
from topos.permissions_v2 import bind_protocol, switches
from topos.principal import OWNER_APP, RELAY_PRINCIPAL, THIRD_PARTY, Principal
from topos.relay_stamp import STAMP_FIELD, canonical_signing_payload
from topos.storage.db import paths

KEY = Ed25519PrivateKey.generate()
ANOTHER_KEY = Ed25519PrivateKey.generate()
OWNER, SOMEONE_ELSE = "owner-1", "someone-else"

#: The list, written out here on purpose: a type joins the code's list only by also joining this one.
SHARE_DOORS = frozenset({
    "permissions_v2_message_search",
    "permissions_v2_message_search_batch",
    "permissions_v2_answer_submit",
    "permissions_v2_answer_fetch",
})
#: Types no node handles: a refusal must not tell them from the handled ones.
UNHANDLED = ("no_such_type", "permissions_v2_source_read", "uma_get_messages")


def refusal(message_id):
    """The dispatcher's refusal for a type the caller may not use (it predates this rule)."""
    return {"id": message_id, "status": "error", "code": 403, "error": "owner_mode_required"}


def stamped(message, *, cls, acting="", client="some-client", key=KEY, iat=None, exp=None):
    now = time.time()
    stamp = {"v": 1, "cls": cls, "client_id": client, "acting_user": acting,
             "iat": now if iat is None else iat, "exp": now + 100 if exp is None else exp}
    stamp["sig"] = base64.b64encode(key.sign(canonical_signing_payload(
        stamp, msg_id=message["id"], msg_type=message["type"]))).decode("ascii")
    return {**message, STAMP_FIELD: stamp}


def engine_database(path, user_id=OWNER):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE engine_config (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                 "updated_at TEXT NOT NULL DEFAULT (datetime('now')))")
    if user_id is not None:
        conn.execute("INSERT INTO engine_config (key, value) VALUES ('user_id', ?)", (user_id,))
    conn.commit()
    return conn


@pytest.fixture
def node(monkeypatch, tmp_path):
    """An unbound node whose engine config names OWNER, with the control plane's stamp key pinned and every
    handler replaced by a recorder."""
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", base64.b64encode(KEY.public_key().public_bytes_raw()).decode("ascii"))
    for name in switches.BY_NAME:
        monkeypatch.delenv(name, raising=False)
    database = tmp_path / "node" / "database.db"
    database.parent.mkdir()
    conn = engine_database(database)
    monkeypatch.setattr(paths, "resolve_active_database", lambda *a, **k: SimpleNamespace(path=database))
    switches.forget_bound()
    assert not switches.is_bound()
    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)
    reached = []
    for name in list(HANDLERS):
        async def recorder(message, _name=name):
            from topos.principal import current_principal
            reached.append((_name, getattr(current_principal(), "cls", None)))
            return {"id": message.get("id"), "status": "ok", "payload": {"recorder": _name}}
        monkeypatch.setitem(HANDLERS, name, recorder)
    yield SimpleNamespace(conn=conn, reached=reached, database=database, monkeypatch=monkeypatch)
    conn.close()
    switches.forget_bound()


async def sweep(node, make, *, dispatch=None):
    """Send one frame of every handled type, and of three no node handles: (types that reached a handler, replies)."""
    dispatch = dispatch or hub.dispatch_relay_message
    node.reached.clear()
    replies = {}
    for name in [*sorted(HANDLERS), *UNHANDLED]:
        replies[name] = await dispatch(make({"id": "frame-" + name, "type": name, "payload": {}}))
    return sorted(name for name, _cls in node.reached), replies


def reachable_by_a_third_party():
    """What the dispatcher's older gates leave a third party: not the owner-only types, not ``signal_*``, not the
    legacy inspection tools."""
    return sorted(name for name in HANDLERS
                  if name not in OWNER_ONLY_MESSAGE_TYPES and not name.startswith("signal_")
                  and name not in hub.LEGACY_INSPECTION_TYPES)


# --- H1 --------------------------------------------------------------------------------------------------------

def test_the_allow_list_is_the_four_share_doors_and_nothing_else():
    assert hub.NON_OWNER_RELAY_TYPES == SHARE_DOORS
    assert isinstance(hub.NON_OWNER_RELAY_TYPES, frozenset)
    assert hub.NON_OWNER_RELAY_TYPES <= set(HANDLERS)
    assert not hub.NON_OWNER_RELAY_TYPES & set(OWNER_ONLY_MESSAGE_TYPES)


@pytest.mark.asyncio
async def test_a_third_party_for_another_user_reaches_only_the_allow_list(node):
    reached, replies = await sweep(node, lambda m: stamped(m, cls=THIRD_PARTY, acting=SOMEONE_ELSE))

    assert reached == sorted(SHARE_DOORS)
    for name, reply in replies.items():
        if name not in SHARE_DOORS:
            assert reply == refusal("frame-" + name), name
    # One shape for every refused type, a type no node handles included: nothing in it but the frame's own id.
    assert len({json.dumps({**reply, "id": None}, sort_keys=True)
                for name, reply in replies.items() if name not in SHARE_DOORS}) == 1
    # The review's examples, by name.
    for name in ("query", "query_live", "get_home_chat_session", "list_home_chat_sessions", "upsert_home_chat_session",
                 "llm_generation", "tools_retrieve", "put_user_identity", "start_ingestion", "app_ingest",
                 "store_message", "compute_invoke"):
        assert name in HANDLERS and replies[name] == refusal("frame-" + name)


@pytest.mark.asyncio
async def test_a_stamp_that_names_nobody_is_not_the_owners(node):
    reached, _ = await sweep(node, lambda m: stamped(m, cls=THIRD_PARTY, acting=""))
    assert reached == sorted(SHARE_DOORS)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["no_owner_row", "blank_owner", "no_engine_config_table", "no_database",
                                   "database_raises"])
@pytest.mark.parametrize("acting", [OWNER, ""])
async def test_a_node_that_cannot_name_its_owner_treats_every_third_party_as_a_non_owner(node, tmp_path, state, acting):
    """Never the open direction: no owner on record matches no stamp, an empty one included."""
    if state == "no_owner_row":
        node.conn.execute("DELETE FROM engine_config")
    elif state == "blank_owner":
        node.conn.execute("UPDATE engine_config SET value='' WHERE key='user_id'")
    elif state == "no_engine_config_table":
        node.conn.execute("DROP TABLE engine_config")
    elif state == "no_database":
        node.monkeypatch.setattr(hub, "get_db_connection", lambda: None)
    else:
        def broken():
            raise sqlite3.OperationalError("unable to open database file")
        node.monkeypatch.setattr(hub, "get_db_connection", broken)
    node.conn.commit()

    reached, replies = await sweep(node, lambda m: stamped(m, cls=THIRD_PARTY, acting=acting))

    assert reached == sorted(SHARE_DOORS)
    assert replies["query"] == refusal("frame-query")


def bind_by_hand(node, *, owner):
    """A sharing config beside the served database that binds it (``switches.is_bound``), written as a bind
    commits it, for an owner the engine config does not name."""
    durable = node.database.parent / switches.DURABLE_DIRECTORY
    durable.mkdir(mode=0o700)
    seed = os.urandom(32)
    key_path = durable / "node-signing.key"
    key_path.write_text(seed.hex() + "\n")
    os.chmod(key_path, 0o600)
    public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw()
    config = {
        "version": "topos-policy-node-config/v1",
        "identity": {"environment_id": "permissions-beta-r1-test", "node_id": bind_protocol.mint_node_id(),
                     "resource_id": "topos_" + os.urandom(16).hex(), "owner_id": owner},
        "cp_issuer_id": "permissions-beta-r1-cp", "frontend_client_id": "topos-app-r1",
        "trusted_cp_keys": {"ck_r1": ANOTHER_KEY.public_key().public_bytes_raw().hex()},
        "node_signing_kid": bind_protocol.node_key_id(public), "node_signing_key_path": str(key_path),
        "canonical_database_path": str(node.database.resolve()), "ledger_path": str(durable / "ledger.db"),
    }
    path = durable / "config.json"
    path.write_text(json.dumps(config))
    os.chmod(path, 0o600)
    switches.forget_bound()
    assert switches.is_bound()


@pytest.mark.asyncio
async def test_on_a_bound_node_the_owner_is_the_bound_identitys(node):
    bind_by_hand(node, owner="bound-owner")          # the engine config still names OWNER

    for_the_bound_owner, _ = await sweep(node, lambda m: stamped(m, cls=THIRD_PARTY, acting="bound-owner"))
    for_the_engine_owner, _ = await sweep(node, lambda m: stamped(m, cls=THIRD_PARTY, acting=OWNER))

    assert for_the_bound_owner == reachable_by_a_third_party()
    assert for_the_engine_owner == sorted(SHARE_DOORS)


@pytest.mark.asyncio
async def test_a_bound_node_whose_identity_cannot_be_read_names_no_owner(node):
    """A bound node never falls back to the engine config: that would be the open direction."""
    bind_by_hand(node, owner="bound-owner")
    node.monkeypatch.setattr(hub, "_bound_owner_id", lambda: None)

    for acting in ("bound-owner", OWNER):
        reached, _ = await sweep(node, lambda m: stamped(m, cls=THIRD_PARTY, acting=acting))
        assert reached == sorted(SHARE_DOORS)


# --- the other side: the owner's own frames reach what they reached before ------------------------------------------

OWNER_FRAMES = {
    "the owner's app": lambda m: stamped(m, cls=OWNER_APP, acting=OWNER, client="topos_home_chat"),
    "a capture app's write for the owner": lambda m: stamped(m, cls=OWNER_APP, acting=OWNER,
                                                             client="chatgpt-shadow-extension"),
    "the owner's outside client": lambda m: stamped(m, cls=THIRD_PARTY, acting=OWNER, client="chatgpt"),
    "the owner's routine": lambda m: stamped(m, cls="owner_automation", acting=OWNER, client="routine_executor"),
    # The control plane stamps a routine's model call with no acting user (routines_engine_bridge.py): the class
    # is its word that this is the owner's own automation, and the rule above reads only a third party's user.
    "the owner's routine, no user named": lambda m: stamped(m, cls="owner_automation", client="routine_executor"),
    "no stamp": lambda m: m,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("label", sorted(OWNER_FRAMES))
async def test_frames_for_the_owner_reach_the_handlers_they_reached_before_the_rule(node, label):
    with_the_rule, replies = await sweep(node, OWNER_FRAMES[label])
    with node.monkeypatch.context() as without:
        without.setattr(hub, "_non_owner_relay_refusal", lambda message, principal: None)
        without_the_rule, replies_without = await sweep(node, OWNER_FRAMES[label])

    assert with_the_rule == without_the_rule
    assert replies == replies_without
    assert "app_ingest" in with_the_rule and "query" in with_the_rule and "llm_generation" in with_the_rule


@pytest.mark.asyncio
async def test_what_each_owner_side_class_reaches(node):
    """Pinned by the dispatcher's older gates, so "as before" is not only "as without the new function"."""
    everything = sorted(HANDLERS)
    owner_app, _ = await sweep(node, OWNER_FRAMES["the owner's app"])
    capture, _ = await sweep(node, OWNER_FRAMES["a capture app's write for the owner"])
    outside, _ = await sweep(node, OWNER_FRAMES["the owner's outside client"])
    unstamped, _ = await sweep(node, OWNER_FRAMES["no stamp"])
    by_hand, _ = await sweep(node, lambda m: m,
                             dispatch=lambda m: hub.handle_control_plane_request(m, principal=RELAY_PRINCIPAL))

    assert owner_app == everything and capture == everything
    assert outside == reachable_by_a_third_party()
    assert set(SHARE_DOORS) < set(outside) and len(outside) > 100
    # No stamp at all is the control plane's relay deferral, exactly as before (not this finding).
    assert unstamped == by_hand
    assert unstamped == sorted(name for name in HANDLERS
                               if name not in OWNER_ONLY_MESSAGE_TYPES and not name.startswith("signal_"))


@pytest.mark.asyncio
async def test_the_share_door_types_keep_their_own_refusal_in_the_dispatcher(monkeypatch, tmp_path):
    """The real handlers: the share doors answer only at the socket's own gate (control_plane_client.py), so the
    registry's handlers for them refuse, and the allow-list lets a non-owner's frame get that far and no further."""
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", base64.b64encode(KEY.public_key().public_bytes_raw()).decode("ascii"))
    conn = engine_database(tmp_path / "node.db")
    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)
    for name in sorted(SHARE_DOORS):
        reply = await hub.dispatch_relay_message(stamped({"id": "door-" + name, "type": name, "payload": {}},
                                                         cls=THIRD_PARTY, acting=SOMEONE_ELSE))
        assert reply == {"id": "door-" + name, "status": "error", "code": 403, "error": "permission_denied"}
    conn.close()


@pytest.mark.asyncio
async def test_a_local_third_party_is_not_a_relay_caller(node):
    """The local door's shared-key client carries no acting user and never came over the relay: the rule is the
    relay's, and the owner's own local clients keep reaching what the older gates leave them."""
    local = Principal(cls=THIRD_PARTY, channel="local_http", client_id="claude_desktop")
    reached, _ = await sweep(node, lambda m: m,
                             dispatch=lambda m: hub.handle_control_plane_request(m, principal=local))
    assert reached == reachable_by_a_third_party()


# --- M1 --------------------------------------------------------------------------------------------------------

def _tampered(message):
    out = stamped(message, cls=THIRD_PARTY, acting=OWNER)
    out[STAMP_FIELD]["cls"] = OWNER_APP                     # promoted after signing
    return out


def _replayed(message):
    donor = stamped({"id": "another-frame", "type": message["type"], "payload": {}}, cls=OWNER_APP, acting=OWNER)
    return {**message, STAMP_FIELD: donor[STAMP_FIELD]}


def _without(field):
    def make(message):
        out = stamped(message, cls=OWNER_APP, acting=OWNER)
        del out[STAMP_FIELD][field]
        return out
    return make


def _with(field, value):
    def make(message):
        out = stamped(message, cls=OWNER_APP, acting=OWNER)
        out[STAMP_FIELD][field] = value
        return out
    return make


#: Each claims the most it can (the owner's app, acting for the owner) and does not verify.
UNVERIFIABLE = {
    "expired": lambda m: stamped(m, cls=OWNER_APP, acting=OWNER, iat=time.time() - 300, exp=time.time() - 10),
    "issued in the future": lambda m: stamped(m, cls=OWNER_APP, acting=OWNER, iat=time.time() + 600,
                                              exp=time.time() + 700),
    "lives too long": lambda m: stamped(m, cls=OWNER_APP, acting=OWNER, exp=time.time() + 86_400),
    "signed by a key this node does not hold": lambda m: stamped(m, cls=OWNER_APP, acting=OWNER, key=ANOTHER_KEY),
    "a third party signed by a key this node does not hold": lambda m: stamped(m, cls=THIRD_PARTY, acting=OWNER,
                                                                               key=ANOTHER_KEY),
    "a class this node does not know": lambda m: stamped(m, cls="grantee", acting=OWNER),
    "a class this node does not know, another spelling": lambda m: stamped(m, cls="root", acting=OWNER),
    "changed after signing": _tampered,
    "lifted from another frame": _replayed,
    "no signature": _without("sig"),
    "a signature that is not base64": _with("sig", "not base64 !"),
    "a signature of the wrong length": _with("sig", base64.b64encode(b"short").decode("ascii")),
    "no expiry": _without("exp"),
    "an expiry that is not a number": _with("exp", "soon"),
    "an empty stamp": lambda m: {**m, STAMP_FIELD: {}},
    "a stamp that is a string": lambda m: {**m, STAMP_FIELD: "owner_app"},
    "a stamp that is a list": lambda m: {**m, STAMP_FIELD: [1, 2, 3]},
    "a stamp that is null": lambda m: {**m, STAMP_FIELD: None},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("label", sorted(UNVERIFIABLE))
async def test_a_stamp_that_does_not_verify_reaches_nothing_a_non_owner_third_party_could_not(node, label):
    verified_third_party, _ = await sweep(node, lambda m: stamped(m, cls=THIRD_PARTY, acting=SOMEONE_ELSE))

    reached, replies = await sweep(node, UNVERIFIABLE[label])

    assert set(reached) <= set(verified_third_party) == set(SHARE_DOORS)
    for name, reply in replies.items():
        if name not in SHARE_DOORS:
            assert reply == refusal("frame-" + name), name
    assert all(cls == THIRD_PARTY for _name, cls in node.reached)      # never the relay deferral, never the owner


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", [OWNER_APP, THIRD_PARTY, "owner_automation"])
async def test_a_node_that_pinned_no_key_does_not_read_a_stamp_as_no_stamp(node, claim):
    """"A key the node does not hold" includes holding none: the stamp is there and cannot be checked."""
    from topos import relay_stamp
    node.monkeypatch.delenv("TOPOS_CP_STAMP_PUBKEY")
    node.monkeypatch.setattr(relay_stamp, "_load_public_key_bytes", lambda: None)

    reached, replies = await sweep(node, lambda m: stamped(m, cls=claim, acting=OWNER))

    assert reached == sorted(SHARE_DOORS)
    assert replies["query"] == refusal("frame-query") and replies["get_messages"] == refusal("frame-get_messages")


@pytest.mark.asyncio
async def test_the_inspection_tools_stay_shut_to_a_stamp_that_does_not_verify(node):
    """The review's own case: a correctly signed stamp of class ``grantee`` used to resolve to the relay deferral
    and reach ten types a verified third party cannot, the row and metadata inspection tools."""
    _reached, replies = await sweep(node, lambda m: stamped(m, cls="grantee", acting=SOMEONE_ELSE))
    for name in sorted(hub.LEGACY_INSPECTION_TYPES):
        assert replies[name] == refusal("frame-" + name)


@pytest.mark.asyncio
async def test_a_frame_with_no_stamp_field_is_the_relay_deferral_as_before(node):
    reached, _ = await sweep(node, lambda m: m)
    assert {cls for _name, cls in node.reached} == {"cp_relay"}
    assert "get_messages" in reached and "query" in reached and len(reached) > len(reachable_by_a_third_party())
