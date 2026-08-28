"""The install check has to be right about worktrees, or it blocks every commit.

``--verify-install`` refuses to let a commit through unless the commit-msg and
pre-push guards are actually wired, which is the correct instinct: a message
leak publishes with the commit and cannot be scrubbed without rewriting
history. But the check read ``.git/hooks`` as a literal path, and inside a
worktree ``.git`` is a FILE holding ``gitdir: <main>/.git/worktrees/<name>``.
Nothing opens under it, both stages read as missing, and the guard blocked
every commit made from a worktree while the hooks were correctly wired the
whole time — the one failure mode that gets a guard deleted rather than fixed.

The remediation it printed could not work either: worktrees here set
``core.hooksPath``, and pre-commit refuses to install while that is set.

These build real repositories because the bug lives entirely in git's own
layout — a mock of the filesystem would have reproduced the wrong thing.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

SCANNER = os.path.abspath(os.path.join("scripts", "scan_repo_for_owner_data.py"))
STAGES = ("commit-msg", "pre-push")


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          check=True)


def _install_hooks(hooks_dir, stages=STAGES):
    """What `pre-commit install --hook-type X` leaves behind, reduced to its marker."""
    for stage in stages:
        path = os.path.join(str(hooks_dir), stage)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"#!/bin/sh\n# start templated\n# hook-type={stage}\nexit 0\n")
        os.chmod(path, 0o755)


def _verify(cwd, terms):
    """--verify-install only runs once there is data to protect, hence the terms file."""
    return subprocess.run(
        [sys.executable, SCANNER, "--database", "/nonexistent.db",
         "--local-terms", str(terms), "--verify-install"],
        cwd=str(cwd), capture_output=True, text=True,
    )


@pytest.fixture()
def repo(tmp_path):
    """A checkout with both guard stages wired, plus a terms file outside it.

    The terms file has to live outside the tree — the scanner refuses to read
    one from inside a repository, and a list of protected names is the worst
    thing to leave lying in a working tree.
    """
    root = tmp_path / "main"
    root.mkdir()
    _git("init", "-q", ".", cwd=root)
    _git("config", "user.email", "t@example.invalid", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=root)
    _install_hooks(root / ".git" / "hooks")
    terms = tmp_path / "terms.txt"
    terms.write_text("Zzyzx Placeholder\n", encoding="utf-8")
    return root, terms


@pytest.fixture()
def worktree(repo):
    """A worktree of it — where ``.git`` is a file, which is the whole bug."""
    root, terms = repo
    path = root.parent / "wt"
    _git("worktree", "add", "-q", "-b", "wt", str(path), cwd=root)
    assert (path / ".git").is_file(), "fixture is not exercising the worktree layout"
    return path, terms


def test_passes_in_an_ordinary_checkout(repo):
    root, terms = repo
    assert _verify(root, terms).returncode == 0


def test_passes_in_a_worktree(worktree):
    """The regression: hooks live in the main checkout, and they count from here."""
    path, terms = worktree
    assert _verify(path, terms).returncode == 0


def test_passes_in_a_worktree_with_core_hookspath_set(worktree, repo):
    """How this project's worktrees are actually configured.

    ``core.hooksPath`` is set per-worktree, in ``config.worktree``, pointing at
    the main checkout's hooks. Reading it wrong is not just a false negative:
    it sends you to `pre-commit install`, which refuses outright while
    hooksPath is set, so there is no way to act on the message either.
    """
    path, terms = worktree
    root, _ = repo
    _git("config", "extensions.worktreeConfig", "true", cwd=path)
    _git("config", "--worktree", "core.hooksPath",
         str(root / ".git" / "hooks"), cwd=path)
    assert _verify(path, terms).returncode == 0


def test_a_genuinely_missing_stage_still_fails(repo):
    """The check has to keep failing where it should — the point is not to pass."""
    root, terms = repo
    os.remove(root / ".git" / "hooks" / "commit-msg")
    out = _verify(root, terms)
    assert out.returncode == 1
    assert "commit-msg" in out.stderr
    assert "pre-push" not in out.stderr.split("\n")[0]


def test_the_failure_names_the_directory_it_read(repo):
    """A report of "not installed" that hides WHERE it looked is undiagnosable.

    That is how the worktree bug survived: the path it checked never appeared
    in the output, so the message looked like a claim about the hooks rather
    than about the path.
    """
    root, terms = repo
    for stage in STAGES:
        os.remove(root / ".git" / "hooks" / stage)
    out = _verify(root, terms)
    assert out.returncode == 1
    assert str(root / ".git" / "hooks") in out.stderr


def test_hooks_path_failure_says_pre_commit_cannot_install(worktree, tmp_path):
    """Do not print a remediation that the configuration forbids."""
    path, terms = worktree
    empty = tmp_path / "empty-hooks"
    empty.mkdir()
    _git("config", "extensions.worktreeConfig", "true", cwd=path)
    _git("config", "--worktree", "core.hooksPath", str(empty), cwd=path)
    out = _verify(path, terms)
    assert out.returncode == 1
    assert "core.hooksPath" in out.stderr
    assert str(empty) in out.stderr
