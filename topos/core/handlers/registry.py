"""Registry mapping control-plane message types to handler functions."""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional

Handler = Callable[[Dict[str, Any]], Awaitable[Optional[Dict[str, Any]]]]

HANDLERS: Dict[str, Handler] = {}


def handles(*msg_types: str) -> Callable[[Handler], Handler]:
    """Register a handler function for one or more message types."""

    def register(fn: Handler) -> Handler:
        for msg_type in msg_types:
            if msg_type in HANDLERS:
                raise ValueError(f"duplicate handler registration for message type {msg_type!r}")
            HANDLERS[msg_type] = fn
        return fn

    return register
