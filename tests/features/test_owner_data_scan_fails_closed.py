"""A scan that read nothing, or could not read what it was handed, is never a pass.

Four ways the owner-data scanner exited 0 without checking anything:

- outside a repository ``git ls-files`` fails and prints nothing, and the scan
  said "clean — 0 files checked";
- a bare run with nothing changed said the same;
- a ``--message-file`` it could not open was passed with exit 0;
- and, not a pass but a needless read, ``--verify-install`` loaded the protected
  names (opening the owner's database) before answering a question about hooks.

Every run names ``--database`` explicitly, and every database here is built by the
test and holds only invented names. The default is the owner's live database,
and a test must never open it.
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
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          check=True)


def _scan(cwd, terms, *args):
    return subprocess.run(
        [sys.executable, SCANNER, "--database", "/nonexistent.db",
         "--local-terms", str(terms), "--allow-fixture", *args],
        cwd=str(cwd), capture_output=True, text=True,
    )


@pytest.fixture()
def terms(tmp_path):
    path = tmp_path / "terms.txt"
    path.write_text(f"{PLANTED}\n", encoding="utf-8")
    return path


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", ".", cwd=root)
    return root


# ------------------------------------------------- nothing read, nothing passed

@pytest.mark.parametrize("args", [["--all"], []], ids=["--all", "bare"])
def test_outside_a_repository_the_scan_is_refused(tmp_path, terms, args):
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    out = _scan(outside, terms, *args)
    assert out.returncode == 2, (out.returncode, out.stdout, out.stderr)
    assert "clean" not in out.stdout
    assert "cannot list the files to scan" in out.stderr


def test_a_bare_run_with_nothing_changed_is_not_clean(repo, terms):
    out = _scan(repo, terms)
    assert out.returncode == 4, (out.returncode, out.stdout, out.stderr)
    assert "clean" not in out.stdout
    assert "nothing checked — 0 of 0 files read" in out.stderr


def test_a_tree_of_nothing_but_binaries_is_not_clean(repo, terms):
    (repo / "logo.png").write_bytes(PNG_BYTES)
    _git("add", "-A", cwd=repo)
    out = _scan(repo, terms, "--all")
    assert out.returncode == 4, (out.returncode, out.stdout, out.stderr)
    assert "clean" not in out.stdout
    assert "0 of 1 files read" in out.stderr
    assert "not scanned: 1 binary" in out.stdout


def test_one_readable_file_is_still_a_pass(repo, terms):
    """The zero-file refusal must not turn an ordinary clean scan into a failure."""
    (repo / "logo.png").write_bytes(PNG_BYTES)
    (repo / "notes.md").write_text("nothing here\n", encoding="utf-8")
    _git("add", "-A", cwd=repo)
    out = _scan(repo, terms, "--all")
    assert out.returncode == 0, (out.stdout, out.stderr)
    assert "clean — 1 files checked" in out.stdout


def test_an_unreadable_message_file_is_refused(repo, terms):
    out = _scan(repo, terms, "--message-file", str(repo / "no-such-message"))
    assert out.returncode == 2, (out.returncode, out.stdout, out.stderr)
    assert "clean" not in out.stdout
    assert "nothing was checked" in out.stderr


# ---------------------------------------- the install check opens no database

def _install_hooks(hooks_dir):
    for stage in ("commit-msg", "pre-push"):
        path = hooks_dir / stage
        path.write_text(f"#!/bin/sh\n# hook-type={stage}\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)


def test_the_install_check_answers_without_reading_the_names(repo, tmp_path, monkeypatch,
                                                             capsys):
    """``no-owner-data-installed`` runs ``--verify-install`` on every commit. With the
    database's default being the owner's, loading the names first meant opening it
    on every commit, to answer a question about the hooks directory."""
    _install_hooks(repo / ".git" / "hooks")
    db = tmp_path / "node.db"
    sqlite3.connect(str(db)).close()

    def refuse(_path):
        raise AssertionError("--verify-install opened the database")

    monkeypatch.setattr(scanner, "_protected_names", refuse)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(sys, "argv", ["scan_repo_for_owner_data.py", "--database", str(db),
                                      "--local-terms", "", "--verify-install"])
    assert scanner.main() == 0
    assert "NOT installed" not in capsys.readouterr().err
