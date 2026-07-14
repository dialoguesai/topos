"""Cooperative engine shutdown flag for background worker threads.

Ctrl+C / SIGTERM is handled on the asyncio main thread, but long-running
enrichment (fact_llm, Ollama HTTP) often runs in worker threads that cannot
receive KeyboardInterrupt. Those workers poll ``is_shutdown_requested()``
between units of work so the process can exit without waiting for a full
batch (or a multi-minute urllib timeout) to finish.
"""

from __future__ import annotations

import logging
import signal
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger("topos.runtime_shutdown")

_FLAG = threading.Event()
_REASON: str = ""
_LOCK = threading.Lock()
_HOOKS_INSTALLED = False
_PREVIOUS_HANDLERS: Dict[int, Any] = {}


def request_shutdown(reason: str = "shutdown") -> None:
    """Mark the engine as shutting down (idempotent, thread-safe)."""
    global _REASON
    with _LOCK:
        if not _FLAG.is_set():
            _REASON = str(reason or "shutdown")
            logger.info("Runtime shutdown requested reason=%s", _REASON)
        _FLAG.set()


def clear_shutdown() -> None:
    """Reset the flag (tests / process restart within the same interpreter)."""
    global _REASON
    with _LOCK:
        _FLAG.clear()
        _REASON = ""


def is_shutdown_requested() -> bool:
    return _FLAG.is_set()


def shutdown_reason() -> str:
    return _REASON


def install_shutdown_signal_hooks() -> None:
    """Chain SIGINT/SIGTERM handlers so workers see shutdown immediately.

    Installed from app startup so we wrap uvicorn's handlers rather than
    replacing them. Safe to call more than once.
    """
    global _HOOKS_INSTALLED
    if _HOOKS_INSTALLED:
        return

    def _chain(signum: int, frame: Optional[object]) -> None:
        try:
            name = signal.Signals(signum).name
        except (ValueError, AttributeError):
            name = str(signum)
        request_shutdown(f"signal:{name}")
        prev = _PREVIOUS_HANDLERS.get(signum)
        if callable(prev):
            prev(signum, frame)
        elif prev is signal.SIG_DFL and signum == getattr(signal, "SIGINT", None):
            raise KeyboardInterrupt

    installed = False
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            _PREVIOUS_HANDLERS[sig] = signal.getsignal(sig)
            signal.signal(sig, _chain)
            installed = True
        except (ValueError, OSError) as exc:
            # signal.signal only works on the main thread.
            logger.debug("Could not install shutdown hook for %s: %s", sig, exc)

    _HOOKS_INSTALLED = installed
