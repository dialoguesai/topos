#!/usr/bin/env python3
"""Stamp unreleased → version, bump package metadata, regenerate snapshots.

Implements PLAN_NODE_RELEASE_MIGRATIONS §4d / §5 release cut:

  python scripts/cut_release.py --version 1.3.3
  python scripts/cut_release.py --bump patch   # or minor | major

Does NOT commit, push, or tag — the release-topos skill / human does that
after reviewing the diff and running privacy eval + just gate.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = REPO_ROOT / "topos" / "upgrades" / "manifests.json"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
VERSION_PY = REPO_ROOT / "topos" / "__version__.py"
PYPROJECT = REPO_ROOT / "pyproject.toml"
UNRELEASED = "unreleased"


def _parse_version(version: str) -> tuple[int, int, int]:
    parts = version.strip().split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        raise SystemExit(f"version must be X.Y.Z, got {version!r}")
    return int(parts[0]), int(parts[1]), int(parts[2])


def _read_current_version() -> str:
    text = VERSION_PY.read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not m:
        raise SystemExit(f"cannot parse version from {VERSION_PY}")
    return m.group(1)


def _bump(kind: str) -> str:
    major, minor, patch = _parse_version(_read_current_version())
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise SystemExit(f"unknown bump kind {kind!r}")


def _stamp_manifest(version: str) -> None:
    data = json.loads(MANIFESTS.read_text(encoding="utf-8"))
    releases = data.get("releases") or []
    unreleased = None
    rest = []
    for release in releases:
        if release.get("version") == UNRELEASED:
            unreleased = release
        else:
            rest.append(release)
    if unreleased is None:
        raise SystemExit(
            "manifests.json has no \"unreleased\" staging entry — "
            "add one before cutting a release"
        )
    for release in rest:
        if release.get("version") == version:
            raise SystemExit(f"manifests.json already has a {version} entry")

    stamped = dict(unreleased)
    stamped["version"] = version
    if not stamped.get("summary") or "Staging entry" in str(stamped.get("summary")):
        stamped["summary"] = f"Release {version}."
    rest.append(stamped)
    # Fresh empty staging for the next cycle.
    rest.append(
        {
            "version": UNRELEASED,
            "summary": (
                "Staging entry for in-flight PRs. cut_release.py stamps this "
                "with the next version at release cut. Never executed by steps_between."
            ),
            "steps": [],
            "notes": [],
        }
    )
    data["releases"] = rest
    MANIFESTS.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"stamped manifests.json → {version}")


def _stamp_changelog(version: str) -> None:
    if not CHANGELOG.is_file():
        raise SystemExit(f"missing {CHANGELOG}")
    text = CHANGELOG.read_text(encoding="utf-8")
    heading = "## [Unreleased]"
    if heading not in text:
        raise SystemExit("CHANGELOG.md missing ## [Unreleased] section")
    today = date.today().isoformat()
    replacement = f"## [Unreleased]\n\n## [{version}] — {today}"
    # If Unreleased is empty aside from whitespace/headings, still stamp.
    new_text = text.replace(heading, replacement, 1)
    if new_text == text:
        raise SystemExit("failed to rewrite Unreleased section")
    CHANGELOG.write_text(new_text, encoding="utf-8")
    print(f"stamped CHANGELOG.md → [{version}]")


def _bump_version_files(version: str) -> None:
    major, minor, patch = _parse_version(version)
    VERSION_PY.write_text(
        (
            '"""Topos Control Plane Version Information."""\n\n'
            f'__version__ = "{version}"\n'
            f"__version_info__ = ({major}, {minor}, {patch})\n\n"
            '__all__ = ["__version__", "__version_info__"]\n'
        ),
        encoding="utf-8",
    )
    pp = PYPROJECT.read_text(encoding="utf-8")
    pp_new, n = re.subn(
        r'(?m)^version = ".*"', f'version = "{version}"', pp, count=1
    )
    if n != 1:
        raise SystemExit("failed to update pyproject.toml version")
    PYPROJECT.write_text(pp_new, encoding="utf-8")
    print(f"bumped package version → {version}")


def _python() -> list[str]:
    """Prefer ``uv run python`` so editable deps resolve outside the system interpreter."""
    try:
        subprocess.check_call(
            ["uv", "--version"],
            cwd=str(REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return ["uv", "run", "python"]
    except (FileNotFoundError, subprocess.CalledProcessError):
        return [sys.executable]


def _regen_checksums() -> None:
    subprocess.check_call(
        [*_python(), str(REPO_ROOT / "scripts" / "sync_migration_checksums.py"), "--write"],
        cwd=str(REPO_ROOT),
    )


def _regen_handled_types() -> None:
    dump = REPO_ROOT / "scripts" / "dump_handled_types.py"
    out = REPO_ROOT / "topos" / "protocol" / "handled_message_types.json"
    if not dump.is_file():
        print("skip handled_message_types regen (script missing)")
        return
    payload = subprocess.check_output(
        [*_python(), str(dump)], cwd=str(REPO_ROOT), text=True
    )
    out.write_text(payload if payload.endswith("\n") else payload + "\n", encoding="utf-8")
    print(f"regenerated {out.relative_to(REPO_ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--version", help="explicit X.Y.Z to stamp")
    g.add_argument(
        "--bump",
        choices=("patch", "minor", "major"),
        help="bump from current topos/__version__.py",
    )
    parser.add_argument(
        "--skip-snapshots",
        action="store_true",
        help="do not regenerate checksums / handled types",
    )
    args = parser.parse_args()
    version = args.version or _bump(args.bump)
    _parse_version(version)

    _stamp_manifest(version)
    _stamp_changelog(version)
    _bump_version_files(version)
    if not args.skip_snapshots:
        _regen_checksums()
        _regen_handled_types()

    print()
    print(f"Release {version} files are stamped.")
    print("Next (release-topos skill):")
    print("  1. Review the diff (manifest entry IS the data-impact review)")
    print("  2. just eval-release")
    print("  3. just gate")
    print("  4. commit, push main, tag v{version}, push tag".format(version=version))


if __name__ == "__main__":
    main()
