"""Packet resolution — how much fact CONTENT the inference packet may carry.

Owner decision 2026-08-25 (PLAN_DERIVATION_LAYER.md, shadow-pilot S5 follow-up): a
per-database setting with three values, paired in the UI with the model that receives
the packet.

    scores_only  — today's behavior: relevance/similarity signal only (default)
    facts        — personal-class fact content: predicate, value, validity dates, altitude
    facts_all    — adds special-class facts (health, beliefs, admin)

Two STRUCTURAL interlocks — floors, not preferences:

  1. Requester floor. Non-owner turns are scores_only ALWAYS. A grantee or third-party
     MCP client can never be widened by the owner's toggle (J3/K1 measured the CP applies
     no filtering on that path — the engine is the only enforcement point).
  2. Model-locality gate. Content flows only while the resolved `primary` binding is
     on-device. A hosted binding (or a remote engine URL) drops the packet to
     scores_only — DECLARED, never silent: the effective state + reason surface in the
     settings payload and on every turn's public result.

This is a READ-TIME disclosure policy. It changes nothing about extraction, storage,
embedding or ranking; flipping it is instant and reversible in both directions. It is a
disclosure dimension, so it joins the retrieval fingerprint and the session cache key —
a downgrade must never be served a cached high-resolution answer.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional

RESOLUTIONS = ("scores_only", "facts", "facts_all")
_ORDER = {r: i for i, r in enumerate(RESOLUTIONS)}

#: Providers that run on this machine. Anything else is an egress boundary.
_LOCAL_PROVIDERS = frozenset({"", "ollama", "local", "scope-head"})


def resolution_order(value: str) -> int:
    return _ORDER.get(str(value or "").strip().lower(), 0)


def primary_binding_locality(conn: Optional[sqlite3.Connection]) -> Dict[str, Any]:
    """Where does the `primary` (query-answering) model actually run?

    Remote engine URL wins: when `topos_engine_service_url` is set the WHOLE task —
    packet included — leaves the machine regardless of the pack's provider string.
    """
    from ..config.settings import settings

    remote_url = (getattr(settings, "topos_engine_service_url", None) or "").strip()
    provider, model = "", str(getattr(settings, "ollama_query_model", "") or "")
    try:
        from ..config.model_packs import resolve_role_model

        bound = resolve_role_model(conn, "primary")
        if bound is not None:
            provider, model = str(bound[0] or ""), str(bound[1] or model)
    except Exception:  # noqa: BLE001 — no pack machinery ⇒ settings default (local ollama)
        pass
    local = (not remote_url) and provider.strip().lower() in _LOCAL_PROVIDERS
    return {"local": local, "provider": provider or "ollama", "model": model,
            "remote_engine_url": bool(remote_url)}


def effective_packet_resolution(
    conn: Optional[sqlite3.Connection],
    *,
    requester_id: str = "owner",
    disclosure_tier: str = "owner_raw",
) -> Dict[str, Any]:
    """The setting after both interlocks. `reason` says which floor applied, if any."""
    from ..config.settings import resolve_packet_resolution, settings

    setting = resolve_packet_resolution(settings, conn)
    locality = primary_binding_locality(conn)
    if str(requester_id or "") != "owner" or str(disclosure_tier or "") != "owner_raw":
        effective, reason = "scores_only", "non_owner_floor"
    elif setting != "scores_only" and not locality["local"]:
        effective, reason = "scores_only", "hosted_binding"
    else:
        effective, reason = setting, "active"
    return {"setting": setting, "effective": effective, "reason": reason, **locality}
