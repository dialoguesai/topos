"""The node's release command must run the matrix and refuse a missing verdict."""
import json
from pathlib import Path
from types import SimpleNamespace

from scripts import run_any_to_any_release as release


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
