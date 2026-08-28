#!/usr/bin/env python3
"""Fail if a commit — its FILES or its MESSAGE — mentions the owner's own data.

Docstrings, comments, commit bodies and test fixtures are all published when a
wheel ships, and none of them are covered by the derived-layer privacy sweeps —
those operate on the database. Releases 1.3.26–1.3.30 were withdrawn over a
prompt template that carried measured personal data, and on 2026-08-27 a
remediation branch was about to commit a BLACK-HOLED entity name into five
files, two of them the privacy tests themselves.

A commit message is its own leak surface and a worse one than a file: it is
published the moment the commit is, it is not covered by any file scan, and
rewriting it after a push means rewriting history. The first run of the
file-scanning hook passed a commit whose own message named a home address and a
real goal — this script now guards both, from two pre-commit stages.

This cannot be a hermetic pytest: the names live in the owner's database, which
tests must never open. Run it before committing, or from a pre-push hook.

    uv run python scripts/scan_repo_for_owner_data.py [--database PATH] [--all] [PATHS...]
    uv run python scripts/scan_repo_for_owner_data.py --message-file .git/COMMIT_EDITMSG

Wired into ``.pre-commit-config.yaml``, where pre-commit passes the staged
files, so a leak blocks the commit rather than being found afterwards.

Exit 0 clean (or skipped for want of a database), 1 on a hit.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys

# Surfaces that are real entities on a live node but ordinary English in source.
GENERIC = {
    "unknown", "owner", "home", "west", "east", "north", "south", "avenue",
    "midtown", "southwest", "northeast", "shadow", "porter", "ollama", "claude",
    "anthropic", "github", "google", "apple", "openai", "python", "slack",
}
SKIP_DIRS = (".git/", "node_modules/", ".venv/", "dist/", "build/", "__pycache__/")
# Data fixtures count. A CSV or JSONL sample carrying real rows is the same
# leak as a docstring, and the first version of this scanner missed exactly that
# — a test asserting on a home address read it from an out-of-tree export while
# the address itself sat hardcoded in the assertion.
TEXT_SUFFIXES = (
    ".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".html", ".ts", ".tsx",
    ".csv", ".jsonl", ".ndjson", ".tsv", ".sql", ".rst", ".cfg", ".ini",
)


def _tracked_and_untracked() -> list:
    out = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True
    ).stdout.splitlines()
    return [line.split()[-1] for line in out if line.strip()]


def _all_files() -> list:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True).stdout
    return out.splitlines()


def _protected_names(db_path: str) -> list:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        names = []
        # A black hole is the owner's explicit "do not keep this". Nothing that
        # names one belongs in source, least of all in the tests that prove the
        # black hole works.
        for (name,) in conn.execute("SELECT canonical_name FROM entity_blackholes"):
            names.append(("black-holed entity", str(name or "")))
        # Places locate the owner; a home or gym address in a fixture is the
        # same leak wearing a different hat.
        for (name,) in conn.execute(
            "SELECT DISTINCT place_name FROM location_events WHERE place_name IS NOT NULL"
        ):
            names.append(("place", str(name or "")))
        # Derived goal text is the owner's own words about their own life, and
        # it reads as innocuous prose in a docstring — which is exactly how
        # Two real goals survived two passes of this scanner before it looked
        # here, and a third survived in this very comment, which cited one of
        # them as an example. Describe the shape, never the value — that rule
        # applies to the guard as much as to the code it guards.
        #
        # The floor is higher than for a name because a short goal ("Ship it",
        # "Call Mum") collides with ordinary English and a noisy hook gets
        # removed. Measured at 15: zero false positives across 1,582 files,
        # against 1,893 goals protected.
        try:
            for (text,) in conn.execute(
                "SELECT DISTINCT goal_text FROM user_goals WHERE goal_text IS NOT NULL"
            ):
                t = str(text or "").strip()
                if len(t) >= 15:
                    names.append(("goal text", t))
        except sqlite3.Error:
            pass
        return names
    finally:
        conn.close()


def _find(haystack: str, needle: str) -> int:
    """1-indexed line of the first hit, or 0."""
    for i, line in enumerate(haystack.splitlines()):
        if needle in line:
            return i + 1
    return 0


def _verify_commit_msg_hook() -> int:
    """Refuse to commit at all unless the MESSAGE guard is actually wired.

    ``pre-commit install`` wires only the pre-commit stage, so a checkout that
    follows the documented one-liner gets file scanning and NO message scanning,
    silently. This repo has already lost a guard exactly that way once — its own
    config records gitleaks sitting inert for a month because `pre-commit
    install` had never been run here.

    Running this from an ``always_run`` pre-commit-stage hook means the absence
    is loud and immediate instead of discovered by a leak.
    """
    hook = os.path.join(".git", "hooks", "commit-msg")
    try:
        body = open(hook, encoding="utf-8", errors="ignore").read()
    except OSError:
        body = ""
    if "hook-type=commit-msg" in body:
        return 0
    print(
        "the commit-message guard is NOT installed in this checkout.\n\n"
        "  uvx pre-commit install --hook-type commit-msg\n\n"
        "Without it, files are scanned and the commit MESSAGE is not — which is "
        "the surface that publishes with the commit and cannot be fixed after a "
        "push without rewriting history.",
        file=sys.stderr,
    )
    return 1


def _scan_text(body: str, names: list, *, where: str) -> int:
    # Comment lines are stripped by git and never become part of the message.
    body = "\n".join(l for l in body.splitlines() if not l.startswith("#"))
    folded = body.lower().replace("\u2019", "'")

    hits = []
    for kind, name in names:
        needle = name.strip().lower().replace("\u2019", "'")
        if needle and needle in folded:
            hits.append((kind, name, _find(folded, needle)))
    if not hits:
        print(f"clean — {where} checked against {len(names)} protected names")
        return 0

    print(f"{len(hits)} leak(s) of the owner's own data in the {where}:\n", file=sys.stderr)
    for kind, name, line in hits:
        print(f"  {kind:<20} {name!r}  (line {line})", file=sys.stderr)
    print(
        "\nA message is published with the commit and cannot be scrubbed later "
        "without rewriting history. Describe the shape, not the value: "
        "\"a home address\" rather than the address.",
        file=sys.stderr,
    )
    return 1


def _scan_message(path: str, names: list) -> int:
    try:
        body = open(path, encoding="utf-8", errors="ignore").read()
    except OSError as exc:
        print(f"cannot read commit message at {path}: {exc}", file=sys.stderr)
        return 0
    return _scan_text(body, names, where="commit MESSAGE")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database", default=os.path.expanduser("~/.topos/database.db"))
    ap.add_argument("--all", action="store_true", help="scan every tracked file, not just changed ones")
    ap.add_argument("--min-length", type=int, default=8)
    ap.add_argument(
        "--text", help="scan a literal string — for checking a DRAFT message before writing it.",
    )
    ap.add_argument(
        "--verify-install", action="store_true",
        help="fail unless the commit-msg hook is actually wired into this checkout.",
    )
    ap.add_argument(
        "--message-file",
        help="scan a commit message instead of files; pre-commit passes this at "
             "the commit-msg stage.",
    )
    ap.add_argument(
        "paths", nargs="*",
        help="files to scan; pre-commit passes the staged ones. Defaults to the "
             "working tree's changed files.",
    )
    args = ap.parse_args()

    if not os.path.exists(args.database):
        # No database is the CI and fresh-clone case. Failing here would fail
        # every commit on any machine without a live node, and a hook that
        # always fails gets removed — which costs more than it saves. The check
        # is real where the data is.
        print(f"SKIPPED — no database at {args.database}, nothing to compare against")
        return 0

    names = [
        (kind, n) for kind, n in _protected_names(args.database)
        if len(n.strip()) >= args.min_length and n.strip().lower() not in GENERIC
    ]
    if args.verify_install:
        return _verify_commit_msg_hook()

    if args.text is not None:
        return _scan_text(args.text, names, where="draft message")

    if args.message_file:
        return _scan_message(args.message_file, names)

    files = args.paths or (_all_files() if args.all else _tracked_and_untracked())
    files = [
        f for f in files
        if os.path.isfile(f) and f.endswith(TEXT_SUFFIXES)
        and not any(s in f for s in SKIP_DIRS)
    ]

    hits = []
    for path in files:
        try:
            body = open(path, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        # Curly and straight apostrophes are the same name to a reader and
        # different strings to a grep, which is how one of these got through.
        folded = body.lower().replace("’", "'")
        for kind, name in names:
            needle = name.strip().lower().replace("’", "'")
            if needle and needle in folded:
                line = next(
                    (i + 1 for i, ln in enumerate(folded.splitlines()) if needle in ln), 0
                )
                hits.append((kind, name, path, line))

    if not hits:
        print(f"clean — {len(files)} files checked against {len(names)} protected names")
        return 0

    print(f"{len(hits)} leak(s) of the owner's own data:\n", file=sys.stderr)
    for kind, name, path, line in hits:
        print(f"  {kind:<20} {name!r}\n      {path}:{line}", file=sys.stderr)
    print(
        "\nReplace with a synthetic name that keeps the same shape "
        "(compound hyphen, possessive, containment) so the test still means what it did.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
