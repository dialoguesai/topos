"""A protected-name set too small to mean anything is refused, not scanned.

On a machine with a node the set runs to thousands of names. The scratch database
a test session builds has none, and a fixture has one to three. A scan against
either still printed "clean", and pre-commit shows nothing of a passing hook but
"Passed", so a push could go out on a check that had nothing to check against.

Now, whenever a database is in play or the account has a node, fewer than
``MIN_PROTECTED_NAMES`` names is a refusal (exit 3). Only ``--allow-fixture``, which
tests pass and no hook does, scans such a set. Two things stay as they were: a
machine with no node and no terms list skips, as CI and a fresh clone always have;
and a short hand-kept list on a machine with no node is the whole set, so it is
scanned.

Every database here is built by the test and holds only invented names.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys

import pytest

from scripts import scan_repo_for_owner_data as scanner

SCANNER = os.path.abspath(os.path.join("scripts", "scan_repo_for_owner_data.py"))
#: Invented, and a name nobody has.
PLANTED = "Pelloquin Zarathand"
FLOOR = scanner.MIN_PROTECTED_NAMES


def _fixture_db(path, people=()):
    """The tables the scanner reads, holding only invented people."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE entity_blackholes (canonical_name TEXT);"
        "CREATE TABLE location_events (place_name TEXT);"
        "CREATE TABLE user_goals (goal_text TEXT);"
        "CREATE TABLE user_identity (display_name TEXT);"
        "CREATE TABLE entities (entity_type TEXT, canonical_name TEXT);"
        "CREATE TABLE contacts (display_name TEXT);"
        "CREATE TABLE contact_identifiers (identifier TEXT, identifier_type TEXT);"
    )
    conn.executemany("INSERT INTO entities VALUES ('person', ?)", [(p,) for p in people])
    conn.commit()
    conn.close()
    return str(path)


def _people(n):
    return [f"Invented Person{i:04d}" for i in range(n)]


def _run(*args, cwd):
    return subprocess.run([sys.executable, SCANNER, *args], cwd=str(cwd),
                          capture_output=True, text=True)


@pytest.fixture()
def tree(tmp_path):
    """A checkout holding the planted name, and a terms file naming it (outside it)."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "."], cwd=root, check=True)
    (root / "notes.md").write_text(f"met {PLANTED}\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    terms = tmp_path / "terms.txt"
    terms.write_text(f"{PLANTED}\n", encoding="utf-8")
    return root, terms


# ------------------------------------------------ too few names: refused

def test_a_database_holding_nothing_is_refused_not_scanned(tree, tmp_path):
    """The shape that waved a push through: a database that exists and is empty."""
    root, terms = tree
    db = _fixture_db(tmp_path / "scratch.db")
    out = _run("--database", db, "--local-terms", str(terms), "notes.md", cwd=root)
    assert out.returncode == 3, (out.stdout, out.stderr)
    assert "clean" not in out.stdout
    assert "refusing to check against 1 protected names" in out.stderr
    assert db in out.stderr
    assert PLANTED not in out.stderr + out.stdout     # counts and paths, never a name


@pytest.mark.parametrize("mode", ["--all", "--text", "--message-file", "named"])
def test_every_scanning_mode_refuses(tree, tmp_path, mode):
    root, terms = tree
    db = _fixture_db(tmp_path / "scratch.db", people=_people(2))
    args = {
        "--all": ["--all"],
        "--text": ["--text", "a draft message"],
        "--message-file": ["--message-file", str(root / "notes.md")],
        "named": ["notes.md"],
    }[mode]
    out = _run("--database", db, "--local-terms", str(terms), *args, cwd=root)
    assert out.returncode == 3, (out.stdout, out.stderr)


@pytest.mark.parametrize("db_names,scanned", [(FLOOR - 1, True), (FLOOR - 2, False)])
def test_the_floor_counts_database_names_and_local_terms_together(tree, tmp_path,
                                                                   db_names, scanned):
    """FLOOR - 1 people plus one hand-typed term is exactly the floor, and is scanned."""
    root, terms = tree
    db = _fixture_db(tmp_path / "node.db", people=_people(db_names))
    out = _run("--database", db, "--local-terms", str(terms), "notes.md", cwd=root)
    assert out.returncode == (1 if scanned else 3), (out.stdout, out.stderr)


def test_allow_fixture_scans_a_small_set(tree, tmp_path):
    """What the tests pass. No hook does."""
    root, terms = tree
    db = _fixture_db(tmp_path / "scratch.db")
    out = _run("--database", db, "--local-terms", str(terms), "--allow-fixture",
               "notes.md", cwd=root)
    assert out.returncode == 1, out.stderr
    assert "notes.md:1" in out.stderr


def test_the_install_check_is_not_a_name_check(tree, tmp_path):
    """--verify-install asks whether the hooks are wired, and answers that."""
    root, terms = tree
    db = _fixture_db(tmp_path / "scratch.db")
    out = _run("--database", db, "--local-terms", str(terms), "--verify-install", cwd=root)
    assert out.returncode == 1
    assert "guard stages NOT installed" in out.stderr


# ------------------------------- whose machine this is: the account's own home

def _main(monkeypatch, capsys, tmp_path, argv, *, account_db):
    """Run main() in-process with the account's home pinned to a test path.

    In-process, so the account lookup can be pointed at a test path and the real
    one is never consulted.
    """
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    monkeypatch.chdir(work)            # not a repo, and not holding the terms file
    monkeypatch.setattr(scanner, "_account_database", lambda: str(account_db))
    monkeypatch.setattr(sys, "argv", ["scan_repo_for_owner_data.py", *argv])
    code = scanner.main()
    return code, capsys.readouterr()


def test_a_redirected_home_refuses_instead_of_skipping(tmp_path, monkeypatch, capsys):
    """HOME aimed at a scratch home resolves --database to a file that is not there,
    and the terms file with it. The old answer was SKIPPED, on the owner's own machine."""
    account = tmp_path / "account" / ".topos" / "database.db"
    account.parent.mkdir(parents=True)
    _fixture_db(account, people=_people(FLOOR))
    scratch = tmp_path / "scratch-home" / ".topos"
    code, out = _main(monkeypatch, capsys, tmp_path,
                      ["--database", str(scratch / "database.db"),
                       "--local-terms", str(scratch / "private-terms.txt"),
                       "--text", "a draft message"],
                      account_db=account)
    assert code == 3
    assert "SKIPPED" not in out.out
    assert str(account) in out.err


def test_a_machine_without_a_node_still_skips(tmp_path, monkeypatch, capsys):
    """CI and a fresh clone: nothing of the owner's is here, so there is nothing to leak."""
    code, out = _main(monkeypatch, capsys, tmp_path,
                      ["--database", str(tmp_path / "none.db"),
                       "--local-terms", str(tmp_path / "none.txt"),
                       "--text", "a draft message"],
                      account_db=tmp_path / "no-node" / ".topos" / "database.db")
    assert code == 0
    assert "SKIPPED" in out.out


def test_a_hand_kept_list_on_a_machine_without_a_node_is_scanned(tmp_path, monkeypatch,
                                                                 capsys):
    """With no node, a short list someone typed is the whole set, not a wrong copy."""
    terms = tmp_path / "terms.txt"
    terms.write_text(f"{PLANTED}\n", encoding="utf-8")
    code, out = _main(monkeypatch, capsys, tmp_path,
                      ["--database", str(tmp_path / "none.db"), "--local-terms", str(terms),
                       "--text", f"met {PLANTED} today"],
                      account_db=tmp_path / "no-node" / ".topos" / "database.db")
    assert code == 1, out
