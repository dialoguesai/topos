"""UMA read message handlers."""
from __future__ import annotations

import topos.core.handlers as hub

from .common import (
    Any,
    Dict,
    List,
    Optional,
    UMAFilterError,
    _is_sqlite_conn,
    _table_exists,
    _table_row_order_clause,
    _uma_transform_progress_hook,
    apply_filter_manifest_async,
    apply_message_contact_pipeline,
    build_sql_constraints,
    connect_postgres,
    extract_field_transforms,
    extract_filter_manifest,
    get_limit_cap,
    load_raw_messages,
    logger,
    parse_dataset_id_from_uma_dataset_resource_id,
    routine_uma_attribution,
    settings,
    strip_contact_runtime_filters,
)
from .registry import handles


def _uma_attribution_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    tags = routine_uma_attribution(mcp_source=payload.get("mcp_source"))
    acc_ch = tags.get("access_channel") or (payload.get("access_channel") or "http").strip() or "http"
    app_id = tags.get("app_id") or (payload.get("requesting_app_id") or "").strip() or None
    out: Dict[str, Any] = {"access_channel": acc_ch, "app_id": app_id}
    if tags.get("access_context"):
        out["access_context"] = tags["access_context"]
    return out

def _resolve_uma_scope(payload: Dict[str, Any], resource_id: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Resolve tenant scope for UMA reads: dataset_id, owner_user_id, tenant_id."""
    dataset_id = (payload.get("dataset_id") or "").strip() or None
    if not dataset_id and resource_id:
        dataset_id = parse_dataset_id_from_uma_dataset_resource_id(resource_id)
    owner_user_id = (payload.get("owner_user_id") or "").strip() or None
    if not owner_user_id and dataset_id:
        owner_user_id = dataset_id.split(":")[0] if ":" in dataset_id else dataset_id
    if not owner_user_id and resource_id and len(resource_id.split(":")) >= 2:
        owner_user_id = resource_id.split(":")[1]
    tenant_id = (payload.get("tenant_id") or "").strip() or None
    return dataset_id, owner_user_id, tenant_id

def _build_uma_scope_clause(
    col_names: set[str],
    dataset_id: Optional[str],
    owner_user_id: Optional[str],
    tenant_id: Optional[str],
) -> tuple[str, tuple[Any, ...]]:
    """
    Build a mandatory scope predicate for UMA table reads.
    Prefer dataset_id (strongest), then owner_user_id, then tenant_id.
    """
    if dataset_id and "dataset_id" in col_names:
        return ' WHERE "dataset_id" = ?', (dataset_id,)
    if owner_user_id and "owner_user_id" in col_names:
        return ' WHERE "owner_user_id" = ?', (owner_user_id,)
    if tenant_id and "tenant_id" in col_names:
        return ' WHERE "tenant_id" = ?', (tenant_id,)
    return "", ()

@handles("uma_get_messages")
async def handle_uma_get_messages(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    """UMA: return messages for a resource (control plane proxies here for My Access). Stage 2b: apply filter_manifest from payload.filters."""
    payload = message.get("payload") or {}
    resource_id = (payload.get("resource_id") or "").strip()
    dataset_id = (payload.get("dataset_id") or "").strip() or None
    if not dataset_id and resource_id:
        dataset_id = parse_dataset_id_from_uma_dataset_resource_id(resource_id)
    limit = min(int(payload.get("limit") or 100), 1000)
    offset = max(0, int(payload.get("offset") or 0))
    filters = payload.get("filters") or {}
    filters_dict = filters if isinstance(filters, dict) else None
    filter_manifest = extract_filter_manifest(filters_dict)
    field_transforms = extract_field_transforms(filters_dict)
    _raw_ms = (payload.get("message_stream") or "conversation").strip().lower()
    message_stream = _raw_ms if _raw_ms in ("conversation", "ai_chat") else "conversation"
    limit = get_limit_cap(
        limit,
        filter_manifest,
        "conversation_messages" if message_stream == "conversation" else "ai_chat_messages",
    )
    requesting_user_email = (payload.get("requesting_user_email") or "").strip() or None
    requesting_app_id = (payload.get("requesting_app_id") or "").strip() or None
    requesting_app_name = (payload.get("requesting_app_name") or "").strip() or None
    app_display = requesting_app_name or requesting_app_id or "(unknown app)"
    logger.debug(
        "[PIPELINE:UMA] uma_get_messages: message_stream=%s, resource_id=%s, dataset_id=%s, limit=%s, offset=%s, requesting_user_email=%s, requesting_app=%s",
        message_stream,
        resource_id[:50] if resource_id else None,
        dataset_id,
        limit,
        offset,
        requesting_user_email or "(unknown)",
        app_display,
    )
    try:
        db_conn = hub.get_db_connection()
        if not db_conn:
            return {"id": req_id, "status": "error", "error": "Database connection not available"}

        from ...disclosure.tier import apply_disclosure_tier_to_rows, resolve_disclosure_tier

        _, owner_uid_for_tier, _ = _resolve_uma_scope(payload, resource_id)
        req_uid_for_tier = (
            (payload.get("requesting_user_id") or payload.get("mcp_requester_id") or "").strip() or None
        )
        uma_disclosure_tier = resolve_disclosure_tier(
            requester_id=req_uid_for_tier or "owner",
            owner_id=owner_uid_for_tier or "owner",
            is_grantee_request=bool(payload.get("is_grantee_request")),
            disclosure_ceiling=str(payload.get("disclosure_ceiling") or "default"),
        )

        def _uma_messages_record_and_return(
            messages_out: list,
            debug_metadata: Optional[Dict[str, Any]] = None,
        ) -> Dict[str, Any]:
            logger.debug("[PIPELINE:UMA] uma_get_messages returned %d messages", len(messages_out))
            _, owner_uid_resolved, _ = _resolve_uma_scope(payload, resource_id)
            owner_uid = (owner_uid_resolved or "").strip()
            req_uid = (
                (payload.get("requesting_user_id") or payload.get("mcp_requester_id") or "").strip() or None
            )
            uma_attr = _uma_attribution_from_payload(payload)
            if owner_uid:
                hub.record_uma_request(
                    db_conn,
                    owner_user_id=owner_uid,
                    resource_id=resource_id,
                    request_type="read",
                    endpoint="messages",
                    requesting_user_id=req_uid,
                    app_id=uma_attr.get("app_id"),
                    requesting_user_email=requesting_user_email,
                    access_channel=uma_attr.get("access_channel"),
                    access_context=uma_attr.get("access_context"),
                )
            payload_out: Dict[str, Any] = {"messages": messages_out}
            if isinstance(debug_metadata, dict) and debug_metadata:
                payload_out["debug_metadata"] = debug_metadata
            return {"id": req_id, "status": "ok", "payload": payload_out}

        if message_stream == "conversation":
            if _table_exists(db_conn, "conversation_messages") and dataset_id:
                filter_where_m, filter_params = build_sql_constraints(
                    filter_manifest, "m.", logical_table_id="conversation_messages"
                )
                query = (
                    """
                        SELECT m.message_id, m.conversation_id, m.sender_type, m.sender_id,
                               m.event_at, m.content, m.content_disclosure, m.content_disclosure_hash,
                               m.content_nsfw, m.content_nsfw_score,
                               m.metadata_json, m.source_id, m.dataset_id,
                               m.reply_to_message_id, m.message_type, m.event_type, m.is_from_self, m.owner_user_id
                        FROM conversation_messages m
                        WHERE m.dataset_id = ?
                        """
                    + filter_where_m
                    + """
                        ORDER BY m.event_at DESC
                        LIMIT ? OFFSET ?
                        """
                )
                cursor = db_conn.execute(query, (dataset_id,) + tuple(filter_params) + (limit, offset))
                messages = []
                for row in cursor.fetchall():
                    messages.append(
                        {
                            "message_id": row[0],
                            "conversation_id": row[1],
                            "sender_type": row[2],
                            "sender_id": row[3],
                            "event_at": row[4],
                            "content": row[5],
                            "content_disclosure": row[6],
                            "content_disclosure_hash": row[7],
                            "content_nsfw": row[8],
                            "content_nsfw_score": row[9],
                            "metadata_json": row[10],
                            "source_id": row[11],
                            "dataset_id": row[12],
                            "reply_to_message_id": row[13],
                            "message_type": row[14],
                            "event_type": row[15],
                            "is_from_self": row[16],
                            "owner_user_id": row[17],
                        }
                    )
                messages = apply_disclosure_tier_to_rows(
                    messages,
                    table="conversation_messages",
                    tier=uma_disclosure_tier,
                )
                allowed_scopes: List[str] = []
                raw_scopes = payload.get("allowed_scopes")
                if isinstance(raw_scopes, list):
                    allowed_scopes = [str(s).strip() for s in raw_scopes if str(s).strip()]
                try:
                    pre_contact_len = len(messages)
                    after_contact, uma_contact_sidecar = apply_message_contact_pipeline(
                        messages,
                        conn=db_conn,
                        dataset_id=dataset_id,
                        allowed_scopes=allowed_scopes,
                        manifest=filter_manifest,
                        filters=filters_dict,
                    )
                    if pre_contact_len > 0 and len(after_contact) == 0:
                        logger.warning(
                            "[PIPELINE:UMA] req=%s conversation_messages: contact pipeline dropped all %s "
                            "SQL row(s) (sharing_policy row_visibility, message_contact_participation, or "
                            "contact_grant_policy). Field transforms (e.g. nsfw_sanitization on content) do not "
                            "run when no rows remain — same query order as ToposUI but a small limit (e.g. MCP "
                            "default 5) can yield only excluded contacts; try limit=50.",
                            req_id,
                            pre_contact_len,
                        )
                    scope_set = {str(s).strip() for s in allowed_scopes if s}
                    contacts_resolve = "contacts:resolve" in scope_set
                    name_nf = (
                        filter_manifest.get_filter("contact_display_names") if filter_manifest else None
                    )
                    names_effective = contacts_resolve and (
                        name_nf is None or bool(name_nf.params.get("enabled"))
                    )
                    pre_name_rows = sum(1 for row in after_contact if row.get("sender_display_name"))
                    manifest_for_generic = strip_contact_runtime_filters(filter_manifest)
                    transform_diag: Dict[str, Any] = {}
                    logger.debug(
                        "[PIPELINE:UMA][TRANSFORM] req=%s stage=conversation_messages start rows=%s",
                        req_id,
                        len(after_contact),
                    )
                    logger.debug(
                        "[PIPELINE:UMA] contact names: contacts_resolve=%s names_effective=%s "
                        "sender_display_name_rows_after_pipeline=%s allowed_scopes=%s",
                        contacts_resolve,
                        names_effective,
                        pre_name_rows,
                        allowed_scopes,
                    )
                    messages = await apply_filter_manifest_async(
                        after_contact,
                        manifest_for_generic,
                        field_transforms=field_transforms,
                        table_id="conversation_messages",
                        diagnostics=transform_diag,
                        progress_hook=_uma_transform_progress_hook(req_id, "conversation_messages"),
                    )
                    logger.debug(
                        "[PIPELINE:UMA][TRANSFORM] req=%s stage=conversation_messages done applied=%s skipped=%s reasons=%s",
                        req_id,
                        transform_diag.get("applied_count", 0),
                        transform_diag.get("skipped_count", 0),
                        transform_diag.get("skip_reasons", {}),
                    )
                    _skip_reasons = transform_diag.get("skip_reasons") or {}
                    if _skip_reasons.get("table_mismatch"):
                        logger.debug(
                            "[PIPELINE:UMA] req=%s table_mismatch skips=%s are expected when field_transforms "
                            "include other tables (browser_visits, etc.) while processing conversation_messages.",
                            req_id,
                            _skip_reasons.get("table_mismatch"),
                        )
                    post_name_rows = sum(1 for row in messages if bool(row.get("sender_display_name")))
                    debug_metadata = {
                        "contact_names_enriched_count": post_name_rows,
                        "contact_rows_filtered_count": max(0, pre_contact_len - len(after_contact)),
                        "field_transforms": transform_diag,
                        "contact_name_enrichment": {
                            "contacts_resolve_in_scope": contacts_resolve,
                            "contact_display_names_effective": names_effective,
                            "sender_display_name_rows_after_contact_pipeline": pre_name_rows,
                            "sender_display_name_rows_final": post_name_rows,
                        },
                        "message_owner": (uma_contact_sidecar.get("message_owner") or {}),
                    }
                except UMAFilterError as exc:
                    return {"id": req_id, "status": "error", "error": str(exc)}
                return _uma_messages_record_and_return(messages, debug_metadata)
            return _uma_messages_record_and_return([], {"contact_names_enriched_count": 0, "field_transforms": {}})

        if _table_exists(db_conn, "ai_chat_messages"):
            has_conversations_table = _table_exists(db_conn, "ai_chat_conversations")
            has_emotions_table = _table_exists(db_conn, "message_emotions")
            filter_where_m, filter_params = build_sql_constraints(
                filter_manifest, "m.", logical_table_id="ai_chat_messages"
            )
            filter_where_plain, filter_params_plain = build_sql_constraints(
                filter_manifest, "", logical_table_id="ai_chat_messages"
            )
            if has_conversations_table and dataset_id:
                user_id = dataset_id.split(":")[0] if ":" in dataset_id else dataset_id
                if has_emotions_table:
                    query = """
                            SELECT m.message_id, m.conversation_id, m.sender_type, m.sender_id,
                                   m.event_at, m.content, m.content_rendered,
                                   m.content_disclosure, m.content_rendered_disclosure,
                                   m.content_nsfw, m.content_nsfw_score,
                                   m.metadata_json, m.sequence, m.source_id,
                                   e.emotion_label, e.confidence
                            FROM ai_chat_messages m
                            LEFT JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                            LEFT JOIN (
                                SELECT e1.message_id, e1.emotion_label, e1.confidence
                                FROM message_emotions e1
                                INNER JOIN (
                                    SELECT message_id, MAX(confidence) as max_confidence
                                    FROM message_emotions
                                    GROUP BY message_id
                                ) e2 ON e1.message_id = e2.message_id AND e1.confidence = e2.max_confidence
                            ) e ON m.message_id = e.message_id
                            WHERE c.owner_user_id = ?
                            """ + filter_where_m + """
                            ORDER BY m.event_at DESC
                            LIMIT ? OFFSET ?
                        """
                else:
                    query = """
                            SELECT m.message_id, m.conversation_id, m.sender_type, m.sender_id,
                                   m.event_at, m.content, m.content_rendered,
                                   m.content_disclosure, m.content_rendered_disclosure,
                                   m.content_nsfw, m.content_nsfw_score,
                                   m.metadata_json, m.sequence, m.source_id,
                                   NULL as emotion_label, NULL as confidence
                            FROM ai_chat_messages m
                            LEFT JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                            WHERE c.owner_user_id = ?
                            """ + filter_where_m + """
                            ORDER BY m.event_at DESC
                            LIMIT ? OFFSET ?
                        """
                cursor = db_conn.execute(query, (user_id,) + tuple(filter_params) + (limit, offset))
            else:
                if has_emotions_table:
                    query = """
                            SELECT m.message_id, m.conversation_id, m.sender_type, m.sender_id,
                                   m.event_at, m.content, m.content_rendered,
                                   m.content_disclosure, m.content_rendered_disclosure,
                                   m.content_nsfw, m.content_nsfw_score,
                                   m.metadata_json, m.sequence, m.source_id,
                                   e.emotion_label, e.confidence
                            FROM ai_chat_messages m
                            LEFT JOIN (
                                SELECT e1.message_id, e1.emotion_label, e1.confidence
                                FROM message_emotions e1
                                INNER JOIN (
                                    SELECT message_id, MAX(confidence) as max_confidence
                                    FROM message_emotions
                                    GROUP BY message_id
                                ) e2 ON e1.message_id = e2.message_id AND e1.confidence = e2.max_confidence
                            ) e ON m.message_id = e.message_id
                            """ + ("WHERE 1=1" + filter_where_m if filter_where_m else "") + """
                            ORDER BY m.event_at DESC
                            LIMIT ? OFFSET ?
                        """
                else:
                    query = """
                            SELECT message_id, conversation_id, sender_type, sender_id,
                                   event_at, content, content_rendered,
                                   content_disclosure, content_rendered_disclosure,
                                   content_nsfw, content_nsfw_score,
                                   metadata_json, sequence, source_id,
                                   NULL as emotion_label, NULL as confidence
                            FROM ai_chat_messages
                            """ + ("WHERE 1=1" + filter_where_plain if filter_where_plain else "") + """
                            ORDER BY event_at DESC
                            LIMIT ? OFFSET ?
                        """
                if has_emotions_table:
                    cursor = db_conn.execute(
                        query,
                        tuple(filter_params) + (limit, offset) if filter_where_m else (limit, offset),
                    )
                else:
                    cursor = db_conn.execute(
                        query,
                        tuple(filter_params_plain) + (limit, offset) if filter_where_plain else (limit, offset),
                    )
            messages = []
            seen_message_ids = set()
            for row in cursor.fetchall():
                message_id = row[0]
                if message_id in seen_message_ids:
                    continue
                seen_message_ids.add(message_id)
                msg = {
                    "message_id": message_id,
                    "conversation_id": row[1],
                    "sender_type": row[2],
                    "sender_id": row[3],
                    "event_at": row[4],
                    "content": row[5],
                    "content_rendered": row[6],
                    "content_disclosure": row[7],
                    "content_rendered_disclosure": row[8],
                    "content_nsfw": row[9],
                    "content_nsfw_score": row[10],
                    "metadata_json": row[11],
                    "sequence": row[12],
                    "source_id": row[13],
                }
                if len(row) > 14 and row[14] is not None:
                    msg["emotion"] = row[14]
                    if row[15] is not None:
                        msg["emotion_confidence"] = float(row[15])
                messages.append(msg)
            messages = apply_disclosure_tier_to_rows(
                messages,
                table="ai_chat_messages",
                tier=uma_disclosure_tier,
            )
            allowed_scopes: List[str] = []
            raw_scopes = payload.get("allowed_scopes")
            if isinstance(raw_scopes, list):
                allowed_scopes = [str(s).strip() for s in raw_scopes if str(s).strip()]
            pre_contact_len = len(messages)
            messages, uma_contact_sidecar = apply_message_contact_pipeline(
                messages,
                conn=db_conn,
                dataset_id=dataset_id,
                allowed_scopes=allowed_scopes,
                manifest=filter_manifest,
                filters=filters_dict,
            )
            if pre_contact_len > 0 and len(messages) == 0:
                logger.warning(
                    "[PIPELINE:UMA] req=%s ai_chat_messages: contact pipeline dropped all %s row(s); "
                    "no field transforms run. Try a larger limit if grant contact filters exclude recent rows.",
                    req_id,
                    pre_contact_len,
                )
            transform_diag = {}
            logger.debug(
                "[PIPELINE:UMA][TRANSFORM] req=%s stage=ai_chat_messages start rows=%s",
                req_id,
                len(messages),
            )
            messages = await apply_filter_manifest_async(
                messages,
                filter_manifest,
                field_transforms=field_transforms,
                table_id="ai_chat_messages",
                diagnostics=transform_diag,
                progress_hook=_uma_transform_progress_hook(req_id, "ai_chat_messages"),
            )
            logger.debug(
                "[PIPELINE:UMA][TRANSFORM] req=%s stage=ai_chat_messages done applied=%s skipped=%s reasons=%s",
                req_id,
                transform_diag.get("applied_count", 0),
                transform_diag.get("skipped_count", 0),
                transform_diag.get("skip_reasons", {}),
            )
            debug_metadata = {
                "contact_names_enriched_count": sum(
                    1 for row in messages if bool(row.get("sender_display_name"))
                ),
                "contact_rows_filtered_count": max(0, pre_contact_len - len(messages)),
                "field_transforms": transform_diag,
                "message_owner": (uma_contact_sidecar.get("message_owner") or {}),
            }
            return _uma_messages_record_and_return(messages, debug_metadata)
        messages = load_raw_messages(
            dataset_id=dataset_id or "",
            schema_id="chatgpt.conversation.v1",
            limit=limit,
            offset=offset,
            filter_manifest=filter_manifest.to_storage_dict() if filter_manifest else None,
        )
        allowed_scopes = []
        raw_scopes = payload.get("allowed_scopes")
        if isinstance(raw_scopes, list):
            allowed_scopes = [str(s).strip() for s in raw_scopes if str(s).strip()]
        pre_contact_len = len(messages)
        messages, uma_contact_sidecar = apply_message_contact_pipeline(
            messages,
            conn=db_conn,
            dataset_id=dataset_id,
            allowed_scopes=allowed_scopes,
            manifest=filter_manifest,
            filters=filters_dict,
        )
        if pre_contact_len > 0 and len(messages) == 0:
            logger.warning(
                "[PIPELINE:UMA] req=%s ai_chat JSONL: contact pipeline dropped all %s row(s); "
                "no field transforms run. Try a larger limit.",
                req_id,
                pre_contact_len,
            )
        transform_diag = {}
        logger.debug(
            "[PIPELINE:UMA][TRANSFORM] req=%s stage=ai_chat_messages_jsonl start rows=%s",
            req_id,
            len(messages),
        )
        messages = await apply_filter_manifest_async(
            messages,
            filter_manifest,
            field_transforms=field_transforms,
            table_id="ai_chat_messages",
            diagnostics=transform_diag,
            progress_hook=_uma_transform_progress_hook(req_id, "ai_chat_messages_jsonl"),
        )
        logger.debug(
            "[PIPELINE:UMA][TRANSFORM] req=%s stage=ai_chat_messages_jsonl done applied=%s skipped=%s reasons=%s",
            req_id,
            transform_diag.get("applied_count", 0),
            transform_diag.get("skipped_count", 0),
            transform_diag.get("skip_reasons", {}),
        )
        debug_metadata = {
            "contact_names_enriched_count": sum(1 for row in messages if bool(row.get("sender_display_name"))),
            "contact_rows_filtered_count": max(0, pre_contact_len - len(messages)),
            "field_transforms": transform_diag,
            "message_owner": (uma_contact_sidecar.get("message_owner") or {}),
        }
        return _uma_messages_record_and_return(messages, debug_metadata)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[PIPELINE:UMA] uma_get_messages error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("uma_get_oplog")
async def handle_uma_get_oplog(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    """UMA: return oplog for a resource (control plane proxies here). Record read for engine request counts."""
    payload = message.get("payload") or {}
    resource_id = (payload.get("resource_id") or "").strip()
    dataset_id = (payload.get("dataset_id") or "").strip() or None
    owner_uid = payload.get("owner_user_id") or ((resource_id.split(":")[1] if len(resource_id.split(":")) >= 2 else "") or (dataset_id.split(":")[0] if dataset_id else ""))
    requesting_app_id = (payload.get("requesting_app_id") or "").strip() or None
    db_conn = hub.get_db_connection()
    if db_conn and owner_uid and resource_id:
        uma_attr = _uma_attribution_from_payload(payload)
        hub.record_uma_request(
            db_conn,
            owner_user_id=owner_uid,
            resource_id=resource_id,
            request_type="read",
            endpoint="oplog",
            requesting_user_id=payload.get("requesting_user_id") or payload.get("mcp_requester_id"),
            app_id=uma_attr.get("app_id") or requesting_app_id,
            access_channel=uma_attr.get("access_channel"),
            access_context=uma_attr.get("access_context"),
        )
    return {"id": req_id, "status": "ok", "payload": {"ops": []}}

@handles("uma_get_rows")
async def handle_uma_get_rows(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    """UMA generic row read for one concrete table name."""
    payload = message.get("payload") or {}
    resource_id = (payload.get("resource_id") or "").strip()
    dataset_id, owner_uid, tenant_id = _resolve_uma_scope(payload, resource_id)
    table_name = (payload.get("table_name") or payload.get("table_id") or "").strip()
    requested_limit = min(int(payload.get("limit") or 100), 1000)
    offset = max(0, int(payload.get("offset") or 0))
    filters = payload.get("filters") or {}
    filters_dict = filters if isinstance(filters, dict) else None
    filter_manifest = extract_filter_manifest(filters_dict)
    field_transforms = extract_field_transforms(filters_dict)
    limit = get_limit_cap(requested_limit, filter_manifest, table_name)
    cap_reason = "permission_max_rows" if limit < requested_limit else None
    allowed_tables = payload.get("allowed_tables") or []
    allowed_set = {str(t).strip() for t in allowed_tables if str(t).strip()}
    if not table_name:
        return {"id": req_id, "status": "error", "error": "table_name required"}
    if allowed_set and table_name not in allowed_set:
        return {"id": req_id, "status": "error", "error": f"table not allowed: {table_name}"}
    try:
        use_postgres = settings.topos_database_mode == "postgres"
        if use_postgres:
            with connect_postgres() as conn:
                if _is_sqlite_conn(conn):
                    exists = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                        (table_name,),
                    ).fetchone()
                else:
                    exists = conn.execute(
                        """
                            SELECT 1
                            FROM information_schema.tables
                            WHERE table_schema='public' AND table_name=%s
                            LIMIT 1
                            """,
                        (table_name,),
                    ).fetchone()
                if not exists:
                    return {"id": req_id, "status": "error", "error": f"Table or view not found: {table_name}"}

                if _is_sqlite_conn(conn):
                    try:
                        cols = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
                        col_names = {str(c["name"]) for c in cols} if cols else set()
                    except Exception:
                        col_names = set()
                else:
                    cols = conn.execute(
                        """
                            SELECT column_name
                            FROM information_schema.columns
                            WHERE table_schema='public' AND table_name=%s
                            """,
                        (table_name,),
                    ).fetchall()
                    col_names = {str(c[0]) for c in cols} if cols else set()

                scope_where, scope_params = _build_uma_scope_clause(
                    col_names=col_names,
                    dataset_id=dataset_id,
                    owner_user_id=owner_uid,
                    tenant_id=tenant_id,
                )
                if not scope_where:
                    return {
                        "id": req_id,
                        "status": "error",
                        "error": f"table not tenant scoped for UMA reads: {table_name}",
                    }
                order_clause = _table_row_order_clause(
                    col_names,
                    table_name=table_name,
                    is_sqlite=_is_sqlite_conn(conn),
                )

                # Pull one extra row to derive has_more without a separate COUNT.
                if _is_sqlite_conn(conn):
                    cursor = conn.execute(
                        f'SELECT * FROM "{table_name}"{scope_where} ORDER BY {order_clause} LIMIT ? OFFSET ?',
                        scope_params + (limit + 1, offset),
                    )
                    all_rows = [dict(r) for r in cursor.fetchall()]
                else:
                    pg_scope_where = scope_where.replace("?", "%s")
                    cursor = conn.execute(
                        f'SELECT * FROM "{table_name}"{pg_scope_where} ORDER BY {order_clause} LIMIT %s OFFSET %s',
                        scope_params + (limit + 1, offset),
                    )
                    col_order = [desc[0] for desc in (cursor.description or [])]
                    all_rows = [dict(zip(col_order, row)) for row in cursor.fetchall()]
        else:
            conn = hub.get_db_connection()
            if not conn:
                return {"id": req_id, "status": "error", "error": "Database connection not available"}
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                (table_name,),
            ).fetchone()
            if not exists:
                return {"id": req_id, "status": "error", "error": f"Table or view not found: {table_name}"}
            try:
                cols = conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
                col_names = {str(c["name"]) for c in cols} if cols else set()
            except Exception:
                col_names = set()
            scope_where, scope_params = _build_uma_scope_clause(
                col_names=col_names,
                dataset_id=dataset_id,
                owner_user_id=owner_uid,
                tenant_id=tenant_id,
            )
            if not scope_where:
                return {
                    "id": req_id,
                    "status": "error",
                    "error": f"table not tenant scoped for UMA reads: {table_name}",
                }
            order_clause = _table_row_order_clause(
                col_names,
                table_name=table_name,
                is_sqlite=True,
            )
            cursor = conn.execute(
                f'SELECT * FROM "{table_name}"{scope_where} ORDER BY {order_clause} LIMIT ? OFFSET ?',
                scope_params + (limit + 1, offset),
            )
            all_rows = [dict(r) for r in cursor.fetchall()]
        has_more = len(all_rows) > limit
        rows = all_rows[:limit]
        try:
            transform_diag = {}
            logger.debug(
                "[PIPELINE:UMA][TRANSFORM] req=%s stage=rows:%s start rows=%s",
                req_id,
                table_name,
                len(rows),
            )
            rows = await apply_filter_manifest_async(
                rows,
                filter_manifest,
                field_transforms=field_transforms,
                table_id=table_name,
                diagnostics=transform_diag,
                progress_hook=_uma_transform_progress_hook(req_id, f"rows:{table_name}"),
            )
            logger.debug(
                "[PIPELINE:UMA][TRANSFORM] req=%s stage=rows:%s done applied=%s skipped=%s reasons=%s",
                req_id,
                table_name,
                transform_diag.get("applied_count", 0),
                transform_diag.get("skipped_count", 0),
                transform_diag.get("skip_reasons", {}),
            )
        except UMAFilterError as exc:
            return {"id": req_id, "status": "error", "error": str(exc)}
        if owner_uid:
            uma_attr = _uma_attribution_from_payload(payload)
            hub.record_uma_request(
                conn,
                owner_user_id=owner_uid,
                resource_id=resource_id,
                request_type="read",
                endpoint=f"rows:{table_name}",
                requesting_user_id=payload.get("requesting_user_id") or payload.get("mcp_requester_id"),
                app_id=uma_attr.get("app_id") or payload.get("requesting_app_id"),
                access_channel=uma_attr.get("access_channel"),
                access_context=uma_attr.get("access_context"),
            )
        return {
            "id": req_id,
            "status": "ok",
            "payload": {
                "rows": rows,
                "table_name": table_name,
                "applied_limit": limit,
                "has_more": bool(has_more),
                "next_offset": (offset + limit) if has_more else None,
                "cap_reason": cap_reason,
                "debug_metadata": {"field_transforms": transform_diag},
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.debug("[PIPELINE:UMA] uma_get_rows error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}
