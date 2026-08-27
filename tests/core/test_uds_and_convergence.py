"""P4 (owner socket) + P5 (convergence): structure over secrets.

protects: only the UDS transport marker — settable solely by the socket
server's ASGI wrapper — mints owner_app without a bearer; TCP requests keep
every rule they have; the automation class is capped at `facts`; and the
stamp-key autopin is TOFU — it never overwrites an existing pin.
"""
import base64
import sqlite3

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from topos.auth import require_api_key, resolve_request_principal
from topos.principal import OWNER_APP, Principal
from topos.uds import UDSChannelApp, current_transport, _transport


def _creds(token):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token) if token else None


# ----------------------------------------------------------------- P4: UDS
def test_uds_transport_mints_owner_without_bearer():
    token = _transport.set("uds")
    try:
        p = resolve_request_principal(credentials=None)
        assert p is not None and p.cls == OWNER_APP and p.channel == "uds"
        require_api_key(credentials=None)  # no raise
    finally:
        _transport.reset(token)


def test_tcp_default_still_requires_bearer():
    assert current_transport() == "tcp"
    with pytest.raises(HTTPException):
        resolve_request_principal(credentials=None)
    with pytest.raises(HTTPException):
        require_api_key(credentials=None)


def test_headers_cannot_fake_the_transport():
    """The marker is a contextvar set only by the UDS server's wrapper — a TCP
    request has no field that reaches it. Garbage bearers on TCP still 401."""
    with pytest.raises(HTTPException):
        resolve_request_principal(credentials=_creds("uds"))
    with pytest.raises(HTTPException):
        resolve_request_principal(credentials=_creds("transport=uds"))


@pytest.mark.asyncio
async def test_wrapper_scopes_the_marker_per_request():
    seen = {}

    async def inner(scope, receive, send):
        seen["transport"] = current_transport()

    await UDSChannelApp(inner)({"type": "http"}, None, None)
    assert seen["transport"] == "uds"
    assert current_transport() == "tcp"  # reset after the request


# ------------------------------------------------- P5: automation cap
@pytest.fixture()
def conn():
    from topos.config.settings import ENGINE_CONFIG_KEY_PACKET_RESOLUTION
    from topos.core.handlers.common import set_engine_config_value

    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE engine_config (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    set_engine_config_value(c, ENGINE_CONFIG_KEY_PACKET_RESOLUTION, "facts_all")
    yield c
    c.close()


def test_owner_automation_capped_at_facts(conn, monkeypatch):
    from topos.query import packet_resolution as pr
    from topos.query.packet_resolution import effective_packet_resolution

    monkeypatch.setattr(
        pr, "primary_binding_locality",
        lambda _c: {"local": True, "provider": "ollama", "model": "m", "remote_engine_url": False},
    )
    info = effective_packet_resolution(
        conn, principal=Principal(cls="owner_automation", channel="cp_relay",
                                  client_id="routine_executor"),
        disclosure_tier="owner_raw",
    )
    assert info["effective"] == "facts"          # facts_all clamped
    assert info["reason"] == "automation_cap"    # and declared

    monkeypatch.setattr(
        pr, "primary_binding_locality",
        lambda _c: {"local": False, "provider": "openai", "model": "m", "remote_engine_url": False},
    )
    hosted = effective_packet_resolution(
        conn, principal=Principal(cls="owner_automation", channel="cp_relay"),
        disclosure_tier="owner_raw",
    )
    assert hosted["effective"] == "scores_only" and hosted["reason"] == "hosted_binding"


# ------------------------------------------------- P5: autopin (TOFU)
def test_autopin_never_overwrites_an_existing_pin(monkeypatch, tmp_path):
    import topos.relay_stamp as rs

    pin = tmp_path / "cp_stamp_key.pub"
    pin.write_text(base64.b64encode(b"\x01" * 32).decode())
    monkeypatch.setattr(rs, "_PINNED_KEY_PATH", str(pin))
    monkeypatch.delenv("TOPOS_CP_STAMP_PUBKEY", raising=False)

    called = {"n": 0}

    class _NoHttp:
        def get(self, *a, **k):
            called["n"] += 1
            raise AssertionError("network must not be touched when a pin exists")

    monkeypatch.setitem(__import__("sys").modules, "httpx", _NoHttp())
    assert rs.autopin_stamp_key() is False
    assert called["n"] == 0


def test_autopin_writes_a_valid_key(monkeypatch, tmp_path):
    import topos.relay_stamp as rs

    pin = tmp_path / "cp_stamp_key.pub"
    monkeypatch.setattr(rs, "_PINNED_KEY_PATH", str(pin))
    monkeypatch.delenv("TOPOS_CP_STAMP_PUBKEY", raising=False)
    monkeypatch.setattr(
        "topos.config.settings.settings.topos_control_plane_url",
        "wss://cp.example/ws/engine", raising=False,
    )
    good = base64.b64encode(b"\x02" * 32).decode()

    class _Resp:
        status_code = 200

        def json(self):
            return {"algorithm": "ed25519", "public_key_b64": good}

    class _Httpx:
        def get(self, url, timeout):
            assert url == "https://cp.example/v1/relay/stamp-public-key"
            return _Resp()

    monkeypatch.setitem(__import__("sys").modules, "httpx", _Httpx())
    assert rs.autopin_stamp_key() is True
    assert pin.read_text().strip() == good


def test_cp_base_derivation():
    from topos.relay_stamp import cp_http_base_from_ws_url

    assert cp_http_base_from_ws_url("wss://cp.logu3s.com/ws/engine") == "https://cp.logu3s.com"
    assert cp_http_base_from_ws_url("ws://localhost:8000/ws/engine") == "http://localhost:8000"
    assert cp_http_base_from_ws_url("https://not-ws.example") is None
    assert cp_http_base_from_ws_url("") is None


# ---- socket resilience: never clobber a live peer, always heal a dead one ---
# AF_UNIX paths are capped near 104 bytes, so these use a SHORT temp dir rather
# than pytest's tmp_path (which overflows it and fails to bind).
import contextlib
import shutil
import socket as _s
import tempfile
from pathlib import Path


@contextlib.contextmanager
def _short_sock_dir():
    d = tempfile.mkdtemp(dir="/tmp", prefix="tu")
    try:
        yield Path(d) / "e.sock"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_socket_is_live_distinguishes_file_from_listener():
    """A socket FILE proves nothing — only a connect does. This is what keeps
    us from unlinking a socket another live node owns (2026-08-26: the owner
    lane died silently exactly that way)."""
    from topos.uds import socket_is_live

    with _short_sock_dir() as p:
        assert socket_is_live(p) is False          # absent
        p.write_text("")                            # plain file, nobody serving
        assert socket_is_live(p) is False
        p.unlink()

        srv = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
        srv.bind(str(p))
        srv.listen(64)
        try:
            assert socket_is_live(p) is True         # a real listener
        finally:
            srv.close()
        assert socket_is_live(p) is False            # closed: file lingers, dead


def test_start_refuses_to_clobber_a_live_socket(monkeypatch):
    """Standing down beats severing another node's listener."""
    import topos.uds as uds_mod

    with _short_sock_dir() as p:
        monkeypatch.setenv("TOPOS_UDS_PATH", str(p))
        srv = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
        srv.bind(str(p))
        srv.listen(64)
        try:
            assert uds_mod.start_uds_server(object()) is None   # stands down
            assert uds_mod.socket_is_live(p) is True            # peer untouched
            assert p.exists()
        finally:
            srv.close()


def test_dead_socket_file_is_reclaimable(monkeypatch):
    """A dead inode must not block the lane: _bind_once clears it first."""
    import topos.uds as uds_mod

    with _short_sock_dir() as p:
        p.write_text("")  # stale file from a crashed run
        assert uds_mod.socket_is_live(p) is False
        monkeypatch.setattr(uds_mod, "socket_path", lambda: p)
        # Bind for real: uvicorn must be able to take the path back.
        server = uds_mod._bind_once(object(), p)
        assert server is not None or not p.exists(), (
            "a dead socket file must be unlinked so the lane can rebind"
        )
