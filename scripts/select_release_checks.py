#!/usr/bin/env python3
"""Select node checks from an explicit Git range; unknown changes run everything.

The fast paths are an allowlist, not a guessed dependency graph. CI uses the PR
base (or push's before SHA); publishing uses the previous reachable stable node
tag, so all changes in a release contribute to its requirements.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PRESENTATION = "topos/cli/tray.py"
DRAW_FUNCTIONS = {
    "_mark_box", "_badge_ring", "_glyph_filename", "create_status_image",
    "_badge_ink", "_pt", "_clear_gap", "_draw_ring", "_badge_center",
    "_draw_exclamation_badge", "_draw_cross_badge", "_draw_download_badge",
    "_draw_reindex_badge", "_draw_spinner_badge",
}
OWNER_STATUS = "topos/cli/status_cmd.py"
GUARDS = ["tests/test_lane_shards.py", "tests/test_local_gate_composition.py",
          "tests/test_release_check_selection.py", "tests/release_policy/test_selection.py"]
SCHEMA = 1


def git(*args: str, root: Path = ROOT) -> str:
    return subprocess.check_output(["git", *args], cwd=root, text=True).strip()


def previous_release(head: str = "HEAD", root: Path = ROOT) -> str:
    """Nearest reachable stable *node* tag, excluding a tag on this commit."""
    head = git("rev-parse", f"{head}^{{commit}}", root=root)
    tags = git("tag", "--merged", head, "--list", "v*", root=root).splitlines()
    candidates = [t for t in tags if re.fullmatch(r"v\d+\.\d+\.\d+", t)
                  and git("rev-parse", f"{t}^{{commit}}", root=root) != head]
    if not candidates:
        raise ValueError("No previous stable node tag is available; fetch node tags first")
    # Version order avoids app-v*, prereleases, and an older tag added recently.
    return max(candidates, key=lambda t: tuple(map(int, t[1:].split("."))))


def _doc(path: str) -> bool:
    p = PurePosixPath(path)
    return (not path.startswith(("topos/", "tests/", "scripts/", ".github/"))
            and (p.suffix.lower() in {".md", ".rst"} or path == "LICENSE"
                 or (path.startswith("docs/") and p.suffix.lower() == ".txt")))


def _drawing_only(before: str, after: str) -> bool:
    def skeleton(source: str) -> str:
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in DRAW_FUNCTIONS:
                # Signatures/defaults/decorators still count as shared changes;
                # only these rendering bodies are allowed to vary.
                node.body = [ast.Pass()]
        return ast.dump(tree, include_attributes=False)
    try:
        return skeleton(before) == skeleton(after)
    except SyntaxError:
        return False


def _version_only(path: str, before: str, after: str) -> bool:
    if path == "pyproject.toml":
        def normalize(text: str) -> str:
            # Only the version in [project], never dependency versions.
            lines = text.splitlines(keepends=True)
            in_project = False
            for i, line in enumerate(lines):
                if line.lstrip().startswith("["):
                    in_project = line.strip() == "[project]"
                elif in_project:
                    lines[i] = re.sub(r'^(version\s*=\s*)"[^"\n]+"', r'\1"VERSION"', line, count=1)
            return "".join(lines)
        return before != after and normalize(before) == normalize(after)
    if path == "topos/__version__.py":
        def normalize(text: str) -> str:
            tree = ast.parse(text)
            for node in tree.body:
                if (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)
                        and node.targets[0].id in {"__version__", "__version_info__"}):
                    value = ast.literal_eval(node.value)
                    if node.targets[0].id == "__version__":
                        if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?", value):
                            raise ValueError("invalid literal version")
                    elif not (isinstance(value, tuple) and len(value) == 3
                              and all(type(part) is int and part >= 0 for part in value)):
                        raise ValueError("invalid version tuple")
                    node.value = ast.Constant(value="VERSION")
            return ast.dump(tree, include_attributes=False)
        try:
            return normalize(before) == normalize(after)
        except (SyntaxError, ValueError):
            return False
    if path == "topos/upgrades/manifests.json":
        try:
            old, new = json.loads(before), json.loads(after)
            old_releases, new_releases = old.pop("releases"), new.pop("releases")
            if old != new:
                return False
            old_shipped = [e for e in old_releases if e["version"] != "unreleased"]
            new_shipped = [e for e in new_releases if e["version"] != "unreleased"]
            staged = [e for e in old_releases + new_releases if e["version"] == "unreleased"]
            added = new_shipped[len(old_shipped):]
            return (new_shipped[:len(old_shipped)] == old_shipped
                    and len(new_shipped) >= len(old_shipped)
                    and all(e.get("steps") == [] for e in staged + added)
                    and all(set(e) <= {"version", "steps", "summary", "notes"} for e in staged + added))
        except (ValueError, KeyError, TypeError):
            return False
    return False


def classify(changes: list[tuple[str, str, str | None, str | None]]) -> dict:
    """Each change is (Git status, path, old text, new text). Renames are D+A."""
    targets: set[str] = set()
    reasons: list[str] = []
    full, runtime, metadata, sharing = False, False, False, False
    for status, path, before, after in changes:
        if _doc(path):
            continue
        if status == "M" and before is not None and after is not None and _version_only(path, before, after):
            metadata = True
            continue
        if status == "M" and path == OWNER_STATUS:
            runtime = True
            targets.add("tests/topos/test_status_cmd.py")
            continue
        if (status == "M" and path == PRESENTATION and before is not None
                and after is not None and _drawing_only(before, after)):
            runtime = True
            targets.add("tests/topos/test_cli_tray.py")
            continue
        # Changed test files run exactly those public tests. Shared fixtures,
        # test infrastructure, deleted tests and private/live batteries use full CI.
        if (status in {"A", "M"} and path.startswith(("tests/topos/", "tests/cli/"))
                and PurePosixPath(path).name.startswith("test_") and path.endswith(".py")):
            targets.add(path)
            continue
        full = True
        reasons.append(f"{path}: shared, unmapped, or removed code requires the full suite")
        if path.startswith(("topos/", "shared/")) or path in {"pyproject.toml", "uv.lock"}:
            sharing = True
    if not changes:
        # An unavailable/empty comparison must never look like a docs change.
        full = sharing = True
        reasons.append("No changed paths established; use the full checks")
    mode = "full" if full else "targeted" if targets else "metadata" if metadata else "docs"
    if not reasons:
        reasons.append({"docs": "Documentation only", "metadata": "Release stamping only; no upgrade steps changed",
                        "targeted": "Only explicitly mapped owner status/drawing code or isolated CLI tests changed"}[mode])
    return {
        "schema": SCHEMA, "mode": mode, "run_public": full,
        "run_targeted": bool(targets) and not full,
        "targets": sorted(targets | set(GUARDS)) if targets and not full else [],
        "privacy": full or runtime, "package": full or runtime or metadata,
        "upgrade": full, "sharing_rig": sharing,
        "reasons": reasons, "changed_files": [p for _, p, _, _ in changes],
    }


def make_plan(base: str, head: str = "HEAD", root: Path = ROOT, *, force_full: bool = False) -> dict:
    head = git("rev-parse", f"{head}^{{commit}}", root=root)
    if base == "previous-release":
        base = previous_release(head, root)
    base = git("rev-parse", f"{base}^{{commit}}", root=root)
    # An ancestor is required: a bad push base must fail, never omit changes.
    subprocess.run(["git", "merge-base", "--is-ancestor", base, head], cwd=root, check=True)
    raw = subprocess.check_output(["git", "diff", "--no-renames", "--name-status", "-z", base, head], cwd=root)
    parts = raw.decode("utf-8").rstrip("\0").split("\0") if raw else []
    changes = []
    for i in range(0, len(parts), 2):
        status, path = parts[i:i + 2]
        texts = []
        for ref, present in ((base, status != "A"), (head, status != "D")):
            if not present:
                texts.append(None)
                continue
            data = subprocess.check_output(["git", "show", f"{ref}:{path}"], cwd=root)
            try:
                texts.append(data.decode("utf-8"))
            except UnicodeDecodeError:
                texts.append(None)
        changes.append((status, path, *texts))
    plan = classify(changes)
    if force_full:
        plan.update(mode="full", run_public=True, run_targeted=False, targets=[], privacy=True,
                    package=True, upgrade=True, sharing_rig=True,
                    reasons=["Scheduled/explicit full verification"])
    plan.update(base=base, head=head, tree=git("rev-parse", f"{head}^{{tree}}", root=root))
    return plan


def summary(plan: dict) -> str:
    checks = [name for name, enabled in (("all public shards", plan["run_public"]),
              ("targeted public tests", plan["run_targeted"]), ("privacy", plan["privacy"]),
              ("package/install smoke", plan["package"]), ("upgrade fixtures", plan["upgrade"]),
              ("isolated sharing rig before publication", plan["sharing_rig"])) if enabled]
    return (f"## Node checks: {plan['mode']}\n\n"
            f"Range: `{plan['base'][:12]}..{plan['head'][:12]}`\n\n"
            f"Required: {', '.join(checks) or 'planner validation only'}\n\n"
            + "\n".join(f"- {reason}" for reason in plan["reasons"]) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="previous-release")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--force-full", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    plan = make_plan(args.base, args.head, force_full=args.force_full)
    payload = json.dumps(plan, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload)
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            for name in ("mode", "run_public", "run_targeted", "privacy", "package", "upgrade", "sharing_rig"):
                stream.write(f"{name}={str(plan[name]).lower()}\n")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as stream:
            stream.write(summary(plan))


if __name__ == "__main__":
    main()
