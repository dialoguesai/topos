"""Kept questions, Wave 2 (lane OFF2N; ``lanes/OFF2_CONTRACT.md`` §D): the owner's "While you're using Topos" and
"Answer others only when …" settings, as the control plane last pushed them to this node.

The control plane's row is the authority (it paces the delivery of kept questions, §F); the node keeps a copy so it
can schedule its own work by it with no request in hand:

- ``while_using``: ``owner_first`` (the default, OD-13) keeps the owner lane's reserve and turns the yield rule on
  (``answer_gate``: background work waits while one of the owner's own frames is in flight); ``even`` ("Share
  evenly") turns both off: every inbound slot is shared in arrival order (``owner_lane.InboundSlots``) and nothing
  waits for the owner.
- ``others_when_idle``: only whether readiness reports ``owner_idle_seconds`` (§C, DR-45). Off (the default), the
  node says nothing about the owner's activity.

``put_away_config`` (owner class only) applies a push at once: the next acquire of an inbound slot and the next poll
of every background wait read it. **Held in memory for the current control-plane connection only** (contract §D as
revised after R-OFF2 F3): nothing is written to disk, and each new connection, and each one that ends, goes back to
``DEFAULT`` (``reset``) until the control plane pushes again. So a control plane whose owner-order switch is off, or a
push that failed, always means the defaults: no idleness reported, the reserve and the yield rule on. A push carries
``seq``, the control-plane row's update counter: one lower than the ``seq`` held on this connection is ignored (the
reply says what is held), so two pushes that cross end on the newer. A payload that is not exactly the contract's
shape is refused and nothing changes. The setting names no one and goes nowhere but this node's own scheduling.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Mapping

#: ``permissions_v2_away_config`` in the node's capabilities: >= 1, this node takes ``put_away_config``.
CAPABILITY_VERSION = 1
VERSION = 1
OWNER_FIRST, EVEN = "owner_first", "even"
WHILE_USING = (OWNER_FIRST, EVEN)
#: What the node holds until the control plane says otherwise (contract §D: "Never set: owner_first, false").
DEFAULT = {"v": VERSION, "while_using": OWNER_FIRST, "others_when_idle": False}

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_current: dict = dict(DEFAULT)
_seq: int | None = None   # the newest push's seq on this connection; None: no push since the connection began
_listeners: list = []
#: A push: what the node holds, and the control plane's order for it.
PUSH_FIELDS = frozenset((*DEFAULT, "seq"))


def parse(payload: Any) -> dict:
    """The settings ``payload`` states (the held shape, no ``seq``), or ValueError: exactly ``v`` 1, ``while_using``
    one of ``WHILE_USING`` and ``others_when_idle`` a boolean, and no other key."""
    if not isinstance(payload, Mapping) or set(payload) != set(DEFAULT):
        raise ValueError("away_config_invalid")
    version, while_using, idle = payload["v"], payload["while_using"], payload["others_when_idle"]
    if type(version) is not int or version != VERSION or while_using not in WHILE_USING or type(idle) is not bool:
        raise ValueError("away_config_invalid")
    return {"v": VERSION, "while_using": while_using, "others_when_idle": idle}


def current() -> dict:
    """What the node holds now."""
    with _lock:
        return dict(_current)


def while_using() -> str:
    with _lock:
        return _current["while_using"]


def shares_evenly() -> bool:
    """"Share evenly": the owner lane's reserve and the yield rule are off."""
    return while_using() == EVEN


def others_when_idle() -> bool:
    with _lock:
        return _current["others_when_idle"] is True


def on_change(listener: Callable[[], None]) -> Callable[[], None]:
    """Call ``listener`` after every change the node applies; returns what stops it."""
    with _lock:
        _listeners.append(listener)

    def stop() -> None:
        with _lock:
            if listener in _listeners:
                _listeners.remove(listener)
    return stop


def apply(settings: Any) -> dict:
    """Hold ``settings`` (``parse`` decides) from now on, and tell the listeners. Returns what is held."""
    parsed = parse(settings)
    with _lock:
        _current.clear()
        _current.update(parsed)
        listeners = list(_listeners)
    for listener in listeners:
        try:
            listener()
        except Exception:  # noqa: BLE001 -- a listener never undoes a setting the node holds
            _log.debug("away settings listener failed", exc_info=True)
    return dict(parsed)


def put(payload: Any) -> dict:
    """``put_away_config``: exactly ``{"v": 1, "seq": <int>, "while_using", "others_when_idle"}`` (else ValueError,
    nothing changes). Held at once, unless its ``seq`` is lower than the one held on this connection: then nothing
    changes. Returns what the node holds either way (the held shape, no ``seq``)."""
    global _seq
    if not isinstance(payload, Mapping) or set(payload) != PUSH_FIELDS:
        raise ValueError("away_config_invalid")
    seq = payload["seq"]
    if type(seq) is not int:
        raise ValueError("away_config_invalid")
    parsed = parse({key: payload[key] for key in DEFAULT})
    with _lock:
        if _seq is not None and seq < _seq:
            return dict(_current)   # an older push that arrived late: the newer one stands
        _seq = seq
    return apply(parsed)


def reset() -> dict:
    """A control-plane connection began or ended: ``DEFAULT`` until the control plane pushes on the new one."""
    global _seq
    with _lock:
        _seq = None
    return apply(DEFAULT)
