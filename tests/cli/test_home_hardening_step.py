"""The hardening upgrade step: ~/.topos 0700, data files 0600, idempotent; owner key persisted (security 13, J6)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from topos import owner_key
from topos.upgrades import STEP_KINDS, hardening, load_unreleased
from topos.upgrades.runner import DEFAULT_EXECUTORS


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


@pytest.fixture
def topos_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TOPOS_ENV_FILE", raising=False)
    monkeypatch.delenv("TOPOS_OWNER_KEY", raising=False)
    d = tmp_path / ".topos"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    for name in ("database.db", "database.db-wal", "database.db-shm", ".env", "pairing.json", "install-id"):
        p = d / name
        p.write_text("x")
        os.chmod(p, 0o644)
    (d / ".env").write_text("TOPOS_KEY=tpn_" + "a" * 40 + "\nTOPOS_OWNER_KEY=ok_" + "b" * 48 + "\n")
    logs = d / "logs"
    logs.mkdir(mode=0o755)
    os.chmod(logs, 0o755)
    for name in ("node.log", "node.log.1", "service.log"):
        (logs / name).write_text("line")
        os.chmod(logs / name, 0o644)
    profiles = d / "profiles"
    profiles.mkdir(mode=0o755)
    os.chmod(profiles, 0o755)
    archived = profiles / "work"
    archived.mkdir(mode=0o755)
    os.chmod(archived, 0o755)
    for name in ("database.db", "database.db-wal"):
        (archived / name).write_text("x")
        os.chmod(archived / name, 0o644)
    agents = d / "agents"
    agents.mkdir(mode=0o755)
    os.chmod(agents, 0o755)
    (agents / "claude-code.token").write_text("tpa_x")
    os.chmod(agents / "claude-code.token", 0o644)
    backups = d / "backups"
    backups.mkdir(mode=0o755)
    os.chmod(backups, 0o755)
    (backups / "database.db.pre-80").write_text("x")
    os.chmod(backups / "database.db.pre-80", 0o644)
    # A symlink out of the folder must never be followed.
    outside = tmp_path / "outside.db"
    outside.write_text("x")
    os.chmod(outside, 0o644)
    os.symlink(outside, d / "database.db.link")
    return d


def test_clamps_the_folder_and_every_data_file(topos_dir: Path):
    report = hardening.harden_topos_dir(topos_dir)
    assert _mode(topos_dir) == 0o700
    for rel in ("database.db", "database.db-wal", "database.db-shm", ".env", "pairing.json", "install-id",
                "logs/node.log", "logs/node.log.1", "logs/service.log",
                "profiles/work/database.db", "profiles/work/database.db-wal",
                "agents/claude-code.token", "backups/database.db.pre-80"):
        assert _mode(topos_dir / rel) == 0o600, rel
    for rel in ("logs", "profiles", "profiles/work", "agents", "backups"):
        assert _mode(topos_dir / rel) == 0o700, rel
    # The symlink target outside ~/.topos is untouched.
    assert _mode(topos_dir.parent / "outside.db") == 0o644
    assert report["changed"] == 19
    assert report["owner_key_persisted"] is True
    assert report["skipped"] == []


def test_second_run_changes_nothing(topos_dir: Path):
    hardening.harden_topos_dir(topos_dir)
    again = hardening.harden_topos_dir(topos_dir)
    assert again["changed"] == 0
    assert again["already_clamped"] == 19


def test_reports_a_missing_owner_key(topos_dir: Path):
    (topos_dir / ".env").write_text("TOPOS_KEY=tpn_" + "a" * 40 + "\n")
    report = hardening.harden_topos_dir(topos_dir)
    assert report["owner_key_persisted"] is False
    assert report["finding"] == (
        "owner key not persisted: ~/.topos/.env has no TOPOS_OWNER_KEY line, so the node runs in legacy "
        "mode (every direct-HTTP door open). Make ~/.topos writable and restart Topos."
    )


def test_missing_folder_is_a_noop(tmp_path: Path):
    report = hardening.harden_topos_dir(tmp_path / "nope")
    assert report["changed"] == 0 and report["skipped"] == ["missing"]


def test_the_report_names_no_secret(topos_dir: Path):
    report = hardening.harden_topos_dir(topos_dir)
    text = json.dumps(report)
    assert "tpn_" + "a" * 40 not in text and "ok_" + "b" * 48 not in text and "tpa_x" not in text


def test_owner_key_persisted_reads_the_file_not_the_process_env(tmp_path: Path, monkeypatch):
    env = tmp_path / ".env"
    monkeypatch.setenv("TOPOS_OWNER_KEY", "ok_from_env")
    env.write_text("TOPOS_KEY=tpn_x\n")
    assert owner_key.owner_key_persisted(env) is False
    env.write_text("TOPOS_KEY=tpn_x\nTOPOS_OWNER_KEY=\n")
    assert owner_key.owner_key_persisted(env) is False
    env.write_text("TOPOS_KEY=tpn_x\nTOPOS_OWNER_KEY=ok_" + "c" * 48 + "\n")
    assert owner_key.owner_key_persisted(env) is True
    assert owner_key.owner_key_persisted(tmp_path / "absent") is False


def test_manifest_declares_the_step_with_a_registered_executor():
    staging = load_unreleased()
    assert staging is not None
    step = next(s for s in staging["steps"] if s["id"] == "harden-home-permissions")
    assert step["kind"] == "local_hardening"
    assert "local_hardening" in STEP_KINDS
    assert DEFAULT_EXECUTORS["local_hardening"] is hardening.exec_local_hardening


def test_executor_clamps_the_home_it_is_given(topos_dir: Path, monkeypatch):
    detail = hardening.exec_local_hardening({"id": "harden-home-permissions", "kind": "local_hardening"}, conn=None)
    assert _mode(topos_dir) == 0o700
    assert _mode(topos_dir / "database.db") == 0o600
    assert detail["changed"] == 19
