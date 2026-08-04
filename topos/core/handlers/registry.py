"""Registry mapping control-plane message types to handler functions.

Handlers may declare themselves owner-only:

    @handles("delete_database_table", owner_only=True)

An owner-only type is one the engine answers for the node's owner but which must
never be reachable from an agent or cross-user surface — today that means the
control plane's ``/mcp`` gateway. The marker lives on the handler rather than in
a hand-maintained list so it cannot drift from the registration it describes. It
is exported into ``topos/protocol/handled_message_types.json`` by
``scripts/dump_handled_types.py`` and enforced by
``tests/test_protocol_handled_types.py``.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional, Set

Handler = Callable[[Dict[str, Any]], Awaitable[Optional[Dict[str, Any]]]]

HANDLERS: Dict[str, Handler] = {}

# Subset of HANDLERS registered with owner_only=True. Always a subset of HANDLERS.
OWNER_ONLY_MESSAGE_TYPES: Set[str] = set()


def handles(*msg_types: str, owner_only: bool = False) -> Callable[[Handler], Handler]:
    """Register a handler function for one or more message types.

    Set ``owner_only=True`` for types that must stay off every agent and
    cross-user surface (see the module docstring).
    """

    def register(fn: Handler) -> Handler:
        for msg_type in msg_types:
            if msg_type in HANDLERS:
                raise ValueError(f"duplicate handler registration for message type {msg_type!r}")
            HANDLERS[msg_type] = fn
            if owner_only:
                OWNER_ONLY_MESSAGE_TYPES.add(msg_type)
        return fn

    return register
