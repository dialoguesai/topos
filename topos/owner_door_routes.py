"""The owner socket's HTTP census for the dangerous-door split (AI-tool on-ramp, spike P0.11b; lane L2).

The dispatcher's split (``core/handlers`` ``dangerous_door_refusal``) sees only what reaches the dispatcher. Most of
the node's HTTP routes on the owner socket call their handlers or services directly, so a tricked coding agent
with a shell could ``curl --unix-socket ~/.topos/engine.sock -X DELETE /v1/source-install`` past it. This module is
the census of every route that answers a mutating method (POST, PUT, PATCH, DELETE), each with a verdict:

* ``dangerous`` — the socket server's ASGI wrapper (``uds.UDSChannelApp``) refuses it 403
  ``{"error": "owner_confirmation_required", "message": "This change needs the Topos app."}`` before the route runs,
  unless the peer may change things (``uds.peer_may_change``: an attested app, the dev proxy on a node that allows
  it, or a live owner-confirmation token) or the entry's ``body_rule`` says this particular body is not a change
  (the settings keys that are not a schedule, a transcript source's install, the entailment review's ``list``);
* ``open`` — a mutating method that is a read or a benign write (a query, a model call, UI preferences, a lab
  bookkeeping row, the owner's own contacts into his own node), served as before;
* ``dispatcher`` — the route forwards to ``handle_control_plane_request``, which decides per message type;
* ``carve_out`` — named by the contract as passing (the transcript-source doors, the agent's dispatch route);
* ``unknown`` — a mutating route the census does not name: REFUSED on the socket until it is listed, and
  ``tests/topos/test_owner_door_routes.py`` fails on it. Fail closed both ways.

Every GET, HEAD and OPTIONS is untouched. Reads stay open. The wrapper applies this only to a request on the real
socket, behind ``TOPOS_OWNER_DOOR_SPLIT`` (on by default), with the same legs as the dispatcher's split.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Callable, Optional, Tuple

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

DANGEROUS = "dangerous"
OPEN = "open"
DISPATCHER = "dispatcher"
CARVE_OUT = "carve_out"
UNKNOWN = "unknown"
READ = "read"

#: The bundled transcript sources (CONTRACTS §2.1): enabling, patching or syncing one is not a dangerous change.
TRANSCRIPT_SOURCE_IDS = frozenset({"claude_code_sessions", "codex_sessions", "cursor_sessions"})
_TRANSCRIPTS = "{" + "|".join(sorted(TRANSCRIPT_SOURCE_IDS)) + "}"
#: ``PUT /sources/{id}/settings`` passes when the body holds only these keys; ``sync_schedule`` and
#: ``proof_standing`` (the owner's standing attestation) stay dangerous.
SOURCE_SETTINGS_OPEN_KEYS = frozenset({"enabled", "posture", "exclude_spam", "dataset_id"})
#: A body a ``body_rule`` will read; a larger one passes no rule (and is replayed to the route only if it passes).
BODY_RULE_MAX_BYTES = 256 * 1024

BodyRule = Callable[[Any], bool]


def _settings_keys_open(body: Any) -> bool:
    return isinstance(body, dict) and set(body) <= SOURCE_SETTINGS_OPEN_KEYS


def transcript_install_body(body: Any) -> bool:
    """An install of a bundled transcript definition, and nothing else, passes ``POST /v1/source-install`` and the
    dispatcher's ``post_source_install`` (CONTRACTS §1.6).

    The id the install USES is ``source_definition_json.source_id`` (``api/source_install._install_source_core``
    never reads the top-level ``source_id``), so that is the field read here. The definition must be the bundled
    one byte for byte: its scope lists are the authorization list of every grant the owner already made, so an
    edited copy is a change to what the owner shares. A version row is never the bundled definition, and a
    top-level id that names another source is a lie about the body."""
    if not isinstance(body, dict) or body.get("source_version_row_json") is not None:
        return False
    definition = body.get("source_definition_json")
    if not isinstance(definition, dict):
        return False
    sid = str(definition.get("source_id") or "").strip()
    if sid not in TRANSCRIPT_SOURCE_IDS:
        return False
    top = str(body.get("source_id") or "").strip()
    if top and top != sid:
        return False
    from .sources.registry import BUNDLED_REGISTRY

    bundled = BUNDLED_REGISTRY.get(sid)
    if bundled is None:
        return False
    # Through JSON on both sides: a caller's copy has lists where the definition has tuples.
    return definition == json.loads(json.dumps(bundled.to_dict()))


def _entailment_list(body: Any) -> bool:
    return isinstance(body, dict) and body.get("operation") == "list"


@lru_cache(maxsize=512)
def _compile(template: str) -> "re.Pattern[str]":
    """``{name}`` is one path segment, ``{a|b}`` one of the names, a trailing ``/*`` any suffix (or none)."""
    suffix = ""
    if template.endswith("/*"):
        template, suffix = template[:-2], "(?:/.*)?"
    parts = []
    for piece in re.split(r"(\{[^}]*\})", template):
        if piece.startswith("{") and piece.endswith("}"):
            inner = piece[1:-1]
            parts.append("(?:" + "|".join(re.escape(x) for x in inner.split("|")) + ")" if "|" in inner else "[^/]+")
        else:
            parts.append(re.escape(piece))
    return re.compile("^" + "".join(parts) + suffix + "$")


@dataclass(frozen=True)
class RouteRule:
    method: str            # one mutating method, or "*"
    template: str          # the FastAPI path template as mounted (include prefix + router prefix + decorator path)
    verdict: str
    reason: str
    body_rule: Optional[BodyRule] = None

    def matches(self, method: str, path: str) -> bool:
        return (self.method == "*" or self.method == method) and _compile(self.template).fullmatch(path) is not None


def _d(method: str, template: str, reason: str, body_rule: Optional[BodyRule] = None) -> RouteRule:
    return RouteRule(method, template, DANGEROUS, reason, body_rule)


def _o(method: str, template: str, reason: str) -> RouteRule:
    return RouteRule(method, template, OPEN, reason)


def _x(method: str, template: str, reason: str) -> RouteRule:
    return RouteRule(method, template, DISPATCHER, reason)


#: Checked first: the contract says these pass, whatever else matches.
CARVE_OUTS: Tuple[RouteRule, ...] = (
    RouteRule("*", f"/api/local/owner/sources/{_TRANSCRIPTS}/*", CARVE_OUT,
              "lane L5's doors for the owner's own transcripts (CONTRACTS §2.8): enabling and syncing a bundled "
              "transcript source is the owner's own files read into his own node (§1.6)"),
    RouteRule("POST", f"/sources/{_TRANSCRIPTS}/sync", CARVE_OUT, "the hooks' and the CLI's sync of the owner's own transcripts"),
    RouteRule("POST", "/api/local/agent/dispatch", CARVE_OUT,
              "gated inside: the dispatcher's owner_agent allowlist and its door split decide per message type"),
)

#: Every mounted route that answers a mutating method, as of 15 Oct 2026 (145 entries; see the test for the two
#: completeness checks). Grouped by API module. "stub" marks a route that answers ``{"status": "stub"}`` today and
#: is gated now so that it is gated the day it does something.
CENSUS: Tuple[RouteRule, ...] = (
    # local_mcp.py — through the dispatcher (reads via POST; truth_seed_fact is gated there)
    _x("POST", "/api/local/get_table_schema", "dispatcher: get_table_schema (the inspection floor decides)"),
    _x("POST", "/api/local/list_database_tables", "dispatcher: list_database_tables"),
    _x("POST", "/api/local/truth_prompts", "dispatcher: truth_prompts (a read)"),
    _x("POST", "/api/local/truth_seed_fact", "dispatcher: truth_seed_fact is in DANGEROUS_TYPES"),
    _x("POST", "/api/local/verify_claim", "dispatcher: verify_claim (a read)"),
    # connected_apps.py — through the dispatcher (mcp_client_* types; enroll/revoke/decide/revoke_elevation are dangerous there)
    _x("POST", "/v1/signal/connected-apps/elevations/decide", "dispatcher: mcp_client_decide_elevation"),
    _x("POST", "/v1/signal/connected-apps/elevations/request", "dispatcher: mcp_client_request_elevation (a third party's request)"),
    _x("POST", "/v1/signal/connected-apps/elevations/revoke", "dispatcher: mcp_client_revoke_elevation"),
    _x("POST", "/v1/signal/connected-apps/enroll", "dispatcher: mcp_client_enroll"),
    _x("POST", "/v1/signal/connected-apps/revoke", "dispatcher: mcp_client_revoke"),
    # app_registry.py
    _d("POST", "/apps", "registers an app that writes into the node (stub)"),
    _d("POST", "/apps/{app_id}/sources", "registers an app source (stub)"),
    # backup.py
    _d("POST", "/backup", "writes a copy of the owner's database to disk (stub)"),
    _d("POST", "/restore", "replaces the owner's database (stub)"),
    # ingestion_sources.py
    _o("POST", "/contacts/import/apple", "the owner's own contacts into his own node"),
    _o("POST", "/contacts/import/google/finish", "the owner's own contacts into his own node"),
    _o("POST", "/contacts/import/google/start", "the owner's own contacts into his own node"),
    _o("POST", "/contacts/import/google/token", "the owner's own contacts into his own node"),
    _d("PUT", "/sources/signal/settings", "the Signal identity and settings"),
    _d("POST", "/sources/signal/upload", "signal_upload: a Signal export into the node (owner-only)"),
    _o("POST", "/sources/{source_id}/contacts/auto-resolve", "contact-name curation; no share, grant or delete"),
    _o("PUT", "/sources/{source_id}/contacts/{contact_id}", "contact-name curation; no share, grant or delete"),
    _d("POST", "/sources/{source_id}/ingest", "writes records under the owner's name: a planting door"),
    _d("PUT", "/sources/{source_id}/settings", "sync_schedule and proof_standing are changes; enabled/posture/exclude_spam pass",
       _settings_keys_open),
    _d("POST", "/sources/{source_id}/sync", "source_sync of a non-transcript source (Q3: needs a confirmation)"),
    # ingestion_compat.py
    _d("POST", "/store_message", "writes a message under the owner's name: a planting door"),
    # sync.py
    _d("POST", "/sync", "syncs every source (stub)"),
    # device.py
    _o("POST", "/device_name", "the device's display name"),
    # llm.py
    _o("POST", "/llm/generate", "a model call; no owner state changes"),
    _o("POST", "/llm_generation", "a model call; no owner state changes"),
    # community_naming_config.py / conversation_context.py
    _d("PUT", "/v1/community-naming-config", "node-wide model configuration"),
    _d("PUT", "/v1/conversation-context-llm-config", "node-wide model configuration"),
    _o("PUT", "/v1/conversations/{conversation_id}/context", "the owner's note on one conversation"),
    # compute_remote.py / engine_run.py / data_commit.py
    _d("POST", "/v1/compute/run", "runs a compute envelope that may leave the machine"),
    _o("POST", "/v1/engine/run", "runs one processing task locally and answers; no durable change of its own"),
    _d("POST", "/v1/data/commit-artifact", "writes an artifact into the owner's data: a planting door"),
    # data_explorer_table_prefs.py
    _d("DELETE", "/v1/data-explorer/table-prefs/{table_name}", "delete_* (as the dispatcher's set)"),
    _o("PUT", "/v1/data-explorer/table-prefs/{table_name}", "explorer column preferences"),
    # disk_space_config.py
    _d("PUT", "/v1/disk-space-policy", "node-wide policy: lowering the floor can fill the disk"),
    # enrichment_lab.py / filter_lab.py
    _o("POST", "/v1/enrichment-lab/job-groups", "lab bookkeeping"),
    _d("DELETE", "/v1/enrichment-lab/job-groups/{group_id}", "delete_*"),
    _o("PATCH", "/v1/enrichment-lab/job-groups/{group_id}", "lab bookkeeping"),
    _d("POST", "/v1/enrichment-lab/job-groups/{group_id}/apply-preferred", "applies a lab result node-wide"),
    _o("PATCH", "/v1/enrichment-lab/job-groups/{group_id}/runs/{run_id}", "lab bookkeeping"),
    _d("DELETE", "/v1/filter-lab/data", "delete_filter_lab_all_data"),
    _o("POST", "/v1/filter-lab/job-groups", "lab bookkeeping"),
    _d("DELETE", "/v1/filter-lab/job-groups/{group_id}", "delete_*"),
    _o("PATCH", "/v1/filter-lab/job-groups/{group_id}", "lab bookkeeping"),
    _d("POST", "/v1/filter-lab/job-groups/{group_id}/apply-preferred", "applies a lab result node-wide"),
    _o("PATCH", "/v1/filter-lab/job-groups/{group_id}/runs/{run_id}", "lab bookkeeping"),
    # enrichment.py
    _o("PATCH", "/v1/sources/{source_id}/enrichments/{enrichment_name}", "toggles one enrichment; derived rows only"),
    _o("POST", "/v1/sources/{source_id}/enrichments/{enrichment_name}/backfill", "recomputes derived rows"),
    _d("DELETE", "/v1/sources/{source_id}/enrichments/{enrichment_name}/data", "delete_*"),
    _o("POST", "/v1/sources/{source_id}/enrichments/{enrichment_name}/test", "a dry run"),
    _o("POST", "/v1/enrichment/process", "recomputes derived rows"),
    # facts_llm_config.py / sanitization_ollama_config.py / signal_extraction_config.py
    _d("DELETE", "/v1/facts-llm-config", "delete_*; node-wide model configuration"),
    _d("PUT", "/v1/facts-llm-config", "node-wide model configuration"),
    _d("DELETE", "/v1/sanitization-ollama-config", "delete_sanitization_ollama_config"),
    _d("PUT", "/v1/sanitization-ollama-config", "put_sanitization_ollama_config (P0.11)"),
    _d("DELETE", "/v1/signal-extraction-config", "delete_signal_extraction_config"),
    _d("PUT", "/v1/signal-extraction-config", "put_signal_extraction_config (P0.11)"),
    # home_chat.py
    _o("POST", "/v1/home-chat/memory", "the owner's chat memory note"),
    _d("DELETE", "/v1/home-chat/sessions/{session_id}", "delete_home_chat_session (P0.11)"),
    _o("PUT", "/v1/home-chat/sessions/{session_id}", "upsert of the owner's own chat session"),
    # ingestion_api.py
    _o("POST", "/v1/ingestion/progress", "worker plumbing (stub)"),
    _d("POST", "/v1/ingestion/reprocess", "ingestion_reprocess (owner-only, rewrites canonical rows)"),
    _d("POST", "/v1/ingestion/start", "starts an import (stub)"),
    _d("POST", "/v1/ingestion/upload", "writes an uploaded file's records into the node"),
    _d("POST", "/v1/ingestion/upload-local-path", "reads any local path into the node"),
    # intelligence_lifecycle.py
    _d("DELETE", "/v1/intelligence/exclusions", "an Off-limits-like edit"),
    _d("POST", "/v1/intelligence/exclusions/entity", "an Off-limits-like edit"),
    _d("POST", "/v1/intelligence/exclusions/fact", "an Off-limits-like edit"),
    _d("POST", "/v1/intelligence/exclusions/record", "an Off-limits-like edit"),
    _d("POST", "/v1/intelligence/exclusions/stat", "an Off-limits-like edit"),
    # messenger_analytics.py
    _o("POST", "/v1/messenger-analytics/commitments/refresh", "recomputes analytics"),
    _o("POST", "/v1/messenger-analytics/person-curate", "the owner's curation of a person's importance"),
    _o("POST", "/v1/messenger-analytics/person-rate", "the owner's curation of a person's importance"),
    _o("POST", "/v1/messenger-analytics/person-undo", "the owner's curation of a person's importance"),
    _o("POST", "/v1/messenger-analytics/readings/refresh", "recomputes analytics"),
    _o("POST", "/v1/messenger-analytics/recompute", "recomputes analytics"),
    # privacy_disclose.py / nsfw_maintenance.py
    _o("POST", "/v1/privacy/disclose", "a disclosure check: a read"),
    _o("POST", "/v1/privacy/disclosure-check", "a disclosure check: a read"),
    _o("POST", "/v1/privacy/nsfw-classify", "classification of given text: a read"),
    _o("POST", "/v1/privacy/nsfw-recheck", "re-labels rows with the classifier; owner socket only already"),
    # query_api.py / tool_index.py
    _o("POST", "/v1/query", "a query: a read"),
    _o("POST", "/v1/tools/index", "indexes tool definitions"),
    _o("POST", "/v1/tools/retrieve", "a read"),
    # permissions_*.py (sharing)
    _d("POST", "/v1/sharing/ai-chat/capture-attestation/attest", "sharing: an attestation"),
    _o("POST", "/v1/sharing/ai-chat/capture-attestation/preview", "a preview: a read"),
    _d("POST", "/v1/sharing/ai-chat/capture-attestation/revoke", "sharing: revokes an attestation"),
    _d("POST", "/v1/sharing/capture-attestation/attest", "sharing: an attestation"),
    _o("POST", "/v1/sharing/capture-attestation/preview", "a preview: a read"),
    _d("POST", "/v1/sharing/capture-attestation/revoke", "sharing: revokes an attestation"),
    _d("POST", "/v1/sharing/identity/command", "permissions_v2_identity_command"),
    _d("POST", "/v1/sharing/imessage/preflight", "sharing: the native iMessage probe"),
    _d("POST", "/v1/sharing/imessage/recover", "sharing: the native iMessage recovery enrolment"),
    _d("POST", "/v1/sharing/imessage/refresh", "sharing: re-proves the recovery enrolment"),
    _d("POST", "/v1/sharing/ingestion/command", "permissions_v2_ingest_snapshot"),
    _d("POST", "/v1/sharing/message-search/entailment-review", "permissions_v2_entailment_review: a decision; list is a read",
       _entailment_list),
    _d("POST", "/v1/sharing/message-search/message-review", "permissions_v2_message_review"),
    _d("POST", "/v1/sharing/message-search/permitted-derivation", "permissions_v2_permitted_derivation"),
    _d("POST", "/v1/sharing/message-search/rebuild", "permissions_v2_message_search_rebuild"),
    _d("POST", "/v1/sharing/source-installs/deactivate", "sharing: deactivates a source install"),
    # shell.py / upgrades.py
    _d("POST", "/v1/shell/update", "installs a new node version"),
    _o("POST", "/v1/shell/update/check", "a read"),
    _d("POST", "/v1/upgrade/consent", "consents to an upgrade step (migrations)"),
    # signal.py (the signal_* family) — fit/evaluate is a read
    _d("PUT", "/v1/signal/affinity/config", "signal_affinity_config"),
    _d("POST", "/v1/signal/affinity/pairs/label", "signal_affinity_label"),
    _d("POST", "/v1/signal/affinity/recompute", "signal_affinity_recompute"),
    _d("POST", "/v1/signal/blackholes/notifications/{notification_id}/dismiss", "signal_dismiss_blackhole_notification"),
    _d("POST", "/v1/signal/blackholes/records", "signal_blackhole_record (Off-limits)"),
    _d("DELETE", "/v1/signal/blackholes/records/{record_id}", "signal_unblackhole_record (Off-limits)"),
    _d("PUT", "/v1/signal/briefs/{dimension}", "signal_update_brief"),
    _d("POST", "/v1/signal/briefs/{dimension}/refresh", "signal_refresh_brief"),
    _d("POST", "/v1/signal/derivation-debt/retry", "signal: derivation"),
    _d("POST", "/v1/signal/derivation/offers/resolve", "signal: derivation pack offers"),
    _d("PUT", "/v1/signal/derivation/packs/{pack_id}", "signal: derivation pack configuration"),
    _d("POST", "/v1/signal/derivation/packs/{pack_id}/backfill", "signal: derivation"),
    _d("PUT", "/v1/signal/entities/communities/{community_id}/name", "signal: entity curation"),
    _d("POST", "/v1/signal/entities/graph/rebuild", "signal: rebuilds the entity graph"),
    _d("DELETE", "/v1/signal/entities/{entity_id}/blackhole", "signal_unblackhole_entity (Off-limits)"),
    _d("POST", "/v1/signal/entities/{entity_id}/blackhole", "signal_blackhole_entity (Off-limits)"),
    _d("POST", "/v1/signal/entities/{entity_id}/exclude", "signal_exclude_entity"),
    _d("POST", "/v1/signal/entities/{entity_id}/merge", "signal_entity_merge"),
    _d("POST", "/v1/signal/entities/{entity_id}/split", "signal_entity_split"),
    _d("POST", "/v1/signal/entity-review/sweep", "signal_entity_review_sweep"),
    _d("POST", "/v1/signal/entity-review/{review_id}/approve", "signal_entity_review_action"),
    _d("POST", "/v1/signal/entity-review/{review_id}/dismiss", "signal_entity_review_action"),
    _d("POST", "/v1/signal/facts/conflicts/promote", "signal: fact edits"),
    _d("POST", "/v1/signal/facts/conflicts/resolve", "signal: fact edits"),
    _d("POST", "/v1/signal/facts/verdict", "signal: fact edits"),
    _d("POST", "/v1/signal/facts/{object_id}/revise", "signal: fact edits"),
    _o("POST", "/v1/signal/fit/evaluate", "signal_evaluate_fit: a read"),
    _d("POST", "/v1/signal/objects/{object_id}/owner_override", "signal_owner_override_object"),
    # source_install.py / source_scrub.py / source_retention.py
    _d("DELETE", "/v1/source-install", "delete_source_install (uninstall)"),
    _d("PATCH", "/v1/source-install", "patch_source_install: a change to a definition, the bundled transcript ones "
                                      "included (their scope lists live there)"),
    _d("POST", "/v1/source-install", "post_source_install; the bundled transcript definition itself passes "
                                     "(transcript_install_body reads the definition the route installs)",
       transcript_install_body),
    _d("POST", "/v1/source-scrub", "post_source_scrub"),
    _o("POST", "/v1/source-test-enrichment", "a dry run over existing rows"),
    _o("POST", "/v1/source-test-enrichment-trigger", "a dry run over existing rows"),
    _d("POST", "/v1/source-test-ingestion", "ingests a sample payload"),
    _d("POST", "/v1/sources/retention", "retention floors (CONTRACTS §1.6)"),
    _d("POST", "/v1/sources/retention/compact", "retention: compacts rows"),
    # owner_sources.py — lane L5's socket doors (CONTRACTS §2.8): open at the socket layer; the door itself serves
    # only the three bundled transcript sources (404/409 for any other id), so there is nothing dangerous to gate here
    _o("POST", "/api/local/owner/sources/{source_id}/preview", "L5 owner door: counts-only preview of a transcript source"),
    _o("POST", "/api/local/owner/sources/{source_id}/enable", "L5 owner door: install row + schedule + backfill"),
    _o("POST", "/api/local/owner/sources/{source_id}/pause", "L5 owner door: pause (refuses the sync door and the scheduler)"),
    _o("POST", "/api/local/owner/sources/{source_id}/resume", "L5 owner door: resume"),
    _o("POST", "/api/local/owner/sources/{source_id}/sync", "L5 owner door: one sync, optionally one session file"),
    # ui_config.py / user_identity.py
    _o("PUT", "/v1/ui-config", "UI preferences"),
    _d("PUT", "/v1/user-identity", "the owner's identity mapping"),
)

#: The HTTP routes an ``owner_agent`` may call on the socket (CONTRACTS §1.2): a positive list, every other route is
#: 403 ``{"error": "owner_agent_not_allowed"}`` at the socket layer, before the route and before any auth of its own.
OWNER_AGENT_ROUTES: Tuple[Tuple[str, str], ...] = (
    ("POST", "/api/local/agent/dispatch"),
    ("GET", "/sources"),
    ("GET", "/sources/{source_id}/settings"),
    ("GET", "/v1/shell/status"),
    ("GET", "/api/local/owner/scope"),
)


def owner_agent_route_allowed(method: str, path: str) -> bool:
    method = str(method or "").upper()
    return any(method == m and _compile(template).fullmatch(path) is not None for m, template in OWNER_AGENT_ROUTES)


READ_RULE = RouteRule("*", "*", READ, "not a mutating method: untouched")
UNKNOWN_RULE = RouteRule("*", "*", UNKNOWN, "a mutating route the census does not name: refused on the socket until it is listed")


def lookup(method: str, path: str) -> RouteRule:
    """The rule for one request: carve-outs first (they are the more specific patterns), then the census."""
    method = str(method or "GET").upper()
    if method not in MUTATING_METHODS:
        return READ_RULE
    for rule in CARVE_OUTS:
        if rule.matches(method, path):
            return rule
    for rule in CENSUS:
        if rule.matches(method, path):
            return rule
    return UNKNOWN_RULE
