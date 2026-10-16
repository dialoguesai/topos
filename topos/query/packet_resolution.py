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

#: Special-class SCOPES (AI-tool on-ramp, CONTRACTS §1.3, ruling D1 (b)): the kinds that stay
#: with the owner and his app until he switches them on for an agent. The node had no
#: scope-level definition before this (facts_direct.py's `special` is per PREDICATE, from the
#: pack's declared sensitivity), so this is it: the two named scopes, plus any registry entry
#: whose primary dimensions name Health or Beliefs (none does today; the rule is there for the
#: day one is added without anyone remembering this set).
SPECIAL_CLASS_SCOPES = frozenset({"health:read", "facts_sensitive:read"})
_SPECIAL_CLASS_DIMENSIONS = frozenset({"Health", "Beliefs"})


def resolution_order(value: str) -> int:
    return _ORDER.get(str(value or "").strip().lower(), 0)


def _scope_entry(scope_id: str) -> Optional[Dict[str, Any]]:
    try:
        from .scope_registry_loader import get_scope_entry

        return get_scope_entry(scope_id)
    except Exception:  # noqa: BLE001 — no registry ⇒ only the named set decides
        return None


def is_special_class_scope(scope_id: str) -> bool:
    """Whether `scope_id` is special-class for an owner's agent (see SPECIAL_CLASS_SCOPES)."""
    sid = str(scope_id or "").strip()
    if not sid:
        return False
    if sid in SPECIAL_CLASS_SCOPES:
        return True
    entry = _scope_entry(sid)
    dims = entry.get("primary_dimensions") if isinstance(entry, dict) else None
    return bool(dims) and any(str(d) in _SPECIAL_CLASS_DIMENSIONS for d in dims)


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
    owner_id: str = "",
    principal: "Optional[object]" = None,
    scope_id: str = "",
) -> Dict[str, Any]:
    """The setting after both interlocks. `reason` says which floor applied, if any.

    Two owner tests, by channel (P1 of the principal fabric):

    - `principal` is the CHANNEL-verified client class (topos/principal.py) —
      which credential authenticated, on which transport. OWNER_APP is the
      owner's own surface regardless of payload ids; THIRD_PARTY is floored
      regardless of payload ids (a legacy-key client claiming
      requester_id == owner_id is a spoof, reason `principal_floor`). Never
      derived from the payload.
    - CP_RELAY (and legacy None) fall back to forwarded-id equality, mirroring
      `resolve_disclosure_tier`: the CP authenticates the caller and — since the
      2026-08-26 containment — stamps requester_id == owner_id for Topos-native
      clients ONLY (see the CP's test_owner_identity_forwarding.py, both sides).
      Comparing against the literal "owner" alone had floored every
      gateway-routed owner turn (live 2026-08-26: "What medications am I
      taking?" answered "unknown" while the fact sat in signal_objects); the
      unconditional stamp before the containment would have opened facts_all to
      every OAuth connector. The disclosure-tier leg stays as the independent
      guard: a grantee is never resolved to owner_raw, so id-equality alone can
      never widen a grantee.
    """
    from ..config.settings import resolve_packet_resolution, settings
    from ..principal import OWNER_AGENT, OWNER_APP, THIRD_PARTY

    setting = resolve_packet_resolution(settings, conn)
    locality = primary_binding_locality(conn)
    req = str(requester_id or "")
    own = str(owner_id or "")
    cls = getattr(principal, "cls", None)
    if cls == THIRD_PARTY:
        # Elevation (P2, "one consent ledger" §03b): an enrolled client may hold
        # an approved, unexpired, per-scope consent record. It lifts the packet
        # floor to min(owner setting, facts) — never facts_all, so special-class
        # content stays owner-first-party — and every other gate keeps its
        # authority: the owner's global scores_only dial caps it, the locality
        # gate floors a hosted binding, and the disclosure TIER stays
        # default_disclosure (elevation is about the fact packet, not raw rows).
        # Note this branch deliberately does not require owner_raw tier.
        client_id = str(getattr(principal, "client_id", "") or "")
        if conn is not None and client_id and scope_id and setting != "scores_only":
            from ..mcp_clients import ELEVATION_CEILING, active_elevation

            grant = active_elevation(conn, client_id=client_id, scope_id=scope_id)
            if grant is not None:
                candidates = (setting, ELEVATION_CEILING, str(grant.get("resolution") or ""))
                effective = min(candidates, key=resolution_order)
                if resolution_order(effective) > 0:
                    if not locality["local"]:
                        effective, reason = "scores_only", "hosted_binding"
                    else:
                        reason = f"consent_grant:{grant.get('id')}"
                    return {"setting": setting, "effective": effective, "reason": reason,
                            "principal_cls": cls or "", **locality}
        is_owner, floor_reason = False, "principal_floor"
    elif cls == OWNER_APP:
        is_owner, floor_reason = True, "non_owner_floor"
    elif cls == "owner_automation":
        # P5: routines act with the owner's authority but are not the owner's
        # eyes — the doc's decision caps automation at `facts`, so special-class
        # content (health, beliefs, admin) never rides an unattended lane. The
        # cap is declared (reason automation_cap), and every other gate below
        # keeps its authority.
        if str(disclosure_tier or "") != "owner_raw":
            effective, reason = "scores_only", "non_owner_floor"
        elif setting != "scores_only" and not locality["local"]:
            effective, reason = "scores_only", "hosted_binding"
        elif resolution_order(setting) > resolution_order("facts"):
            effective, reason = "facts", "automation_cap"
        else:
            effective, reason = setting, "active"
        return {"setting": setting, "effective": effective, "reason": reason,
                "principal_cls": cls or "", **locality}
    elif cls == OWNER_AGENT and getattr(principal, "channel", None) != "uds":
        # The class is minted on the socket only (rev-1 security 19): the word on
        # any other channel — a stamp, a forwarder — is floored like a third
        # party, whatever ids the payload carries.
        is_owner, floor_reason = False, "principal_floor"
    elif cls == OWNER_AGENT:
        # The owner's approved coding agent, ruling D1 (b) (AI-tool on-ramp,
        # CONTRACTS §1.3): a special-class scope is scores_only for it until the
        # owner switches that kind on; every other scope reads at the owner's
        # own dial, as his app does. The model-locality gate is SKIPPED for this
        # class — the agent's model is always hosted, and the ruling is that
        # the owner's dial applies anyway; the reason says so. The disclosure
        # tier stays the independent guard, as for owner_app.
        if is_special_class_scope(scope_id):
            effective, reason = "scores_only", "owner_agent_special_class"
        elif str(disclosure_tier or "") != "owner_raw":
            effective, reason = "scores_only", "non_owner_floor"
        elif setting == "facts_all":
            # The consented-client ceiling (mcp_clients.ELEVATION_CEILING) holds for the agent too: the
            # per-PREDICATE special gate (facts_direct) opens exactly at facts_all, so the owner's own dial
            # would hand the agent health/beliefs facts through facts:read, whatever the scope's dimensions say
            # (security review F3, 16 Oct 2026). Special-class CONTENT stays owner-first-party.
            effective, reason = "facts", "owner_agent_ruling_capped"
        else:
            effective, reason = setting, "owner_agent_ruling"
        return {"setting": setting, "effective": effective, "reason": reason,
                "principal_cls": cls or "", **locality}
    else:  # CP_RELAY or legacy: the forwarded-id equality test
        is_owner = req == "owner" or (bool(own) and own != "owner" and req == own)
        floor_reason = "non_owner_floor"
    if not is_owner or str(disclosure_tier or "") != "owner_raw":
        effective, reason = "scores_only", floor_reason
    elif setting != "scores_only" and not locality["local"]:
        effective, reason = "scores_only", "hosted_binding"
    else:
        effective, reason = setting, "active"
    return {"setting": setting, "effective": effective, "reason": reason,
            "principal_cls": cls or "", **locality}
