"""Kept questions, Wave 2 (lane OFF2N; ``lanes/OFF2_CONTRACT.md`` §C, §D): the owner's order, over the relay.

- ``get_sharing_readiness`` (no payload): whether this node has caught up, as counts (``permissions_v2.sharing_
  readiness``), the same object the heartbeat and the register carry. Answered from memory only: no SQLite read, no
  write gate; in the client's fast lane, so neither a full set of inbound slots nor a held write gate delays it.

- ``put_away_config``: the owner's "While you're using Topos" (``while_using``) and "Answer others only when …"
  (``others_when_idle``), which the control plane sends on every change and after each register. Held in memory for
  this control-plane connection only and applied live (``permissions_v2.away_config``; R-OFF2 F3); a push whose
  ``seq`` is lower than the one held is ignored. The reply is what the node now holds. A payload that is not exactly
  ``{"v": 1, "seq": <int>, "while_using": "owner_first" | "even", "others_when_idle": bool}`` is the node's usual error
  reply, and nothing changes.

Both are registered ``owner_only``, so the dispatcher answers every principal but ``owner_app`` (the owner's socket,
or a relayed frame the control plane signed as the owner's own app) with its usual ``owner_mode_required``, and
``off_loop``: they run in a worker thread. Neither touches the database.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .registry import handles


@handles("put_away_config", owner_only=True, off_loop=True)
async def handle_put_away_config(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...permissions_v2 import away_config

    try:
        held = away_config.put(message.get("payload"))
    except ValueError:
        return {"id": req_id, "status": "error", "error": "away_config_invalid"}
    except Exception:  # noqa: BLE001 -- never a path or an exception's text on the wire
        return {"id": req_id, "status": "error", "error": "away_config_unavailable"}
    return {"id": req_id, "status": "ok", "payload": held}


@handles("get_sharing_readiness", owner_only=True, off_loop=True)
async def handle_get_sharing_readiness(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from ...engine.registration import sharing_readiness

    readiness = sharing_readiness()
    if readiness is None:
        return {"id": message.get("id"), "status": "error", "error": "sharing_readiness_unavailable"}
    return {"id": message.get("id"), "status": "ok", "payload": readiness}
