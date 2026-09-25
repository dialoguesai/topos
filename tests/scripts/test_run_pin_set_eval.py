"""scripts/run_pin_set_eval.py grades the database the engine opens, or refuses.

protects: `AdapterFactory.create("local_database", db_path=--db)` first asks
`core.state.get_db_connection()` for the process handle, which opens AND MIGRATES
the database the settings resolve (TOPOS_DATABASE_PATH, else ~/.topos/database.db)
whatever --db names. Measured 2026-09-24 on 222ca06e: with TOPOS_DATABASE_PATH
naming a scratch file that did not exist, a run with a different --db created it
at user_version 78, wrote a pre-migration backup and pruned an older one, and the
cases' scope manifests, entity links and retrieval read it.

The cases themselves are not run here: they reach the retrieval models.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from topos.config.settings import settings

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "run_pin_set_eval.py"


def test_a_db_the_engine_does_not_open_is_refused_before_anything_opens(tmp_path):
    """The sentinel method: name a settings database that does not exist and hand
    the script a different --db. Before the refusal, the run created it."""
    sentinel = tmp_path / "settings" / "database.db"
    other = tmp_path / "other.db"
    sqlite3.connect(str(other)).close()
    before = other.read_bytes()
    env = dict(os.environ)
    env["TOPOS_DATABASE_PATH"] = str(sentinel)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--db", str(other), "--out", str(tmp_path / "out.json")],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "is not the database the engine opens" in result.stderr
    assert not sentinel.parent.exists(), "the run opened the settings database"
    assert other.read_bytes() == before
    assert not (tmp_path / "out.json").exists()


@pytest.fixture(scope="module")
def pin_set_eval() -> ModuleType:
    """The script, loaded by path. It defaults TOPOS_SCOPE_SHADOW to 0 at import when
    the variable is unset; importing with it set keeps that out of the session."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("TOPOS_SCOPE_SHADOW", os.environ.get("TOPOS_SCOPE_SHADOW") or "0")
        spec = importlib.util.spec_from_file_location("_run_pin_set_eval_under_test", SCRIPT)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("spelling", ["same", "through_a_symlink", "default"])
def test_the_database_the_engine_opens_is_graded(pin_set_eval, tmp_path, monkeypatch, spelling):
    db = tmp_path / "pinned" / "eval.db"
    db.parent.mkdir()
    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(db))
    monkeypatch.setattr(settings, "topos_database_path", str(db))
    graded = []

    async def record(db_path):
        graded.append(db_path)
        return []

    monkeypatch.setattr(pin_set_eval, "_run", record)
    if spelling == "same":
        argv = ["--db", str(db)]
    elif spelling == "through_a_symlink":
        alias = tmp_path / "alias"
        alias.symlink_to(db.parent, target_is_directory=True)
        argv = ["--db", str(alias / db.name)]
    else:
        argv = []  # --db defaults to TOPOS_DATABASE_PATH
    assert pin_set_eval.main([*argv, "--out", str(tmp_path / "out.json")]) == 0
    assert [p.resolve() for p in graded] == [db.resolve()]
