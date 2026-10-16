"""NR183 O1: the owner's own requests go ahead of recipients' when the node is busy (the owner's ruling D3).

Every relayed request that is not a ping or a fast-lane read waits for one of the control-plane client's inbound
handler slots (``settings.control_plane_inbound_concurrency_limit``, 16) and one of its waiting places
(``control_plane_inbound_max_pending``, 128). Until now that was one pool in arrival order, so a burst of share
searches and asks filled it and the owner's own app waited behind them, or was told "busy". With
``TOPOS_PERMISSIONS_V2_OWNER_LANE`` on:

- ``OWNER_LANE_SLOTS`` of the slots (4) are kept for the owner's interactive frames, and everyone else shares the
  rest (12) and can never take them. An owner frame takes a kept slot when one is free, and a shared one only when
  every kept slot is busy, never more than ``OWNER_LANE_SHARED_MAX`` (2) shared slots at once. A borrowed slot stays
  the owner frame's until that frame ends, even if a kept slot frees meanwhile: with 6 owner frames started and the
  4 on kept slots ending first, the 2 still running hold shared slots, and recipients run on 10 of 12 while the
  owner has only 2 frames. So recipients lose at most ``OWNER_LANE_SHARED_MAX`` slots, and only while borrowed owner
  frames run. That slows recipients; it never turns one away at admission (their waiting places are their own,
  below), though a request slowed past the control plane's budget meets R3a's busy reply, as any slowness would.
  ``OWNER_LANE_SHARED_MAX=0`` removes it: the owner then runs on its kept slots only. When a slot comes free, a
  waiting owner frame that may take it gets it before a waiting recipient's frame.
- Each class has its own waiting places, counted on its own frames only: the shared class 124 (128 less the kept
  slots), the owner lane 32 (as the fast lane). The owner's activity never makes a recipient's frame "busy", and
  recipients' activity never makes an owner frame "busy" (disclosure rule DR-04, the owner's ruling of 15 Oct:
  whether a recipient gets an answer must not depend on the node's other activity).

**Which owner frames.** Only the owner's interactive requests, someone at the screen waiting: ``OWNER_LANE_TYPES``,
a closed list (the home chat's searches and reads through the agent gateway, the explorer's table reads, the home
chat's generation and its own store). Every other owner type (a capture app's ingest, a source sync, an upload, a
reprocess, settings, sharing commands) stays in the shared class, in arrival order with everyone: under a steady
background flow strict owner-first had given every freed slot to owner frames and none to the recipients waiting.

**Whose frame it is** is read from the frame's signed principal stamp (``topos/relay_stamp.py``), the control
plane's own classification with proof: the owner's lane takes a frame only when

- it carries a stamp that VERIFIES under this node's pinned control-plane key, with the frame's own id and type
  bound into the signature, inside the stamp's time window, of class ``owner_app`` (the owner's own app: the home
  chat's searches through the agent gateway, the owner's write doors, sources and settings);
- its type is not one a recipient may reach (``core.handlers.NON_OWNER_RELAY_TYPES``: the share search, its batch
  form, the ask and its fetch), whatever its stamp and the list say, and it is one of ``OWNER_LANE_TYPES``;
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

#: The kinds of slot: a shared one, a kept one (only an owner frame may take it), and a shared one an owner frame
#: holds (``shared_cap`` at most at once).
GENERAL, RESERVED, BORROWED = "general", "reserved", "borrowed"
#: The owner lane's waiting places (frames in hand, handled or waiting), as the fast lane's.
OWNER_LANE_IN_HAND = 32
OWNER_APP = "owner_app"   # topos.principal.OWNER_APP
#: The principal classes whose verified frames take the owner's lane: this one line, and nothing else, says which.
#: Any other class is the shared class, a class this node does not know included (``owner_agent`` of the node's
#: unix socket among them: it is minted there, never on a relayed frame, whose stamp may carry only
#: ``relay_stamp.ALLOWED_CLASSES``, so its requests never wait for these slots).
OWNER_LANE_CLASSES = frozenset({OWNER_APP})


def is_owner_frame(message: Any, types: Any = None) -> bool:
    """True only for an interactive frame (a type in ``types``, ``OWNER_LANE_TYPES``'s default when None) that the
    control plane signed as the owner's own app (module docstring); False otherwise, and False on any error."""
    try:
        if not isinstance(message, Mapping):
            return False
        stamp = message.get("principal_stamp")
        # Cheap first: a frame that does not even claim the owner's app is not checked further.
        if not isinstance(stamp, Mapping) or stamp.get("cls") not in OWNER_LANE_CLASSES:
            return False
        from ..core.handlers import NON_OWNER_RELAY_TYPES

        kind = str(message.get("type") or "").strip().lower()
        if kind in NON_OWNER_RELAY_TYPES:
            return False   # a recipient door's type is never the owner's lane, whatever the stamp or the list says
        interactive = switches.OWNER_LANE_TYPES.bound if types is None else types
        if kind not in {str(name).strip().lower() for name in interactive}:
            return False   # background owner work shares the slots in arrival order
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
        lane = InboundSlots(limit, switches.number(switches.OWNER_LANE_SLOTS),
                            shared_cap=switches.number(switches.OWNER_LANE_SHARED_MAX),
                            types=switches.entries(switches.OWNER_LANE_TYPES))
    except Exception:  # noqa: BLE001 -- a lane that cannot be made is off: the semaphore, as before
        _log.warning("inbound owner lane could not be set up; it is off")
        return None
    _log.info("inbound owner lane on: %d of %d handler slots kept for the owner's interactive requests (%d types), "
              "at most %d shared slots borrowed", lane.reserved, lane.limit, len(lane.types), lane.shared_cap)
    return lane


class InboundSlots:
    """``limit`` handler slots, ``reserved`` of which only owner frames may take (``acquire(True)``).

    ``reserved`` is at least 0 and at most ``limit - 1``, and ``shared_cap`` at most one less than the shared slots:
    the shared class always keeps a slot. An owner frame takes a kept slot first and a shared one (``BORROWED``) only
    when every kept slot is busy and fewer than ``shared_cap`` are borrowed; a borrowed slot is held until its frame
    ends and is never swapped for a kept slot that frees. A freed slot goes to the oldest waiting owner frame that may
    take it, then to the oldest waiting shared one. Every slot taken is given back with
    ``release``, once. ``admit`` and ``left`` count each class's frames in hand against its own waiting places."""

    def __init__(self, limit: int, reserved: int, *, shared_cap: int = 2, types: Any = None):
        self.limit = max(1, int(limit))
        self.reserved = max(0, min(int(reserved), self.limit - 1))
        self.general = self.limit - self.reserved
        # No slot kept (one slot in all): there is no lane, and owner frames take the shared slot as anyone does.
        self.shared_cap = self.general if self.reserved == 0 else max(0, min(int(shared_cap), self.general - 1))
        self.types = tuple(switches.OWNER_LANE_TYPES.bound if types is None else types)
        self._used = {GENERAL: 0, RESERVED: 0}
        self._borrowed = 0
        self._in_hand = {True: 0, False: 0}
        self._waiting: dict[bool, deque] = {True: deque(), False: deque()}
        self.granted = {(True, GENERAL): 0, (True, RESERVED): 0, (False, GENERAL): 0}

    def owner_frame(self, message: Any) -> bool:
        """The class a frame waits in: True only when ``is_owner_frame`` says a plain yes; no on any error."""
        try:
            return is_owner_frame(message, self.types) is True
        except Exception:  # noqa: BLE001
            return False

    def in_use(self) -> dict:
        """Slots in use: shared ones (by either class) and kept ones."""
        return dict(self._used)

    def borrowed(self) -> int:
        """Shared slots owner frames hold now."""
        return self._borrowed

    def waiting(self) -> dict:
        return {"owner": sum(not f.done() for f in self._waiting[True]),
                "general": sum(not f.done() for f in self._waiting[False])}

    def in_hand(self) -> dict:
        return {"owner": self._in_hand[True], "general": self._in_hand[False]}

    def admit(self, owner: bool, max_pending: int) -> bool:
        """Whether a frame of this class may wait for a slot, counting its own class's frames in hand only (DR-04):
        the shared class up to ``max_pending - reserved``, the owner lane up to ``OWNER_LANE_IN_HAND``. Counted in when
        yes; ``left`` counts it out."""
        owner = owner is True
        limit = OWNER_LANE_IN_HAND if owner else max(1, int(max_pending) - self.reserved)
        if self._in_hand[owner] >= limit:
            return False
        self._in_hand[owner] += 1
        return True

    def left(self, owner: bool) -> None:
        """A frame ``admit`` counted in is done, however it ended."""
        owner = owner is True
        self._in_hand[owner] = max(0, self._in_hand[owner] - 1)

    def _free(self, owner: bool) -> str | None:
        if owner is True:
            if self._used[RESERVED] < self.reserved:
                return RESERVED   # DR-04: the owner's own slots first, so recipients keep all theirs
            if self._borrowed < self.shared_cap and self._used[GENERAL] < self.general:
                return BORROWED   # then a shared one, never more than shared_cap at once
            return None
        if self._used[GENERAL] < self.general:
            return GENERAL
        return None

    def _take(self, owner: bool, slot: str) -> str:
        if slot == BORROWED:
            self._borrowed += 1
            self._used[GENERAL] += 1
            self.granted[(True, GENERAL)] += 1
            return slot
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
        kind = GENERAL if slot == BORROWED else slot
        if self._used.get(kind, 0) <= 0 or (slot == BORROWED and self._borrowed <= 0):
            raise RuntimeError(f"inbound slot released more often than taken: {slot}")
        if slot == BORROWED:
            self._borrowed -= 1
        self._used[kind] -= 1
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
