"""Control-plane message handling.

Handlers are registered per message type in domain modules; dispatch is a
registry lookup. Names are re-exported here for backwards compatibility
(tests monkeypatch e.g. topos.core.handlers.get_db_connection).
"""
from __future__ import annotations

from .common import (  # noqa: F401
    Any,
    Dict,
    HTTPException,
    List,
    MESSENGER_COMMUNITIES_TABLE,
    MESSENGER_PARTICIPANT_IMPORTANCE_TABLE,
    MESSENGER_SOCIAL_EDGES_TABLE,
    Optional,
    REGISTRY,
    RUNTIME_PROFILE_OPERATIONS,
    RawFileStore,
    ScopedTokenValidationError,
    UMAFilterError,
    _TABLE_ROW_CANONICAL_TIME_ORDER,
    _TABLE_ROW_TIME_ORDER_COLUMNS,
    _is_sqlite_conn,
    _normalize_contact_key,
    _resource_owner_for_mcp_log,
    _table_exists,
    _table_row_order_clause,
    _uma_transform_progress_hook,
    apply_filter_manifest,
    apply_filter_manifest_async,
    apply_message_contact_pipeline,
    asyncio,
    avg_message_length,
    base64,
    build_sql_constraints,
    compute_and_persist_messenger_analytics,
    connect_postgres,
    datetime,
    engine_state,
    enrich_contact_rows_with_resolved_display_names,
    enrich_conversation_thread_previews,
    ensure_messenger_analytics_tables,
    extract_field_transforms,
    extract_filter_manifest,
    get_db_connection,
    get_engine_config_value,
    get_limit_cap,
    get_mcp_request_counts,
    get_or_create_user_id,
    get_services,
    get_signal_identity,
    get_source_settings,
    get_uma_request_counts,
    get_user_id,
    hashlib,
    ingest_file_payload,
    ingest_ui_payload,
    json,
    layer_for_category,
    layer_kind_labels,
    load_raw_messages,
    logging,
    messages_by_sender,
    messages_per_day,
    os,
    put_signal_identity,
    put_source_settings,
    record_mcp_request,
    record_uma_request,
    resolve_file_format,
    resolve_participant_labels,
    resolve_runtime_profile,
    routine_uma_attribution,
    set_engine_config_value,
    settings,
    store_user_id,
    strip_contact_runtime_filters,
    time_module,
    timezone,
    total_messages,
    update_sync_result,
    uuid,
    validate_scoped_invocation_token,
)
from .config import (  # noqa: F401
    ALLOWED_PINNED_WIDGETS,
    MAX_PINNED_WIDGETS,
    UI_CONFIG_KEY,
    _default_ui_config,
    _normalize_ui_config,
)
from .device import (  # noqa: F401
    COMPUTE_ENVELOPE_SCHEMA_VERSION,
    _compute_envelope,
    _operation_to_msg_type,
)
from .messages import (  # noqa: F401
    _query_avg_message_length_db,
    _query_combined_avg_message_length,
    _query_combined_messages_by_sender,
    _query_combined_messages_per_day,
    _query_combined_total_messages,
    _query_messages_by_sender_db,
    _query_messages_per_day_db,
    _query_total_messages_db,
)
from .ingest import (  # noqa: F401
    _download_ingestion_payload,
    _owner_user_id_from_dataset_id,
)
from .database_explorer import (  # noqa: F401
    POOLED_DECLARED_ENDPOINT_POLICY,
    _POOLED_SCOPE_COLUMNS,
    _device_id_for_topos_key,
    _ensure_pooled_scope_journal_table,
    _pooled_endpoint_policy_for_message,
    _pooled_read_enforcement_enabled,
    _pooled_scope_backfill_apply,
    _pooled_scope_backfill_dry_run,
    _pooled_scope_backfill_rollback,
    _pooled_scope_columns_for_table,
    _pooled_scope_missing_predicate,
    _pooled_scope_tables,
    _pooled_table_scope_for_columns,
    _primary_dataset_id_for_engine_context,
    _resolve_table_row_order_clause,
    _safe_sql_identifier,
    _sqlite_query_plan,
)
from .messenger_analytics import (  # noqa: F401
    _build_messenger_contact_graph,
    _messenger_source_scope,
    _normalize_messenger_source_filter,
)
from .contacts_import import (  # noqa: F401
    _GOOGLE_CONTACT_IMPORT_SESSIONS,
    _resolve_contact_import_targets,
)
from .common import logger
from .registry import HANDLERS, OWNER_ONLY_MESSAGE_TYPES, handles  # noqa: F401
from . import (  # noqa: F401  (imported for handler registration side effects)
    aggregate,
    config,
    derivation,
    sources,
    filter_lab,
    enrichment_lab,
    home_chat,
    routines,
    device,
    messages,
    ingest,
    signal_features,
    permissions_v2,
    permissions_v2_bind,
    permissions_v2_release,
    permissions_v2_ingestion,
    permissions_v2_identity,
    permissions_v2_owner,
    query,
    database_explorer,
    enrichment,
    tool_index,
    messenger_analytics,
    mcp_clients,
    mcp_client_elevations,
    contacts_import,
    complexity,
    timeline_daily,
    graph_query,
    graph_summary,
)


# --- Legacy inspection floor (22 Sep 2026 decision) ------------------------------
# These tools return unprojected substrate: rows (and the JSONL files behind them)
# or the names, shapes and counts of the owner's tables. Neither a disclosure
# tier nor a scope manifest is applied on their way out, so the only thing that
# bounds them is who may call them. Decided by the channel-verified class, never
# by a payload field (topos/principal.py):
#
# * OWNER_APP (the socket) is served.
# * THIRD_PARTY is refused unconditionally, on every channel and for every tool —
#   an enrolled tpk_ client on the local door, the shared key over TCP, and a
#   relay message the control plane stamped ``third_party``.
# * The control-plane relay deferral (CP_RELAY: the owner's hosted web app and
#   the sharing card's row counts), the routine lane (``owner_automation``) and
#   legacy no-principal mode are served — the control plane classified those
#   callers at its own door — but the owner's off-limits floor still applies to
#   them: once anything is black-holed, only the socket reads these tools,
#   because an owner MCP policy must not override the global floor.
#
# Before this the dispatcher had no gate on any of them (released 1.3.57), and
# the beta lineage's gate fired only once the owner had black-holed something —
# on for the careful owner, off for the new one.
LEGACY_INSPECTION_ROW_TYPES = frozenset(
    {"get_table_rows", "get_messages", "get_oplog", "get_analytics", "read_jsonl_file", "list_jsonl_files"}
)
LEGACY_INSPECTION_METADATA_TYPES = frozenset(
    {"list_database_tables", "get_table_schema", "get_table_count", "graph_summary"}
)
LEGACY_INSPECTION_TYPES = LEGACY_INSPECTION_ROW_TYPES | LEGACY_INSPECTION_METADATA_TYPES
#: Classes the control plane vouches for on the relay. Anything else that is not
#: the owner's own socket is a third party, whatever it calls itself.
_OWNER_SIDE_RELAY_CLASSES = frozenset({"cp_relay", "owner_automation"})


def _legacy_inspection_refusal(message: Dict[str, Any], msg_type: str) -> Optional[Dict[str, Any]]:
    """The uniform 403 for a caller the floor refuses, or None to dispatch."""
    if msg_type not in LEGACY_INSPECTION_TYPES:
        return None
    from ...principal import OWNER_APP, current_principal

    cls = getattr(current_principal(), "cls", None)
    if cls == OWNER_APP:
        return None
    refusal = {"id": message.get("id"), "status": "error", "code": 403, "error": "owner_mode_required"}
    if cls is not None and cls not in _OWNER_SIDE_RELAY_CLASSES:
        return refusal
    from ...features.lifecycle.blackhole_guard import BlackholeGuard

    conn = get_db_connection()
    if conn is None or BlackholeGuard(conn).active:
        return refusal
    return None


# --- A relay caller who is not this node's owner reaches only the share doors (review S4, H1) ---------------------
# The control plane stamps ``third_party`` on a frame it forwards for a caller who is not the owner's own
# application, and names that caller in the stamp. Until 1.5.0 the node had no rule of its own for such a frame
# outside the share doors: whether a recipient could reach ``query`` or a home chat session was decided by which
# frames the control plane happened to forward. Now a verified ``third_party`` stamp whose acting user is not this
# node's owner reaches only the types below, and every other type, handled or not, gets the refusal the dispatcher
# already gives a caller for a type it may not use.
#
# The list is what the control plane sends under such a stamp, each entry with the path that sends it. Its other
# ``third_party`` stamp, the MCP gateway's (``mcp_gateway.py`` ``_forward``), names the user the engine key is
# registered to, the owner, and is not this case. A capture app's write is stamped ``owner_app`` for the owner
# (``owner_write_stamp.stamp_app_ingest``), and another person's inbox write carries no stamp at all.
#
# All four are answered only at the socket's own gate (``control_plane_client._handle_message``), before this
# dispatcher; their handlers here refuse. They are listed so that the list is the whole of what a non-owner may
# send, and a share door's frame that does reach the dispatcher still gets its handler's own refusal.
NON_OWNER_RELAY_TYPES = frozenset({
    # A recipient's search of one share: permissions_v2/journey_conjunction.py (the app and the outside client),
    # routes/permissions_beta_source_read.py, and the batch route's one-by-one fallback (_relay_compat).
    "permissions_v2_message_search",
    # Several searches of one share in one frame: routes/permissions_beta_message_search_batch.py (_relay_native).
    "permissions_v2_message_search_batch",
    # A recipient's question to a share: journey_conjunction.py and routes/permissions_beta_source_read.py.
    "permissions_v2_answer_submit",
    # The answer to that question, fetched: the same two paths.
    "permissions_v2_answer_fetch",
})


# --- On a node that has sharing on, a frame that names another user is refused (review R1 node, R-H1 and R-M1) ------
# The rule above reads only a frame the control plane stamped ``third_party``. That left an ``owner_app`` stamp naming
# another user reaching every handled type, and an ``owner_automation`` stamp naming another user or a frame with no
# stamp at all reaching everything the relay deferral reaches. The owner's decision (7 Oct 2026): narrow it now, close
# it in 1.5.1, when the control plane stamps every frame it forwards. Both rules apply on a BOUND node only: there the
# owner's id was checked against the control plane's at the bind (``self_bind``), while an unbound node's own id can
# differ from the account's (``core/handlers/device.py`` keeps it), and comparing would turn the owner's own app away.
# Both only ever refuse.
#
# (a) A verified stamp of ANY class whose acting user is not the owner reaches only ``NON_OWNER_RELAY_TYPES``. A stamp
#     that names nobody is left as it is: the control plane stamps the routine lane's model call and query
#     ``owner_automation`` with no acting user (``routines_engine_bridge.py``).
# (b) A frame with NO stamp that names another user in one of the identity fields the control plane forwards is
#     refused the same way. The fields, each as (where in the frame, key):
RELAY_IDENTITY_FIELDS = (
    ("caller", "requester_id"),            # mcp_gateway.py ``_caller_block``: who asked, on every gateway forward
    ("payload", "requester_id"),           # mcp_query.py ``prepare_engine_query_payload``: the same, for ``query``
    ("payload", "mcp_requester_id"),       # mcp_gateway.py ``_forward`` and routine_access_attribution.py
    ("payload", "user_id"),                # the signed-in caller on the owner's own routes (home chat, routines,
                                           # settings, sources), or the owner a write is for
    ("payload", "requesting_user_id"),     # who writes into an inbox (``app_ingest``)
)
#     A frame names another user when one of them holds a non-empty string that is not the owner's id. The control
#     plane sends two unstamped frames that name someone other than the owner on purpose (read at ``f30e4d1f``; every
#     other unstamped site names the signed-in caller its route requires to own the key, or the owner). Each is listed
#     with the fields that are not compared for it; None means the whole frame is not compared.
UNSTAMPED_NAMING_EXCEPTIONS: Dict[str, Optional[frozenset]] = {
    # Another person's write into the owner's inbox: ``requesting_user_id`` names the WRITER, and the control plane
    # stamps the frame only when the writer is the owner (routes/ingestion.py ``control_plane_app_ingest``,
    # usage_inbox_flush.py, owner_write_stamp.py ``stamp_app_ingest``). ``user_id``, the owner the write is for, is
    # still compared.
    "app_ingest": frozenset({("payload", "requesting_user_id")}),
    # The handshake (routes/engine_ws.py): it names the user the engine key is registered to. Its own handler
    # compares that with the node's id and keeps the node's (``device.handle_connection_info``).
    "connection_info": None,
}


def _owner_mode_refusal(message: Dict[str, Any]) -> Dict[str, Any]:
    """The dispatcher's one refusal for a type the caller may not use: the frame's id and nothing of the node."""
    return {"id": message.get("id"), "status": "error", "code": 403, "error": "owner_mode_required"}


def _bound_owner_id() -> Optional[str]:
    """The owner the node's committed sharing config names, read from disk; None when it cannot be read.

    The file ``switches.is_bound`` just found to bind this node, where the heartbeat's key-id hint reads it
    (``self_bind.node_key_id_hint``): no lock, no runtime load, nothing written."""
    from pathlib import Path

    from ...permissions_v2 import switches
    from ...permissions_v2.runtime import NodeProtocolConfig, _private_file

    try:
        named = switches.explicit(switches.CONFIG_PATH)
        path = Path(named) if named is not None else switches.default_config_path()
        if path is None or not path.is_absolute():
            return None
        return NodeProtocolConfig.parse(_private_file(path.resolve(strict=True))).identity.owner_id
    except Exception:  # noqa: BLE001 -- unreadable is unknown, and unknown is never the owner
        return None


def _relay_owner_id() -> Optional[str]:
    """This node's owner: the bound identity's owner id, else ``engine_config.user_id``; None when the node cannot say.

    A bound node whose identity cannot be read answers None and never falls back to the engine config. Read-only:
    the engine config table is not created here when it is missing."""
    from ...permissions_v2 import switches

    try:
        if switches.is_bound():
            return _bound_owner_id() or None
        conn = get_db_connection()
        if conn is None:
            return None
        row = conn.execute("SELECT value FROM engine_config WHERE key = 'user_id'").fetchone()
    except Exception:  # noqa: BLE001 -- no database, no table, no row: the node cannot say
        return None
    value = row[0] if row else None
    return value if isinstance(value, str) and value.strip() else None


def _non_owner_relay_refusal(message: Dict[str, Any], principal: "Optional[object]") -> Optional[Dict[str, Any]]:
    """The refusal for a relay frame of a caller who is not this node's owner, or None to dispatch (H1; R-M1).

    Decided by the verified stamp alone. A ``third_party`` stamp whose acting user is not the owner, on every node:
    the owner is looked up for every such frame, whatever its type, so the work done does not depend on the type,
    and a node that cannot say who its owner is treats every third party as a non-owner. A stamp of an owner-side
    class (``owner_app``, ``owner_automation``) that names a user who is not the owner, on a bound node (rule (a)
    above); one that names nobody is the control plane's word that the frame is the owner's own and is not read."""
    from ...permissions_v2 import switches
    from ...principal import CP_RELAY, THIRD_PARTY

    cls = getattr(principal, "cls", None)
    if getattr(principal, "channel", None) != "cp_relay" or cls == CP_RELAY:   # not the relay's, or no stamp at all
        return None
    acting = getattr(principal, "acting_user", "") or ""
    if cls == THIRD_PARTY:
        owner = _relay_owner_id()
        if owner is not None and acting == owner:
            return None
    else:
        if not acting or not switches.is_bound():
            return None
        if acting == _bound_owner_id():           # None when the identity cannot be read: never the owner
            return None
    msg_type = str(message.get("type") or "").strip().lower()
    if msg_type in NON_OWNER_RELAY_TYPES:
        return None
    return _owner_mode_refusal(message)


def _unstamped_naming_refusal(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The refusal for a frame with no stamp that names another user, on a bound node, or None to dispatch (rule (b)
    above). Reads only what the frame says of itself, which nothing verifies, and so can only refuse: a frame that
    names the owner or nobody is the relay deferral, as before. On a bound node whose identity cannot be read, a
    frame that names any user is refused."""
    from ...permissions_v2 import switches

    msg_type = str(message.get("type") or "").strip().lower()
    not_compared = UNSTAMPED_NAMING_EXCEPTIONS.get(msg_type, frozenset())
    if not_compared is None or msg_type in NON_OWNER_RELAY_TYPES:
        return None
    named = []
    for where, key in RELAY_IDENTITY_FIELDS:
        if (where, key) in not_compared:
            continue
        holder = message.get(where)
        value = holder.get(key) if isinstance(holder, dict) else None
        if isinstance(value, str) and value.strip():
            named.append(value)
    if not named or not switches.is_bound():
        return None
    owner = _bound_owner_id()
    if owner is not None and all(value == owner for value in named):
        return None
    return _owner_mode_refusal(message)


async def handle_control_plane_request(
    message: Dict[str, Any],
    principal: "Optional[object]" = None,
) -> Optional[Dict[str, Any]]:
    """Dispatch one control-plane message.

    `principal` is the CHANNEL-verified client class (topos/principal.py),
    supplied by the entry point that authenticated the caller — the HTTP routes
    pass what resolve_request_principal returned; the relay wrapper in app.py
    passes RELAY_PRINCIPAL. It is scoped onto a contextvar for the duration of
    the handler so the query pipeline's disclosure floors can read it without
    threading a parameter through every handler signature. It is never read
    from the message: a payload field claiming a principal is audit data at
    best and a spoof at worst.
    """
    from ...principal import reset_principal, set_principal

    msg_type = str(message.get("type") or "").strip().lower()
    handler = HANDLERS.get(msg_type)
    if handler is None:
        logger.warning("unhandled control plane message type: %r", msg_type)
        return {
            "id": message.get("id"),
            "status": "error",
            "error": f"unhandled message type: {msg_type}",
        }
    # User-facing / home-chat paths: log receipt so WS-forwarded work is visible in engine logs
    # (uvicorn.access only covers local HTTP, not control-plane WebSocket RPCs).
    if msg_type in {
        "llm_generation",
        "query",
        "query_live",
        "tools_retrieve",
        "upsert_home_chat_session",
        "delete_home_chat_session",
    }:
        logger.info("control plane request received type=%s id=%s", msg_type, message.get("id"))
    # None inherits the ambient principal (nested dispatch inside a handler must
    # not erase the channel's stamp); entry points always pass theirs explicitly.
    from ...principal import current_principal

    token = set_principal(principal if principal is not None else current_principal())
    try:
        # Signal APIs are the owner's inspection/curation surface, not a grant
        # transport. The HTTP dispatcher must not let a shared key sidestep the
        # owner-only proxy and invoke their unfiltered readers or mutations.
        if msg_type.startswith("signal_"):
            from ...principal import OWNER_APP

            if getattr(current_principal(), "cls", None) != OWNER_APP:
                return {"id": message.get("id"), "status": "error", "code": 403, "error": "owner_mode_required"}
        from .registry import OWNER_ONLY_MESSAGE_TYPES
        from ...principal import OWNER_APP

        if msg_type in OWNER_ONLY_MESSAGE_TYPES and getattr(current_principal(), "cls", None) != OWNER_APP:
            return {"id": message.get("id"), "status": "error", "code": 403, "error": "owner_mode_required"}
        # The legacy inspection floor (fix/legacy-inspection-gate 33afaefd) replaces
        # the lineage's black-hole-conditional block: row tools refuse every
        # non-owner principal unconditionally, metadata tools refuse third parties.
        refusal = _legacy_inspection_refusal(message, msg_type)
        if refusal is not None:
            return refusal
        return await handler(message)
    finally:
        reset_principal(token)


#: When the node last said in its log that a stamp did not verify, for each cause (``_note_unverified_stamp``).
_unverified_stamp_noted: Dict[str, float] = {}

#: The bind's ``cause`` (contract A2A-1, amendment 8) for a bind frame whose stamp did not verify, by the reason
#: ``relay_stamp.check_relay_stamp`` gives. A node code, never data.
STAMP_CAUSES = {"clock": "stamp_clock", "key": "stamp_key", "no_key": "stamp_key_unavailable"}
STAMP_CAUSE_OTHER = "stamp_invalid"
_BIND_TYPE = "permissions_v2_bind"


def _note_unverified_stamp(reason: str = "", offset: Optional[float] = None) -> None:
    """One log line a minute for each cause, naming no frame: after a control-plane key change, or on a computer
    whose clock is off, every stamped frame lands here. The line says which it is (review R1 node, R-M2)."""
    now = time_module.monotonic()
    noted = _unverified_stamp_noted.get(reason)
    if noted is not None and now - noted < 60.0:
        return
    _unverified_stamp_noted[reason] = now
    from ... import relay_stamp

    if reason == relay_stamp.NO_KEY:
        logger.warning("relay stamp not verified: this node has pinned no control-plane stamp key; "
                       "stamped frames are refused until it has one (it keeps trying to pin one)")
    elif reason == relay_stamp.WRONG_CLOCK:
        seconds = abs(int(offset or 0))
        logger.warning("relay stamp not verified: a clock is off. The stamp is signed by the pinned control-plane "
                       "stamp key, but by this node's clock it was issued %d s %s, and only %d s either side is "
                       "accepted: this node's clock is about %d s %s the control plane's. Stamped frames are "
                       "refused until this computer's clock is right",
                       seconds, "ago" if (offset or 0) >= 0 else "from now", relay_stamp.SKEW_S,
                       seconds, "ahead of" if (offset or 0) >= 0 else "behind")
    elif reason == relay_stamp.WRONG_KEY:
        logger.warning("relay stamp not verified: its signature does not verify under the pinned control-plane "
                       "stamp key (the control plane's key changed, this home pinned another control plane's key, "
                       "or the stamp was altered). Stamped frames are refused until the pinned key is the control "
                       "plane's: to pin it again, remove the pinned key file and restart")
    else:
        logger.warning("relay stamp not verified: it is not a stamp this node can read (malformed, a life over "
                       "the limit, or a class this node does not know); such frames are refused")


async def dispatch_relay_message(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The control-plane relay's entry point: stamp verification, the non-owner rule, then dispatch.

    A message carrying a VERIFIED Ed25519 stamp resolves to the CP's
    classification (topos/relay_stamp.py). A message with NO stamp keeps the
    CP_RELAY deferral, as before. A message whose stamp is there but does not
    verify is neither (review S4, M1): until 1.5.0 it was read as "no stamp",
    which is a more privileged class than a verified third party, so a frame the
    control plane had classified ``third_party`` gained the relay deferral by
    arriving late, under a rotated key or with a class this node does not know.
    It is now the least class there is, a third party that names nobody, which
    the non-owner rule refuses for every type but the share doors (and those
    refuse it themselves: they ask for a verified stamp). A stamp field of any
    value, null included, counts as a stamp. The log says why it did not verify
    (a clock, a key), and a bind frame's refusal says so in the bind's ``cause``
    (review R1 node, R-M2); no other type's refusal carries anything more.

    Module-level so the relay wiring in app.py and the tests exercise the same
    function.
    """
    from ...principal import RELAY_PRINCIPAL, THIRD_PARTY, Principal
    from ...relay_stamp import NO_STAMP, check_relay_stamp

    principal, reason, offset = check_relay_stamp(message)
    cause = None
    if principal is None:
        if reason != NO_STAMP:
            _note_unverified_stamp(reason, offset)
            principal = Principal(cls=THIRD_PARTY, channel="cp_relay")
            # Only the bind's answer has a ``cause`` (contract A2A-1, amendment 8); it says clock or key (R-M2).
            if str(message.get("type") or "").strip().lower() == _BIND_TYPE:
                cause = STAMP_CAUSES.get(reason, STAMP_CAUSE_OTHER)
        else:
            principal = RELAY_PRINCIPAL
            # No stamp at all: the deferral, unless on a bound node the frame names another user (R-H1, rule (b)).
            refusal = _unstamped_naming_refusal(message)
            if refusal is not None:
                return refusal
    refusal = _non_owner_relay_refusal(message, principal)
    if refusal is not None:
        return {**refusal, "cause": cause} if cause else refusal
    return await handle_control_plane_request(message, principal=principal)
