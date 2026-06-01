from __future__ import annotations

import random
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any, Literal, Tuple

from websockets.exceptions import InvalidStatus, InvalidURI

ConnectionState = Literal["idle", "connecting", "connected", "backing_off", "degraded", "stopping"]
FailureCategory = Literal["none", "auth", "ssl", "network", "protocol", "timeout", "unknown"]


@dataclass
class ResilienceConfig:
    initial_backoff_s: float = 1.0
    max_backoff_s: float = 30.0
    jitter_ratio: float = 0.2


@dataclass
class ConnectionSnapshot:
    state: ConnectionState
    connected: bool
    attempt: int
    consecutive_failures: int
    last_failure_category: FailureCategory
    last_failure_reason: str
    last_state_change_at: str | None
    last_connected_at: str | None
    last_disconnected_at: str | None
    outbox_depth: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "connected": self.connected,
            "attempt": self.attempt,
            "consecutive_failures": self.consecutive_failures,
            "last_failure_category": self.last_failure_category,
            "last_failure_reason": self.last_failure_reason,
            "last_state_change_at": self.last_state_change_at,
            "last_connected_at": self.last_connected_at,
            "last_disconnected_at": self.last_disconnected_at,
            "outbox_depth": self.outbox_depth,
        }


class ExponentialBackoff:
    def __init__(self, config: ResilienceConfig):
        self._config = config
        self._attempt = 0

    @property
    def attempt(self) -> int:
        return self._attempt

    def reset(self) -> None:
        self._attempt = 0

    def next_delay(self) -> float:
        self._attempt += 1
        base = self._config.initial_backoff_s * (2 ** (self._attempt - 1))
        bounded = min(self._config.max_backoff_s, max(0.0, base))
        jitter = bounded * max(0.0, self._config.jitter_ratio) * random.random()
        return bounded + jitter


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify_connection_error(exc: BaseException) -> Tuple[FailureCategory, str]:
    if isinstance(exc, TimeoutError):
        return "timeout", "Connection timed out."

    if isinstance(exc, InvalidStatus):
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        if status_code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            return "auth", f"Authentication rejected with HTTP {status_code}."
        return "protocol", f"Server rejected websocket upgrade with HTTP {status_code}."

    if isinstance(exc, InvalidURI):
        return "protocol", f"Invalid websocket URL: {exc}"

    if isinstance(exc, ssl.SSLError):
        return "ssl", f"TLS/SSL error: {exc}"

    if isinstance(exc, OSError):
        return "network", f"Network error: {exc}"

    message = str(exc).strip() or exc.__class__.__name__
    return "unknown", message


def is_fatal_connection_category(category: FailureCategory) -> bool:
    return category in {"auth", "protocol"}
