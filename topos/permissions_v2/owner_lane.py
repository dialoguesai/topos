"""NR183 O1: the owner's own requests go ahead of recipients' when the node is busy (the owner's ruling D3).

Every relayed request that is not a ping or a fast-lane read waits for one of the control-plane client's inbound
handler slots (``settings.control_plane_inbound_concurrency_limit``, 16) and one of its waiting places
(``control_plane_inbound_max_pending``, 128). Until now that was one pool in arrival order, so a burst of share
searches and asks filled it and the owner's own app waited behind them, or was told "busy". With
``TOPOS_PERMISSIONS_V2_OWNER_LANE`` on, ``OWNER_LANE_SLOTS`` of the slots (4 by default) and as many waiting places
are kept for the owner's frames: everyone else shares the rest and can never take them. The owner's frames also use
any slot that is free, and when one comes free a waiting owner frame gets it before a waiting recipient's.

**Whose frame it is** is read from the frame's signed principal stamp (``topos/relay_stamp.py``), the control
plane's own classification with proof: the owner's lane takes a frame only when

- it carries a stamp that VERIFIES under this node's pinned control-plane key, with the frame's own id and type
  bound into the signature, inside the stamp's time window, of class ``owner_app`` (the owner's own app: the home
  chat's searches through the agent gateway, the owner's write doors, sources and settings);
- its type is not one a recipient may reach (``core.handlers.NON_OWNER_RELAY_TYPES``: the share search, its batch
  form, the ask and its fetch), whatever its stamp says;
- the dispatcher would serve it as the owner's: on a bound node a stamp that names another user, or one that names
  nobody on a frame naming another user, is refused there (rules (a) and (b)), and here it is no owner's.

Everything else is the shared class: a recipient's request, a frame with no stamp (the control plane never stamps a
recipient's frame, and does not yet stamp every owner frame: ``llm_generation`` comes unstamped), a stamp that does
not verify, ``owner_automation`` (routines are the owner's, but not someone waiting at the screen) and
``third_party``. Anything this cannot read, and any error while reading, is the shared class: a mistake here can
only put an owner frame behind recipients, which is how every frame was served before, never a recipient's frame
on the owner's slots.

**What it does not change.** Which frames are handled and what they are answered: the dispatcher still verifies
every stamp itself and decides with its own rules. R3a's shedding (``control_plane_client._shed_at_dequeue``) runs
at the same moment, once a slot is held, for both classes. Off, the client uses its one semaphore exactly as before
and none of this is read.

The pool belongs to one event loop (the client's) and is not thread-safe.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from typing import Any, Mapping

from . import switches

_log = logging.getLogger(__name__)

#: The two kinds of slot: one anyone may take, one only an owner frame may take.
GENERAL, RESERVED = "general", "reserved"
OWNER_APP = "owner_app"   # topos.principal.OWNER_APP
#: The principal classes whose verified frames take the owner's lane: this one line, and nothing else, says which.
#: Any other class is the shared class, a class this node does not know included (``owner_agent`` of the node's
#: unix socket among them: it is minted there, never on a relayed frame, whose stamp may carry only
#: ``relay_stamp.ALLOWED_CLASSES``, so its requests never wait for these slots).
OWNER_LANE_CLASSES = frozenset({OWNER_APP})


def is_owner_frame(message: Any) -> bool:
    """True only for a frame the control plane signed as the owner's own app (module docstring); False otherwise,
    and False on any error."""
    try:
        if not isinstance(message, Mapping):
            return False
        stamp = message.get("principal_stamp")
        # Cheap first: a frame that does not even claim the owner's app is not checked further.
        if not isinstance(stamp, Mapping) or stamp.get("cls") not in OWNER_LANE_CLASSES:
            return False
        from ..core.handlers import NON_OWNER_RELAY_TYPES

        if str(message.get("type") or "").strip().lower() in NON_OWNER_RELAY_TYPES:
            return False   # a recipient door's type is never the owner's lane, whatever the stamp says
        from ..relay_stamp import VERIFIED, check_relay_stamp

        principal, reason, _offset = check_relay_stamp(message)
        if (reason != VERIFIED or principal is None or principal.cls not in OWNER_LANE_CLASSES
                or principal.channel != "cp_relay"):
            return False
        # The dispatcher's own two rules for an owner-side stamp, so a frame it refuses as another user's never
        # holds the owner's slot (dispatch_relay_message: rule (b) when the stamp names nobody, then rule (a)).
        from ..core.handlers import _non_owner_relay_refusal, _unstamped_naming_refusal

        if not principal.acting_user and _unstamped_naming_refusal(message) is not None:
            return False
        return _non_owner_relay_refusal(message, principal) is None
    except Exception:  # noqa: BLE001 -- what cannot be read is the shared class
        return False


def slots_for(limit: int) -> "InboundSlots | None":
    """The pool for a client with ``limit`` handler slots when the lane is on; None when it is off (the client then
    keeps its one semaphore). Read once, when the client is made."""
    try:
        if not switches.on(switches.OWNER_LANE):
            return None
        lane = InboundSlots(limit, switches.number(switches.OWNER_LANE_SLOTS))
    except Exception:  # noqa: BLE001 -- a lane that cannot be made is off: the semaphore, as before
        _log.warning("inbound owner lane could not be set up; it is off")
        return None
    _log.info("inbound owner lane on: %d of %d handler slots kept for the owner's own app", lane.reserved, lane.limit)
    return lane


class InboundSlots:
    """``limit`` handler slots, ``reserved`` of which only owner frames may take (``acquire(True)``).

    ``reserved`` is at least 0 and at most ``limit - 1``: the shared class always keeps a slot. An owner frame takes a
    shared slot first and a reserved one only when none is free, so the reserve is there for the next owner frame
    as long as the shared slots last. A freed slot goes to the oldest waiting owner frame that may take it, then to
    the oldest waiting shared one. Every slot taken is given back with ``release``, once."""

    def __init__(self, limit: int, reserved: int):
        self.limit = max(1, int(limit))
        self.reserved = max(0, min(int(reserved), self.limit - 1))
        self.general = self.limit - self.reserved
        self._used = {GENERAL: 0, RESERVED: 0}
        self._waiting: dict[bool, deque] = {True: deque(), False: deque()}
        self.granted = {(True, GENERAL): 0, (True, RESERVED): 0, (False, GENERAL): 0}

    @staticmethod
    def owner_frame(message: Any) -> bool:
        """The class a frame waits in: True only when ``is_owner_frame`` says a plain yes; no on any error."""
        try:
            return is_owner_frame(message) is True
        except Exception:  # noqa: BLE001
            return False

    def in_use(self) -> dict:
        return dict(self._used)

    def waiting(self) -> dict:
        return {"owner": sum(not f.done() for f in self._waiting[True]),
                "general": sum(not f.done() for f in self._waiting[False])}

    def pending_limit(self, owner: bool, max_pending: int) -> int:
        """How many requests may be in hand (handled or waiting) when a frame of this class arrives: the shared
        class leaves ``reserved`` of the waiting places to the owner's frames, as it leaves them the slots."""
        return int(max_pending) if owner is True else max(1, int(max_pending) - self.reserved)

    def _free(self, owner: bool) -> str | None:
        if self._used[GENERAL] < self.general:
            return GENERAL
        if owner is True and self._used[RESERVED] < self.reserved:
            return RESERVED
        return None

    def _take(self, owner: bool, slot: str) -> str:
        self._used[slot] += 1
        self.granted[(owner, slot)] += 1
        return slot

    async def acquire(self, owner: bool) -> str:
        """A slot for a frame of this class (anything but True is the shared class), waiting for one if need be.
        Cancelled while waiting, it holds nothing; cancelled just after a slot was handed to it, it gives it back."""
        owner = owner is True
        queue = self._waiting[owner]
        slot = self._free(owner)
        if slot is not None and not any(not f.done() for f in queue):
            return self._take(owner, slot)
        future = asyncio.get_running_loop().create_future()
        queue.append(future)
        try:
            return await future
        except asyncio.CancelledError:
            if future.done() and not future.cancelled():
                self.release(future.result())   # handed a slot, then cancelled before it could run
            else:
                with contextlib.suppress(ValueError):
                    queue.remove(future)
            raise

    def release(self, slot: str) -> None:
        if self._used.get(slot, 0) <= 0:
            raise RuntimeError(f"inbound slot released more often than taken: {slot}")
        self._used[slot] -= 1
        self._wake()

    def _wake(self) -> None:
        for owner in (True, False):   # D3: a waiting owner frame first
            queue = self._waiting[owner]
            while queue:
                if queue[0].done():   # cancelled while it waited
                    queue.popleft()
                    continue
                slot = self._free(owner)
                if slot is None:
                    break
                queue.popleft().set_result(self._take(owner, slot))
