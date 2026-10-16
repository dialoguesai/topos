"""The dangerous-door split on the owner socket (CONTRACTS §1.6, PLAN §2.4, spike P0.11; lane L2).

protects: with ``TOPOS_OWNER_DOOR_SPLIT`` on, a dangerous mutation that arrives on the REAL owner socket from an
unattested peer is refused ``owner_confirmation_required`` ("This change needs the Topos app."), before the
handler runs. The same call is served when it carries a verified relay stamp (the web app, unchanged), when the
socket peer is an attested app process, when it presents a live owner-confirmation token the CLI wrote after
asking the human, or when it is the web app's dev proxy and the node was started with
``TOPOS_DEV_PROXY_ALLOWED=1``. In-process principals stamped ``channel="uds"`` (every HTTP route, the node's own
loops, 84 test files) pass by construction: they are not a socket peer. Reads are untouched. ``source_sync`` and
``post_source_install`` for the three bundled transcript sources are not dangerous. With the flag off, everything
behaves as today.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import stat
import time
from typing import Any, Dict, List

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import topos.core.handlers as hub
from topos import uds
from topos.core.handlers import handle_control_plane_request
from topos.core.handlers.registry import HANDLERS, OWNER_ONLY_MESSAGE_TYPES
from topos.principal import OWNER_APP, THIRD_PARTY, Principal
from topos.relay_stamp import STAMP_FIELD, canonical_signing_payload

OWNER = Principal(cls=OWNER_APP, channel="uds")
OWNER_AGENT = Principal(cls="owner_agent", channel="uds", client_id="agent-claude-code")
TPK_CLIENT = Principal(cls=THIRD_PARTY, channel="local_http", client_id="claude-desktop")

REFUSAL = {"status": "error", "code": 403, "error": "owner_confirmation_required",
           "message": "This change needs the Topos app."}

KEY = Ed25519PrivateKey.generate()
PUB_B64 = base64.b64encode(KEY.public_key().public_bytes_raw()).decode()

#: A sample of the dangerous set, each named by the contract or the spike, used where the full matrix is overkill.
SAMPLE = ("permissions_v2_mutate", "signal_blackhole_entity", "create_routine", "delete_home_chat_session",
          "ollama_install", "post_source_scrub", "truth_seed_fact", "put_signal_extraction_config")
#: Reads that the split must leave exactly as they are, on the socket.
READS = ("query", "aggregate", "get_sources", "get_table_rows", "list_database_tables", "signal_list_entities",
         "signal_get_entity", "permissions_v2_status", "mcp_client_list", "get_database_explorer_summary",
         "blackhole_status", "blackhole_check_text", "pooled_scope_backfill_dry_run", "signal_data_health")
TRANSCRIPT_SOURCES = ("claude_code_sessions", "codex_sessions", "cursor_sessions")


def _dangerous() -> List[str]:
    return sorted(hub.DANGEROUS_TYPES)


# ----------------------------------------------------------------------------- fixtures


@pytest.fixture()
def split_on(monkeypatch):
    monkeypatch.setenv("TOPOS_OWNER_DOOR_SPLIT", "1")


@pytest.fixture()
def split_off(monkeypatch):
    monkeypatch.setenv("TOPOS_OWNER_DOOR_SPLIT", "0")


@pytest.fixture()
def socket_dir(tmp_path, monkeypatch):
    """The owner socket's directory for this test: the confirmation token lives next to the socket."""
    monkeypatch.setenv("TOPOS_UDS_PATH", str(tmp_path / "engine.sock"))
    monkeypatch.delenv("TOPOS_OWNER_DOOR_TEAM_IDS", raising=False)
    monkeypatch.delenv("TOPOS_UDS_TEAM_IDS", raising=False)
    monkeypatch.delenv("TOPOS_DEV_PROXY_ALLOWED", raising=False)
    return tmp_path


@pytest.fixture()
def probe(monkeypatch):
    seen: List[str] = []

    def install(msg_type: str) -> List[str]:
        assert msg_type in HANDLERS, msg_type

        async def handler(message: Dict[str, Any]) -> Dict[str, Any]:
            seen.append(msg_type)
            return {"id": message.get("id"), "status": "ok", "payload": {"probe": msg_type}}

        monkeypatch.setitem(HANDLERS, msg_type, handler)
        return seen

    return install


@pytest.fixture()
def unbound(monkeypatch):
    from topos.permissions_v2 import switches

    monkeypatch.setattr(switches, "is_bound", lambda *a, **k: False)


UNATTESTED = uds.SocketPeer(pid=4242, executable="/usr/bin/curl")


def _message(msg_type: str, **payload: Any) -> Dict[str, Any]:
    return {"id": f"req-{msg_type}", "type": msg_type,
            "payload": {"source_id": "imessage", "table_name": "entities", **payload}}


async def _on_socket(msg_type: str, peer: uds.SocketPeer, principal: Principal = OWNER, **payload: Any):
    with uds.as_socket_peer(peer):
        return await handle_control_plane_request(_message(msg_type, **payload), principal=principal)


def _presenting(token: str) -> uds.SocketPeer:
    """What the socket wrapper builds for a request presenting ``token`` in X-Topos-Owner-Confirm: the token is
    verified (and consumed) once, where the peer is built."""
    return uds.socket_peer_from_scope({"type": "http", "client": ("uds", 4242),
                                       "headers": [(b"x-topos-owner-confirm", token.encode())]})


def _refused(out: Dict[str, Any], msg_type: str) -> None:
    assert out == {"id": f"req-{msg_type}", **REFUSAL}, out


def _stamped(message: Dict[str, Any], *, cls: str, acting_user: str = "") -> Dict[str, Any]:
    now = time.time()
    stamp = {"v": 1, "cls": cls, "client_id": "web-app", "acting_user": acting_user, "iat": now, "exp": now + 120}
    payload = canonical_signing_payload(stamp, msg_id=message["id"], msg_type=message["type"])
    stamp["sig"] = base64.b64encode(KEY.sign(payload)).decode()
    return {**message, STAMP_FIELD: stamp}


# ----------------------------------------------------------------------------- the set


def test_the_dangerous_set_is_the_mutations_the_contract_and_the_spike_name() -> None:
    named = {
        # every owner_only mutation (derived from the registry)
        "permissions_v2_mutate", "permissions_v2_bind", "permissions_v2_identity_command", "truth_seed_fact",
        "delete_database_table", "delete_database_rows", "mcp_client_enroll", "mcp_client_revoke",
        "signal_blackhole_record", "signal_unblackhole_record", "source_sync", "signal_upload", "ingestion_reprocess",
        # signal_* mutations
        "signal_blackhole_entity", "signal_unblackhole_entity", "signal_exclude_entity", "signal_entity_merge",
        "signal_entity_split", "signal_update_brief", "signal_owner_override_object", "signal_set_worn_badge",
        # the ungated mutations P0.11 found
        "create_routine", "patch_routine", "delete_routine", "delete_routine_runs", "ollama_install",
        "ollama_pull_model", "delete_home_chat_session", "delete_filter_lab_all_data", "delete_jsonl_file",
        "put_sanitization_ollama_config", "put_signal_extraction_config",
        # source install / uninstall / scrub (CONTRACTS §1.6)
        "post_source_install", "patch_source_install", "delete_source_install", "post_source_scrub",
    }
    missing = named - hub.DANGEROUS_TYPES
    assert not missing, sorted(missing)


def test_reads_are_not_in_the_dangerous_set() -> None:
    reads = set(READS) | {"signal_list_blackholes", "signal_list_facts", "signal_search_vectors",
                          "permissions_v2_share_catalog", "permissions_v2_evidence_review_read",
                          "mcp_client_list_elevations", "ollama_list_models", "ollama_install_status",
                          "get_routine", "list_routines", "graph_cypher_query"}
    leaked = reads & hub.DANGEROUS_TYPES
    assert not leaked, sorted(leaked)


def test_every_owner_only_and_signal_type_is_decided_one_way_or_the_other() -> None:
    """A new owner_only or signal_* type is dangerous unless somebody lists it as a read: nothing falls through."""
    families = set(OWNER_ONLY_MESSAGE_TYPES) | {t for t in HANDLERS if t.startswith("signal_")}
    undecided = families - hub.DANGEROUS_TYPES - hub.OWNER_DOOR_READ_TYPES
    assert not undecided, sorted(undecided)
    assert not (hub.OWNER_DOOR_READ_TYPES & hub.DANGEROUS_TYPES)
    assert hub.OWNER_DOOR_READ_TYPES <= set(HANDLERS), sorted(hub.OWNER_DOOR_READ_TYPES - set(HANDLERS))
    assert hub.DANGEROUS_TYPES <= set(HANDLERS), sorted(hub.DANGEROUS_TYPES - set(HANDLERS))


def test_every_delete_type_is_dangerous() -> None:
    deletes = {t for t in HANDLERS if t.startswith("delete_")}
    assert deletes and deletes <= hub.DANGEROUS_TYPES, sorted(deletes - hub.DANGEROUS_TYPES)


def test_the_exemption_list_ships_empty_and_the_transcript_ids_are_literal() -> None:
    assert hub.OWNER_DOOR_SPLIT_EXEMPT == frozenset()
    assert hub.TRANSCRIPT_SOURCE_IDS == frozenset({"claude_code_sessions", "codex_sessions", "cursor_sessions"})
    assert hub.OWNER_CONFIRMATION_MESSAGE == "This change needs the Topos app."


def test_the_flag_defaults_on(monkeypatch) -> None:
    monkeypatch.delenv("TOPOS_OWNER_DOOR_SPLIT", raising=False)
    assert hub.owner_door_split_enabled() is True
    monkeypatch.setenv("TOPOS_OWNER_DOOR_SPLIT", "0")
    assert hub.owner_door_split_enabled() is False


# ----------------------------------------------------------------------------- refused


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_type", _dangerous())
async def test_an_unattested_socket_peer_is_refused_on_every_dangerous_type(split_on, socket_dir, probe, msg_type):
    seen = probe(msg_type)

    out = await _on_socket(msg_type, UNATTESTED)

    _refused(out, msg_type)
    assert seen == [], f"an unattested socket peer reached {msg_type}"


@pytest.mark.asyncio
async def test_the_owner_agent_hears_its_own_refusal_first(split_on, socket_dir, probe) -> None:
    seen = probe("create_routine")
    out = await _on_socket("create_routine", UNATTESTED, principal=OWNER_AGENT)
    assert out["error"] == "owner_agent_not_allowed" and seen == []


@pytest.mark.asyncio
async def test_a_third_party_still_hears_owner_mode_required(split_on, socket_dir, probe) -> None:
    """Precedence unchanged: the class gates come first; the door split only ever speaks to the owner's socket."""
    seen = probe("permissions_v2_mutate")
    out = await _on_socket("permissions_v2_mutate", UNATTESTED, principal=TPK_CLIENT)
    assert out == {"id": "req-permissions_v2_mutate", "status": "error", "code": 403, "error": "owner_mode_required"}
    assert seen == []


# ----------------------------------------------------------------------------- served


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_type", SAMPLE)
async def test_a_verified_owner_app_relay_stamp_is_served(split_on, socket_dir, unbound, probe, monkeypatch, msg_type):
    """The web app, through the control plane: unchanged. The real relay entry point, a real signed stamp."""
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", PUB_B64)
    seen = probe(msg_type)

    out = await hub.dispatch_relay_message(_stamped(_message(msg_type), cls=OWNER_APP))

    assert seen == [msg_type], out
    assert out["status"] == "ok"


@pytest.mark.asyncio
async def test_a_verified_owner_automation_stamp_is_as_before(split_on, socket_dir, unbound, probe, monkeypatch):
    """The routine lane: served on a routine door it reaches today, refused on an owner_only door as today."""
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", PUB_B64)
    seen = probe("create_routine_run")
    out = await hub.dispatch_relay_message(_stamped(_message("create_routine_run"), cls="owner_automation"))
    assert seen == ["create_routine_run"] and out["status"] == "ok"

    probe("permissions_v2_mutate")
    out = await hub.dispatch_relay_message(_stamped(_message("permissions_v2_mutate"), cls="owner_automation"))
    assert out["error"] == "owner_mode_required"


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_type", SAMPLE)
async def test_an_in_process_uds_principal_passes_by_construction(split_on, socket_dir, probe, msg_type) -> None:
    """No socket peer: what every HTTP route, the node's own loops and 84 test files mint in-process."""
    seen = probe(msg_type)
    assert uds.current_socket_peer() is None

    out = await handle_control_plane_request(_message(msg_type), principal=OWNER)

    assert seen == [msg_type], out


@pytest.mark.asyncio
async def test_an_attested_app_peer_is_served(split_on, socket_dir, probe, monkeypatch) -> None:
    monkeypatch.setenv("TOPOS_OWNER_DOOR_TEAM_IDS", "TEAMAPP01")
    seen = probe("permissions_v2_mutate")

    app_peer = uds.SocketPeer(pid=515, executable="/Applications/Topos.app/Contents/MacOS/Topos", team_id="TEAMAPP01")
    out = await _on_socket("permissions_v2_mutate", app_peer)

    assert seen == ["permissions_v2_mutate"], out


@pytest.mark.asyncio
async def test_a_peer_of_another_team_or_with_no_allowlist_is_refused(split_on, socket_dir, probe, monkeypatch):
    seen = probe("permissions_v2_mutate")
    other = uds.SocketPeer(pid=515, executable="/Applications/Other.app/Contents/MacOS/Other", team_id="TEAMOTHER")

    monkeypatch.setenv("TOPOS_OWNER_DOOR_TEAM_IDS", "TEAMAPP01")
    _refused(await _on_socket("permissions_v2_mutate", other), "permissions_v2_mutate")

    monkeypatch.delenv("TOPOS_OWNER_DOOR_TEAM_IDS")        # no allowlist at all: the leg is inert, never open
    same_team = uds.SocketPeer(pid=515, executable="/Applications/Topos.app/Contents/MacOS/Topos", team_id="TEAMAPP01")
    _refused(await _on_socket("permissions_v2_mutate", same_team), "permissions_v2_mutate")
    assert seen == []


@pytest.mark.asyncio
async def test_a_live_confirmation_token_is_served_once(split_on, socket_dir, probe) -> None:
    """The CLI asked the human, wrote the token (0600, next to the socket) and presents it: one token, one change."""
    seen = probe("delete_home_chat_session")
    token = uds.write_owner_confirmation()
    path = uds.owner_confirmation_path()
    assert path == socket_dir / "owner-confirm.token" and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(token) == 64

    out = await _on_socket("delete_home_chat_session", _presenting(token))
    assert seen == ["delete_home_chat_session"], out
    assert not path.exists(), "a used confirmation is consumed"

    _refused(await _on_socket("delete_home_chat_session", _presenting(token)), "delete_home_chat_session")
    assert seen == ["delete_home_chat_session"]


@pytest.mark.asyncio
async def test_a_wrong_expired_loose_or_missing_token_is_refused(split_on, socket_dir, probe) -> None:
    seen = probe("delete_home_chat_session")
    path = uds.owner_confirmation_path()

    presented = _presenting

    # no file at all
    _refused(await _on_socket("delete_home_chat_session", presented("a" * 64)), "delete_home_chat_session")
    # the wrong token
    token = uds.write_owner_confirmation()
    _refused(await _on_socket("delete_home_chat_session", presented("b" * 64)), "delete_home_chat_session")
    assert path.exists(), "a wrong token does not consume the owner's confirmation"
    # expired
    path.write_text(json.dumps({"token": token, "expires_at": time.time() - 1}))
    os.chmod(path, 0o600)
    _refused(await _on_socket("delete_home_chat_session", presented(token)), "delete_home_chat_session")
    # a life over the cap: a file that promises more than OWNER_CONFIRM_MAX_LIFE_S is no confirmation
    path.write_text(json.dumps({"token": token, "expires_at": time.time() + uds.OWNER_CONFIRM_MAX_LIFE_S + 60}))
    os.chmod(path, 0o600)
    _refused(await _on_socket("delete_home_chat_session", presented(token)), "delete_home_chat_session")
    # group/other readable
    path.write_text(json.dumps({"token": token, "expires_at": time.time() + 60}))
    os.chmod(path, 0o644)
    _refused(await _on_socket("delete_home_chat_session", presented(token)), "delete_home_chat_session")
    # malformed
    path.write_text("not json")
    os.chmod(path, 0o600)
    _refused(await _on_socket("delete_home_chat_session", presented(token)), "delete_home_chat_session")
    assert seen == []


def test_verify_owner_confirmation_is_strict_about_its_inputs(socket_dir) -> None:
    assert uds.verify_owner_confirmation("") is False
    assert uds.verify_owner_confirmation(None) is False  # type: ignore[arg-type]
    token = uds.write_owner_confirmation(ttl_s=10_000)      # asked for more than the cap: clamped, still valid
    assert uds.verify_owner_confirmation(token, consume=False) is True
    assert uds.verify_owner_confirmation(token, consume=False) is True
    assert uds.verify_owner_confirmation(token) is True
    assert uds.verify_owner_confirmation(token) is False     # consumed


@pytest.mark.asyncio
async def test_the_dev_proxy_header_alone_is_refused(split_on, socket_dir, probe) -> None:
    seen = probe("mcp_client_enroll")
    _refused(await _on_socket("mcp_client_enroll", uds.SocketPeer(pid=4242, executable="/usr/bin/node", dev_proxy=True)),
             "mcp_client_enroll")
    assert seen == []


@pytest.mark.asyncio
async def test_the_dev_proxy_is_served_only_when_the_node_allows_it(split_on, socket_dir, probe, monkeypatch) -> None:
    monkeypatch.setenv("TOPOS_DEV_PROXY_ALLOWED", "1")
    seen = probe("mcp_client_enroll")

    out = await _on_socket("mcp_client_enroll", uds.SocketPeer(pid=4242, executable="/usr/bin/node", dev_proxy=True))
    assert seen == ["mcp_client_enroll"], out

    # the env alone, without the header, opens nothing
    _refused(await _on_socket("mcp_client_enroll", UNATTESTED), "mcp_client_enroll")
    assert seen == ["mcp_client_enroll"]


# ----------------------------------------------------------------------------- untouched


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_type", SAMPLE)
async def test_with_the_flag_off_everything_behaves_as_today(split_off, socket_dir, probe, msg_type) -> None:
    seen = probe(msg_type)
    out = await _on_socket(msg_type, UNATTESTED)
    assert seen == [msg_type], out


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_type", READS)
async def test_reads_are_untouched_on_the_socket(split_on, socket_dir, probe, msg_type) -> None:
    seen = probe(msg_type)
    out = await _on_socket(msg_type, UNATTESTED)
    assert seen == [msg_type], out


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id", TRANSCRIPT_SOURCES)
async def test_a_bundled_transcript_source_syncs_and_installs_with_the_flag_on(split_on, socket_dir, probe, source_id):
    """The hooks and the CLI sync the owner's own transcripts into his own node: not a dangerous change."""
    for msg_type in ("source_sync", "post_source_install", "patch_source_install"):
        seen = probe(msg_type)
        seen.clear()
        out = await _on_socket(msg_type, UNATTESTED, source_id=source_id)
        assert seen == [msg_type], out


@pytest.mark.asyncio
async def test_any_other_source_sync_is_still_dangerous(split_on, socket_dir, probe) -> None:
    seen = probe("source_sync")
    _refused(await _on_socket("source_sync", UNATTESTED, source_id="imessage"), "source_sync")
    _refused(await _on_socket("source_sync", UNATTESTED, source_id=""), "source_sync")
    assert seen == []


@pytest.mark.asyncio
async def test_the_exemption_list_is_honoured(split_on, socket_dir, probe, monkeypatch) -> None:
    seen = probe("create_routine")
    monkeypatch.setattr(hub, "OWNER_DOOR_SPLIT_EXEMPT", frozenset({"create_routine"}))
    out = await _on_socket("create_routine", UNATTESTED)
    assert seen == ["create_routine"], out


# ----------------------------------------------------------------------------- the socket layer


@pytest.mark.asyncio
async def test_the_wrapper_builds_the_peer_from_the_connection_and_the_headers(socket_dir) -> None:
    """What the socket server's ASGI wrapper hands the dispatcher: the accepted connection's process, plus the two
    headers the legs read. Reset after the request, like the transport marker."""
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        pid = uds._register_peer_connection(a)
        assert pid is None or pid == os.getpid()
        seen: Dict[str, Any] = {}

        async def inner(scope, receive, send):
            seen["peer"] = uds.current_socket_peer()
            seen["transport"] = uds.current_transport()

        scope = {"type": "http", "client": ("uds", pid or 0),
                 "headers": [(b"host", b"topos-owner"), (b"x-topos-dev-proxy", b"1"), (b"x-topos-owner-confirm", b"tok")]}
        await uds.UDSChannelApp(inner)(scope, None, None)
        peer = seen["peer"]
        assert seen["transport"] == "uds"
        assert isinstance(peer, uds.SocketPeer)
        assert peer.pid == pid and peer.dev_proxy is True and peer.confirmation == "tok"
        assert peer.confirmed is False, "a token with no file behind it confirms nothing"
        if pid is not None:
            assert peer.executable, "the registered connection names the peer's executable"
        assert uds.current_socket_peer() is None and uds.current_transport() == "tcp"
    finally:
        uds._release_peer_connection(pid if "pid" in dir() else None)
        a.close()
        b.close()


@pytest.mark.asyncio
async def test_a_request_without_a_connection_record_is_an_unattested_peer(socket_dir, monkeypatch) -> None:
    monkeypatch.setenv("TOPOS_OWNER_DOOR_TEAM_IDS", "TEAMAPP01")
    seen: Dict[str, Any] = {}

    async def inner(scope, receive, send):
        seen["peer"] = uds.current_socket_peer()

    await uds.UDSChannelApp(inner)({"type": "http", "client": None, "headers": []}, None, None)
    peer = seen["peer"]
    assert isinstance(peer, uds.SocketPeer) and peer.pid is None and peer.executable is None
    assert peer.attested(uds.door_team_ids()) is False
    assert peer.dev_proxy is False and peer.confirmation == ""


def test_the_door_allowlist_does_not_switch_global_attestation_on(socket_dir, monkeypatch) -> None:
    """P4.1 for this subset only: naming the app's team for the doors must not close the socket to curl and uv."""
    monkeypatch.setenv("TOPOS_OWNER_DOOR_TEAM_IDS", "TEAMAPP01")
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        assert uds.peer_admitted(a) is True          # this unsigned interpreter is still admitted at accept
    finally:
        a.close()
        b.close()
    assert uds.door_team_ids() == frozenset({"TEAMAPP01"})
    monkeypatch.delenv("TOPOS_OWNER_DOOR_TEAM_IDS")
    monkeypatch.setenv("TOPOS_UDS_TEAM_IDS", "TEAMGLOBAL")
    assert uds.door_team_ids() == frozenset({"TEAMGLOBAL"})    # a global P4.1 allowlist counts for the doors too
