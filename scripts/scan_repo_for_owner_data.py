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
    # Transcript role labels the extractor mints as person entities. They name
    # a turn, not a human, and every diarised fixture in the tree contains them.
    "speaker 1", "speaker 2", "speaker 3", "speaker one", "speaker two",
}
SKIP_DIRS = (".git/", "node_modules/", ".venv/", "dist/", "build/", "__pycache__/")

# An on-device list of extra terms to protect, deliberately OUTSIDE the repo.
#
# The database can only tell us about people the node has already seen. It knows
# nothing about a landlord, a doctor, a child's school, a family member never
# messaged from this machine — and those are exactly the names someone would
# paste into a docstring while debugging. This is where they go.
#
# It lives outside the working tree on purpose. A .gitignore entry is one
# `git add -f`, one `git add -A` from a different directory, or one contributor
# who removes the line, away from being committed — and a list of protected
# names is the single worst file to commit by accident. A path that is not in
# the tree cannot be added to it.
DEFAULT_LOCAL_TERMS = os.path.expanduser("~/.topos/private-terms.txt")
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


def _load_local_terms(path: str) -> list:
    """Extra protected terms from an on-device file.

    Refuses a path inside the repository. A list of the names you are hiding is
    the worst possible thing to commit, and the mistake is easy: put it in the
    tree "just for now", gitignore it, and it survives until someone runs
    `git add -f` or clones and re-adds it. Making it un-committable by location
    is stronger than making it ignored by convention.
    """
    if not path:
        return []
    resolved = os.path.realpath(os.path.expanduser(path))
    repo_root = os.path.realpath(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True
        ).stdout.strip() or "."
    )
    if resolved.startswith(repo_root + os.sep):
        raise SystemExit(
            f"refusing to read protected terms from inside the repository:\n"
            f"  {resolved}\n"
            f"Move it outside the working tree — {DEFAULT_LOCAL_TERMS} is the default.\n"
            f"A gitignored file is one `git add -f` away from being committed."
        )
    try:
        lines = open(resolved, encoding="utf-8").read().splitlines()
    except OSError:
        return []
    out = []
    for raw in lines:
        term = raw.split("#", 1)[0].strip()
        if term:
            out.append(("local list", term))
    return out


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
        # The OWNER's own name. Not every person — 1,249 contacts would collide
        # with ordinary English ("Unknown", "Claude", "Porter") and a noisy hook
        # gets deleted, which costs more than it catches. The owner is the one
        # person whose name is both unambiguous and everywhere.
        #
        # The floor keeps the FULL name and drops the bare first name: "Jonny"
        # is 5 characters and appears in synthetic fixtures
        # ("jonny@example.com") that are not a leak, while the full name is
        # specific enough to mean only one person.
        try:
            for (display,) in conn.execute(
                "SELECT DISTINCT display_name FROM user_identity"
                " WHERE display_name IS NOT NULL"
            ):
                full = str(display or "").strip()
                if len(full) < 8:
                    continue
                names.append(("owner name", full))
                # Handle form: a display name is also how the owner's accounts
                # are named, and a login is the same leak wearing different
                # punctuation — no name scan would see it. Derived from the
                # display name, never hardcoded. (Writing an example of one here
                # is what this scanner caught on its own second run. Describe
                # the shape, not the value, applies to the guard as well.)
                squashed = "".join(full.split()).lower()
                if len(squashed) >= 8:
                    names.append(("owner handle", squashed))
        except sqlite3.Error:
            pass
        # Other people. Restricted to FULL names — a name with a space in it —
        # because that is what makes a person identifiable and what keeps this
        # usable. Single first names are most of the 1,249 contacts and collide
        # with ordinary English ("Unknown", "Porter", "May"); a hook that fires
        # on those gets deleted, and a deleted hook protects nobody.
        #
        # Measured 2026-08-28: 562 full names in the database, **35 of them
        # present in tracked files across 71 sites** — roughly 25 private
        # individuals plus public figures from the owner's reading. None were
        # reachable by the place, goal, black-hole or owner-name scans.
        try:
            for query in (
                "SELECT DISTINCT canonical_name FROM entities"
                " WHERE entity_type='person' AND canonical_name IS NOT NULL",
                "SELECT DISTINCT display_name FROM contacts WHERE display_name IS NOT NULL",
            ):
                for (person,) in conn.execute(query):
                    full = str(person or "").strip()
                    # A space is the whole filter: a first name and a surname
                    # together identify a human; a first name alone
                    # identifies a string. (Naming a real example here is what
                    # this scanner caught on its own third run.)
                    if " " in full and len(full) >= 6:
                        names.append(("person name", full))
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
        "--local-terms", default=os.environ.get("TOPOS_PRIVATE_TERMS", DEFAULT_LOCAL_TERMS),
        help="on-device file of extra terms to protect; never inside the repo.",
    )
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

    if not os.path.exists(args.database) and not _load_local_terms(args.local_terms):
        # No database AND no local list is the CI and fresh-clone case. Failing
        # here would fail every commit on any machine without a live node, and a
        # hook that always fails gets removed — which costs more than it saves.
        # The check is real where the data is.
        print(f"SKIPPED — no database at {args.database} and no local terms file")
        return 0

    from_db = _protected_names(args.database) if os.path.exists(args.database) else []
    names = [
        (kind, n) for kind, n in from_db
        if len(n.strip()) >= args.min_length and n.strip().lower() not in GENERIC
    ]
    # Local terms bypass the length floor and the GENERIC list. Those exist to
    # keep DATABASE-derived names from flooding the hook; a term you typed by
    # hand is already a deliberate choice, and second-guessing it would silently
    # drop exactly the short name someone went out of their way to protect.
    local = _load_local_terms(args.local_terms)
    names.extend(local)
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
        src = f"{len(names)} protected names"
        src += f" ({len(local)} from {args.local_terms})" if local else " (no local terms file)"
        print(f"clean — {len(files)} files checked against {src}")
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
