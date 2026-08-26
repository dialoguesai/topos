"""P4.1 (Team ID attestation) + dual-mint (owner-key self-mint on boot).

protects: attestation is permissive without an allowlist and fail-closed with
one; the node self-mints an owner key idempotently and never rewrites an
existing one, so an upgrade auto-activates the fabric without stranding the app.
"""
import os
import socket

import pytest

from topos import owner_key, uds


# ---------------------------------------------------------- P4.1 attestation
def test_attestation_permissive_without_allowlist(monkeypatch):
    monkeypatch.delenv("TOPOS_UDS_TEAM_IDS", raising=False)
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        assert uds.peer_admitted(a) is True  # this test process, unsigned, admitted
    finally:
        a.close()
        b.close()


def test_attestation_fail_closed_for_wrong_team(monkeypatch):
    monkeypatch.setenv("TOPOS_UDS_TEAM_IDS", "25AMARRV2F")  # the shell app's team
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        # The peer here is the unsigned pytest interpreter — not team 25AMARRV2F.
        assert uds.peer_admitted(a) is False
    finally:
        a.close()
        b.close()


def test_attestation_admits_matching_team(monkeypatch):
    """With the peer's actual team on the allowlist, it is admitted."""
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        pid = uds._peer_pid(a)
        exe = uds._pid_executable(pid) if pid else None
        team = uds._team_id_of(exe) if exe else None
        if not team:
            pytest.skip("peer interpreter is unsigned in this environment")
        monkeypatch.setenv("TOPOS_UDS_TEAM_IDS", team)
        assert uds.peer_admitted(a) is True
    finally:
        a.close()
        b.close()


def test_peer_pid_is_this_process():
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        assert uds._peer_pid(a) == os.getpid()
    finally:
        a.close()
        b.close()


# ------------------------------------------------------------- dual-mint
def test_self_mint_creates_key(tmp_path, monkeypatch):
    monkeypatch.delenv("TOPOS_OWNER_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("TOPOS_KEY=abc\n")
    key = owner_key.ensure_owner_key(env_path=env)
    assert key and key.startswith("ok_")
    assert f"TOPOS_OWNER_KEY={key}" in env.read_text()
    assert "TOPOS_KEY=abc" in env.read_text()  # existing content preserved
    assert oct(env.stat().st_mode)[-3:] == "600"


def test_self_mint_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.delenv("TOPOS_OWNER_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("TOPOS_OWNER_KEY=ok_existing_value\n")
    key = owner_key.ensure_owner_key(env_path=env)
    assert key == "ok_existing_value"  # never rewritten
    assert env.read_text().count("TOPOS_OWNER_KEY=") == 1


def test_process_env_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("TOPOS_OWNER_KEY", "ok_from_pairing")
    env = tmp_path / ".env"  # does not exist
    assert owner_key.ensure_owner_key(env_path=env) == "ok_from_pairing"
    assert not env.exists()  # nothing minted; pairing already provided it


def test_activates_enforcement_end_to_end(tmp_path, monkeypatch):
    """The point of the dual-mint: after self-mint, resolve_request_principal
    distinguishes owner from third-party where legacy mode could not."""
    import sqlite3

    from topos.auth import resolve_request_principal
    from topos.config.settings import settings
    from topos.principal import OWNER_APP, THIRD_PARTY
    from fastapi.security import HTTPAuthorizationCredentials

    monkeypatch.delenv("TOPOS_OWNER_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("TOPOS_KEY=legacy-shared\n")
    minted = owner_key.ensure_owner_key(env_path=env)
    monkeypatch.setattr(settings, "topos_key", "legacy-shared", raising=False)
    monkeypatch.setattr(settings, "topos_owner_key", minted, raising=False)

    def _c(t):
        return HTTPAuthorizationCredentials(scheme="Bearer", credentials=t)

    assert resolve_request_principal(_c(minted)).cls == OWNER_APP
    assert resolve_request_principal(_c("legacy-shared")).cls == THIRD_PARTY
