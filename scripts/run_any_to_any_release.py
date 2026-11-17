#!/usr/bin/env python3
"""Run the six-pair Rig D release gate against this node checkout.

Set A2A_RIG_CP_TREE, A2A_RIG_CP_PYTHON, A2A_RIG_PACKS,
A2A_RIG_RUNS, and A2A_RIG_OUT to absolute paths. The output directory must
be new. The control-plane sequence resets the isolated rig, tests six pairs,
the isolation and invented-answer batteries, writes a verdict, and leaves the
rig down. This command fails closed on a missing or failed verdict.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

NODE_TREE = Path(__file__).resolve().parents[1]
REQUIRED = ("A2A_RIG_CP_TREE", "A2A_RIG_CP_PYTHON", "A2A_RIG_PACKS", "A2A_RIG_RUNS", "A2A_RIG_OUT")


def stale_rig_tree(cp_tree: Path) -> str | None:
    """Why the control-plane tree may not run the rig, or None when it carries main's newest rig change.

    The rig tree is set by hand and nothing moved it: 1.5.55 and 1.5.56 ran from a tree of 23 Oct whose runner still
    took each owner's offline probes in a window of its own, a confound control-plane#213 removed from main on 8 Nov.
    1.5.56 failed M3 twice on it alone. A tree ahead of main (a branch with main merged in) is accepted."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(cp_tree), *args], capture_output=True, text=True, check=False)

    if git("fetch", "--quiet", "origin", "+refs/heads/main:refs/remotes/origin/main").returncode:
        return "could not fetch control-plane main to check that the rig tree is current"
    newest = git("log", "-1", "--format=%H %s", "refs/remotes/origin/main", "--", "scripts/a2a_rig")
    if newest.returncode or not newest.stdout.strip():
        return "could not read control-plane main's newest rig change"
    sha, _, subject = newest.stdout.strip().partition(" ")
    if git("merge-base", "--is-ancestor", sha, "HEAD").returncode:
        return (f"the rig tree lacks control-plane main's rig change {sha[:8]} ({subject}); "
                f"move it to main first: git -C {cp_tree} checkout --detach origin/main")
    return None


def run(env: dict[str, str] | None = None) -> int:
    values = dict(os.environ if env is None else env)
    missing = [name for name in REQUIRED if not values.get(name)]
    if missing:
        print("Release gate needs: " + ", ".join(missing), file=sys.stderr)
        return 2
    paths = {name: Path(values[name]).expanduser() for name in REQUIRED}
    if any(not path.is_absolute() for path in paths.values()):
        print("Release gate paths must be absolute", file=sys.stderr)
        return 2
    cp_tree, cp_python = paths["A2A_RIG_CP_TREE"], paths["A2A_RIG_CP_PYTHON"]
    packs, runs, out = (paths[name] for name in ("A2A_RIG_PACKS", "A2A_RIG_RUNS", "A2A_RIG_OUT"))
    sequence = cp_tree / "scripts" / "a2a_rig" / "sequence.sh"
    if not sequence.is_file() or not cp_python.is_file() or not packs.is_dir() or out.exists():
        print("Release gate tree, interpreter, packs, or fresh output path is unavailable", file=sys.stderr)
        return 2
    refusal = stale_rig_tree(cp_tree)
    if refusal:
        print(f"Release gate refused: {refusal}", file=sys.stderr)
        return 2
    values.update(A2A_RIG_NODE_TREE=str(NODE_TREE), A2A_RIG_CP_TREE=str(cp_tree),
                  A2A_RIG_NODE_PYTHON=sys.executable, A2A_RIG_CP_PYTHON=str(cp_python), PY=str(cp_python))
    result = subprocess.run(["/bin/zsh", str(sequence), str(packs), str(runs), str(out), "release"],
                            cwd=cp_tree, env=values, check=False)
    if result.returncode:
        return result.returncode
    try:
        verdict = json.loads((out / "release-verdict.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"Release verdict unavailable: {type(exc).__name__}", file=sys.stderr)
        return 1
    if verdict.get("pass") is not True or verdict.get("failures") != []:
        print("Release verdict did not pass", file=sys.stderr)
        return 1
    print("Any-to-any six-pair release gate passed")
    # M3's warning band (control-plane scripts/a2a_rig/DOORS.md): a pass the owner should still see.
    for warning in verdict.get("warnings") or []:
        print(f"Warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
