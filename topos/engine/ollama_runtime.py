"""Start a local Ollama server when a request needs it and :11434 is down.

Reachability is already probed everywhere; nothing on the product path used
to spawn the server. Install opens Ollama.app after a one-click install —
this module does the same (or ``ollama serve``) at request time, only for a
localhost base URL, and only once under a process lock.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from typing import Callable, Optional
from urllib.parse import urlparse

logger = logging.getLogger("topos.engine.ollama_runtime")

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_WAIT_SEC = 20.0
_POLL_INTERVAL = 0.5

_LOCK = threading.Lock()


def _default_base_url() -> str:
    try:
        from ..config.settings import settings

        return str(getattr(settings, "engine_ollama_base_url", None) or "http://localhost:11434").rstrip("/")
    except Exception:
        return "http://localhost:11434"


def is_local_base_url(url: str) -> bool:
    raw = str(url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else f"http://{raw}")
    host = (parsed.hostname or "").lower()
    return host in _LOCAL_HOSTS


def default_is_reachable(base_url: str) -> bool:
    from .backends.ollama import OllamaAdapter

    return bool(OllamaAdapter(base_url=base_url).is_reachable())


def default_app_present() -> bool:
    from .ollama_install import default_app_present as _app_present

    return bool(_app_present())


def default_open_app() -> None:
    from .ollama_install import default_open_app as _open_app

    _open_app()


def default_cli_present() -> bool:
    return shutil.which("ollama") is not None


def default_spawn_serve() -> None:
    subprocess.Popen(
        ["ollama", "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def ensure_running(
    *,
    base_url: Optional[str] = None,
    is_reachable: Optional[Callable[[], bool]] = None,
    is_local: Optional[Callable[[str], bool]] = None,
    app_present: Optional[Callable[[], bool]] = None,
    open_app: Optional[Callable[[], None]] = None,
    cli_present: Optional[Callable[[], bool]] = None,
    spawn_serve: Optional[Callable[[], None]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    wait_sec: float = _WAIT_SEC,
    poll_interval: float = _POLL_INTERVAL,
) -> bool:
    """Bring a local Ollama up if a request needs it.

    Returns True when the server answers ``/api/tags`` (already up, or up
    after we launched). Returns False when the URL is remote, nothing is
    installed, or the wait timed out — callers then hit the existing 502 /
    deferred path.
    """
    url = (base_url if base_url is not None else _default_base_url()).rstrip("/")
    local_check = is_local if is_local is not None else is_local_base_url
    if not local_check(url):
        return False

    reachable = is_reachable if is_reachable is not None else (lambda: default_is_reachable(url))
    if reachable():
        return True

    with _LOCK:
        if reachable():
            return True

        launched = False
        present = app_present if app_present is not None else default_app_present
        if present():
            try:
                (open_app if open_app is not None else default_open_app)()
                launched = True
                logger.info("opened Ollama.app; waiting for %s", url)
            except Exception as exc:  # noqa: BLE001 — launch is best-effort
                logger.warning("failed to open Ollama.app: %s", exc)

        if not launched:
            has_cli = cli_present if cli_present is not None else default_cli_present
            if has_cli():
                try:
                    (spawn_serve if spawn_serve is not None else default_spawn_serve)()
                    launched = True
                    logger.info("spawned `ollama serve`; waiting for %s", url)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("failed to spawn ollama serve: %s", exc)

        if not launched:
            logger.info("ollama is down and nothing on this machine can start it")
            return False

        sleeper = sleep if sleep is not None else time.sleep
        deadline = time.monotonic() + float(wait_sec)
        interval = max(0.0, float(poll_interval))
        while time.monotonic() < deadline:
            if reachable():
                return True
            sleeper(interval)
        logger.warning("ollama did not become reachable at %s within %.1fs", url, wait_sec)
        return False
