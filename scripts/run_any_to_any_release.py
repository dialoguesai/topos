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
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
