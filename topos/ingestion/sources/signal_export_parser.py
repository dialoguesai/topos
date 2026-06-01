"""Parse Signal export files (JSON) into normalized records for conversation_messages.

Supported format: JSON array of message objects. Each object may have:
- conversationId or conversation_id
- body or content
- sent_at (ms or sec) or created_at
- type: "outgoing" | "incoming" (for from_self)
- source or sender (phone number for identity matching)

message_id is stable: signal_import:{conversation_id}:{sent_at}:{content_hash} for idempotent re-upload.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("topos.ingestion.sources.signal_export_parser")


def _norm_ts(sent_at: Any) -> str:
    """Normalize sent_at (ms or sec) to ISO ts string."""
    if sent_at is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(sent_at, str):
        return sent_at
    if isinstance(sent_at, (int, float)):
        if sent_at > 1e12:  # milliseconds
            sent_at = sent_at / 1000.0
        return datetime.fromtimestamp(sent_at, tz=timezone.utc).isoformat()
    return str(sent_at)


def _stable_message_id(conversation_id: str, sent_at: Any, content: str) -> str:
    """Stable id for idempotent upsert."""
    raw = f"{conversation_id}:{sent_at}:{content}"
    h = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
    ts = int(sent_at) if isinstance(sent_at, (int, float)) else 0
    if ts > 1e12:
        ts = int(ts / 1000)
    return f"signal_import:{conversation_id}:{ts}:{h}"


def parse_signal_export_json(
    data: bytes | str,
    *,
    my_phone_number: Optional[str] = None,
    owner_user_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Parse JSON export (array of message objects) into staging records for ConversationsTablesManager.
    Each record has: message_id, conversation_id, ts, sender_type (self|contact), content, source_id=signal,
    from_self, owner_user_id (if identity provided).
    """
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    try:
        arr = json.loads(data)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}") from e
    if not isinstance(arr, list):
        arr = [arr]
    records: List[Dict[str, Any]] = []
    for i, obj in enumerate(arr):
        if not isinstance(obj, dict):
            continue
        conv_id = str(obj.get("conversationId") or obj.get("conversation_id") or f"conv_{i}")
        body = obj.get("body") or obj.get("content") or ""
        sent_at = obj.get("sent_at") or obj.get("created_at") or obj.get("date")
        msg_type = (obj.get("type") or "").lower()
        source_phone = obj.get("source") or obj.get("sender") or obj.get("sender_phone")
        if isinstance(source_phone, dict):
            source_phone = source_phone.get("number") or source_phone.get("phone")
        source_phone = str(source_phone).strip() if source_phone else None

        from_self = msg_type == "outgoing"
        if my_phone_number and source_phone:
            norm_phone = my_phone_number.replace(" ", "").replace("-", "").strip()
            norm_source = (source_phone or "").replace(" ", "").replace("-", "").strip()
            if norm_phone and norm_source and norm_phone in norm_source or norm_source in norm_phone:
                from_self = True
            elif msg_type == "outgoing":
                from_self = True
        sender_type = "self" if from_self else "contact"
        message_type = "system" if msg_type and msg_type not in {"outgoing", "incoming"} else "message"
        event_type = f"signal_type:{msg_type}" if message_type == "system" else None
        reply_to_message_id = (
            obj.get("quoteId")
            or obj.get("quotedMessageId")
            or obj.get("replyToMessageId")
            or obj.get("reply_to_message_id")
        )

        message_id = _stable_message_id(conv_id, sent_at, body)
        ts = _norm_ts(sent_at)
        content = body
        if not content and message_type == "system":
            content = f"[system_event:{msg_type}]"
        rec = {
            "message_id": message_id,
            "conversation_id": conv_id,
            "thread_id": conv_id,
            "ts": ts,
            "sender_type": sender_type,
            "content": content,
            "source_id": "signal",
            "from_self": from_self,
            "sender_id": source_phone,
            "message_type": message_type,
            "event_type": event_type,
        }
        if reply_to_message_id is not None:
            rec["reply_to_message_id"] = str(reply_to_message_id)
        metadata = {}
        for key in (
            "quoteId", "quotedMessageId", "replyToMessageId", "reply_to_message_id",
            "quoteAuthorAci", "quoteAuthorUuid", "quoteAuthor", "quoteText", "quoteBody",
            "storyReplyContext", "groupV2Change", "groupUpdate", "groupChange",
            "callId", "callHistoryDetails", "expiresTimer", "expirationStartTimestamp",
            "isErased", "isViewOnce", "isStory",
        ):
            if key in obj and obj.get(key) is not None:
                metadata[key] = obj.get(key)
        if metadata:
            rec["_metadata"] = metadata
        if owner_user_id:
            rec["owner_user_id"] = owner_user_id
        records.append(rec)
    return records
