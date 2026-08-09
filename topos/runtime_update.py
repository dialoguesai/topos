"""Runtime PyPI update checks for the Topos node (non-blocking while server runs)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
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

    return UpdateInfo(package_name=package_name, installed=installed, latest=latest)


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
