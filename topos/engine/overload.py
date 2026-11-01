"""Content-free overload signals shared by local and remote compute clients.

This is a transport contract, not a scheduler. No retries or queue positions
are invented here; admission belongs to the host that owns the compute.
"""
from __future__ import annotations

from typing import Any

_MESSAGES = {
    "compute_busy": "Compute is busy; the provider's pending queue is full.",
    "compute_unavailable": "Compute is temporarily unavailable.",
    "compute_rate_limited": "Compute is rate limited.",
}


def retry_after_seconds(value: Any) -> int | None:
    """Accept bounded delta-seconds only; absent/invalid is unknown, not zero."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) or (isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 5):
        seconds = int(value)
        if 0 <= seconds <= 86400:
            return seconds
    return None


class ComputeUnavailable(RuntimeError):
    def __init__(self, code: str, retry_after: Any = None) -> None:
        if code not in _MESSAGES:
            raise ValueError("Unknown compute availability code")
        self.code = code
        self.status_code = 429 if code == "compute_rate_limited" else 503
        self.retry_after = retry_after_seconds(retry_after)
        super().__init__(_MESSAGES[code])

    def detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.retry_after is not None:
            detail["retry_after_seconds"] = self.retry_after
        return detail

    def headers(self) -> dict[str, str]:
        return {"Retry-After": str(self.retry_after)} if self.retry_after is not None else {}

    def envelope(self, request_id: Any) -> dict[str, Any]:
        result = {"id": request_id, "status": "error", "error": str(self),
                  "error_code": self.status_code, "error_type": self.code}
        if self.retry_after is not None:
            result["retry_after_seconds"] = self.retry_after
        return result


def provider_unavailable(status_code: int, body: str, retry_after: Any = None) -> ComputeUnavailable | None:
    if status_code == 429:
        return ComputeUnavailable("compute_rate_limited", retry_after)
    if status_code == 503:
        # Versioned Ollama scheduler's ErrMaxQueue. A generic 503 is not proof
        # of queue saturation. Never copy the provider body into the response.
        code = "compute_busy" if "maximum pending requests exceeded" in body.lower() else "compute_unavailable"
        return ComputeUnavailable(code, retry_after)
    return None


def unavailable_from_detail(status_code: int, detail: Any) -> ComputeUnavailable | None:
    if not isinstance(detail, dict) or not isinstance(detail.get("code"), str) or detail["code"] not in _MESSAGES:
        return None
    error = ComputeUnavailable(detail["code"], detail.get("retry_after_seconds"))
    return error if error.status_code == status_code else None
