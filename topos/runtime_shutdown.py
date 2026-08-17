"""Cooperative shutdown for background worker threads, scoped to a runtime run.

Ctrl+C / SIGTERM is handled on the asyncio main thread, but long-running
enrichment (fact_llm, Ollama HTTP) often runs in worker threads that cannot
receive KeyboardInterrupt. Those workers poll ``is_shutdown_requested()``
between units of work so the process can exit without waiting for a full batch
(or a multi-minute urllib timeout) to finish.

The unit of scope is a GENERATION, not one process-lifetime boolean. A
generation is one runtime run: an app lifespan, or the *ambient* generation of
a process that never starts an app (CLI commands, one-off scripts, the test
suite). Workers capture the generation they started under and ask **it**
whether to stop — which is the question a shared boolean could not answer:

  * Ending a run RETIRES its generation. Its own still-draining workers see a
    flag that stays set forever, so they stop and stay stopped. The previous
    design cleared one shared flag at the next startup, which un-stopped them.
  * A fresh, unset generation replaces it, so the process stays usable for
    whatever comes next. Asking "is anything shutting down?" outside a live run
    answers False, because nothing is — a finished run must not speak for the
    process it happened to run in.
  * At most one generation is live: beginning a run retires the outgoing one,
    so a run that dies without calling ``end_runtime`` cannot leave workers
    polling a flag nobody will ever set.

Cancelling ONE unit of work is a DIFFERENT concern and does not belong here.
Pass a ``threading.Event`` down to the worker instead — see
``features.facts.llm_extract.extract_owner_facts_llm(cancel=...)``. Routing a
per-batch cancel through this module (``request_shutdown``) is what made one
cancelled fact-extraction batch disable LLM fact extraction for the entire
life of the node process, silently, with the job still reporting success.
"""

from __future__ import annotations

import logging
import signal
import threading
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("topos.runtime_shutdown")


class RuntimeGeneration:
    """One runtime run. Capture it when work starts; poll it while work runs.

    Retirement is one-way on purpose: a retired generation can never go back to
    "keep going", so a worker that has been told to stop cannot be un-stopped by
    something that happens later in the process.
    """

    __slots__ = ("id", "_event", "_reason", "_lock")

    def __init__(self, generation_id: int) -> None:
        self.id = int(generation_id)
        self._event = threading.Event()
        self._reason = ""
        self._lock = threading.Lock()

    def request_stop(self, reason: str = "shutdown") -> None:
        """Tell this run's workers to stop (idempotent, thread-safe)."""
        with self._lock:
            first = not self._event.is_set()
            if first:
                self._reason = str(reason or "shutdown")
            self._event.set()
        if first:
            logger.info(
                "Runtime stop requested generation=%d reason=%s", self.id, self._reason
            )

    def is_stop_requested(self) -> bool:
        return self._event.is_set()

    def clear(self) -> None:
        """Un-set this generation. Tests only — see ``clear_shutdown``."""
        with self._lock:
            self._event.clear()
            self._reason = ""

    @property
    def reason(self) -> str:
        return self._reason

    def __repr__(self) -> str:  # pragma: no cover — diagnostics only
        state = "stopped" if self._event.is_set() else "live"
        return f"<RuntimeGeneration {self.id} {state}>"


_GEN_LOCK = threading.Lock()
_NEXT_ID = 0


def _mint() -> RuntimeGeneration:
    """Caller holds ``_GEN_LOCK`` (or is module import, which is single-threaded)."""
    global _NEXT_ID
    generation = RuntimeGeneration(_NEXT_ID)
    _NEXT_ID += 1
    return generation


#: Generation 0 — the ambient run of a process that never starts an app. A CLI
#: command's SIGINT sets this one, so signal handling works with no app at all.
_current: RuntimeGeneration = _mint()


def current_generation() -> RuntimeGeneration:
    """The live generation. Capture this once when a unit of work starts."""
    with _GEN_LOCK:
        return _current


def begin_runtime(reason: str = "app_startup") -> RuntimeGeneration:
    """Start a runtime run; return its generation.

    Retires the outgoing generation ("superseded"), which keeps the invariant
    that at most one generation is live. A run that crashed without calling
    ``end_runtime`` therefore cannot leave workers running against the new one.
    """
    global _current
    with _GEN_LOCK:
        outgoing = _current
        _current = _mint()
        fresh = _current
    if not outgoing.is_stop_requested():
        outgoing.request_stop("superseded")
    logger.debug("Runtime generation %d began reason=%s", fresh.id, reason)
    return fresh


def end_runtime(reason: str = "app_shutdown") -> RuntimeGeneration:
    """Retire the current run and install a fresh, unset generation.

    Returns the retired generation so a caller can wait on its workers. The
    swap happens BEFORE the stop is signalled so nothing can capture a
    generation that is about to be retired.
    """
    global _current
    with _GEN_LOCK:
        outgoing = _current
        _current = _mint()
    outgoing.request_stop(reason)
    return outgoing


def request_shutdown(reason: str = "shutdown") -> None:
    """Stop the current run — signals, and anything meaning "we are going down".

    NOT for cancelling one unit of work: this stops every worker in the run.
    """
    current_generation().request_stop(reason)


def clear_shutdown() -> None:
    """Un-set the current generation's flag.

    Tests that set the flag deliberately use this to undo it. Production code
    should use ``begin_runtime``/``end_runtime``, which get a fresh generation
    rather than reviving a run that has already been told to stop.
    """
    current_generation().clear()


def is_shutdown_requested() -> bool:
    """Is the CURRENT run stopping? Long-running work should capture its own
    generation via ``current_generation()`` and poll that instead, so a run that
    ends mid-batch still stops the batch it started."""
    return current_generation().is_stop_requested()


def shutdown_reason() -> str:
    return current_generation().reason


_HOOKS_INSTALLED = False
_PREVIOUS_HANDLERS: Dict[int, Any] = {}


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


def stop_checker(
    generation: Optional[RuntimeGeneration] = None,
    cancel: Optional[threading.Event] = None,
) -> Callable[[], bool]:
    """``() -> bool`` combining "this run is stopping" with "this job was cancelled".

    The two are deliberately separate inputs: ``generation`` is the run the work
    started under, ``cancel`` is scoped to this one call and is what a caller
    sets when ITS task is cancelled. Neither can silence the other, and neither
    outlives its own scope.
    """
    gen = generation if generation is not None else current_generation()

    def _should_stop() -> bool:
        return (cancel is not None and cancel.is_set()) or gen.is_stop_requested()

    return _should_stop


def stop_reason(
    generation: Optional[RuntimeGeneration] = None,
    cancel: Optional[threading.Event] = None,
) -> str:
    """Why ``stop_checker`` would answer True right now ("" when it would not)."""
    if cancel is not None and cancel.is_set():
        return "cancelled"
    gen = generation if generation is not None else current_generation()
    return gen.reason if gen.is_stop_requested() else ""
