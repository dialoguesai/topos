"""ASGI lifespan helper for FastAPI tests (compat across FastAPI/Starlette versions)."""

from __future__ import annotations

from asgi_lifespan import LifespanManager

__all__ = ["LifespanManager"]
