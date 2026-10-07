"""The upgrade matrix and the fixture builder refuse a real home (sixth round; third re-check, R4-L4).

protects: `scripts/run_upgrade_matrix.py` runs a whole upgrade, reprocessing included, on whatever `--db` names, and
`scripts/build_upgrade_fixture.py` DELETES whatever `--out` names before it writes a fixture there. Both are made
for a scratch file, and neither refused anything: given a database of the real home, one would have upgraded it and
the other removed it. They now make the one check the pre-flight makes
(`census_support.refuse_a_real_database`), before anything else: the path is not the real home's or under it, and
the file is not another name for a database there. One check, three callers.
"The real home" here is a folder in the test's own temporary directory. Nothing of a real home is named.
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.public

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
for folder in (SCRIPTS, SCRIPTS / "permissions_v2"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
matrix = importlib.import_module("run_upgrade_matrix")
fixture = importlib.import_module("build_upgrade_fixture")
cs = importlib.import_module("census_support")
preflight = importlib.import_module("carry_preflight")


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A stand-in for the real home, holding a database with one row to lose."""
    saved = dict(os.environ)
    home = tmp_path / "the-live-home"
    home.mkdir()
    conn = sqlite3.connect(home / "database.db")
    conn.execute("CREATE TABLE kept (word TEXT)")
    conn.execute("INSERT INTO kept VALUES ('still here')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(cs, "LIVE_HOME", home)
    yield home
    os.environ.clear()                                   # the matrix sets process environment for its run
    os.environ.update(saved)


def untouched(database: Path) -> bool:
    conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master ORDER BY name")]
        return tables == ["kept"] and conn.execute("SELECT word FROM kept").fetchall() == [("still here",)]
    finally:
        conn.close()


def a_hard_link_to(database: Path, folder: Path) -> Path:
    folder.mkdir()
    os.link(database, folder / "fixture.db")
    return folder / "fixture.db"


def test_the_matrix_refuses_a_database_of_the_real_home(live, tmp_path, capsys):
    """Rule: `run_matrix` asks `refuse_a_real_database` first. Without it the real database is migrated and every
    planned step run on it."""
    for path in (live / "database.db", a_hard_link_to(live / "database.db", tmp_path / "linked")):
        with pytest.raises(cs.CensusRefused) as refused:
            matrix.run_matrix(path, stage_unreleased=matrix.NEXT)
        assert str(refused.value) == "live_store_refused"
        assert matrix.main(["--db", str(path), "--stage-unreleased"]) == 2
        assert json.loads(capsys.readouterr().err.strip().splitlines()[-1]) == {"refused": "live_store_refused"}
    assert untouched(live / "database.db")
    assert os.environ.get("TOPOS_DATABASE_PATH") != str(live / "database.db")      # refused before the run is set up


def test_the_matrix_refuses_a_database_with_another_name(live, tmp_path):
    other = tmp_path / "a-fixture.db"
    sqlite3.connect(other).close()
    linked = a_hard_link_to(other, tmp_path / "linked")
    with pytest.raises(cs.CensusRefused) as refused:
        matrix.run_matrix(linked)
    assert str(refused.value) == "database_is_a_hard_link"


@pytest.mark.parametrize("build", ["build_from_current", "build_from_pypi"])
def test_the_fixture_builder_refuses_to_write_over_the_real_home(live, tmp_path, build, monkeypatch):
    """It unlinks its output path before it writes. Rule: both builders ask `refuse_a_real_database` before that."""
    monkeypatch.setattr(fixture, "_run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ran a command")))
    for path in (live / "database.db", live / "backups" / "a-new-fixture.db"):
        with pytest.raises(cs.CensusRefused) as refused:
            getattr(fixture, build)("1.4.4", path)
        assert str(refused.value) == "live_store_refused"
    assert untouched(live / "database.db") and not (live / "backups").exists()
    linked = a_hard_link_to(live / "database.db", tmp_path / "linked")
    with pytest.raises(cs.CensusRefused):
        getattr(fixture, build)("1.4.4", linked)
    assert linked.exists() and untouched(live / "database.db")           # not even the link was removed


def test_the_fixture_builders_command_line_refuses_with_the_word(live, capsys):
    assert fixture.main(["--version", "1.4.4", "--out", str(live / "database.db"), "--from-current"]) == 2
    assert json.loads(capsys.readouterr().err.strip().splitlines()[-1]) == {"refused": "live_store_refused"}
    assert untouched(live / "database.db")


def test_a_scratch_path_is_still_taken(live, tmp_path):
    out = tmp_path / "scratch" / "fixture.db"
    fixture.build_from_current("1.4.4", out)
    assert out.is_file() and cs.refuse_a_real_database(out) == out
    assert cs.refuse_a_real_database(tmp_path / "not-there-yet.db") == tmp_path / "not-there-yet.db"


def test_the_three_tools_make_one_check():
    """Not three copies: each names `refuse_a_real_database`, and none compares a path with the home itself."""
    for module in (matrix, fixture, preflight):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "refuse_a_real_database(" in source, module.__name__
        assert ".topos\"" not in source and "Path.home()" not in source, module.__name__
