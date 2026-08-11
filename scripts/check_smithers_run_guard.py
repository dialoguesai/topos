#!/usr/bin/env python3
"""Block commits while a Smithers run owns the working tree.

Smithers build workflows tell their agents to leave every change in the working
tree for the owner to review. That instruction is not self-enforcing: a run once
ignored it and pushed three unreviewed commits to main (run-1785412964283). The
owner's answer was a sentinel file plus a git hook, so that an agent physically
cannot commit while a run is in progress:

    touch "$(git rev-parse --git-common-dir)/SMITHERS_NO_COMMIT"   # arm
    rm    "$(git rev-parse --git-common-dir)/SMITHERS_NO_COMMIT"   # lift

That guard lived only in .git/hooks/pre-commit — untracked, on one machine —
which is why the logic now lives here instead.

.git/hooks/pre-commit can only have one owner, and the guard was it. So
`pre-commit install` had never been run in this checkout, and the gitleaks hook
in .pre-commit-config.yaml had been inert since July. That is the hook's whole
point: secret-scan.yml in CI runs the same ruleset, but only after a commit
exists, and this repo is public — by then the secret is in history and, once
pushed, it is out. Catching it at commit time is the difference between an
amended commit and a rotation. As a configured hook the guard composes with
gitleaks instead of displacing it, and `fail_fast` keeps it first and decisive.

A file under .git also travels with nothing, so a fresh clone had no guard at
all — which is exactly the checkout an agent is most likely to be working in.
Here it is version controlled and present the moment someone runs
`uvx pre-commit install`.

Worktrees are why both git dirs are checked. `git rev-parse --git-dir` inside a
worktree resolves to .git/worktrees/<name>, so a sentinel written at the
top-level .git/ was invisible from every worktree. --git-common-dir resolves to
the shared .git in both cases; --git-dir is still honoured so the original
arming instruction keeps working.

Not a security boundary: `git commit --no-verify` bypasses this, as it bypassed
the hook it replaces. It is a guardrail against an agent that has been told not
to commit and does anyway, which is the failure that actually happened.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SENTINEL_NAME = "SMITHERS_NO_COMMIT"


def git_dirs() -> list[Path]:
    """Both the worktree-local and shared git dirs, de-duplicated, in check order."""
    found: list[Path] = []
    for flag in ("--git-common-dir", "--git-dir"):
        result = subprocess.run(
            ["git", "rev-parse", flag],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            continue
        path = Path(result.stdout.strip())
        if not path.parts:
            continue
        resolved = path.resolve()
        if resolved not in found:
            found.append(resolved)
    return found


def main() -> int:
    for git_dir in git_dirs():
        sentinel = git_dir / SENTINEL_NAME
        if sentinel.exists():
            print(
                "BLOCKED: a Smithers run is in progress; committing is disabled.\n"
                "Leave changes in the working tree for the owner to review.\n"
                f"(Owner: rm {sentinel} to lift.)",
                file=sys.stderr,
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
