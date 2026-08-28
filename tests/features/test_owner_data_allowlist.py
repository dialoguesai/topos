"""Hits that are NOT leaks, exempted one at a time and without naming them.

A scrub is right for someone else's name in a fixture and WRONG for the owner's byline on
their own essay, the account handle in a deploy runbook, or e2e fixtures that must use the
real account to test anything at all. Without a way to say so, the only ways forward are to
mutilate the repo or to reach for `--no-verify` — and a guard people routinely bypass has
stopped being a guard.

The hard part is that the exemption must not become the leak. "This name is fine here",
written into a tracked file, publishes the name exactly as the source did. So an entry
carries a hash of (path, kind, term) and nothing else: it cannot be read, it cannot be
hand-written, and it pins to one term in one file.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

SCANNER = os.path.join("scripts", "scan_repo_for_owner_data.py")
SECRET = "Marisol Trevino"


def _run(*args, cwd=None):
    return subprocess.run([sys.executable, os.path.abspath(SCANNER), *args],
                          capture_output=True, text=True, cwd=cwd)


@pytest.fixture()
def repo(tmp_path):
    """A tiny tree with one protected term in one file, and a terms file naming it.

    The terms file sits OUTSIDE the tree on purpose — the scanner refuses to read one from
    inside a repository, which is the right call and which silently turned three of these
    assertions green for the wrong reason when the fixture got it wrong.
    """
    root = tmp_path / "repo"
    root.mkdir()
    terms = tmp_path / "terms.txt"
    terms.write_text(f"{SECRET}\n", encoding="utf-8")
    (root / "essay.md").write_text(f"# An essay\n\nBy {SECRET}\n", encoding="utf-8")
    return root, str(terms)


def _scan(repo, *extra):
    root, terms = repo
    return _run("--database", "/nonexistent.db", "--local-terms", terms,
                *extra, "essay.md", cwd=str(root))


def test_the_hit_fails_before_anything_allows_it(repo):
    assert _scan(repo).returncode == 1


def test_emit_allow_prints_a_line_instead_of_failing(repo):
    """You cannot hand-write an entry — the key is a hash — so the tool has to offer one."""
    out = _scan(repo, "--emit-allow")
    assert out.returncode == 0
    body = out.stdout.strip().splitlines()
    entry = [ln for ln in body if not ln.startswith("#")]
    assert len(entry) == 1
    assert "<reason" in entry[0]


def test_the_emitted_line_does_not_contain_the_name(repo):
    """The whole point. An allowlist that names what it allows has published it."""
    out = _scan(repo, "--emit-allow")
    assert SECRET.lower() not in out.stdout.lower()
    for token in SECRET.split():
        assert token.lower() not in out.stdout.lower()


def test_an_entry_with_a_reason_clears_the_hit(repo):
    root, _ = repo
    key = [ln for ln in _scan(repo, "--emit-allow").stdout.splitlines()
           if not ln.startswith("#")][0].split()[0]
    (root / ".owner-data-allow").write_text(
        f"{key}  essay byline — the owner is the author\n", encoding="utf-8")
    out = _scan(repo)
    assert out.returncode == 0
    assert "1 allowed by" in out.stdout


def test_an_entry_without_a_reason_is_refused(repo):
    """An exemption nobody justified is indistinguishable from one nobody understood, and
    it outlives whoever added it. That is how an allowlist becomes the hole."""
    root, _ = repo
    key = [ln for ln in _scan(repo, "--emit-allow").stdout.splitlines()
           if not ln.startswith("#")][0].split()[0]
    (root / ".owner-data-allow").write_text(f"{key}\n", encoding="utf-8")
    out = _scan(repo)
    assert out.returncode == 1
    assert "no reason" in out.stderr


def test_the_exemption_does_not_travel_to_another_file(repo):
    """Pinned to one term in one file. Copying the leak elsewhere must still fail, or an
    allowlist entry becomes a licence to spread the name."""
    root, _ = repo
    key = [ln for ln in _scan(repo, "--emit-allow").stdout.splitlines()
           if not ln.startswith("#")][0].split()[0]
    (root / ".owner-data-allow").write_text(f"{key}  byline\n", encoding="utf-8")
    (root / "copy.md").write_text(f"By {SECRET}\n", encoding="utf-8")
    _, terms = repo
    out = _run("--database", "/nonexistent.db", "--local-terms", terms,
               "essay.md", "copy.md", cwd=str(root))
    assert out.returncode == 1
    assert "copy.md" in out.stderr
    assert "essay.md" not in out.stderr


def test_a_different_name_in_an_allowed_file_still_fails(repo):
    """The file is not exempt — one term in it is."""
    root, terms_path = repo
    key = [ln for ln in _scan(repo, "--emit-allow").stdout.splitlines()
           if not ln.startswith("#")][0].split()[0]
    (root / ".owner-data-allow").write_text(f"{key}  byline\n", encoding="utf-8")
    with open(terms_path, "a", encoding="utf-8") as fh:
        fh.write("Otto Halvorsen\n")
    (root / "essay.md").write_text(
        f"# An essay\n\nBy {SECRET}\n\nThanks to Otto Halvorsen.\n", encoding="utf-8")
    out = _scan(repo)
    assert out.returncode == 1
    assert "Otto Halvorsen" in out.stderr
    assert SECRET not in out.stderr


def test_a_missing_allow_file_allows_nothing(repo):
    root, _ = repo
    assert not (root / ".owner-data-allow").exists()
    assert _scan(repo).returncode == 1


def test_comments_and_blank_lines_are_ignored(repo):
    root, _ = repo
    key = [ln for ln in _scan(repo, "--emit-allow").stdout.splitlines()
           if not ln.startswith("#")][0].split()[0]
    (root / ".owner-data-allow").write_text(
        f"# why these are here\n\n{key}  essay byline\n\n", encoding="utf-8")
    assert _scan(repo).returncode == 0
