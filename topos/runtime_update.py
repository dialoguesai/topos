"""Runtime PyPI update checks for the Topos node (non-blocking while server runs)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Optional
import subprocess
import sys
import threading
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version as package_version
from urllib.error import URLError
from urllib.request import urlopen

from packaging.version import InvalidVersion, Version

DEFAULT_PACKAGE_NAME = "topos-node"
DEFAULT_CHECK_INTERVAL_SECONDS = 6 * 60 * 60
INITIAL_CHECK_DELAY_SECONDS = 10.0
PYPI_TIMEOUT_SECONDS = 2.0

_logger = logging.getLogger("topos.runtime_update")
_lock = threading.Lock()
_update_info: UpdateInfo | None = None
_announcement_logged = False
_local_install_logged = False
_monitor_task: asyncio.Task[None] | None = None
_hotkey_thread: threading.Thread | None = None


@dataclass(frozen=True)
class UpdateInfo:
    package_name: str
    installed: str
    latest: str


def should_skip_update_check(*, cli_skip: bool = False) -> bool:
    if cli_skip:
        return True
    env_value = (os.getenv("TOPOS_SKIP_UPDATE_CHECK") or "").strip().lower()
    return env_value in {"1", "true", "yes", "on"}


def get_installed_package_version(package_name: str = DEFAULT_PACKAGE_NAME) -> str | None:
    try:
        return package_version(package_name)
    except PackageNotFoundError:
        return None


def get_module_version() -> str | None:
    try:
        from topos.__version__ import __version__
    except Exception:
        return None
    return __version__ or None


def get_runtime_version(package_name: str = DEFAULT_PACKAGE_NAME) -> str:
    return get_module_version() or get_installed_package_version(package_name) or "unknown"


def get_latest_pypi_version(
    package_name: str = DEFAULT_PACKAGE_NAME,
    timeout_seconds: float = PYPI_TIMEOUT_SECONDS,
) -> str | None:
    url = f"https://pypi.org/pypi/{package_name}/json"
    try:
        with urlopen(url, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError):
        return None
    return str(payload.get("info", {}).get("version") or "").strip() or None


def is_update_available() -> bool:
    with _lock:
        return _update_info is not None


def get_update_info() -> UpdateInfo | None:
    with _lock:
        return _update_info


def _set_update_info(info: UpdateInfo | None) -> None:
    global _update_info
    with _lock:
        _update_info = info


def _is_newer_version(installed: str, latest: str) -> bool:
    try:
        return Version(latest) > Version(installed)
    except InvalidVersion:
        return False


def check_for_update(package_name: str = DEFAULT_PACKAGE_NAME) -> UpdateInfo | None:
    """Return update details when PyPI has a newer release, else None."""
    if should_skip_update_check():
        return None

    installed = get_installed_package_version(package_name) or get_module_version()
    if not installed or installed == "unknown":
        return None

    latest = get_latest_pypi_version(package_name)
    if not latest or not _is_newer_version(installed, latest):
        return None

    # An install that did not come from PyPI cannot be moved by a PyPI release.
    # Offering the update anyway is what produced an update button that could
    # be pressed forever with nothing to show for it.
    source = local_install_source(package_name)
    if source is not None:
        _log_local_install_once(package_name, source)
        return None

    return UpdateInfo(package_name=package_name, installed=installed, latest=latest)


# uv records what a tool was installed FROM in its receipt. A plain
# `name = "topos-node"` requirement came from an index; anything carrying a
# path, url or git ref did not, and `uv tool upgrade` will faithfully reinstall
# THAT source no matter what PyPI publishes.
_LOCAL_REQUIREMENT_RE = re.compile(r'\b(directory|editable|path|url|git)\s*=\s*"([^"]+)"')


def _uv_tool_receipt(package_name: str = DEFAULT_PACKAGE_NAME) -> Path:
    """Where uv keeps the record of how this tool was installed."""
    base = os.getenv("UV_TOOL_DIR")
    if not base:
        data_home = os.getenv("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
        base = os.path.join(data_home, "uv", "tools")
    return Path(base) / package_name / "uv-receipt.toml"


def local_install_source(package_name: str = DEFAULT_PACKAGE_NAME) -> Optional[str]:
    """The local path this engine was installed from, or None if it came from PyPI.

    The deploy lane installs the engine from a working copy
    (`uv tool install ~/.topos/deploy-head`), and that is a perfectly good way
    to run a build that is not on PyPI yet. What it is not is upgradable:
    `uv tool upgrade` re-resolves that same directory, rebuilds the same
    version, and exits 0. Reported live 2026-08-18 as "the installer is
    broken" — the menu offered 1.3.21, said "Installing update…", restarted,
    came back on 1.3.20, and offered 1.3.21 again, forever. Nothing failed;
    the update simply could never have applied, and nothing said so.

    Parsed with a regex rather than tomllib: the receipt is small, uv owns its
    shape, and tomllib does not exist on the 3.10 this package still supports.
    """
    try:
        text = _uv_tool_receipt(package_name).read_text(encoding="utf-8")
    except OSError:
        return None  # no receipt: not a uv tool install, so nothing to warn about
    match = _LOCAL_REQUIREMENT_RE.search(text)
    return match.group(2) if match else None


def _installed_version_on_disk(package_name: str = DEFAULT_PACKAGE_NAME) -> Optional[str]:
    """What is in the tool's site-packages right now, read fresh from disk.

    Deliberately not `importlib.metadata`: this runs inside the very process
    uv just rewrote underneath, whose own metadata was resolved at import time.
    The question after an upgrade is what is on disk now, not what this
    interpreter loaded minutes ago.

    Returns None when it cannot tell, so an unknown is never mistaken for an
    unchanged version — the caller must not turn "I could not read it" into
    "the update failed".
    """
    tool_dir = _uv_tool_receipt(package_name).parent
    distribution = package_name.replace("-", "_")
    try:
        matches = sorted(tool_dir.glob(f"lib/python*/site-packages/{distribution}-*.dist-info"))
    except OSError:
        return None
    if not matches:
        return None
    stem = matches[-1].name[: -len(".dist-info")]
    return stem[len(distribution) + 1 :] or None


def _log_local_install_once(package_name: str, source: str) -> None:
    global _local_install_logged
    with _lock:
        if _local_install_logged:
            return
        _local_install_logged = True
    _logger.info(
        "%s was installed from %s, not from PyPI, so published releases do not "
        "apply to it and no update is offered. To follow PyPI again: "
        "`uv tool install --force %s`.",
        package_name,
        source,
        package_name,
    )


def resolve_uv_binary() -> Optional[str]:
    """Find a usable `uv`, without assuming a login shell's PATH.

    A GUI-launched node inherits PATH=/usr/bin:/bin:/usr/sbin:/sbin, where uv
    never lives — so a bare `uv` raised FileNotFoundError and self-update was
    impossible for every app install. The macOS shell now passes TOPOS_UV_BIN
    (its own bundled uv); the fallbacks cover terminal installs and anyone
    running an older shell.
    """
    explicit = os.environ.get("TOPOS_UV_BIN", "").strip()
    if explicit and os.access(explicit, os.X_OK):
        return explicit
    found = shutil.which("uv")
    if found:
        return found
    for candidate in (
        os.path.expanduser("~/.local/bin/uv"),
        "/opt/homebrew/bin/uv",
        "/usr/local/bin/uv",
        # Last resort: the uv the macOS app ships inside its own bundle. Shells
        # before 0.2.13 set neither TOPOS_UV_BIN nor PATH, and a DMG user who
        # never installed uv has a copy *only* here — leaving them unable to
        # update from the menu or by hand.
        "/Applications/Topos.app/Contents/Resources/uv",
        os.path.expanduser("~/Applications/Topos.app/Contents/Resources/uv"),
    ):
        if os.access(candidate, os.X_OK):
            return candidate
    return None


def apply_package_update(package_name: str = DEFAULT_PACKAGE_NAME) -> bool:
    """Install the latest PyPI release via `uv tool upgrade`. Returns True on success."""
    source = local_install_source(package_name)
    if source is not None:
        # `uv tool upgrade` would exit 0 here and change nothing, which is the
        # one outcome this function must never report as success.
        _logger.error(
            "Update refused: %s is installed from %s, not from PyPI, so a published "
            "release cannot replace it. Reinstall from the index with "
            "`uv tool install --force %s`, or update that working copy and reinstall it.",
            package_name,
            source,
            package_name,
        )
        return False
    before = _installed_version_on_disk(package_name) or get_installed_package_version(package_name)
    uv = resolve_uv_binary()
    if uv is None:
        _logger.error(
            "Update failed: could not find `uv`. PATH=%s. Install uv, or run "
            "`uv tool upgrade %s` yourself.",
            os.environ.get("PATH", ""),
            package_name,
        )
        return False
    try:
        result = subprocess.run(
            [uv, "tool", "upgrade", package_name],
            check=False,
            capture_output=True,
            text=True,
            timeout=900,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Previously this raised out of the worker thread and the only trace
        # was last_result="failed" — a silent failure by construction.
        _logger.error("Update failed to run `%s tool upgrade %s`: %s", uv, package_name, exc)
        return False
    if result.returncode != 0:
        _logger.error(
            "Update failed (`uv tool upgrade %s` exited %d): %s",
            package_name,
            result.returncode,
            (result.stderr or result.stdout or "").strip()[:500],
        )
        return False
    # Exit 0 is not proof the version moved: uv is content to reinstall what is
    # already there. Read it back rather than trusting the return code — a
    # "success" that leaves the same version is how this looked broken.
    after = _installed_version_on_disk(package_name)
    if before and after and after == before:
        _logger.error(
            "Update ran but %s is still %s. `uv tool upgrade` exited 0 without "
            "changing anything, so nothing was installed. Output: %s",
            package_name,
            before,
            (result.stdout or result.stderr or "").strip()[:500],
        )
        return False
    _logger.info("Update installed: %s. Restart Topos to run it.", package_name)
    return True


def _log_update_available_once(info: UpdateInfo) -> None:
    global _announcement_logged
    with _lock:
        if _announcement_logged:
            return
        _announcement_logged = True

    _logger.warning(
        "New Topos version available: %s -> %s. "
        "Timestamps will show in amber until you restart. "
        "Update with `uv tool upgrade %s` then stop (Ctrl+C) and re-run `topos-node`. "
        "In an interactive terminal, type `:update` and press Enter to install now.",
        info.installed,
        info.latest,
        info.package_name,
    )


async def _monitor_loop(
    *,
    package_name: str = DEFAULT_PACKAGE_NAME,
    initial_delay_seconds: float = INITIAL_CHECK_DELAY_SECONDS,
    interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
) -> None:
    await asyncio.sleep(initial_delay_seconds)
    while True:
        try:
            info = await asyncio.to_thread(check_for_update, package_name)
            if info:
                _set_update_info(info)
                _log_update_available_once(info)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _logger.debug("Runtime update check failed (non-fatal): %s", exc)
        await asyncio.sleep(interval_seconds)


def start_runtime_update_monitor(
    *,
    cli_skip: bool = False,
    package_name: str = DEFAULT_PACKAGE_NAME,
) -> asyncio.Task[None] | None:
    """Schedule periodic PyPI checks; safe to call once at app startup."""
    global _monitor_task
    if should_skip_update_check(cli_skip=cli_skip):
        return None
    if _monitor_task is not None and not _monitor_task.done():
        return _monitor_task

    loop = asyncio.get_running_loop()
    _monitor_task = loop.create_task(
        _monitor_loop(package_name=package_name),
        name="topos-runtime-update-monitor",
    )
    return _monitor_task


async def stop_runtime_update_monitor() -> None:
    global _monitor_task
    task = _monitor_task
    _monitor_task = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def _handle_hotkey_line(line: str, package_name: str = DEFAULT_PACKAGE_NAME) -> None:
    command = line.strip().lower()
    if command not in {":update", ":u"}:
        return

    info = get_update_info() or check_for_update(package_name)
    if not info:
        _logger.info("Topos is already on the latest published version.")
        return

    _logger.info("Updating %s (%s -> %s)...", package_name, info.installed, info.latest)
    if apply_package_update(package_name):
        _logger.warning(
            "Update installed. Stop Topos (Ctrl+C) and re-run `topos-node` to use %s.",
            info.latest,
        )
    else:
        _logger.error("Update failed. Run `uv tool upgrade %s` manually.", package_name)


def _hotkey_listener_loop(package_name: str = DEFAULT_PACKAGE_NAME) -> None:
    try:
        for line in sys.stdin:
            _handle_hotkey_line(line, package_name=package_name)
    except Exception as exc:  # noqa: BLE001
        _logger.debug("Update hotkey listener stopped: %s", exc)


def start_update_hotkey_listener(
    *,
    cli_skip: bool = False,
    package_name: str = DEFAULT_PACKAGE_NAME,
) -> None:
    """Listen for `:update` on stdin (interactive terminals only)."""
    global _hotkey_thread
    if should_skip_update_check(cli_skip=cli_skip):
        return
    if not sys.stdin.isatty():
        return
    if _hotkey_thread is not None and _hotkey_thread.is_alive():
        return

    _hotkey_thread = threading.Thread(
        target=_hotkey_listener_loop,
        kwargs={"package_name": package_name},
        name="topos-update-hotkey",
        daemon=True,
    )
    _hotkey_thread.start()


def can_prompt_for_input() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()
