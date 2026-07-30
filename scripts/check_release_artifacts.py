#!/usr/bin/env python3
"""Publish / CI guards for release artifacts (PLAN §4d.4 / §6).

Checks:
  * manifests.json has an entry for the given version (empty steps OK)
  * no leftover \"unreleased\" executable work is required at tag time —
    unreleased may exist empty for the next cycle, but publish requires the
    tagged version to be stamped (not still named unreleased)
  * CHANGELOG.md has a ``## [X.Y.Z]`` section
  * every step kind has an executor (or is ``none``)
  * depends_on ids resolve within the release's steps
  * migration checksums are clean
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = REPO_ROOT / "topos" / "upgrades" / "manifests.json"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

# Keep in sync with topos.upgrades.runner.DEFAULT_EXECUTORS + kind "none".
EXECUTABLE_KINDS = frozenset({
    "enrichment_reprocess",
    "engine_endpoint",
    "canonical_reprocess",
    "derived_rebuild",
    "reembed",
    "none",
})


def _load_releases() -> list[dict]:
    return json.loads(MANIFESTS.read_text(encoding="utf-8"))["releases"]


def check_manifest(version: str) -> None:
    releases = _load_releases()
    match = next((r for r in releases if r.get("version") == version), None)
    if match is None:
        raise SystemExit(
            f"manifests.json has no entry for {version}. "
            "Run scripts/cut_release.py before tagging."
        )
    if any(r.get("version") == "unreleased" and (r.get("steps") or []) for r in releases):
        raise SystemExit(
            "manifests.json still has non-empty \"unreleased\" steps — "
            "stamp them with cut_release.py before tagging"
        )
    # Validate all releases' step graph (not only the tagged one).
    for release in releases:
        if release.get("version") == "unreleased":
            continue
        steps = release.get("steps") or []
        ids = {s.get("id") for s in steps}
        for step in steps:
            kind = step.get("kind")
            if kind not in EXECUTABLE_KINDS:
                raise SystemExit(
                    f"release {release['version']} step {step.get('id')!r} "
                    f"has unknown kind {kind!r}"
                )
            for dep in step.get("depends_on") or []:
                if dep not in ids:
                    raise SystemExit(
                        f"release {release['version']} step {step.get('id')!r} "
                        f"depends_on unknown id {dep!r}"
                    )
    print(f"manifest ok for {version}")


def check_changelog(version: str) -> None:
    text = CHANGELOG.read_text(encoding="utf-8")
    if not re.search(rf"^## \[{re.escape(version)}\]", text, flags=re.M):
        raise SystemExit(
            f"CHANGELOG.md missing ## [{version}] section — "
            "run scripts/cut_release.py before tagging"
        )
    print(f"changelog ok for {version}")


def check_checksums() -> None:
    subprocess.check_call(
        [sys.executable, str(REPO_ROOT / "scripts" / "sync_migration_checksums.py"), "--check"],
        cwd=str(REPO_ROOT),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        required=True,
        help="version under review (tag without v, or pyproject version)",
    )
    parser.add_argument(
        "--skip-checksums",
        action="store_true",
        help="skip migration checksum verification",
    )
    args = parser.parse_args()
    check_manifest(args.version)
    check_changelog(args.version)
    if not args.skip_checksums:
        check_checksums()
    print("release artifacts ok")


if __name__ == "__main__":
    main()
