"""ASGI lifespan helper for FastAPI tests (compat across FastAPI/Starlette versions)."""

from __future__ import annotations

import functools

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
LifespanManager = functools.partial(
    _LifespanManager, startup_timeout=30, shutdown_timeout=30
)

__all__ = ["LifespanManager"]
