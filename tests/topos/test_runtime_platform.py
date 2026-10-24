"""The install interpreter, and the startup line for a Mac that cannot load the installed scipy.

On macOS 27.0.1 (24 Oct 2026) dyld refused scipy 1.15.3's Fortran extensions on
Python 3.10 through 3.13; scipy 1.16.3 loaded. Every embeddings and entities job
failed with its own traceback and nothing named the cause.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from packaging.version import Version

from topos import runtime_platform, runtime_update
from topos.runtime_platform import INSTALL_PYTHON, log_platform_problems, scipy_load_problem

REPO = Path(__file__).resolve().parents[2]


def _the_mac(monkeypatch, *, macos: int | None, scipy: str | None) -> None:
    monkeypatch.setattr(runtime_platform, "_macos_major", lambda: macos)
    monkeypatch.setattr(runtime_platform, "_installed_scipy", lambda: Version(scipy) if scipy else None)
    monkeypatch.setattr(runtime_platform.platform, "mac_ver", lambda: (f"{macos}.0.1", ("", "", ""), "arm64"))
    monkeypatch.setattr(runtime_update, "resolve_uv_binary", lambda: "/Applications/Topos.app/Contents/Resources/uv")


def test_old_scipy_on_macos_27_logs_one_error_naming_the_fix(monkeypatch, caplog):
    _the_mac(monkeypatch, macos=27, scipy="1.15.3")
    logger = logging.getLogger("topos.app.platform-test")
    with caplog.at_level(logging.ERROR, logger=logger.name):
        assert log_platform_problems(logger) is True
    errors = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(errors) == 1
    message = errors[0].getMessage()
    assert "scipy 1.15.3" in message
    assert "macOS 27.0.1" in message
    # The fix uses the uv this node can actually reach (the app's own copy for a DMG install).
    assert f"/Applications/Topos.app/Contents/Resources/uv tool upgrade --python {INSTALL_PYTHON} topos-node" in message


@pytest.mark.parametrize(
    ("macos", "scipy"),
    [
        (27, "1.16.3"),  # loads on macOS 27
        (27, "1.18.0"),
        (26, "1.15.3"),  # older macOS loads 1.15.3
        (None, "1.15.3"),  # not a Mac
        (27, None),  # no scipy installed
    ],
)
def test_silent_when_the_installed_scipy_loads(monkeypatch, caplog, macos, scipy):
    _the_mac(monkeypatch, macos=macos, scipy=scipy)
    with caplog.at_level(logging.DEBUG):
        assert scipy_load_problem() is None
        assert log_platform_problems(logging.getLogger("topos.app.platform-test")) is False
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]


@pytest.mark.parametrize(
    ("platform_name", "release", "expected"),
    [("darwin", "27.0.1", 27), ("darwin", "26.4", 26), ("darwin", "", None), ("linux", "", None)],
)
def test_macos_major_reads_the_product_version(monkeypatch, platform_name, release, expected):
    monkeypatch.setattr(runtime_platform.sys, "platform", platform_name)
    monkeypatch.setattr(runtime_platform.platform, "mac_ver", lambda: (release, ("", "", ""), "arm64"))
    assert runtime_platform._macos_major() == expected


# `uv tool install topos-node` with no --python lets uv choose the interpreter: the
# owner's node got 3.10 that way, and a forced reinstall got 3.14.
_INSTALL = re.compile(r"uv tool install(?P<args>[^`\n\"]*?)\btopos-node\b")


def _install_instructions():
    paths = [REPO / "README.md", REPO / "CONTRIBUTING.md", REPO / "RELEASING.md"]
    paths += sorted((REPO / "docs").rglob("*.md"))
    paths += sorted((REPO / "topos").rglob("*.py"))
    for path in paths:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for match in _INSTALL.finditer(line):
                yield f"{path.relative_to(REPO)}:{number}", match.group("args")


def test_every_install_instruction_names_the_python():
    found = list(_install_instructions())
    assert found, "the README's install line should have matched"
    missing = [where for where, args in found if "--python" not in args]
    assert missing == []
