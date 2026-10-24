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

``put_away_config`` (owner class only) stores it in ``engine_config`` under ``ENGINE_CONFIG_KEY``, as JSON, as
``put_facts_llm_config`` stores its pick, and applies it at once: the next acquire of an inbound slot and the next
poll of every background wait read it. ``load`` reads it once when the app starts. Never set, it is ``DEFAULT``. A
payload that is not exactly the contract's shape is refused and nothing is stored. The setting names no one and goes
nowhere but this node's own scheduling.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable, Mapping

#: The ``engine_config`` row the settings are kept in (one key, JSON).
ENGINE_CONFIG_KEY = "permissions_v2_away_config"
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
_listeners: list = []


def parse(payload: Any) -> dict:
    """The settings ``payload`` states, or ValueError: exactly ``v`` 1, ``while_using`` one of ``WHILE_USING`` and
    ``others_when_idle`` a boolean, and no other key."""
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


def store(conn, payload: Any) -> dict:
    """``put_away_config``: keep ``payload`` in ``engine_config`` and hold it. A payload ``parse`` refuses raises
    ValueError before anything is written; a write that fails raises and changes nothing held."""
    parsed = parse(payload)
    from ..core.state import set_engine_config_value

    set_engine_config_value(conn, ENGINE_CONFIG_KEY, json.dumps(parsed, sort_keys=True))
    return apply(parsed)


def load(conn) -> dict:
    """At start: hold what the control plane last pushed, or ``DEFAULT`` when nothing (or nothing readable) is kept."""
    from ..core.state import get_engine_config_value

    raw = get_engine_config_value(conn, ENGINE_CONFIG_KEY)
    if raw is None:
        return apply(DEFAULT)
    try:
        return apply(json.loads(raw))
    except (TypeError, ValueError):
        _log.warning("away settings kept on this node are not readable; the defaults hold until the next push")
        return apply(DEFAULT)
