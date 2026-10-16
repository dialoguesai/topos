"""`key_rotated` relay handler (CONTRACTS §3 key rotation; L8 payload, exact):
`{"topos_id", "topos_key": "tpn_<40 hex>", "grace_seconds", "revoke_at"}` arrives once over the OLD key's socket.
The node replies ok, rewrites TOPOS_KEY in ~/.topos/.env (0600) via _save_topos_key, reconnects with the new key
before revoke_at, refuses unstamped and third-party frames, never logs either key, and is idempotent."""

from __future__ import annotations

import asyncio
import logging
import secrets
import stat
from pathlib import Path

import pytest

from topos.core.handlers import device, dispatch_relay_message, handle_control_plane_request
from topos.principal import CP_RELAY, OWNER_APP, THIRD_PARTY, Principal

OLD = "tpn_" + secrets.token_hex(20)
NEW = "tpn_" + secrets.token_hex(20)


class _FakeClient:
    def __init__(self, api_key: str, connects: bool = True):
        self.api_key = api_key
        self.calls: list[str] = []
        self._connects = connects
        self._started = False

    async def stop(self) -> None:
        self.calls.append("stop")
        self._started = False

    def start(self) -> None:
        self.calls.append("start")
        self._started = True

    def get_connection_status(self) -> dict:
        return {"connected": bool(self._started and self._connects), "state": "connected" if self._started and self._connects else "backing_off"}


@pytest.fixture
def env_path(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / ".topos" / ".env"
    path.parent.mkdir()
    path.write_text(f"TOPOS_KEY={OLD}\nTOPOS_OWNER_KEY=ok_{secrets.token_hex(24)}\n")
    path.chmod(0o600)
    monkeypatch.setenv("TOPOS_ENV_FILE", str(path))
    monkeypatch.setenv("TOPOS_KEY", OLD)
    device._ROTATION_STATE.clear()
    monkeypatch.setattr(device, "_client", lambda: _FakeClient(OLD))
    return path


def _frame(key: str = NEW, req_id: str = "r1") -> dict:
    return {
        "id": req_id,
        "type": "key_rotated",
        "payload": {"topos_id": "topos_0123", "topos_key": key, "grace_seconds": 600.0,
                    "revoke_at": "2026-10-16T00:10:00+00:00"},
    }


def _as_owner(frame: dict):
    return asyncio.run(handle_control_plane_request(frame, principal=Principal(cls=OWNER_APP, channel="cp_relay")))


def test_rewrites_env_replies_ok_schedules_reconnect_and_logs_no_key(env_path: Path, monkeypatch, caplog):
    scheduled = []
    monkeypatch.setattr(device, "_schedule_reconnect", lambda **kw: scheduled.append(kw))
    with caplog.at_level(logging.DEBUG, logger="topos"):
        out = _as_owner(_frame())
    assert out == {"id": "r1", "status": "ok"}
    text = env_path.read_text()
    assert f"TOPOS_KEY={NEW}\n" in text and OLD not in text
    assert "TOPOS_OWNER_KEY=ok_" in text  # the rest of the file is kept
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert len(scheduled) == 1
    assert scheduled[0]["new_key"] == NEW and scheduled[0]["previous_key"] == OLD
    assert scheduled[0]["revoke_at"] == "2026-10-16T00:10:00+00:00"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert NEW not in logged and OLD not in logged
    assert "key_rotated" in logged


def test_unstamped_frame_is_refused_and_env_untouched(env_path: Path, monkeypatch):
    monkeypatch.setattr(device, "_schedule_reconnect", lambda **kw: (_ for _ in ()).throw(AssertionError("must not reconnect")))
    before = env_path.read_text()
    out = asyncio.run(dispatch_relay_message(_frame()))  # no relay_stamp field → the unstamped deferral class
    assert out == {"id": "r1", "status": "error", "code": 403, "error": "owner_mode_required"}
    assert env_path.read_text() == before


@pytest.mark.parametrize("principal", [
    Principal(cls=THIRD_PARTY, channel="cp_relay"),
    Principal(cls=CP_RELAY, channel="cp_relay"),
    Principal(cls=OWNER_APP, channel="local_http"),
])
def test_third_party_deferral_and_local_principals_are_refused(env_path: Path, monkeypatch, principal):
    monkeypatch.setattr(device, "_schedule_reconnect", lambda **kw: (_ for _ in ()).throw(AssertionError("must not reconnect")))
    before = env_path.read_text()
    out = asyncio.run(handle_control_plane_request(_frame(), principal=principal))
    assert out["status"] == "error" and out["code"] == 403
    assert env_path.read_text() == before


def test_second_rotation_to_the_same_key_is_idempotent(env_path: Path, monkeypatch):
    scheduled = []
    monkeypatch.setattr(device, "_schedule_reconnect", lambda **kw: scheduled.append(kw))
    assert _as_owner(_frame())["status"] == "ok"
    first = env_path.read_text()
    monkeypatch.setattr(device, "_client", lambda: _FakeClient(NEW))
    assert _as_owner(_frame(req_id="r2")) == {"id": "r2", "status": "ok"}
    assert env_path.read_text() == first
    assert len(scheduled) == 1


def test_malformed_key_is_refused_without_a_write(env_path: Path, monkeypatch):
    monkeypatch.setattr(device, "_schedule_reconnect", lambda **kw: (_ for _ in ()).throw(AssertionError("must not reconnect")))
    before = env_path.read_text()
    out = _as_owner(_frame(key="tpk_" + "a" * 40))
    assert out["status"] == "error" and out["code"] == 422
    assert "tpk_" not in out["error"]
    assert env_path.read_text() == before


def test_reconnect_switches_the_client_to_the_new_key(monkeypatch):
    client = _FakeClient(OLD)
    monkeypatch.setattr(device, "_sleep_async", lambda s: asyncio.sleep(0))
    asyncio.run(device._reconnect_with_new_key(client, new_key=NEW, previous_key=OLD,
                                                revoke_at="2999-01-01T00:00:00+00:00", wait_seconds=0.0))
    assert client.api_key == NEW
    assert client.calls == ["stop", "start"]
    assert device._ROTATION_STATE.get("previous_key") is None


def test_reconnect_falls_back_to_the_old_key_when_the_new_one_never_connects(monkeypatch, caplog):
    client = _FakeClient(OLD, connects=False)
    monkeypatch.setattr(device, "_sleep_async", lambda s: asyncio.sleep(0))
    with caplog.at_level(logging.INFO, logger="topos"):
        asyncio.run(device._reconnect_with_new_key(client, new_key=NEW, previous_key=OLD,
                                                    revoke_at="2999-01-01T00:00:00+00:00", wait_seconds=0.0))
    assert client.api_key == OLD
    assert client.calls == ["stop", "start", "stop", "start"]
    assert device._ROTATION_STATE.get("previous_key") == OLD
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert NEW not in logged and OLD not in logged
    assert "did not connect" in logged
