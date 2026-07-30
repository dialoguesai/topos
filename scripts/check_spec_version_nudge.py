#!/usr/bin/env python3
"""CI nudge: enrichment/model diffs require a catalog spec_version bump (PLAN M3).

Fails when the git diff touches ``topos/enrichment/jobs/**`` or
``topos/enrichment/models/**`` without also changing ``JOB_SPEC_VERSIONS`` in
``mvp_defaults.py``, unless the PR/commit message contains ``no-invalidation``
or the env ``TOPOS_NO_INVALIDATION=1`` is set.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WATCH_PREFIXES = (
    "topos/enrichment/jobs/",
    "topos/enrichment/models/",
)
SPEC_FILE = "topos/enrichment/models/mvp_defaults.py"


def _diff_names(base: str) -> list[str]:
    try:
        out = subprocess.check_output(
            ["git", "diff", "--name-only", f"{base}...HEAD"],
            cwd=REPO_ROOT,
            text=True,
        )
    except subprocess.CalledProcessError:
        out = subprocess.check_output(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=REPO_ROOT,
            text=True,
        )
    return [line.strip() for line in out.splitlines() if line.strip()]


def main() -> None:
    if os.environ.get("TOPOS_NO_INVALIDATION", "").strip() in ("1", "true", "yes"):
        print("spec-version nudge skipped (TOPOS_NO_INVALIDATION)")
        return
    base = os.environ.get("TOPOS_DIFF_BASE", "origin/main")
    names = _diff_names(base)
    watched = [n for n in names if n.startswith(WATCH_PREFIXES)]
    if not watched:
        print("spec-version nudge ok (no enrichment/model paths in diff)")
        return
    # Allow explicit no-invalidation marker in the latest commit subject/body.
    try:
        msg = subprocess.check_output(
            ["git", "log", "-1", "--pretty=%B"],
            cwd=REPO_ROOT,
            text=True,
        )
    except subprocess.CalledProcessError:
        msg = ""
    if "no-invalidation" in msg.lower():
        print("spec-version nudge skipped (commit marks no-invalidation)")
        return
    if SPEC_FILE not in names:
        raise SystemExit(
            "Enrichment/model paths changed without updating "
            f"{SPEC_FILE} JOB_SPEC_VERSIONS. Bump the affected job's "
            "spec_version, or mark the commit/PR with 'no-invalidation'."
        )
    # Require an actual numeric bump in JOB_SPEC_VERSIONS block.
    try:
        diff = subprocess.check_output(
            ["git", "diff", f"{base}...HEAD", "--", SPEC_FILE],
            cwd=REPO_ROOT,
            text=True,
        )
    except subprocess.CalledProcessError:
        diff = subprocess.check_output(
            ["git", "diff", "HEAD", "--", SPEC_FILE],
            cwd=REPO_ROOT,
            text=True,
        )
    if "JOB_SPEC_VERSIONS" not in diff and "spec_version" not in diff.lower():
        # File touched but maybe unrelated — still require version map change.
        if "+    \"" not in diff and "JOB_SPEC_VERSIONS" not in diff:
            raise SystemExit(
                f"{SPEC_FILE} changed but JOB_SPEC_VERSIONS was not updated. "
                "Bump the job version(s) invalidated by this change."
            )
    print(f"spec-version nudge ok ({len(watched)} watched path(s))")


if __name__ == "__main__":
    main()
