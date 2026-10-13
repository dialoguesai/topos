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

Handlers may declare that they run off the event loop (NR166 R3b):

    @handles("get_table_rows", off_loop=True)

Nearly every handler is an ``async def`` whose body never awaits: it resolves the
calling thread's connection and runs its queries where it stands, which is the
app's event loop. One slow table walk there stalls every other request the app
is serving. A handler marked ``off_loop`` is run by the dispatcher in a worker
thread instead (``run_whole``), where ``get_db_connection`` hands it that
thread's own connection. The mark is only for a handler that never awaits:
``run_whole`` refuses one that does, and ``tests/core/test_off_loop_handlers.py``
reads every marked handler's source for an ``await``.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional, Set

Handler = Callable[[Dict[str, Any]], Awaitable[Optional[Dict[str, Any]]]]

HANDLERS: Dict[str, Handler] = {}

# Subset of HANDLERS registered with owner_only=True. Always a subset of HANDLERS.
OWNER_ONLY_MESSAGE_TYPES: Set[str] = set()

# Subset of HANDLERS registered with off_loop=True: run in a worker thread by the dispatcher.
OFF_LOOP_MESSAGE_TYPES: Set[str] = set()


def handles(*msg_types: str, owner_only: bool = False, off_loop: bool = False) -> Callable[[Handler], Handler]:
    """Register a handler function for one or more message types.

    Set ``owner_only=True`` for types that must stay off every agent and
    cross-user surface (see the module docstring). Set ``off_loop=True`` for a
    handler that never awaits and reads or writes the database: the dispatcher
    runs it in a worker thread.
    """

    def register(fn: Handler) -> Handler:
        for msg_type in msg_types:
            if msg_type in HANDLERS:
                raise ValueError(f"duplicate handler registration for message type {msg_type!r}")
            HANDLERS[msg_type] = fn
            if owner_only:
                OWNER_ONLY_MESSAGE_TYPES.add(msg_type)
            if off_loop:
                OFF_LOOP_MESSAGE_TYPES.add(msg_type)
        return fn

    return register


def run_whole(handler: Handler, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Run a handler that never awaits, start to finish, on the calling thread.

    For a worker thread: the handler's body runs here, so the connection it
    resolves is this thread's. A handler that does await cannot be run this way
    (there is no loop under it to resume it) and is refused, loudly, rather than
    left half run.
    """
    coroutine = handler(message)
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value
    coroutine.close()
    raise RuntimeError(f"{getattr(handler, '__name__', 'handler')} awaits: it cannot be marked off_loop")
