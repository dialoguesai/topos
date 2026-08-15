#!/usr/bin/env python3
"""Fail fast, and in one line, when the release env cannot run the gates.

`pytest` is declared under `[project.optional-dependencies] dev`, not a default
dependency group, so a plain `uv sync` in a fresh checkout or worktree installs
everything the package needs at runtime and nothing the gates need to check it.
Nothing then says so. The privacy eval runs its harnesses as subprocesses and
reports what came back, which was five failures — two of them
`UAR != 0` / `CER != 0`, i.e. the shape of a privacy incident — for a missing
test runner.

That cost more than the minute it takes to install. This turns it into one
message naming the cause and the fix, before any gate has a chance to
mis-describe it.

Checks only what a gate cannot run without. Anything a gate can itself report
honestly belongs in the gate, not here.
"""

from __future__ import annotations

import importlib.util
import sys

#: (module, why the release gates need it, what installs it)
REQUIRED = (
    ("pytest", "the public test lane and the privacy pytest gate", "uv sync --extra dev"),
    ("pytest_asyncio", "async tests in the public lane", "uv sync --extra dev"),
    ("httpx", "the release smoke test's TestClient boot", "uv sync --extra dev"),
)


def main() -> int:
    missing = [
        (name, why, fix)
        for name, why, fix in REQUIRED
        if importlib.util.find_spec(name) is None
    ]
    if not missing:
        return 0

    fixes = sorted({fix for _, _, fix in missing})
    print("release env preflight FAILED — the gates cannot run here.", file=sys.stderr)
    print(file=sys.stderr)
    for name, why, _ in missing:
        print(f"  missing {name!r} — needed for {why}", file=sys.stderr)
    print(file=sys.stderr)
    print(f"  fix: {' && '.join(fixes)}", file=sys.stderr)
    print(
        "\n  (`dev` is an optional extra, so a plain `uv sync` leaves it out.\n"
        "   Without this check the privacy eval reports the absence as failed\n"
        "   privacy gates, which is a much louder claim than the truth.)",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
