"""Prepaid-wallet probe for Topos-hosted ingest LLMs (platform + redpill).

Chat/routines already 402 on the control plane before generate. Ingest adapters
run on the engine with a node key and never saw the wallet. This module is the
short-TTL allow check they call first.

Fail-open on transport errors, but reuse the last successful probe so a brief
outage does not flip an empty wallet back to allowed.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Optional, Tuple

logger = logging.getLogger("topos.engine.hosted_llm_wallet")

INSUFFICIENT_CREDITS = "insufficient_credits"
_CACHE_TTL_SECONDS = 15.0

_lock = threading.Lock()
_cached_at = 0.0
_cached_allowed: Optional[bool] = None
_last_successful_allowed: Optional[bool] = None


def reset_hosted_llm_wallet_cache() -> None:
    """Test helper — drop the TTL cache and last successful probe."""
    global _cached_at, _cached_allowed, _last_successful_allowed
    with _lock:
        _cached_at = 0.0
        _cached_allowed = None
        _last_successful_allowed = None


def hosted_llm_wallet_allows(*, force: bool = False) -> bool:
    """True when a Topos-hosted ingest call may proceed.

    Denied only when a successful probe reports ``wallet_balance_usd <= 0``.
    Missing config, transport errors, and malformed payloads fail open, except
    that a previous successful deny/allow is reused until the next success.
    """
    global _cached_at, _cached_allowed, _last_successful_allowed
    now = time.monotonic()
    with _lock:
        if (
            not force
            and _cached_allowed is not None
            and (now - _cached_at) < _CACHE_TTL_SECONDS
        ):
            return _cached_allowed

    allowed, from_probe = _probe()
    with _lock:
        if from_probe:
            _last_successful_allowed = allowed
            _cached_allowed = allowed
        elif _last_successful_allowed is not None:
            _cached_allowed = _last_successful_allowed
        else:
            _cached_allowed = True
        _cached_at = time.monotonic()
        return bool(_cached_allowed)


def ingest_uses_hosted_llm() -> bool:
    """True when any ingest LLM role is currently Topos-hosted (not BYOK/local)."""
    try:
        from ..config.conversation_context_llm import resolve_context_llm_request
        from ..config.facts_llm import resolve_facts_llm_request
        from ..config.settings import settings
        from ..config.signal_extraction import get_signal_extraction_provider
        from ..core.state import get_db_connection

        conn = get_db_connection()
        if get_signal_extraction_provider() in {"platform", "redpill"}:
            return True
        facts_provider, _ = resolve_facts_llm_request(settings, conn)
        if facts_provider in {"platform", "redpill"}:
            return True
        ctx_provider, _ = resolve_context_llm_request(settings, conn)
        return ctx_provider in {"platform", "redpill"}
    except Exception:  # noqa: BLE001 — a sweep must never crash on config
        logger.debug("ingest hosted-provider check failed", exc_info=True)
        return False


def _probe() -> Tuple[bool, bool]:
    """Return ``(allowed, from_successful_probe)``."""
    try:
        from ..config.settings import settings
        from ..relay_stamp import cp_http_base_from_ws_url
    except Exception:
        return True, False

    api_key = str(getattr(settings, "topos_key", None) or "").strip()
    base = cp_http_base_from_ws_url(str(getattr(settings, "topos_control_plane_url", "") or ""))
    if not base:
        raw = str(getattr(settings, "topos_control_plane_url", "") or "").strip()
        if raw.startswith("http://") or raw.startswith("https://"):
            base = raw.split("/ws/")[0].rstrip("/")
    if not base or not api_key:
        return True, False

    url = f"{base}/v1/billing/status"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        logger.debug("hosted llm wallet probe failed: %s", exc)
        return True, False
    except Exception as exc:  # noqa: BLE001
        logger.debug("hosted llm wallet probe failed: %s", exc)
        return True, False

    try:
        balance = float(payload.get("wallet_balance_usd") or 0)
    except (TypeError, ValueError):
        return True, False
    return balance > 0, True
