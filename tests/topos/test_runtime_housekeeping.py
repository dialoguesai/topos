"""Node-start housekeeping: stale pytest tmp sweep.

Pins the safety contract: only OLD ``pytest-*`` roots under this user's tmp
are removed (age-gated so an in-flight run is never touched), files/symlinks
are skipped, disable knob works, and nothing ever raises.
"""

from __future__ import annotations

import os
import time

import pytest

from topos import runtime_housekeeping as hk


@pytest.fixture()
def fake_tmp(tmp_path, monkeypatch):
    """Point the sweeper at a synthetic pytest-of-<user> root."""
    monkeypatch.setattr(hk.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(hk.getpass, "getuser", lambda: "tester")
    root = tmp_path / "pytest-of-tester"
    root.mkdir()
    return root


def _make_root(root, name: str, *, age_hours: float, payload_bytes: int = 64):
    d = root / name
    d.mkdir()
    (d / "junk.db").write_bytes(b"x" * payload_bytes)
    old = time.time() - age_hours * 3600
    os.utime(d, (old, old))
    return d


def test_removes_only_stale_roots(fake_tmp):
    stale = _make_root(fake_tmp, "pytest-1", age_hours=48, payload_bytes=128)
    fresh = _make_root(fake_tmp, "pytest-2", age_hours=1)

    result = hk.sweep_stale_pytest_tmp(max_age_hours=24)

    assert result["removed"] == 1
    assert result["freed_bytes"] >= 128
    assert not stale.exists()
    assert fresh.exists()


def test_locked_root_from_killed_run_is_removed(fake_tmp):
    # pytest's own GC skips roots with a .lock; the sweeper must not.
    stale = _make_root(fake_tmp, "pytest-3", age_hours=48)
    (stale / ".lock").write_text("pid 12345")
    old = time.time() - 48 * 3600
    os.utime(stale, (old, old))

    assert hk.sweep_stale_pytest_tmp(max_age_hours=24)["removed"] == 1
    assert not stale.exists()


def test_plain_files_and_missing_root_are_safe(fake_tmp):
    (fake_tmp / "loose-file").write_text("keep me")
    result = hk.sweep_stale_pytest_tmp(max_age_hours=24)
    assert result["removed"] == 0
    assert (fake_tmp / "loose-file").exists()

    (fake_tmp / "loose-file").unlink()
    fake_tmp.rmdir()  # root gone entirely → sweep is a no-op, never raises
    assert hk.sweep_stale_pytest_tmp(max_age_hours=24) == {
        "removed": 0,
        "freed_bytes": 0,
        "skipped": 0,
    }


def test_zero_age_disables_sweep(fake_tmp):
    stale = _make_root(fake_tmp, "pytest-4", age_hours=48)
    assert hk.sweep_stale_pytest_tmp(max_age_hours=0)["removed"] == 0
    assert stale.exists()


def test_env_knob_disables_sweep(fake_tmp, monkeypatch):
    stale = _make_root(fake_tmp, "pytest-5", age_hours=48)
    monkeypatch.setenv("TOPOS_TMP_SWEEP_MAX_AGE_HOURS", "0")
    assert hk.sweep_stale_pytest_tmp()["removed"] == 0
    assert stale.exists()


def test_empty_parent_is_removed_after_sweep(fake_tmp):
    _make_root(fake_tmp, "pytest-6", age_hours=48)
    hk.sweep_stale_pytest_tmp(max_age_hours=24)
    assert not fake_tmp.exists()
