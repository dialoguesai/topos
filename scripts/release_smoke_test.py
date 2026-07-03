#!/usr/bin/env python3
"""Smoke-test a built topos-node wheel the way PyPI users install it.

Creates an isolated virtualenv, pip-installs the wheel (optionally after upgrading
from a prior PyPI release), then verifies CLI entry points and critical imports.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

IMPORT_CHECK_SCRIPT = """
import importlib
import sys

modules = [
    "topos",
    "topos.app",
    "topos.cli.commands",
    "topos.core.logging",
    "topos.engine.backends.huggingface",
    "topos.sanitization.privacy_filter",
    "topos.sanitization.nsfw_classifier",
    "transformers",
    "sentence_transformers",
    "torch",
]
failures = []
for name in modules:
    try:
        importlib.import_module(name)
    except Exception as exc:
        failures.append(f"{name}: {exc}")
if failures:
    print("import_check_failed", file=sys.stderr)
    for line in failures:
        print(f"  {line}", file=sys.stderr)
    raise SystemExit(1)
print("imports_ok")
"""

APP_BOOT_CHECK = """
import os

os.environ.setdefault("TOPOS_SKIP_UPDATE_CHECK", "1")
os.environ.setdefault("TOPOS_KEY", "release-smoke-test-key")

from fastapi.testclient import TestClient
from topos.app import app

client = TestClient(app)
for path in ("/", "/healthcheck", "/version"):
    response = client.get(path)
    if response.status_code != 200:
        raise SystemExit(f"{path} returned {response.status_code}: {response.text[:200]}")
print("app_boot_ok")
"""


def _run(cmd: list[str], *, env: dict[str, str] | None = None, timeout: int | None = None) -> None:
    print("+ " + " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, env=env, timeout=timeout)


def _find_wheel(dist_dir: Path) -> Path:
    wheels = sorted(dist_dir.glob("topos_node-*.whl"))
    if not wheels:
        raise SystemExit(f"No topos_node wheel found under {dist_dir}")
    return wheels[-1]


def _previous_pypi_version(current: str) -> str | None:
    from packaging.version import Version

    with urllib.request.urlopen("https://pypi.org/pypi/topos-node/json", timeout=60) as resp:
        data = json.load(resp)
    versions = [
        v
        for v in data.get("releases", {})
        if v and not Version(v).is_prerelease and data["releases"][v]
    ]
    older = sorted((v for v in versions if Version(v) < Version(current)), key=Version)
    return older[-1] if older else None


def _install_and_verify(python: Path, wheel: Path, *, upgrade_from: str | None) -> None:
    pip = [str(python), "-m", "pip"]
    _run([*pip, "install", "--upgrade", "pip"])
    if upgrade_from:
        _run([*pip, "install", f"topos-node=={upgrade_from}"])
        _run([*pip, "install", "--upgrade", str(wheel)])
    else:
        _run([*pip, "install", str(wheel)])

    bin_dir = python.parent
    topos_node = bin_dir / ("topos-node.exe" if os.name == "nt" else "topos-node")
    if not topos_node.exists():
        raise SystemExit(f"Console script missing after install: {topos_node}")

    env = os.environ.copy()
    env["TOPOS_SKIP_UPDATE_CHECK"] = "1"
    env["TOPOS_KEY"] = "release-smoke-test-key"
    _run([str(topos_node), "--help"], env=env)
    _run([str(topos_node), "--discover"], env=env)
    _run([str(python), "-c", IMPORT_CHECK_SCRIPT], env=env)
    _run([str(python), "-c", APP_BOOT_CHECK], env=env, timeout=120)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wheel",
        type=Path,
        help="Path to topos_node wheel (default: newest in dist/)",
    )
    parser.add_argument(
        "--dist-dir",
        type=Path,
        default=Path("dist"),
        help="Directory containing built wheels (default: dist/)",
    )
    parser.add_argument(
        "--upgrade-from",
        metavar="VERSION",
        help="Install this PyPI version first, then upgrade to --wheel",
    )
    parser.add_argument(
        "--test-upgrade",
        action="store_true",
        help="Auto-pick the latest PyPI release older than the wheel version",
    )
    args = parser.parse_args(argv)

    wheel = args.wheel or _find_wheel(args.dist_dir)
    if not wheel.is_file():
        raise SystemExit(f"Wheel not found: {wheel}")

    upgrade_from = args.upgrade_from
    if args.test_upgrade and not upgrade_from:
        # topos_node-1.0.2-...whl -> 1.0.2
        version_part = wheel.name.removeprefix("topos_node-").split("-", 1)[0]
        upgrade_from = _previous_pypi_version(version_part)
        if upgrade_from:
            print(f"Upgrade smoke: topos-node=={upgrade_from} -> {wheel.name}")
        else:
            print("Upgrade smoke skipped: no older PyPI release found")

    with tempfile.TemporaryDirectory(prefix="topos-release-smoke-") as tmp:
        venv_dir = Path(tmp) / "venv"
        _run([sys.executable, "-m", "venv", str(venv_dir)])
        python = venv_dir / "bin" / "python"
        if not python.exists():
            python = venv_dir / "Scripts" / "python.exe"
        _install_and_verify(python, wheel, upgrade_from=upgrade_from)

    print("release_smoke_ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
