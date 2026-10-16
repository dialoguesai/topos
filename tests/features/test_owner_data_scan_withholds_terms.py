"""A hit is reported by kind and location, never by the text that matched.

The scanner's output is read in terminals, CI logs and the transcripts of coding
agents that commit through these hooks. It used to print each protected name it
found (``{name!r}``), so a hook that fired copied the owner's data one hop further
than the file it caught it in. Every mode that can hit is checked here, and so is
every other thing the scanner prints alongside a hit.

Every run names ``--database`` explicitly and every name is invented.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys

import pytest

SCANNER = os.path.abspath(os.path.join("scripts", "scan_repo_for_owner_data.py"))
#: Invented, and names nobody has.
PLANTED = "Pelloquin Zarathand"
PHONE = "6125550147"
PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          check=True)


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git("init", "-q", ".", cwd=root)
    terms = tmp_path / "terms.txt"
    terms.write_text(f"{PLANTED}\n", encoding="utf-8")
    db = tmp_path / "node.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        "CREATE TABLE entity_blackholes (canonical_name TEXT);"
        "CREATE TABLE location_events (place_name TEXT);"
        "CREATE TABLE user_goals (goal_text TEXT);"
        "CREATE TABLE user_identity (display_name TEXT);"
        "CREATE TABLE entities (entity_type TEXT, canonical_name TEXT);"
        "CREATE TABLE contacts (display_name TEXT);"
        "CREATE TABLE contact_identifiers (identifier TEXT, identifier_type TEXT);"
    )
    conn.execute("INSERT INTO contact_identifiers VALUES (?, 'phone')", (f"+1{PHONE}",))
    conn.commit()
    conn.close()
    return root, terms, db


def _scan(repo, *args):
    root, terms, db = repo
    return subprocess.run(
        [sys.executable, SCANNER, "--database", str(db), "--local-terms", str(terms),
         "--allow-fixture", *args],
        cwd=str(root), capture_output=True, text=True,
    )


def _never_printed(out):
    printed = (out.stdout + out.stderr).lower()
    assert PLANTED.lower() not in printed
    assert PHONE not in "".join(ch for ch in printed if ch.isdigit())


@pytest.mark.parametrize("ext", [".py", ".css", ".js", ""], ids=["py", "css", "js", "no-suffix"])
def test_a_named_file_hit_gives_the_location_not_the_name(repo, ext):
    root, _, _ = repo
    rel = f"src/thing{ext}"
    (root / "src").mkdir()
    (root / rel).write_text(f"// first\n// owner {PLANTED}\n", encoding="utf-8")
    out = _scan(repo, rel)
    assert out.returncode == 1, (out.stdout, out.stderr)
    assert f"{rel}:2" in out.stderr
    assert "local list" in out.stderr
    _never_printed(out)


def test_a_phone_hit_gives_the_location_not_the_number(repo):
    root, _, _ = repo
    (root / "notes.md").write_text(f"call ({PHONE[:3]}) {PHONE[3:6]}-{PHONE[6:]}\n",
                                   encoding="utf-8")
    out = _scan(repo, "notes.md")
    assert out.returncode == 1, (out.stdout, out.stderr)
    assert "notes.md:1" in out.stderr and "contact phone" in out.stderr
    _never_printed(out)


def test_the_pre_push_sweep_withholds_it(repo):
    root, _, _ = repo
    (root / "Dockerfile").write_text(f'LABEL maintainer="{PLANTED}"\n', encoding="utf-8")
    (root / "logo.png").write_bytes(PNG_BYTES)
    _git("add", "-A", cwd=root)
    out = _scan(repo, "--all")
    assert out.returncode == 1, (out.stdout, out.stderr)
    assert "Dockerfile:1" in out.stderr
    _never_printed(out)


def test_a_hit_beside_a_refusal_withholds_it(repo):
    root, _, _ = repo
    (root / "a.sh").write_text(f"echo {PLANTED}\n", encoding="utf-8")
    (root / "logo.png").write_bytes(PNG_BYTES)
    out = _scan(repo, "a.sh", "logo.png")
    assert out.returncode == 1, (out.stdout, out.stderr)
    assert "a.sh:1" in out.stderr and "logo.png: binary" in out.stderr
    _never_printed(out)


@pytest.mark.parametrize("mode", ["--text", "--message-file"])
def test_a_message_hit_gives_the_line_not_the_name(repo, tmp_path, mode):
    body = f"subject\n\nthanks to {PLANTED}\n"
    if mode == "--text":
        out = _scan(repo, "--text", body)
    else:
        msg = tmp_path / "COMMIT_EDITMSG"
        msg.write_text(body, encoding="utf-8")
        out = _scan(repo, "--message-file", str(msg))
    assert out.returncode == 1, (out.stdout, out.stderr)
    assert "(line 3)" in out.stderr
    _never_printed(out)


def test_emit_allow_prints_keys_not_names(repo):
    root, _, _ = repo
    (root / "essay.md").write_text(f"By {PLANTED}\n", encoding="utf-8")
    out = _scan(repo, "--emit-allow", "essay.md")
    assert out.returncode == 0, out.stderr
    assert "<reason" in out.stdout
    _never_printed(out)
