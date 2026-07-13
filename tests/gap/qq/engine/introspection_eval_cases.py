"""I-series: MCP self-knowledge & connector-introspection cases (qq-introspect-1).

PLAN_MCP_INTROSPECTION.md §6. These cases exercise the CONTROL PLANE's MCP
gateway composition (introspection tools + the query_scope meta-intercept),
not engine retrieval — they are MCP-lane ONLY and are skipped when the runner
runs with --no-mcp. Checkers are deterministic and operate on the raw tool
payload dict; no engine imports, no oracles.

Sub-lanes (reported separately, all NON-GATING at introduction — red-first per
NH/IMB convention; promote I-C1 + the absent lane to gates once P2 is deployed):
  present  — server/scope/connector self-description accuracy
  absent   — honest abstention for unknown connectors (never fabricate)
  guard    — data queries that must NOT be intercepted (precision pins)
  boundary — secret-hygiene checks runnable on the owner lane

Grantee-boundary cases (I-B1/B2/B5/B6: fleet invisibility, tool hiding) are
pinned hermetically in the CP repo (tests/control_plane/
test_mcp_introspection_gateway.py) — a grantee auth context cannot be minted
from this owner-key runner. I-B4 (egress-log Q&A) is v2 (plan P4) and is
deliberately absent rather than permanently red here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Tuple

INTROSPECTION_CATALOG_VERSION = "qq-introspect-1"

CheckFn = Callable[[Dict[str, Any]], Tuple[bool, str]]


@dataclass(frozen=True)
class IntrospectionCase:
    id: str
    tool: str  # MCP tool to call: describe_connector | list_connectors | list_scopes | mcp_session_context | query_scope
    args: Dict[str, Any]
    evaluate: CheckFn
    lane: str  # "present" | "absent" | "guard" | "boundary"
    description: str = ""
    max_latency_ms: int = field(default=15000)


def _blob(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, default=str, ensure_ascii=False).lower()


def _card(payload: Dict[str, Any]) -> Dict[str, Any]:
    """The card, whether returned directly (tool call) or wrapped in a
    query_scope envelope (public_result)."""
    pr = payload.get("public_result")
    if isinstance(pr, dict) and pr.get("kind"):
        return pr
    return payload


def _intercepted(payload: Dict[str, Any]) -> bool:
    return str(payload.get("game_layer_strategy") or "") == "introspection"


# --- checkers ---------------------------------------------------------------


def eval_session_digest(payload: Dict[str, Any]) -> Tuple[bool, str]:
    digest = payload.get("capability_digest")
    if not isinstance(digest, dict):
        return False, "capability_digest missing from mcp_session_context"
    installed = ((digest.get("connectors") or {}).get("installed")) or 0
    if installed < 1:
        return False, f"digest reports {installed} installed connectors"
    hints = payload.get("hints") or {}
    if "introspection" not in hints:
        return False, "hints.introspection routing hint missing"
    return True, f"digest ok ({installed} connectors)"


def eval_session_identity(payload: Dict[str, Any]) -> Tuple[bool, str]:
    if not str(payload.get("user_id") or "").strip():
        return False, "user_id missing"
    if not isinstance(payload.get("engine_connected"), bool):
        return False, "engine_connected not a boolean"
    return True, "identity + engine status present"


def eval_scope_catalog_described(payload: Dict[str, Any]) -> Tuple[bool, str]:
    scopes = payload.get("scopes") or []
    if payload.get("kind") != "scope_catalog" or len(scopes) < 10:
        return False, f"expected scope_catalog with >=10 scopes, got {len(scopes)}"
    undescribed = [s.get("scope_id") for s in scopes if not str(s.get("description") or "").strip()]
    if undescribed:
        return False, f"scopes missing description: {undescribed}"
    return True, f"{len(scopes)} scopes, all described"


def eval_messages_feeding_sources(payload: Dict[str, Any]) -> Tuple[bool, str]:
    scopes = payload.get("scopes") or []
    if len(scopes) != 1:
        return False, f"expected exactly messages:read, got {len(scopes)} scopes"
    feeders = {f.get("connector_id") for f in scopes[0].get("feeding_sources") or []}
    if "topos.messaging_sync" not in feeders:
        return False, f"messages:read feeders missing topos.messaging_sync: {sorted(feeders)}"
    return True, "messaging_sync feeds messages:read"


def eval_activity_row_count(payload: Dict[str, Any]) -> Tuple[bool, str]:
    for scope in payload.get("scopes") or []:
        if scope.get("scope_id") != "activity:read":
            continue
        tables = scope.get("tables") or []
        counted = [t for t in tables if isinstance(t.get("row_count"), int)]
        if counted:
            return True, f"activity tables carry row counts: {counted}"
        return False, f"activity:read tables lack row_count (engine offline?): {tables}"
    return False, "activity:read not in scope catalog"


def _eval_connector_card_shape(payload: Dict[str, Any]) -> Tuple[bool, str]:
    card = _card(payload)
    if card.get("kind") != "connector_card":
        return False, f"expected connector_card, got kind={card.get('kind')!r}"
    if '"record_id"' in _blob(payload):
        return False, "canonical data rows leaked into an introspection answer"
    if card.get("found"):
        caps = card.get("capabilities") or {}
        if not (caps.get("sources") or caps.get("remote_tools") or caps.get("client")):
            return False, "found card with empty capabilities"
        return True, f"connector card for {card.get('connector_id')}"
    if "capabilities" in card:
        return False, "not-found card must not carry capabilities"
    if (card.get("catalog") or {}).get("kind") != "catalog_pointer":
        return False, "not-found card missing catalog pointer"
    return True, "honest not-found card"


def eval_flagship_github(payload: Dict[str, Any]) -> Tuple[bool, str]:
    if not _intercepted(payload):
        return False, (
            "meta-question reached data retrieval "
            f"(turn_outcome={payload.get('turn_outcome')!r}) — the original bug"
        )
    return _eval_connector_card_shape(payload)


def eval_connector_card(payload: Dict[str, Any]) -> Tuple[bool, str]:
    return _eval_connector_card_shape(payload)


def eval_slash_scope_connector(payload: Dict[str, Any]) -> Tuple[bool, str]:
    """Slash-scope context: a generic 'this connector' question carried on a
    scope_id that names a connector must resolve THAT connector, not fall to
    an unhelpful not-found (the reported live failure)."""
    if not _intercepted(payload):
        return False, (
            "generic connector question reached data retrieval "
            f"(turn_outcome={payload.get('turn_outcome')!r})"
        )
    card = _card(payload)
    if card.get("kind") != "connector_card":
        return False, f"expected connector_card, got kind={card.get('kind')!r}"
    if not card.get("found"):
        return False, (
            "scope_id named a connector but the card is not-found — the scope_id "
            "referent was not used"
        )
    return True, f"scope_id resolved to {card.get('connector_id')}"


def eval_found_connector(connector_id: str, *needles: str) -> CheckFn:
    def _check(payload: Dict[str, Any]) -> Tuple[bool, str]:
        card = _card(payload)
        if card.get("kind") != "connector_card" or not card.get("found"):
            return False, f"expected found connector_card, got {card.get('kind')!r}/found={card.get('found')!r}"
        if card.get("connector_id") != connector_id:
            return False, f"resolved to {card.get('connector_id')!r}, expected {connector_id!r}"
        blob = _blob(card)
        missing = [n for n in needles if n.lower() not in blob]
        if missing:
            return False, f"card missing needles: {missing}"
        return True, f"{connector_id} card ok"

    return _check


def eval_found_with_needles(*needles: str) -> CheckFn:
    """Found card carrying all needles — connector_id NOT pinned (live
    registries may own a source under a different id than the bundled group,
    e.g. browser sources under 'browser-history-plugin', not 'topos.browser')."""

    def _check(payload: Dict[str, Any]) -> Tuple[bool, str]:
        card = _card(payload)
        if card.get("kind") != "connector_card" or not card.get("found"):
            return False, f"expected found connector_card, got {card.get('kind')!r}/found={card.get('found')!r}"
        blob = _blob(card)
        missing = [n for n in needles if n.lower() not in blob]
        if missing:
            return False, f"card missing needles: {missing}"
        return True, f"{card.get('connector_id')} card carries {list(needles)}"

    return _check


def eval_fleet_card(payload: Dict[str, Any]) -> Tuple[bool, str]:
    card = _card(payload)
    if card.get("kind") != "connector_fleet":
        return False, f"expected connector_fleet, got {card.get('kind')!r}"
    connectors = card.get("connectors") or []
    if card.get("count") != len(connectors) or len(connectors) < 4:
        return False, f"fleet count mismatch or too small: {card.get('count')} vs {len(connectors)}"
    if (card.get("discover_more") or {}).get("kind") != "catalog_pointer":
        return False, "fleet card missing discover_more catalog pointer"
    return True, f"fleet of {len(connectors)} with catalog pointer"


def eval_absent_connector(payload: Dict[str, Any]) -> Tuple[bool, str]:
    card = _card(payload)
    if card.get("kind") != "connector_card":
        return False, f"expected connector_card, got {card.get('kind')!r}"
    if card.get("found"):
        return False, f"fabricated a card for an absent connector: {card.get('connector_id')!r}"
    if "capabilities" in card:
        return False, "absent connector must not have capabilities"
    if (card.get("catalog") or {}).get("kind") != "catalog_pointer":
        return False, "absence answer missing the catalog pointer"
    return True, "honest absence with pointer"


def eval_absent_no_sync_time(payload: Dict[str, Any]) -> Tuple[bool, str]:
    ok, reason = eval_absent_connector(payload)
    if not ok:
        return ok, reason
    if "last_ingest_at" in _blob(payload):
        return False, "fabricated a sync timestamp for an absent connector"
    return True, "no fabricated sync time"


def eval_not_intercepted(payload: Dict[str, Any]) -> Tuple[bool, str]:
    if _intercepted(payload):
        return False, "data query was wrongly intercepted as introspection"
    outcome = str(payload.get("turn_outcome") or "")
    if not outcome or outcome == "error":
        return False, f"data path errored: {payload.get('deny_reason')!r}"
    return True, f"reached data path (turn_outcome={outcome})"


def eval_scope_redirect(payload: Dict[str, Any]) -> Tuple[bool, str]:
    introspection = payload.get("introspection") or {}
    if introspection.get("redirected_from_scope") != "github_activity":
        return False, (
            "source-id-as-scope did not redirect "
            f"(turn_outcome={payload.get('turn_outcome')!r}, deny={payload.get('deny_reason')!r})"
        )
    card = _card(payload)
    if card.get("kind") != "connector_card" or "scope_hint" not in card:
        return False, "redirect card missing connector card or scope_hint"
    return True, "github_activity redirected to a connector card with scope_hint"


def eval_server_card(payload: Dict[str, Any]) -> Tuple[bool, str]:
    card = _card(payload)
    if card.get("kind") != "server_card":
        return False, f"expected server_card, got {card.get('kind')!r}"
    if not card.get("tool_families") or not card.get("access_modes"):
        return False, "server card missing tool_families/access_modes"
    return True, "server card ok"


def eval_secret_hygiene(payload: Dict[str, Any]) -> Tuple[bool, str]:
    card = _card(payload)
    if card.get("kind") != "connector_card":
        # An empty/error payload must not count as "hygienic" — there was no card.
        return False, f"expected a connector_card to inspect, got kind={card.get('kind')!r}"
    blob = _blob(payload)
    for forbidden in ("ciphertext", "client_secret_hash", "refresh_token", "access_token"):
        if f'"{forbidden}"' in blob:
            return False, f"card leaked secret-shaped field {forbidden!r}"
    return True, "no secret-shaped fields in card"


def eval_catalog_pointer_not_enumeration(payload: Dict[str, Any]) -> Tuple[bool, str]:
    card = _card(payload)
    if card.get("kind") != "catalog_pointer":
        return False, f"expected catalog_pointer, got {card.get('kind')!r}"
    if card.get("connectors"):
        return False, "discovery answer enumerated connectors (must be link-only)"
    return True, "link-only catalog pointer"


def _qs(scope_id: str, intent: str) -> Dict[str, Any]:
    return {"scope_id": scope_id, "intent": intent, "access_mode": "summary"}


INTROSPECTION_CASES: List[IntrospectionCase] = [
    # --- present: server self-knowledge -------------------------------------
    IntrospectionCase(
        "I-S1", "mcp_session_context", {}, eval_session_digest, "present",
        "Session context carries the CP-local capability digest + routing hint.",
    ),
    IntrospectionCase(
        "I-S3", "mcp_session_context", {}, eval_session_identity, "present",
        "Who am I / is my engine connected.",
    ),
    IntrospectionCase(
        "I-S4", "list_scopes", {}, eval_scope_catalog_described, "present",
        "Scope catalog: every scope carries human description text.",
    ),
    IntrospectionCase(
        "I-S5", "query_scope",
        _qs("activity:read", "What's the difference between raw, summary, and inference modes?"),
        eval_server_card, "present",
        "Access-mode contract question intercepts to the server card.",
    ),
    IntrospectionCase(
        "I-S7", "query_scope", _qs("activity:read", "What can you do?"),
        eval_server_card, "present",
        "Bare capability question intercepts to the server card.",
    ),
    # --- present: scope/data catalog ----------------------------------------
    IntrospectionCase(
        "I-K1", "list_scopes", {"scope_id": "messages:read"},
        eval_messages_feeding_sources, "present",
        "Which connector feeds my messages data.",
    ),
    IntrospectionCase(
        "I-K3", "list_scopes", {}, eval_activity_row_count, "present",
        "Scope catalog carries engine row counts (activity tables).",
    ),
    # --- present: connector cards -------------------------------------------
    IntrospectionCase(
        "I-C1", "query_scope",
        _qs("activity:read", "Tell me what I can do with Github connector?"),
        eval_flagship_github, "present",
        "REGRESSION FLAGSHIP: the observed live failure — meta-question must "
        "return a connector card, never canonical rows.",
    ),
    IntrospectionCase(
        "I-C2", "describe_connector", {"connector": "github"},
        eval_connector_card, "present",
        "Direct tool: GitHub card (found or honest not-found; never rows).",
    ),
    IntrospectionCase(
        "I-C3", "describe_connector", {"connector": "browser"},
        eval_found_with_needles("browser_visits", "activity:read"),
        "present",
        "Browser connector: sources + the scope to query them with "
        "(id not pinned — live registries own browser sources under their own row).",
    ),
    IntrospectionCase(
        "I-C7", "describe_connector", {"connector": "chatgpt"},
        eval_found_connector("topos.chatgpt", "chatgpt_file_ingestion", "owner_upload"),
        "present",
        "ChatGPT connector (sources-only facet): deliveries described.",
    ),
    IntrospectionCase(
        "I-C9", "describe_connector", {"connector": "github_activity"},
        eval_found_connector("topos.github"),
        "present",
        "source_id used as the connector name resolves to topos.github.",
    ),
    IntrospectionCase(
        "I-C10", "list_connectors", {}, eval_fleet_card, "present",
        "Fleet listing: owner connectors only + catalog pointer.",
    ),
    IntrospectionCase(
        "I-C11", "query_scope", _qs("activity:read", "What other connectors could I add?"),
        eval_catalog_pointer_not_enumeration, "present",
        "Discovery: link-out only, never a catalog enumeration in chat.",
    ),
    IntrospectionCase(
        "I-R1", "query_scope", _qs("github_activity", "what did I do last week"),
        eval_scope_redirect, "present",
        "The literal '/github_activity' slash-scope redirects to the connector "
        "card with a scope_hint instead of a bare validation error.",
    ),
    IntrospectionCase(
        "I-R2", "query_scope",
        _qs("github_activity", "What can you tell me about this connector?"),
        eval_slash_scope_connector, "present",
        "REGRESSION: '/github_activity What can you tell me about this "
        "connector?' — generic 'this connector' text + a connector-naming "
        "scope_id must resolve the connector card, not fall to not-found.",
    ),
    # --- absent: honest abstention ------------------------------------------
    IntrospectionCase(
        "I-A1", "query_scope", _qs("activity:read", "What can I do with the Spotify connector?"),
        eval_absent_connector, "absent",
        "Uninstalled connector: honest not-found + catalog pointer.",
    ),
    IntrospectionCase(
        "I-A2", "describe_connector", {"connector": "Velmora Fitness"},
        eval_absent_connector, "absent",
        "Fabricated connector name: abstain.",
    ),
    IntrospectionCase(
        "I-A3", "query_scope", _qs("activity:read", "When did my Slack connector last sync?"),
        eval_absent_no_sync_time, "absent",
        "Absent connector: no fabricated sync timestamp.",
    ),
    # --- guard: must NOT intercept (precision pins for §4.4) -----------------
    IntrospectionCase(
        "I-G1", "query_scope",
        _qs("messages:read", "find the message where Sam asked me to connect to the github repo"),
        eval_not_intercepted, "guard",
        "Data query with connector-ish words stays on the data path.",
    ),
    IntrospectionCase(
        "I-G2", "query_scope", _qs("activity:read", "what did I browse about github actions yesterday"),
        eval_not_intercepted, "guard",
        "Browsing-content question stays on the data path.",
    ),
    IntrospectionCase(
        "I-G3", "query_scope",
        _qs("ai_conversations:read", "show my AI conversations about connector design"),
        eval_not_intercepted, "guard",
        "Meta-words as CONTENT stay on the data path.",
    ),
    # --- boundary: secret hygiene (owner-lane subset) ------------------------
    IntrospectionCase(
        "I-B3", "describe_connector", {"connector": "github"},
        eval_secret_hygiene, "boundary",
        "Cards never carry token material — key_hint mask only.",
    ),
]

LANES = ("present", "absent", "guard", "boundary")


def cases_by_lane() -> Dict[str, List[IntrospectionCase]]:
    return {lane: [c for c in INTROSPECTION_CASES if c.lane == lane] for lane in LANES}
