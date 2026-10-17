#!/usr/bin/env python3
"""Automatically run/record the required sharing gate, or verify it before PyPI.

The rig remains local because its private fixture and control-plane checkout
are unavailable on public GitHub runners. A passing run records the exact
clean Git tree. After a squash merge the same evidence can be recorded on the
merged commit without running the hour-long rig again.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.request import Request, urlopen

try:
    from scripts.select_release_checks import ROOT, git, make_plan, summary
except ModuleNotFoundError:  # direct `python scripts/node_release_gate.py`
    from select_release_checks import ROOT, git, make_plan, summary

CONTEXT = "release/sharing-rig"


def description(tree: str) -> str:
    return f"Rig D passed; tree={tree}"


def validate_evidence(evidence: dict, tree: str) -> None:
    if (evidence.get("schema") != 1 or evidence.get("pass") is not True
            or evidence.get("tree") != tree or evidence.get("failures") != []):
        raise ValueError("Sharing evidence must pass and match this exact node tree")


def verify_statuses(statuses: list[dict], tree: str) -> None:
    # GitHub lists newest first. Never accept an old success after a new failure.
    latest = next((s for s in statuses if s.get("context") == CONTEXT), None)
    if not latest or latest.get("state") != "success" or latest.get("description") != description(tree):
        raise ValueError("This release requires Rig D: run `just release-checks` on this clean commit "
                         "or reuse matching evidence with `just release-checks --evidence <file>`")


def check_clean() -> None:
    if git("status", "--porcelain", "--untracked-files=normal"):
        raise ValueError("Commit or remove local changes before recording a release gate")


def record_status(plan: dict, repository: str, state: str) -> None:
    detail = description(plan["tree"]) if state == "success" else f"Rig D {state}; tree={plan['tree']}"
    subprocess.run(["gh", "api", "--method", "POST", f"repos/{repository}/statuses/{plan['head']}",
                    "-f", f"state={state}", "-f", f"context={CONTEXT}",
                    "-f", f"description={detail}"], cwd=ROOT, check=True, stdout=subprocess.DEVNULL)


def run_gate(plan: dict, evidence_path: Path | None, repository: str) -> None:
    if not plan["sharing_rig"]:
        print("No sharing-impact changes since the previous node release; Rig D is not required.")
        return
    check_clean()
    if evidence_path:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    else:
        out = os.environ.get("A2A_RIG_OUT")
        if not out:
            raise ValueError("Sharing impact detected. Set the five A2A_RIG_* paths; see `just eval-release`.")
        subprocess.run([sys.executable, str(ROOT / "scripts/preflight_release_env.py")], cwd=ROOT, check=True)
        record_status(plan, repository, "pending")
        try:
            subprocess.run([sys.executable, str(ROOT / "scripts/run_any_to_any_release.py")], cwd=ROOT, check=True)
            verdict = json.loads((Path(out) / "release-verdict.json").read_text(encoding="utf-8"))
            if verdict.get("pass") is not True or verdict.get("failures") != []:
                raise ValueError("The sharing rig did not pass")
            check_clean()
            if git("rev-parse", "HEAD^{tree}") != plan["tree"] or git("rev-parse", "HEAD") != plan["head"]:
                raise ValueError("The node checkout changed during the rig; rerun on the final commit")
            evidence = {"schema": 1, "pass": True, "failures": [], "tree": plan["tree"],
                        "head": plan["head"], "base": plan["base"]}
            evidence_path = Path(out) / "node-release-gate.json"
            evidence_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
        except (OSError, ValueError, subprocess.CalledProcessError):
            record_status(plan, repository, "failure")
            raise
        print(f"Saved matching-tree evidence: {evidence_path}")
    validate_evidence(evidence, plan["tree"])
    # Explicit repository prevents accidentally marking another origin. `gh`
    # uses the release operator's existing repository permission, no new secret.
    record_status(plan, repository, "success")
    print(f"Recorded {CONTEXT} for {plan['head'][:12]}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="previous-release")
    parser.add_argument("--verify", action="store_true", help="Read-only publication check")
    parser.add_argument("--force-rig", action="store_true", help="Run the full weekly/manual rig regardless of impact")
    parser.add_argument("--evidence", type=Path, help="Reuse a passing local run with this exact tree")
    parser.add_argument("--repository", default=os.getenv("GITHUB_REPOSITORY", "dialoguesai/topos"))
    args = parser.parse_args()
    try:
        plan = make_plan(args.base)
        if args.force_rig:
            plan["sharing_rig"] = True
        print(summary(plan))
        if os.getenv("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
                stream.write(summary(plan))
        if args.verify:
            if plan["sharing_rig"]:
                token = os.environ.get("GITHUB_TOKEN")
                if not token:
                    raise ValueError("GITHUB_TOKEN is required to verify sharing gate evidence")
                # Bound endpoint/repository and token stay separate from shell text.
                request = Request(f"https://api.github.com/repos/{args.repository}/commits/{plan['head']}/statuses?per_page=100",
                                  headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
                with urlopen(request, timeout=30) as response:
                    verify_statuses(json.load(response), plan["tree"])
            print("Automatic node release policy passed")
        else:
            run_gate(plan, args.evidence, args.repository)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Release policy stopped: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
