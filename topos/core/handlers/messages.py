"""Message store, oplog, projection, and analytics handlers."""
from __future__ import annotations

import topos.core.handlers as hub

from .common import (
    Any,
    Dict,
    List,
    Optional,
    UMAFilterError,
    _resource_owner_for_mcp_log,
    _table_exists,
    _uma_transform_progress_hook,
    apply_filter_manifest_async,
    apply_message_contact_pipeline,
    avg_message_length,
    build_sql_constraints,
    extract_field_transforms,
    extract_filter_manifest,
    get_limit_cap,
    get_or_create_user_id,
    get_user_id,
    ingest_ui_payload,
    load_raw_messages,
    logger,
    messages_by_sender,
    messages_per_day,
    record_mcp_request,
    settings,
    strip_contact_runtime_filters,
    total_messages,
)
from .registry import handles


def _query_messages_per_day_db(
    conn, dataset_id: Optional[str], table_name: str, source_filter: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Query messages per day from database table."""
    try:
        # Check if table exists
        if not _table_exists(conn, table_name):
            logger.debug("Table %s does not exist, returning empty result", table_name)
            return []
        
        # For messages table, if dataset_id is provided but doesn't match any rows,
        # fall back to returning all messages (more lenient for local mode)
        if table_name == "messages" and dataset_id:
            # First check if any rows match this dataset_id
            cursor = conn.execute("SELECT COUNT(*) FROM messages WHERE dataset_id = ?", (dataset_id,))
            matching_count = cursor.fetchone()[0]
            if matching_count == 0:
                logger.debug("No messages found for dataset_id=%s, falling back to all messages", dataset_id)
                dataset_id = None  # Fall back to no filter
        
        if table_name == "ai_chat_messages":
            # Check if conversations table exists for join
            has_conversations_table = _table_exists(conn, "ai_chat_conversations")
            
            if has_conversations_table:
                # Join with conversations table to filter by owner_user_id
                query = """
                    SELECT DATE(m.event_at) as day, COUNT(*) as message_count 
                    FROM ai_chat_messages m
                    LEFT JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                """
                conditions = []
                params = []
                if dataset_id:
                    # Extract user_id from dataset_id (format: user_id:dataset_name)
                    user_id = dataset_id.split(":")[0] if ":" in dataset_id else dataset_id
                    conditions.append("c.owner_user_id = ?")
                    params.append(user_id)
                if source_filter:
                    conditions.append("m.source_id = ?")
                    params.append(source_filter)
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                query += " GROUP BY day ORDER BY day DESC"
            else:
                # No conversations table, query messages directly (no user filtering)
                query = "SELECT DATE(event_at) as day, COUNT(*) as message_count FROM ai_chat_messages"
                conditions = []
                params = []
                if source_filter:
                    conditions.append("source_id = ?")
                    params.append(source_filter)
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                query += " GROUP BY day ORDER BY day DESC"
        else:
            query = f"SELECT DATE(event_at) as day, COUNT(*) as message_count FROM {table_name}"
            conditions = []
            params = []
            if dataset_id:
                conditions.append("dataset_id = ?")
                params.append(dataset_id)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " GROUP BY day ORDER BY day DESC"
        cursor = conn.execute(query, params)
        return [{"day": row[0], "message_count": row[1]} for row in cursor.fetchall()]
    except Exception as e:
        logger.warning("Failed to query messages_per_day from %s: %s", table_name, e)
        return []

def _query_total_messages_db(
    conn, dataset_id: Optional[str], table_name: str, source_filter: Optional[str] = None
) -> Dict[str, Any]:
    """Query total messages from database table."""
    try:
        # Check if table exists
        if not _table_exists(conn, table_name):
            logger.debug("Table %s does not exist, returning 0", table_name)
            return {"total_messages": 0}
        
        # For messages table, if dataset_id is provided but doesn't match any rows,
        # fall back to returning all messages (more lenient for local mode)
        if table_name == "messages" and dataset_id:
            cursor = conn.execute("SELECT COUNT(*) FROM messages WHERE dataset_id = ?", (dataset_id,))
            matching_count = cursor.fetchone()[0]
            if matching_count == 0:
                logger.debug("No messages found for dataset_id=%s, falling back to all messages", dataset_id)
                dataset_id = None  # Fall back to no filter
        
        if table_name == "ai_chat_messages":
            # Check if conversations table exists for join
            has_conversations_table = _table_exists(conn, "ai_chat_conversations")
            
            if has_conversations_table:
                # Join with conversations table to filter by owner_user_id
                query = """
                    SELECT COUNT(*) as total_messages 
                    FROM ai_chat_messages m
                    LEFT JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                """
                conditions = []
                params = []
                if dataset_id:
                    user_id = dataset_id.split(":")[0] if ":" in dataset_id else dataset_id
                    conditions.append("c.owner_user_id = ?")
                    params.append(user_id)
                if source_filter:
                    conditions.append("m.source_id = ?")
                    params.append(source_filter)
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
            else:
                # No conversations table, query messages directly
                query = "SELECT COUNT(*) as total_messages FROM ai_chat_messages"
                conditions = []
                params = []
                if source_filter:
                    conditions.append("source_id = ?")
                    params.append(source_filter)
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
        else:
            query = f"SELECT COUNT(*) as total_messages FROM {table_name}"
            conditions = []
            params = []
            if dataset_id:
                conditions.append("dataset_id = ?")
                params.append(dataset_id)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
        cursor = conn.execute(query, params)
        row = cursor.fetchone()
        return {"total_messages": row[0] if row else 0}
    except Exception as e:
        logger.warning("Failed to query total_messages from %s: %s", table_name, e)
        return {"total_messages": 0}

def _query_avg_message_length_db(
    conn, dataset_id: Optional[str], table_name: str, source_filter: Optional[str] = None
) -> Dict[str, Any]:
    """Query average message length from database table."""
    try:
        # Check if table exists
        if not _table_exists(conn, table_name):
            logger.debug("Table %s does not exist, returning 0", table_name)
            return {"avg_length": 0.0, "min_length": 0, "max_length": 0}
        
        # For messages table, if dataset_id is provided but doesn't match any rows,
        # fall back to returning all messages (more lenient for local mode)
        if table_name == "messages" and dataset_id:
            cursor = conn.execute("SELECT COUNT(*) FROM messages WHERE dataset_id = ?", (dataset_id,))
            matching_count = cursor.fetchone()[0]
            if matching_count == 0:
                logger.debug("No messages found for dataset_id=%s, falling back to all messages", dataset_id)
                dataset_id = None  # Fall back to no filter
        
        if table_name == "ai_chat_messages":
            # Check if conversations table exists for join
            has_conversations_table = _table_exists(conn, "ai_chat_conversations")
            
            if has_conversations_table:
                # Join with conversations table to filter by owner_user_id
                query = """
                    SELECT AVG(LENGTH(m.content)) as avg_length, 
                           MIN(LENGTH(m.content)) as min_length, 
                           MAX(LENGTH(m.content)) as max_length 
                    FROM ai_chat_messages m
                    LEFT JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                """
                conditions = []
                params = []
                if dataset_id:
                    user_id = dataset_id.split(":")[0] if ":" in dataset_id else dataset_id
                    conditions.append("c.owner_user_id = ?")
                    params.append(user_id)
                if source_filter:
                    conditions.append("m.source_id = ?")
                    params.append(source_filter)
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
            else:
                # No conversations table, query messages directly
                query = """
                    SELECT AVG(LENGTH(content)) as avg_length, 
                           MIN(LENGTH(content)) as min_length, 
                           MAX(LENGTH(content)) as max_length 
                    FROM ai_chat_messages
                """
                conditions = []
                params = []
                if source_filter:
                    conditions.append("source_id = ?")
                    params.append(source_filter)
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
        else:
            query = f"SELECT AVG(LENGTH(content)) as avg_length, MIN(LENGTH(content)) as min_length, MAX(LENGTH(content)) as max_length FROM {table_name}"
            conditions = []
            params = []
            if dataset_id:
                conditions.append("dataset_id = ?")
                params.append(dataset_id)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
        cursor = conn.execute(query, params)
        row = cursor.fetchone()
        if row and row[0] is not None:
            return {
                "avg_length": float(row[0]),
                "min_length": int(row[1] or 0),
                "max_length": int(row[2] or 0),
            }
        return {"avg_length": 0.0, "min_length": 0, "max_length": 0}
    except Exception as e:
        logger.warning("Failed to query avg_message_length from %s: %s", table_name, e)
        return {"avg_length": 0.0, "min_length": 0, "max_length": 0}

def _query_messages_by_sender_db(
    conn, dataset_id: Optional[str], table_name: str, source_filter: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Query messages by sender from database table."""
    try:
        # Check if table exists
        if not _table_exists(conn, table_name):
            logger.debug("Table %s does not exist, returning empty result", table_name)
            return []
        
        # For messages table, if dataset_id is provided but doesn't match any rows,
        # fall back to returning all messages (more lenient for local mode)
        if table_name == "messages" and dataset_id:
            cursor = conn.execute("SELECT COUNT(*) FROM messages WHERE dataset_id = ?", (dataset_id,))
            matching_count = cursor.fetchone()[0]
            if matching_count == 0:
                logger.debug("No messages found for dataset_id=%s, falling back to all messages", dataset_id)
                dataset_id = None  # Fall back to no filter
        
        if table_name == "ai_chat_messages":
            # Check if conversations table exists for join
            has_conversations_table = _table_exists(conn, "ai_chat_conversations")
            
            if has_conversations_table:
                # Join with conversations table to filter by owner_user_id
                query = """
                    SELECT m.sender_type, COUNT(*) as count 
                    FROM ai_chat_messages m
                    LEFT JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                """
                conditions = []
                params = []
                if dataset_id:
                    user_id = dataset_id.split(":")[0] if ":" in dataset_id else dataset_id
                    conditions.append("c.owner_user_id = ?")
                    params.append(user_id)
                if source_filter:
                    conditions.append("m.source_id = ?")
                    params.append(source_filter)
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                query += " GROUP BY m.sender_type ORDER BY count DESC"
            else:
                # No conversations table, query messages directly
                query = "SELECT sender_type, COUNT(*) as count FROM ai_chat_messages"
                conditions = []
                params = []
                if source_filter:
                    conditions.append("source_id = ?")
                    params.append(source_filter)
                if conditions:
                    query += " WHERE " + " AND ".join(conditions)
                query += " GROUP BY sender_type ORDER BY count DESC"
        else:
            query = f"SELECT sender_type, COUNT(*) as count FROM {table_name}"
            conditions = []
            params = []
            if dataset_id:
                conditions.append("dataset_id = ?")
                params.append(dataset_id)
            if conditions:
                query += " WHERE " + " AND ".join(conditions)
            query += " GROUP BY sender_type ORDER BY count DESC"
        cursor = conn.execute(query, params)
        return [{"sender_type": row[0], "count": row[1]} for row in cursor.fetchall()]
    except Exception as e:
        logger.warning("Failed to query messages_by_sender from %s: %s", table_name, e)
        return []

def _query_combined_messages_per_day(conn, dataset_id: Optional[str]) -> List[Dict[str, Any]]:
    """Query combined messages per day from all sources."""
    try:
        # Union messages from messages table and ai_chat_messages table
        query = """
            SELECT DATE(event_at) as day, COUNT(*) as message_count FROM (
                SELECT event_at FROM messages
                UNION ALL
                SELECT event_at FROM ai_chat_messages
            )
        """
        # Note: dataset_id filtering is complex for combined queries, so we'll get all messages
        # In a production system, you'd want to properly join with conversations table
        query += " GROUP BY day ORDER BY day DESC"
        cursor = conn.execute(query)
        return [{"day": row[0], "message_count": row[1]} for row in cursor.fetchall()]
    except Exception as e:
        logger.warning("Failed to query combined_messages_per_day: %s", e)
        return []

def _query_combined_total_messages(conn, dataset_id: Optional[str]) -> Dict[str, Any]:
    """Query combined total messages from all sources."""
    try:
        query = """
            SELECT COUNT(*) as total_messages FROM (
                SELECT message_id FROM messages
                UNION ALL
                SELECT message_id FROM ai_chat_messages
            )
        """
        cursor = conn.execute(query)
        row = cursor.fetchone()
        total = row[0] if row else 0
        
        # Also get breakdown by source
        demo_count = _query_total_messages_db(conn, dataset_id, "messages").get("total_messages", 0)
        chatgpt_count = _query_total_messages_db(conn, dataset_id, "ai_chat_messages", "chatgpt").get("total_messages", 0)
        if chatgpt_count == 0:
            chatgpt_count = _query_total_messages_db(conn, dataset_id, "chatgpt_messages").get("total_messages", 0)
        
        return {
            "total_messages": total,
            "demo_messages": demo_count,
            "chatgpt_messages": chatgpt_count,
            "jsonl_messages": 0,  # Would need to query raw files
        }
    except Exception as e:
        logger.warning("Failed to query combined_total_messages: %s", e)
        return {"total_messages": 0, "demo_messages": 0, "chatgpt_messages": 0, "jsonl_messages": 0}

def _query_combined_avg_message_length(conn, dataset_id: Optional[str]) -> Dict[str, Any]:
    """Query combined average message length from all sources."""
    try:
        query = """
            SELECT AVG(LENGTH(content)) as avg_length, MIN(LENGTH(content)) as min_length, MAX(LENGTH(content)) as max_length FROM (
                SELECT content FROM messages
                UNION ALL
                SELECT content FROM ai_chat_messages
            )
        """
        cursor = conn.execute(query)
        row = cursor.fetchone()
        if row and row[0] is not None:
            return {
                "avg_length": float(row[0]),
                "min_length": int(row[1] or 0),
                "max_length": int(row[2] or 0),
            }
        return {"avg_length": 0.0, "min_length": 0, "max_length": 0}
    except Exception as e:
        logger.warning("Failed to query combined_avg_message_length: %s", e)
        return {"avg_length": 0.0, "min_length": 0, "max_length": 0}

def _query_combined_messages_by_sender(conn, dataset_id: Optional[str]) -> List[Dict[str, Any]]:
    """Query combined messages by sender from all sources."""
    try:
        query = """
            SELECT sender_type, COUNT(*) as count FROM (
                SELECT sender_type FROM messages
                UNION ALL
                SELECT sender_type FROM ai_chat_messages
            )
            GROUP BY sender_type ORDER BY count DESC
        """
        cursor = conn.execute(query)
        return [{"sender_type": row[0], "count": row[1]} for row in cursor.fetchall()]
    except Exception as e:
        logger.warning("Failed to query combined_messages_by_sender: %s", e)
        return []

@handles("store_message")
async def handle_store_message(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    from ...ingestion.log_preview import field_preview

    content_preview = field_preview(payload.get("content"))
    
    # Derive dataset_id from user_id if not provided
    dataset_id = payload.get("dataset_id")
    if not dataset_id:
        # Get user_id from database
        conn = hub.get_db_connection()
        if conn:
            user_id = get_user_id(conn)
            if not user_id:
                # Create user_id if it doesn't exist
                user_id = get_or_create_user_id(conn)
            if user_id:
                # Use default dataset name
                base_dataset_id = getattr(settings, 'default_dataset_id', 'default')
                dataset_id = f"{user_id}:{base_dataset_id}"
                logger.debug(
                    "[PIPELINE:ENTRY] store_message: derived dataset_id=%s from user_id=%s",
                    dataset_id,
                    user_id,
                )
            else:
                logger.warning("[PIPELINE:ENTRY] store_message: user_id not found in database, cannot derive dataset_id")
        else:
            logger.warning("[PIPELINE:ENTRY] store_message: database connection not available, cannot derive dataset_id")
    else:
        # If dataset_id is provided but doesn't have user_id prefix, try to add it
        if ":" not in dataset_id:
            conn = hub.get_db_connection()
            if conn:
                user_id = get_user_id(conn)
                if not user_id:
                    user_id = get_or_create_user_id(conn)
                if user_id:
                    dataset_id = f"{user_id}:{dataset_id}"
                    logger.debug(
                        "[PIPELINE:ENTRY] store_message: added user_id prefix to dataset_id: %s",
                        dataset_id,
                    )
    
    logger.debug(
        "[PIPELINE:ENTRY] store_message received: dataset_id=%s, sender_type=%s, content_preview=%s",
        dataset_id,
        payload.get("sender_type"),
        content_preview,
    )
    
    if not dataset_id:
        error_msg = "dataset_id required (could not derive from user_id)"
        logger.error("[PIPELINE:ERROR] store_message: %s", error_msg)
        return {"id": req_id, "status": "error", "error": error_msg}
    
    try:
        # Route through chatgpt_ui_conversation source (UI stream, automatic enrichment)
        # Use ingest_ui_payload directly with source_id to enable direct database processing
        from ...ingestion.ingest_helpers import ingest_ui_payload
        
        result = await ingest_ui_payload(
            dataset_id=dataset_id,
            schema_id="chatgpt.conversation.v1",
            payload=payload,
            job_id=payload.get("message_id"),
            source_id="chatgpt_ui_conversation",  # Enable direct processing without JSONL
        )
        if result.get("status") != "ok":
            logger.debug("[PIPELINE:ERROR] store_message failed: %s", result.get("error"))
            return {"id": req_id, "status": "error", "error": result.get("error", "ingestion failed")}
        logger.debug(
            "[PIPELINE:COMPLETE] store_message processed: job_id=%s, records_processed=%s",
            result.get("job_id"),
            result.get("records_processed"),
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[PIPELINE:ERROR] store_message exception: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("get_messages")
async def handle_get_messages(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    _payload = message.get("payload") or {}
    _mcp_source = _payload.get("mcp_source")
    _mcp_requester_id = _payload.get("mcp_requester_id")
    payload = message.get("payload") or {}
    dataset_id = payload.get("dataset_id")
    limit = int(payload.get("limit") or 100)
    offset = int(payload.get("offset") or 0)
    _raw_ms = (payload.get("message_stream") or "ai_chat").strip().lower()
    message_stream = _raw_ms if _raw_ms in ("conversation", "ai_chat") else "ai_chat"
    logger.debug(
        "[PIPELINE:QUERY] get_messages: dataset_id=%s, limit=%s, offset=%s, message_stream=%s",
        dataset_id,
        limit,
        offset,
        message_stream,
    )
    try:
        db_conn = hub.get_db_connection()
        if not db_conn:
            return {"id": req_id, "status": "error", "error": "Database connection not available"}

        # Human / messenger lane: canonical conversation_messages (iMessage, Signal, …).
        if message_stream == "conversation":
            ds = (str(dataset_id).strip() if dataset_id is not None else "")
            if not ds:
                return {
                    "id": req_id,
                    "status": "error",
                    "error": "dataset_id is required when message_stream is 'conversation' (e.g. your Topos dataset id for iMessage rows)",
                }
            if not _table_exists(db_conn, "conversation_messages"):
                return {
                    "id": req_id,
                    "status": "error",
                    "error": "conversation_messages table not available",
                }
            filters_dict = payload.get("filters")
            if not isinstance(filters_dict, dict):
                filters_dict = {}
            # Owner self-read: do not apply grant-oriented contact sharing exclusions.
            filters_for_pipeline = dict(filters_dict)
            cgp = dict(filters_for_pipeline.get("contact_grant_policy") or {})
            if "inherit_contact_defaults" not in cgp:
                cgp["inherit_contact_defaults"] = False
            filters_for_pipeline["contact_grant_policy"] = cgp
            filter_manifest = extract_filter_manifest(filters_for_pipeline)
            field_transforms = extract_field_transforms(filters_dict)
            req_limit = min(max(0, int(payload.get("limit") or 100)), 1000)
            offset_clamped = max(0, int(payload.get("offset") or 0))
            eff_limit = get_limit_cap(req_limit, filter_manifest, "conversation_messages")
            filter_where_m, filter_params = build_sql_constraints(
                filter_manifest, "m.", logical_table_id="conversation_messages"
            )
            extra_sql: List[str] = []
            extra_params: List[Any] = []
            ifs = payload.get("is_from_self")
            if isinstance(ifs, bool):
                extra_sql.append("m.is_from_self = ?")
                extra_params.append(1 if ifs else 0)
            st = payload.get("sender_type")
            if isinstance(st, str) and st.strip():
                extra_sql.append("m.sender_type = ?")
                extra_params.append(st.strip())
            sid = payload.get("source_id")
            if isinstance(sid, str) and sid.strip():
                extra_sql.append("m.source_id = ?")
                extra_params.append(sid.strip())
            extra_clause = ""
            if extra_sql:
                extra_clause = " AND " + " AND ".join(extra_sql)
            query = (
                """
                    SELECT m.message_id, m.conversation_id, m.sender_type, m.sender_id,
                           m.event_at, m.content, m.metadata_json, m.source_id, m.dataset_id,
                           m.reply_to_message_id, m.message_type, m.event_type, m.is_from_self, m.owner_user_id
                    FROM conversation_messages m
                    WHERE m.dataset_id = ?
                    """
                + filter_where_m
                + extra_clause
                + """
                    ORDER BY m.event_at DESC
                    LIMIT ? OFFSET ?
                    """
            )
            cursor = db_conn.execute(
                query,
                (ds,) + tuple(filter_params) + tuple(extra_params) + (eff_limit, offset_clamped),
            )
            messages: List[Dict[str, Any]] = []
            for row in cursor.fetchall():
                messages.append(
                    {
                        "message_id": row[0],
                        "conversation_id": row[1],
                        "sender_type": row[2],
                        "sender_id": row[3],
                        "event_at": row[4],
                        "content": row[5],
                        "metadata_json": row[6],
                        "source_id": row[7],
                        "dataset_id": row[8],
                        "reply_to_message_id": row[9],
                        "message_type": row[10],
                        "event_type": row[11],
                        "is_from_self": row[12],
                        "owner_user_id": row[13],
                    }
                )
            # Owner's engine: same contact pipeline as UMA conversation lane.
            # Optional allowed_scopes from MCP owner policy (control plane); default full owner lane.
            raw_owner_scopes = payload.get("allowed_scopes")
            if isinstance(raw_owner_scopes, list) and raw_owner_scopes:
                owner_allowed_scopes = [
                    str(s).strip() for s in raw_owner_scopes if str(s).strip()
                ]
            else:
                owner_allowed_scopes = ["messages:read", "contacts:resolve"]
            try:
                pre_len = len(messages)
                after_contact, uma_contact_sidecar = apply_message_contact_pipeline(
                    messages,
                    conn=db_conn,
                    dataset_id=ds,
                    allowed_scopes=owner_allowed_scopes,
                    manifest=filter_manifest,
                    filters=filters_for_pipeline,
                )
                manifest_for_generic = strip_contact_runtime_filters(filter_manifest)
                transform_diag: Dict[str, Any] = {}
                messages = await apply_filter_manifest_async(
                    after_contact,
                    manifest_for_generic,
                    field_transforms=field_transforms,
                    table_id="conversation_messages",
                    diagnostics=transform_diag,
                    progress_hook=_uma_transform_progress_hook(req_id, "get_messages:conversation_messages"),
                )
                logger.debug(
                    "[PIPELINE:QUERY] get_messages conversation stream: raw=%s after_pipeline=%s",
                    pre_len,
                    len(messages),
                )
            except UMAFilterError as exc:
                return {"id": req_id, "status": "error", "error": str(exc)}
            record_mcp_request(
                db_conn,
                "get_messages",
                source=_mcp_source,
                requester_id=_mcp_requester_id,
                resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
            )
            return {"id": req_id, "status": "ok", "payload": {"messages": messages, "message_stream": "conversation"}}

        # Query canonical messages from ai_chat_messages table (AI chat lane)
        if _table_exists(db_conn, "ai_chat_messages"):
            has_conversations_table = _table_exists(db_conn, "ai_chat_conversations")
            
            # Check if message_emotions table exists
            has_emotions_table = _table_exists(db_conn, "message_emotions")
            
            if has_conversations_table and dataset_id:
                # Join with conversations to filter by owner_user_id
                engine_uid = (get_user_id(db_conn) or "").strip()
                owner_uid = engine_uid or (dataset_id.split(":")[0] if ":" in str(dataset_id) else str(dataset_id).strip())
                legacy_local_owners = ("user", "manual-user")
                if has_emotions_table:
                    # Include emotion data via LEFT JOIN
                    # Get the emotion with highest confidence per message (in case multiple models exist)
                    query = """
                            SELECT m.message_id, m.conversation_id, m.sender_type, m.sender_id,
                                   m.event_at, m.content, m.content_rendered, m.metadata_json, m.sequence, m.source_id,
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
                            WHERE (c.owner_user_id = ? OR c.owner_user_id IN (?, ?))
                            ORDER BY m.event_at DESC
                            LIMIT ? OFFSET ?
                        """
                else:
                    query = """
                            SELECT m.message_id, m.conversation_id, m.sender_type, m.sender_id,
                                   m.event_at, m.content, m.content_rendered, m.metadata_json, m.sequence, m.source_id,
                                   NULL as emotion_label, NULL as confidence
                            FROM ai_chat_messages m
                            LEFT JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                            WHERE (c.owner_user_id = ? OR c.owner_user_id IN (?, ?))
                            ORDER BY m.event_at DESC
                            LIMIT ? OFFSET ?
                        """
                cursor = db_conn.execute(
                    query,
                    (owner_uid, legacy_local_owners[0], legacy_local_owners[1], limit, offset),
                )
            else:
                # Query without user filtering
                if has_emotions_table:
                    # Include emotion data via LEFT JOIN
                    # Get the emotion with highest confidence per message (in case multiple models exist)
                    query = """
                            SELECT m.message_id, m.conversation_id, m.sender_type, m.sender_id,
                                   m.event_at, m.content, m.content_rendered, m.metadata_json, m.sequence, m.source_id,
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
                            ORDER BY m.event_at DESC
                            LIMIT ? OFFSET ?
                        """
                else:
                    query = """
                            SELECT message_id, conversation_id, sender_type, sender_id,
                                   event_at, content, content_rendered, metadata_json, sequence, source_id,
                                   NULL as emotion_label, NULL as confidence
                            FROM ai_chat_messages
                            ORDER BY event_at DESC
                            LIMIT ? OFFSET ?
                        """
                cursor = db_conn.execute(query, (limit, offset))
            
            messages = []
            seen_message_ids = set()  # Track to avoid duplicates from JOIN
            for row in cursor.fetchall():
                message_id = row[0]
                # Skip duplicates (in case JOIN returns multiple emotion records)
                if message_id in seen_message_ids:
                    continue
                seen_message_ids.add(message_id)
                
                message = {
                    "message_id": message_id,
                    "conversation_id": row[1],
                    "sender_type": row[2],
                    "sender_id": row[3],
                    "event_at": row[4],
                    "content": row[5],
                    "content_rendered": row[6],
                    "metadata_json": row[7],
                    "sequence": row[8],
                    "source_id": row[9],
                }
                # Add emotion data if available
                if len(row) > 10:
                    emotion_label = row[10] if row[10] is not None else None
                    confidence = row[11] if row[11] is not None else None
                    if emotion_label:
                        message["emotion"] = emotion_label
                        if confidence is not None:
                            message["emotion_confidence"] = float(confidence)
                messages.append(message)
            
            logger.debug(
                "[PIPELINE:QUERY] get_messages returned %d messages from canonical table",
                len(messages),
            )
            record_mcp_request(
                db_conn,
                "get_messages",
                source=_mcp_source,
                requester_id=_mcp_requester_id,
                resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
            )
            return {
                "id": req_id,
                "status": "ok",
                "payload": {"messages": messages, "message_stream": "ai_chat"},
            }
        else:
            # Fallback to raw messages if canonical table doesn't exist
            messages = load_raw_messages(
                dataset_id=dataset_id or "",
                schema_id="chatgpt.conversation.v1",
                limit=limit,
                offset=offset,
            )
            logger.debug(
                "[PIPELINE:QUERY] get_messages returned %d messages from raw files",
                len(messages),
            )
            record_mcp_request(
                db_conn,
                "get_messages",
                source=_mcp_source,
                requester_id=_mcp_requester_id,
                resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
            )
            return {
                "id": req_id,
                "status": "ok",
                "payload": {"messages": messages, "message_stream": "ai_chat"},
            }
    except Exception as exc:  # noqa: BLE001
        logger.debug("[PIPELINE:QUERY] get_messages error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("get_oplog")
async def handle_get_oplog(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    _payload = message.get("payload") or {}
    _mcp_source = _payload.get("mcp_source")
    _mcp_requester_id = _payload.get("mcp_requester_id")
    conn = hub.get_db_connection()
    record_mcp_request(
        conn,
        "get_oplog",
        source=_mcp_source,
        requester_id=_mcp_requester_id,
        resource_owner_user_id=_resource_owner_for_mcp_log(conn),
    )
    return {"id": req_id, "status": "ok", "payload": {"ops": []}}

@handles("replay_projection")
async def handle_replay_projection(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    return {"id": req_id, "status": "ok", "payload": {"status": "ok"}}

@handles("replay_projection_preview")
async def handle_replay_projection_preview(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    return {
        "id": req_id,
        "status": "ok",
        "payload": {"ops_replayed": 0, "total_ops": 0, "count": 0, "messages": []},
    }

@handles("get_analytics")
async def handle_get_analytics(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    _payload = message.get("payload") or {}
    _mcp_source = _payload.get("mcp_source")
    _mcp_requester_id = _payload.get("mcp_requester_id")
    payload = message.get("payload") or {}
    query = str(payload.get("query") or "").lower()
    dataset_id = payload.get("dataset_id")
    # Fallback to user_id:default if dataset_id is missing (for local mode compatibility)
    if not dataset_id:
        # Try to get user_id from database
        db_conn = hub.get_db_connection()
        if db_conn:
            user_id = get_user_id(db_conn)
            if user_id:
                dataset_id = f"{user_id}:{settings.topos_default_dataset_id}"
    logger.debug(
        "[PIPELINE:ANALYTICS] Query received: query=%s, dataset_id=%s",
        query,
        dataset_id,
    )
    try:
        # Get database connection (will create if needed)
        db_conn = hub.get_db_connection()
        
        # Try to query database tables first, fallback to raw files
        if db_conn:
            # Demo app queries (messages table)
            if query == "messages_per_day":
                result = _query_messages_per_day_db(db_conn, dataset_id, "messages")
                record_mcp_request(
                db_conn,
                "get_analytics",
                source=_mcp_source,
                requester_id=_mcp_requester_id,
                resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
            )
                return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
            if query == "total_messages":
                result = _query_total_messages_db(db_conn, dataset_id, "messages")
                record_mcp_request(
                db_conn,
                "get_analytics",
                source=_mcp_source,
                requester_id=_mcp_requester_id,
                resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
            )
                return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
            if query == "avg_message_length":
                result = _query_avg_message_length_db(db_conn, dataset_id, "messages")
                record_mcp_request(
                db_conn,
                "get_analytics",
                source=_mcp_source,
                requester_id=_mcp_requester_id,
                resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
            )
                return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
            if query == "messages_by_sender":
                result = _query_messages_by_sender_db(db_conn, dataset_id, "messages")
                record_mcp_request(
                db_conn,
                "get_analytics",
                source=_mcp_source,
                requester_id=_mcp_requester_id,
                resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
            )
                return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
            
            # Canonical AI Chat Messages queries (ai_chat_messages table - all sources)
            if query == "canonical_messages_per_day":
                result = _query_messages_per_day_db(db_conn, dataset_id, "ai_chat_messages")
                record_mcp_request(
                db_conn,
                "get_analytics",
                source=_mcp_source,
                requester_id=_mcp_requester_id,
                resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
            )
                return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
            if query == "canonical_total_messages":
                result = _query_total_messages_db(db_conn, dataset_id, "ai_chat_messages")
                record_mcp_request(
                db_conn,
                "get_analytics",
                source=_mcp_source,
                requester_id=_mcp_requester_id,
                resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
            )
                return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
            if query == "canonical_avg_message_length":
                result = _query_avg_message_length_db(db_conn, dataset_id, "ai_chat_messages")
                record_mcp_request(
                db_conn,
                "get_analytics",
                source=_mcp_source,
                requester_id=_mcp_requester_id,
                resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
            )
                return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
            if query == "canonical_messages_by_sender":
                result = _query_messages_by_sender_db(db_conn, dataset_id, "ai_chat_messages")
                record_mcp_request(
                db_conn,
                "get_analytics",
                source=_mcp_source,
                requester_id=_mcp_requester_id,
                resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
            )
                return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
            
            # ChatGPT ingestion queries - try ai_chat_messages first, then chatgpt_messages (for backward compatibility)
            if query == "chatgpt_messages_per_day":
                result = _query_messages_per_day_db(db_conn, dataset_id, "ai_chat_messages", source_filter="chatgpt")
                if not result:
                    result = _query_messages_per_day_db(db_conn, dataset_id, "chatgpt_messages")
                record_mcp_request(
                db_conn,
                "get_analytics",
                source=_mcp_source,
                requester_id=_mcp_requester_id,
                resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
            )
                return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
            if query == "chatgpt_total_messages":
                result = _query_total_messages_db(db_conn, dataset_id, "ai_chat_messages", source_filter="chatgpt")
                if result.get("total_messages", 0) == 0:
                    result = _query_total_messages_db(db_conn, dataset_id, "chatgpt_messages")
                record_mcp_request(
                db_conn,
                "get_analytics",
                source=_mcp_source,
                requester_id=_mcp_requester_id,
                resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
            )
                return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
            if query == "chatgpt_avg_message_length":
                result = _query_avg_message_length_db(db_conn, dataset_id, "ai_chat_messages", source_filter="chatgpt")
                if result.get("avg_length", 0) == 0:
                    result = _query_avg_message_length_db(db_conn, dataset_id, "chatgpt_messages")
                record_mcp_request(
                db_conn,
                "get_analytics",
                source=_mcp_source,
                requester_id=_mcp_requester_id,
                resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
            )
                return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
            if query == "chatgpt_messages_by_sender":
                result = _query_messages_by_sender_db(db_conn, dataset_id, "ai_chat_messages", source_filter="chatgpt")
                if not result:
                    result = _query_messages_by_sender_db(db_conn, dataset_id, "chatgpt_messages")
                record_mcp_request(
                db_conn,
                "get_analytics",
                source=_mcp_source,
                requester_id=_mcp_requester_id,
                resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
            )
                return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
            
            # Combined queries (messages + ai_chat_messages/chatgpt_messages)
            if query == "combined_messages_per_day":
                result = _query_combined_messages_per_day(db_conn, dataset_id)
                record_mcp_request(
                db_conn,
                "get_analytics",
                source=_mcp_source,
                requester_id=_mcp_requester_id,
                resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
            )
                return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
            if query == "combined_total_messages":
                result = _query_combined_total_messages(db_conn, dataset_id)
                record_mcp_request(
                db_conn,
                "get_analytics",
                source=_mcp_source,
                requester_id=_mcp_requester_id,
                resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
            )
                return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
            if query == "combined_avg_message_length":
                result = _query_combined_avg_message_length(db_conn, dataset_id)
                record_mcp_request(
                db_conn,
                "get_analytics",
                source=_mcp_source,
                requester_id=_mcp_requester_id,
                resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
            )
                return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
            if query == "combined_messages_by_sender":
                result = _query_combined_messages_by_sender(db_conn, dataset_id)
                record_mcp_request(
                db_conn,
                "get_analytics",
                source=_mcp_source,
                requester_id=_mcp_requester_id,
                resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
            )
                return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
        
        # JSONL queries (raw file store) - only handle explicit jsonl_* queries
        if query.startswith("jsonl_"):
            messages = load_raw_messages(
                dataset_id=dataset_id or "",
                schema_id="chatgpt.conversation.v1",
                limit=None,
                offset=0,
            )
            logger.debug(
                "[PIPELINE:ANALYTICS] Loaded %d messages from raw store for query: %s",
                len(messages),
                query,
            )
            if query == "jsonl_messages_per_day":
                result = messages_per_day(messages)
            elif query == "jsonl_total_messages":
                result = total_messages(messages)
            elif query == "jsonl_avg_message_length":
                result = avg_message_length(messages)
            elif query == "jsonl_messages_by_sender":
                result = messages_by_sender(messages)
            else:
                result = []
            logger.debug(
                "[PIPELINE:ANALYTICS] Query result: query=%s, result_size=%s",
                query,
                len(result) if isinstance(result, list) else 1,
            )
            record_mcp_request(
                db_conn,
                "get_analytics",
                source=_mcp_source,
                requester_id=_mcp_requester_id,
                resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
            )
            return {"id": req_id, "status": "ok", "payload": {"query": query, "result": result}}
        
        # Unknown query - return empty result
        logger.warning("[PIPELINE:ANALYTICS] Unknown query: %s", query)
        record_mcp_request(
                db_conn,
                "get_analytics",
                source=_mcp_source,
                requester_id=_mcp_requester_id,
                resource_owner_user_id=_resource_owner_for_mcp_log(db_conn),
            )
        return {"id": req_id, "status": "ok", "payload": {"query": query, "result": []}}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[PIPELINE:ANALYTICS] Query error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}
