"""Is this node's database actually readable right now?

``/healthcheck`` answered ``{"status": "ok"}`` without ever touching SQLite, so
"the node is up" and "the node can serve data" were the same green dot. They are
not the same thing. On 2026-08-17 a connection's sqlite3 statement cache was
corrupted by cross-thread use and every data read failed for nearly two hours
while healthcheck stayed green — the app showed a connected Topos over an empty
graph, and the only way to know better was to read the node log.

This probe closes that gap. It is deliberately the cheapest possible question
(``SELECT 1``), asked the same way every other read is asked: off the event loop,
on the worker's OWN connection. Asking it on the loop would reintroduce the stall
this whole change set exists to remove, and asking it on a caller-passed handle
would reintroduce the corruption.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional, Tuple

logger = logging.getLogger("topos.core.db_health")

#: Healthcheck is polled by the tray, the desktop app and the control plane, and
#: a caller waiting on it cannot tell "slow" from "down". Bounded well under the
#: control plane's 20s request timeout so a wedged database reports as unhealthy
#: rather than timing out the whole healthcheck.
_PROBE_TIMEOUT_S = 2.0


def _probe_sync() -> Tuple[Optional[bool], Optional[str]]:
    """``SELECT 1`` on the calling thread's own connection. Never raises."""
    try:
        import topos.core.handlers as hub

        conn = hub.get_db_connection()
    except Exception as exc:  # noqa: BLE001 — a diagnostic never breaks healthcheck
        return False, f"database connection unavailable: {type(exc).__name__}"
    if conn is None:
        # No database configured (or not opened yet). Not a failure claim —
        # `None` means "unknown", and only `False` accuses the node.
        return None, None
    try:
        conn.execute("SELECT 1").fetchone()
        return True, None
    except KeyError as exc:
        # The statement-cache corruption. Its message is the SQL of an unrelated
        # statement, so reporting `exc` verbatim sends the reader to the wrong
        # file; say what it actually means instead.
        logger.error(
            "database probe failed: sqlite3 statement cache is corrupted on this "
            "connection (cross-thread use). Key names an unrelated statement: %s",
            exc,
        )
        return False, "database connection corrupted (statement cache)"
    except Exception as exc:  # noqa: BLE001
        logger.error("database probe failed: %s: %s", type(exc).__name__, exc)
        return False, f"{type(exc).__name__}: {exc}"


async def probe_db_health() -> Tuple[Optional[bool], Optional[str]]:
    """``(db_ok, db_error)`` — ``(None, None)`` when there is no verdict to give.

    Runs on the default thread pool deliberately: that is the same pool real
    reads run on, so the probe measures what a data request would actually meet.
    The cost is that a saturated pool can delay it, which is why a timeout is
    reported as UNKNOWN and not as a failure — see below.
    """
    try:
        return await asyncio.wait_for(asyncio.to_thread(_probe_sync), _PROBE_TIMEOUT_S)
    except asyncio.TimeoutError:
        # NOT `False`. Healthcheck is on the control plane's fast-inbound path
        # precisely so it answers while the node is busy, and this probe queues
        # on the same pool as enrichment work — so a timeout is at least as
        # likely to mean "every worker is busy" as "the database is broken".
        # Calling a loaded node's database dead would recreate, in a new place,
        # the exact thing this field exists to stop: the UI asserting something
        # about the node that is not true. A genuinely poisoned handle raises
        # immediately and is caught above, well inside this window.
        # The worker thread is left to finish; a `SELECT 1` is bounded.
        logger.warning(
            "database probe exceeded %.1fs — reporting unknown, not unhealthy "
            "(the thread pool may simply be saturated)",
            _PROBE_TIMEOUT_S,
        )
        return None, None
    except Exception as exc:  # noqa: BLE001 — healthcheck must always answer
        logger.warning("database probe could not run: %s", exc)
        return None, None
