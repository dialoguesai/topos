#!/usr/bin/env python3
"""Sync dependency pins in pyproject.toml from uv.lock.

PyPI installs only read project.dependencies, not uv.lock. Run this before
each topos-node release so end-user upgrades do not float the whole tree.

Usage:
    cd topos
    uv lock
    python scripts/sync-dep-pins.py          # update pyproject.toml
    python scripts/sync-dep-pins.py --check  # exit 1 if pins are stale
    python scripts/sync-dep-pins.py --dry-run
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
LOCKFILE = ROOT / "uv.lock"

# Transitive deps pinned explicitly so they do not float on `uv tool upgrade`.
TRANSITIVE_PINS = ("huggingface-hub", "tqdm", "hf-xet")

# lock package name when it differs from the pyproject dependency string
LOCK_ALIASES: dict[str, str] = {
    "uvicorn[standard]": "uvicorn",
    "psycopg[binary]": "psycopg",
}


@dataclass(frozen=True)
class DepSection:
    header: str
    names: tuple[str, ...]


SECTIONS = (
    DepSection(
        "dependencies",
        (
            "fastapi",
            "uvicorn[standard]",
            "httpx",
            "pydantic",
            "pydantic-settings",
            "websockets",
            "certifi",
            "cryptography",
            "python-multipart",
            "click",
            "packaging",
            "mcp",
            "google-cloud-storage",
            "google-cloud-run",
            "networkx",
            "transformers",
            "huggingface-hub",
            "tqdm",
            "hf-xet",
            "torch",
            "python-louvain",
            "asgi-lifespan",
        ),
    ),
    DepSection(
        "engine",
        ("duckdb", "psycopg[binary]"),
    ),
    DepSection(
        "signal",
        ("pysqlcipher3",),
    ),
    DepSection(
        "dev",
        ("pytest", "pytest-asyncio", "httpx"),
    ),
)

# Packages where ~= from lock is too loose or lock version is not portable.
CUSTOM_SPECS: dict[str, str] = {
    "torch": ">=2.8.0,<3",
    "networkx": ">=3.2.1,<3.7",
}


def parse_lock_versions(lock_text: str) -> dict[str, str]:
    versions: dict[str, str] = {}
    current_name: str | None = None
    for line in lock_text.splitlines():
        name_match = re.match(r'^name = "([^"]+)"$', line)
        if name_match:
            current_name = name_match.group(1)
            continue
        version_match = re.match(r'^version = "([^"]+)"$', line)
        if version_match and current_name:
            version = version_match.group(1)
            # Prefer the highest resolved version when uv.lock has marker splits.
            if current_name not in versions or _version_key(version) > _version_key(versions[current_name]):
                versions[current_name] = version
            current_name = None
    return versions


def _version_key(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in version.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _version_parts(version: str) -> tuple[int, int, int]:
    parts = version.split(".")
    nums = [int("".join(ch for ch in p if ch.isdigit()) or "0") for p in parts[:3]]
    while len(nums) < 3:
        nums.append(0)
    return nums[0], nums[1], nums[2]


def compatible_spec(version: str) -> str:
    major, minor, patch = _version_parts(version)
    if major == 0 and minor == 0:
        return f">={version},<{major}.{minor}.{patch + 1}"
    if patch == 0 and len(version.split(".")) == 2:
        return f">={version},<{major}.{minor + 1}"
    return f"~={version}"


def resolve_spec(dep: str, versions: dict[str, str]) -> str:
    if dep in CUSTOM_SPECS:
        return CUSTOM_SPECS[dep]
    lock_name = LOCK_ALIASES.get(dep, dep)
    version = versions.get(lock_name)
    if not version:
        raise KeyError(f"{dep} ({lock_name}) not found in {LOCKFILE}")
    return compatible_spec(version)


def build_section_lines(names: tuple[str, ...], versions: dict[str, str]) -> list[str]:
    lines: list[str] = []
    for dep in names:
        spec = resolve_spec(dep, versions)
        lines.append(f'  "{dep}{spec}",')
    return lines


def replace_section(content: str, section: DepSection, lines: list[str]) -> str:
    if section.header == "dependencies":
        pattern = re.compile(
            r"(dependencies = \[)\n(?:  \"[^\"]+\",\n)+\]",
            re.MULTILINE,
        )
        replacement = "dependencies = [\n" + "\n".join(lines) + "\n]"
    else:
        pattern = re.compile(
            rf"({section.header} = \[)\n(?:  \"[^\"]+\",\n)+\]",
            re.MULTILINE,
        )
        replacement = f"{section.header} = [\n" + "\n".join(lines) + "\n]"
    updated, count = pattern.subn(replacement, content, count=1)
    if count != 1:
        raise RuntimeError(f"Could not update [{section.header}] in {PYPROJECT}")
    return updated


def expected_content(versions: dict[str, str]) -> str:
    content = PYPROJECT.read_text(encoding="utf-8")
    for section in SECTIONS:
        lines = build_section_lines(section.names, versions)
        content = replace_section(content, section, lines)
    return content


def current_specs(content: str, dep: str) -> str | None:
    escaped = re.escape(dep)
    match = re.search(rf'"{escaped}([^"]+)"', content)
    return match.group(1) if match else None


def check_pins(content: str, versions: dict[str, str]) -> list[str]:
    errors: list[str] = []
    for section in SECTIONS:
        for dep in section.names:
            expected = resolve_spec(dep, versions)
            actual = current_specs(content, dep)
            if actual is None:
                errors.append(f"missing dependency entry: {dep}")
            elif actual != expected:
                errors.append(f"{dep}: have {dep}{actual}, want {dep}{expected}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if pyproject pins are stale")
    parser.add_argument("--dry-run", action="store_true", help="print changes without writing")
    args = parser.parse_args()

    if not LOCKFILE.exists():
        print(f"error: lockfile not found: {LOCKFILE}", file=sys.stderr)
        return 1

    versions = parse_lock_versions(LOCKFILE.read_text(encoding="utf-8"))
    content = PYPROJECT.read_text(encoding="utf-8")
    errors = check_pins(content, versions)

    if args.check:
        if errors:
            print("Dependency pins are out of sync with uv.lock:", file=sys.stderr)
            for err in errors:
                print(f"  - {err}", file=sys.stderr)
            print(f"\nRun: cd {ROOT} && uv lock && python scripts/sync-dep-pins.py", file=sys.stderr)
            return 1
        print("Dependency pins match uv.lock.")
        return 0

    if not errors:
        print("Dependency pins already match uv.lock.")
        return 0

    updated = expected_content(versions)
    if args.dry_run:
        print(updated)
        return 0

    PYPROJECT.write_text(updated, encoding="utf-8")
    print(f"Updated {PYPROJECT.relative_to(ROOT)} ({len(errors)} pin(s) changed).")
    for err in errors:
        print(f"  - {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
