"""ASGI lifespan helper for FastAPI tests (compat across FastAPI/Starlette versions)."""

from __future__ import annotations

import typing

from asgi_lifespan import LifespanManager as _LifespanManager

# asgi-lifespan defaults both timeouts to 5s — a library default, not a product
# requirement. Startup does real DB work (stage9 migration, source-install
# rehydration), and late in a full suite run the accumulated background
# threads from earlier app instances can slow it past a 5s budget, failing
# tests that pass in isolation. Worse than the failure itself: a mid-write
# startup cancellation can strand an open transaction on the shared guard DB
# and poison every later test with "database is locked". 30s asserts "startup
# completes", not "startup beats 5s on a loaded dev machine". Explicit
# timeouts passed by a test still win.
STARTUP_TIMEOUT_SECONDS = 30
SHUTDOWN_TIMEOUT_SECONDS = 30

# 30s is not a tight fit, and a timeout here is not an argument for raising it.
# Measured startup at every LifespanManager(app) site in tests/ is 0.08s-0.24s
# (2026-08-09, py3.10: all 9 files that use this helper, plus 5 shuffled runs of
# test_ingestion_sources.py) — over 120x headroom. Startup only blows the budget
# when something *outside* it starves the event loop, so widening the budget
# buys a longer wait for the same failure. The known culprit is the Hugging Face
# model prewarm; tests/conftest.py disables it suite-wide, and that comment
# carries the full history.
#
# asgi-lifespan signals the timeout as a bare `TimeoutError()` with no message
# (_concurrency/asyncio.py: `except asyncio.TimeoutError: raise TimeoutError`),
# which is what made CI run 31293718827 read as an unexplained flake. Carry the
# diagnosis on the exception so the next one reads as what it is.
_TIMEOUT_HINT = (
    "App startup did not complete within {seconds}s. This budget is ~120x the "
    "measured cost of a healthy startup (0.08s-0.24s), so it is contention, not "
    "a too-small timeout — widening it buys a longer wait for the same failure. "
    "WRITE GATE: {holder}. "
    "The gate is the first suspect, and the holder above names it when one is "
    "live: a writer that takes the process-wide gate and then blocks on SQLite "
    "pins every other writer for the full 30s busy_timeout, and this startup is "
    "one of them. That reads as a startup problem while the real culprit is "
    "another test's leaked background work — an upgrade runner or pipeline "
    "worker still writing after its own app shut down. Second suspect is a "
    "background thread loading/downloading models "
    "(SANITIZATION_PREWARM_ON_STARTUP is off for tests by default — check it "
    "was not re-enabled)."
)


def _describe_gate() -> str:
    """Name the write-gate holder, if the engine is importable and one exists."""
    try:
        from topos.storage.db.write_gate import describe_gate_holder

        return describe_gate_holder()
    except Exception:  # noqa: BLE001 — a diagnostic must never mask the timeout
        return "unavailable"


class LifespanManager(_LifespanManager):
    """`asgi_lifespan.LifespanManager` with this suite's timeouts, and a startup
    timeout that explains itself instead of raising a bare `TimeoutError`."""

    def __init__(
        self,
        app: typing.Any,
        startup_timeout: typing.Optional[float] = STARTUP_TIMEOUT_SECONDS,
        shutdown_timeout: typing.Optional[float] = SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            app, startup_timeout=startup_timeout, shutdown_timeout=shutdown_timeout
        )

    async def __aenter__(self) -> "LifespanManager":
        try:
            await super().__aenter__()
        except TimeoutError as exc:
            # The base __aenter__ already unwound its exit stack before raising.
            # Read the holder HERE, not at module import: it is only meaningful
            # at the moment the timeout fires.
            raise TimeoutError(
                _TIMEOUT_HINT.format(
                    seconds=self.startup_timeout, holder=_describe_gate()
                )
            ) from exc
        return self


__all__ = ["LifespanManager", "STARTUP_TIMEOUT_SECONDS", "SHUTDOWN_TIMEOUT_SECONDS"]
