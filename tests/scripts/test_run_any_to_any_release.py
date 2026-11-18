"""The node's release command must run the matrix, refuse a missing verdict, and refuse a stale rig tree."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import run_any_to_any_release as release


@pytest.fixture(autouse=True)
def current_rig_tree(monkeypatch, request):
    """The fake control-plane trees below are not Git checkouts; the staleness check has tests of its own."""
    if "cp_repos" not in request.fixturenames:
        monkeypatch.setattr(release, "stale_rig_tree", lambda tree: None)


def inputs(tmp_path: Path) -> dict[str, str]:
    cp = tmp_path / "cp"
    sequence = cp / "scripts" / "a2a_rig" / "sequence.sh"
    sequence.parent.mkdir(parents=True)
    sequence.write_text("#!/bin/zsh\n")
    python = cp / "python"
    python.write_text("")
    packs = tmp_path / "packs"
    packs.mkdir()
    return {"A2A_RIG_CP_TREE": str(cp), "A2A_RIG_CP_PYTHON": str(python),
            "A2A_RIG_PACKS": str(packs), "A2A_RIG_RUNS": str(tmp_path / "runs"),
            "A2A_RIG_OUT": str(tmp_path / "fresh")}


def test_missing_inputs_fail_before_any_run(tmp_path, monkeypatch):
    monkeypatch.setattr(release.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    assert release.run({}) == 2
    values = inputs(tmp_path)
    Path(values["A2A_RIG_OUT"]).mkdir()
    assert release.run(values) == 2


def test_release_command_requires_a_passing_verdict(tmp_path, monkeypatch):
    values = inputs(tmp_path)
    calls = []

    def fake_run(args, *, cwd, env, check):
        calls.append((args, cwd, env, check))
        Path(args[-2]).mkdir()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    assert release.run(values) == 1
    assert len(calls) == 1 and calls[0][0][-1] == "release"
    assert calls[0][2]["A2A_RIG_NODE_TREE"] == str(release.NODE_TREE)
    def failed_run(args, *, cwd, env, check):
        out = Path(args[-2])
        out.mkdir()
        (out / "release-verdict.json").write_text(json.dumps({"pass": True, "failures": ["pair a-b failed"]}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(release.subprocess, "run", failed_run)
    assert release.run({**values, "A2A_RIG_OUT": str(tmp_path / "fresh-again")}) == 1


def test_release_command_accepts_only_clean_verdict(tmp_path, monkeypatch):
    values = inputs(tmp_path)

    def fake_run(args, *, cwd, env, check):
        out = Path(values["A2A_RIG_OUT"])
        out.mkdir()
        (out / "release-verdict.json").write_text(json.dumps({"pass": True, "failures": []}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    assert release.run(values) == 0


def test_release_command_prints_m3_warnings_beside_a_pass(tmp_path, monkeypatch, capsys):
    values = inputs(tmp_path)
    warning = "M3 warning: message-search offline, owner a vs c separated by KS, medians 2.0 ms apart (fails at 10 ms)"

    def fake_run(args, *, cwd, env, check):
        out = Path(values["A2A_RIG_OUT"])
        out.mkdir()
        (out / "release-verdict.json").write_text(json.dumps({"pass": True, "failures": [], "warnings": [warning]}))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(release.subprocess, "run", fake_run)
    assert release.run(values) == 0
    assert f"Warning: {warning}" in capsys.readouterr().out


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, path: str, text: str, message: str) -> str:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(text)
    _git(repo, "add", path)
    _git(repo, "-c", "user.name=rig", "-c", "user.email=rig@example.invalid", "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def cp_repos(tmp_path):
    """A control-plane origin whose main has an old and a new rig change, and a rig tree cloned from it."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    old = _commit(origin, "scripts/a2a_rig/run/runner.py", "one owner at a time\n", "rig: old runner")
    new = _commit(origin, "scripts/a2a_rig/run/runner.py", "owners together\n", "rig D: M3 owners timed together")
    _commit(origin, "control_plane/app.py", "unrelated\n", "control plane: unrelated change")
    tree = tmp_path / "rig-tree"
    subprocess.run(["git", "clone", "-q", str(origin), str(tree)], check=True)
    return tree, old, new


def test_a_rig_tree_without_mains_newest_rig_change_is_refused(cp_repos):
    tree, old, new = cp_repos
    _git(tree, "checkout", "-q", "--detach", old)
    refusal = release.stale_rig_tree(tree)
    assert refusal and new[:8] in refusal and "M3 owners timed together" in refusal


def test_a_rig_tree_at_or_ahead_of_mains_rig_change_runs(cp_repos):
    tree, old, new = cp_repos
    _git(tree, "checkout", "-q", "--detach", new)  # behind main only on a change outside the rig
    assert release.stale_rig_tree(tree) is None
    _git(tree, "checkout", "-q", "-b", "rig-branch", "origin/main")
    _commit(tree, "scripts/a2a_rig/run/score.py", "a branch's own change\n", "rig: branch change")
    assert release.stale_rig_tree(tree) is None


def test_a_stale_rig_tree_stops_the_release_before_the_rig_runs(cp_repos, tmp_path, monkeypatch):
    tree, old, _ = cp_repos
    _git(tree, "checkout", "-q", "--detach", old)
    values = inputs(tmp_path)
    sequence = tree / "scripts" / "a2a_rig" / "sequence.sh"
    sequence.write_text("#!/bin/zsh\n")
    values["A2A_RIG_CP_TREE"] = str(tree)
    real_run = subprocess.run

    def guard(args, **kwargs):
        if args[0] != "git":
            raise AssertionError("the rig ran on a stale tree")
        return real_run(args, **kwargs)

    monkeypatch.setattr(release.subprocess, "run", guard)
    assert release.run(values) == 2
