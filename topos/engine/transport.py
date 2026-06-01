from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict

MessageHandler = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any] | None]]


@dataclass(frozen=True)
class TransportConfig:
    mode: str = "ws"


def normalize_transport_mode(value: str | None) -> str:
    mode = (value or "").strip().lower()
    if mode in {"ws", "endpoint"}:
        return mode
    return "ws"


async def dispatch_compute_message(
    *,
    mode: str,
    payload: Dict[str, Any],
    handler: MessageHandler,
) -> Dict[str, Any] | None:
    normalized = normalize_transport_mode(mode)
    if normalized == "ws":
        return await handler(payload)
    # endpoint transport currently shares the same business handler.
    return await handler(payload)


async def handle_ws_raw_message(raw: str, handler: MessageHandler) -> str | None:
    message = json.loads(raw)
    response = await dispatch_compute_message(mode="ws", payload=message, handler=handler)
    if response is None:
        return None
    return json.dumps(response)


async def handle_endpoint_request(payload: Dict[str, Any], handler: MessageHandler) -> Dict[str, Any] | None:
    return await dispatch_compute_message(mode="endpoint", payload=payload, handler=handler)
