"""Bounded owner-attested ChatGPT export snapshot ingestion; never a live reader.

``chatgpt-owner-snapshot/v1`` reads one ``conversations.json`` export that the
owner attested is their own account. It is deliberately narrower than
``parsers/chatgpt_export.py``, which decides inclusion for the legacy import:
this reader decides which rows may later count as the owner's own words, so it
drops or withholds anything it cannot prove and rejects anything malformed.

- Only the active branch (``current_node`` ancestry) is read. Regenerations and
  edited-away prompts are the export's history, not the conversation.
- A ``user`` node becomes an owner prompt (``sender_type`` "human") only when it
  is ordinary visible text: ``text`` or the text parts of ``multimodal_text``,
  no author name, message metadata inside a closed allow-list and no set message
  or node field an ordinary export does not carry. Hidden and
  custom-instruction nodes, canvas, automation and scheduled-task runs, GPT
  starters, targeted replies and any metadata this reader does not know are
  dropped, never guessed to be typed.
- ``assistant`` text is kept as ``sender_type`` "assistant" so a conversation
  stays readable; it is never owner speech. System, tool, hidden and non-text
  nodes are dropped.
- A conversation in which ANY user node, on any branch, names an author, carries
  author metadata or a participant/shared-link marker is withheld whole: a group
  chat or a continued shared link cannot show which prompts the owner typed.
- Event time is the export's ``create_time`` in seconds since the Unix epoch,
  read as an exact decimal. Only microsecond-representable instants on or after
  2022 are accepted, never in the future and never out of order along the
  branch; there is no unit guessing and no ingestion-time substitute.

It runs no enrichment and no fact pass. Durable authority is supplied only by
IngestProvenanceService, never by the snapshot's own fields.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import json
import re
from typing import Any, Callable, Dict, List

from ..permissions_v2.ingest_protocol import CHATGPT_READER_CONTRACT, CHATGPT_SOURCE_ID

READER_CONTRACT = CHATGPT_READER_CONTRACT
SOURCE_ID = CHATGPT_SOURCE_ID
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_CONVERSATIONS = 1000
MAX_NODES = 20000
MAX_MESSAGES = 1000
MAX_TEXT_BYTES = 64 * 1024
MAX_TOTAL_TEXT_BYTES = 1024 * 1024
MAX_TITLE_BYTES = 1024
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
# Seconds only: ChatGPT did not exist before 2022, and a millisecond value read as
# seconds is past any representable date, so neither unit is mistaken for the other.
_EARLIEST = Decimal(int((datetime(2022, 1, 1, tzinfo=timezone.utc) - _EPOCH).total_seconds()))
# Export ids become part of canonical ids (``<source>:<conversation>:<node>``).
_EXPORT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_ROLES = frozenset({"user", "assistant", "system", "tool"})
_TEXT_TYPES = frozenset({"text", "multimodal_text"})
# The only message metadata an owner prompt may carry. Anything else, including
# canvas, automation/scheduled-task, starter-prompt and targeted-reply keys, means
# the text was not simply typed into this conversation by the account owner.
_OWNER_METADATA = frozenset({
    "request_id", "timestamp_", "message_type", "model_slug", "default_model_slug", "requested_model_slug",
    "parent_id", "serialization_metadata", "dictation", "attachments", "system_hints", "selected_sources",
    "selected_github_repos", "voice_mode_message", "real_time_audio_has_video",
})
# Present on ordinary prompts too; only a false value is ordinary.
_FALSE_ONLY_METADATA = frozenset({"is_visually_hidden_from_conversation", "is_user_system_message"})
# The fields an ordinary export's message and mapping node carry. A group chat must
# record each sender somewhere, so a set field outside these drops an owner prompt
# exactly as unknown metadata does.
_MESSAGE_FIELDS = frozenset({"id", "author", "create_time", "update_time", "content", "status", "end_turn", "weight",
                             "metadata", "recipient", "channel"})
_NODE_FIELDS = frozenset({"id", "message", "parent", "children"})
# Group chat or shared-link continuation: withholds the whole conversation.
_PARTICIPANT_MARKERS = frozenset({
    "participants", "participant_ids", "participant_id", "group_chat", "group_chat_id", "is_group_chat",
    "is_multiplayer", "shared_conversation_id", "share_id", "is_shared", "conversation_origin", "sender_user_id",
})


class SnapshotRejected(ValueError):
    """Content-free reason from the closed reader contract."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _reject(code: str) -> None:
    raise SnapshotRejected(code)


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _no_constant(_name):
    raise ValueError("non-finite number")


def _set(value: Any) -> bool:
    return value not in (None, False, "", 0, [], {})


def _export_id(value: Any) -> str:
    if type(value) is not str or _EXPORT_ID.fullmatch(value) is None:
        _reject("snapshot_identity_invalid")
    return value


def _event_time(value: Any, now: datetime) -> str:
    # ``type`` rather than isinstance: a JSON true is not a time.
    if type(value) not in (int, Decimal):
        _reject("snapshot_time_unsupported")
    try:
        micros = Decimal(value) * 1_000_000
        if value < _EARLIEST or micros != micros.to_integral_value():
            _reject("snapshot_time_unsupported")
        event = _EPOCH + timedelta(microseconds=int(micros))
    except (InvalidOperation, OverflowError, ValueError):
        _reject("snapshot_time_unsupported")
    if event > now:
        _reject("snapshot_time_future")
    return event.isoformat(timespec="microseconds")


def _text(content: Any) -> str | None:
    """The turn's visible text; ``None`` means "not a text turn", never an error."""
    if type(content) is not dict or type(content.get("content_type")) is not str:
        _reject("snapshot_schema_unsupported")
    content_type = content["content_type"]
    if content_type not in _TEXT_TYPES:
        return None
    parts = content.get("parts")
    if type(parts) is not list:
        _reject("snapshot_schema_unsupported")
    texts = []
    for part in parts:
        if type(part) is str:
            if part.strip():
                texts.append(part)
        elif content_type != "multimodal_text" or type(part) is not dict:
            _reject("snapshot_schema_unsupported")
    text = "\n\n".join(texts)
    if "\x00" in text:
        _reject("snapshot_text_unsupported")
    return text if text.strip() else None


def _participants(conversation: Dict[str, Any], mapping: Dict[str, Any]) -> bool:
    """True when any user node, on any branch, cannot be the single account owner."""
    if any(_set(conversation.get(key)) for key in _PARTICIPANT_MARKERS):
        return True
    for node in mapping.values():
        message = node.get("message")
        if message is None or message["author"]["role"] != "user":
            continue
        author, metadata = message["author"], message.get("metadata")
        if (set(author) - {"role", "name", "metadata"} or author.get("name") is not None
                or author.get("metadata") not in (None, {})):
            return True
        if type(metadata) is dict and any(_set(metadata.get(key)) for key in _PARTICIPANT_MARKERS):
            return True
    return False


def _visible(message: Dict[str, Any]) -> bool:
    metadata = message.get("metadata")
    if metadata is not None and type(metadata) is not dict:
        _reject("snapshot_schema_unsupported")
    metadata = metadata or {}
    status, weight, recipient = message.get("status"), message.get("weight"), message.get("recipient")
    return (not _set(metadata.get("is_visually_hidden_from_conversation")) and status in (None, "finished_successfully")
            and (weight is None or (type(weight) in (int, Decimal) and weight == 1)) and recipient in (None, "all"))


def _owner_prompt(node: Dict[str, Any]) -> bool:
    message = node["message"]
    if any(_set(value) for key, value in node.items() if key not in _NODE_FIELDS) or \
            any(_set(value) for key, value in message.items() if key not in _MESSAGE_FIELDS):
        return False
    metadata = message.get("metadata") or {}
    for key, value in metadata.items():
        if key in _FALSE_ONLY_METADATA:
            if _set(value):
                return False
        elif key not in _OWNER_METADATA:
            return False
    return True


def _active_path(mapping: Dict[str, Any], current: Any) -> List[str]:
    if type(current) is not str or current not in mapping:
        _reject("snapshot_branch_unresolved")
    path, seen, cursor = [], set(), current
    while cursor is not None:
        if cursor in seen:
            _reject("snapshot_branch_unresolved")
        seen.add(cursor)
        path.append(cursor)
        parent = mapping[cursor]["parent"]
        if parent is not None and cursor not in mapping[parent]["children"]:
            _reject("snapshot_branch_unresolved")
        cursor = parent
    path.reverse()
    return path


def parse_chatgpt_snapshot(data: bytes, *, now: datetime) -> Dict[str, Any]:
    """Parse immutable export bytes. Returned staging is not authority.

    Returns ``{"conversations", "messages", "withheld_conversations"}``. Any
    malformed or out-of-contract structure rejects the entire snapshot before a
    canonical write.
    """
    if type(data) is not bytes or not 0 < len(data) <= MAX_SNAPSHOT_BYTES:
        _reject("snapshot_size_unsupported")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() != timedelta(0):
        _reject("snapshot_clock_invalid")
    try:
        export = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys, parse_float=Decimal,
                            parse_constant=_no_constant)
    except (UnicodeError, ValueError, RecursionError):
        _reject("snapshot_json_invalid")
    if type(export) is not list:
        _reject("snapshot_schema_unsupported")
    if len(export) > MAX_CONVERSATIONS:
        _reject("snapshot_conversation_limit")
    conversations: List[Dict[str, Any]] = []
    messages: List[Dict[str, Any]] = []
    export_ids, withheld, nodes_read, text_bytes = set(), 0, 0, 0
    for conversation in export:
        if type(conversation) is not dict or type(conversation.get("mapping")) is not dict or not conversation["mapping"]:
            _reject("snapshot_schema_unsupported")
        ids = {conversation[key] for key in ("conversation_id", "id") if conversation.get(key) is not None}
        if len(ids) != 1:
            _reject("snapshot_identity_invalid")
        export_id = _export_id(next(iter(ids)))
        if export_id in export_ids:
            _reject("snapshot_conversation_ambiguous")
        export_ids.add(export_id)
        title = conversation.get("title")
        if title is not None and (type(title) is not str or "\x00" in title or len(title.encode("utf-8")) > MAX_TITLE_BYTES):
            _reject("snapshot_schema_unsupported")
        mapping = conversation["mapping"]
        nodes_read += len(mapping)
        if nodes_read > MAX_NODES:
            _reject("snapshot_message_limit")
        for node_id, node in mapping.items():
            _export_id(node_id)
            if type(node) is not dict or node.get("id") != node_id:
                _reject("snapshot_identity_invalid")
            parent, children, message = node.get("parent"), node.get("children"), node.get("message")
            if (parent is not None and (type(parent) is not str or parent not in mapping or parent == node_id)) or \
                    type(children) is not list or any(type(child) is not str or child not in mapping for child in children):
                _reject("snapshot_branch_unresolved")
            if message is None:
                continue
            if type(message) is not dict or message.get("id") != node_id:
                _reject("snapshot_identity_invalid")
            author = message.get("author")
            if type(author) is not dict or author.get("role") not in _ROLES:
                _reject("snapshot_author_unsupported")
        if _participants(conversation, mapping):
            withheld += 1
            continue
        conversation_id = f"{SOURCE_ID}:{export_id}"
        rows: List[Dict[str, Any]] = []
        for node_id in _active_path(mapping, conversation.get("current_node")):
            message = mapping[node_id]["message"]
            if message is None or message["author"]["role"] not in ("user", "assistant") or not _visible(message):
                continue
            text = _text(message.get("content"))
            if text is None:
                continue
            owner = message["author"]["role"] == "user"
            if owner and not _owner_prompt(mapping[node_id]):
                continue
            event_at = _event_time(message.get("create_time"), now)
            if rows and event_at < rows[-1]["event_at"]:
                _reject("snapshot_time_order_invalid")
            size = len(text.encode("utf-8"))
            text_bytes += size
            if size > MAX_TEXT_BYTES or text_bytes > MAX_TOTAL_TEXT_BYTES:
                _reject("snapshot_text_limit")
            rows.append({
                "message_id": f"{conversation_id}:{node_id}", "conversation_id": conversation_id,
                "source_record_id": f"{export_id}:{node_id}", "sender_type": "human" if owner else "assistant",
                "sender_id": "self" if owner else "assistant", "actor_role": "authored" if owner else "addressed",
                "event_at": event_at, "content": text, "sequence": len(rows),
            })
        if not rows:
            continue
        if len(messages) + len(rows) > MAX_MESSAGES:
            _reject("snapshot_message_limit")
        messages.extend(rows)
        conversations.append({"conversation_id": conversation_id, "source_record_id": export_id, "title": title,
                              "created_at": rows[0]["event_at"], "updated_at": rows[-1]["event_at"]})
    return {"conversations": conversations, "messages": messages, "withheld_conversations": withheld}


_MESSAGE_COLUMNS = ("message_id", "conversation_id", "sender_type", "sender_id", "event_at", "content", "metadata_json",
                    "sequence", "source_id", "source_record_id", "actor_role", "ingested_at")
_CONVERSATION_COLUMNS = ("conversation_id", "owner_user_id", "title", "source_id", "created_at", "updated_at",
                         "source_record_id", "ingested_at")


def write_trusted_ai_chat_batch(conn, parsed: Dict[str, Any], *, trusted_context: Any) -> Dict[str, int]:
    """Insert one parsed export inside its job's caller-owned batch transaction.

    Never the legacy store: its upsert replaces bodies and commits. Every
    conversation and message is new, or the whole batch is refused: lane ids are
    namespaced, so an existing row is another writer's or another enrollment's.
    """
    from ..permissions_v2.canonical import PolicyError
    from ..permissions_v2.ingest_provenance import IngestProvenanceService, VerifiedIngestContext

    if type(trusted_context) is not VerifiedIngestContext or type(trusted_context.service) is not IngestProvenanceService:
        raise PolicyError("ingest_canonical_context_required")
    trusted_context.require_batch(conn)
    trusted_context.assert_current(conn, source_id=SOURCE_ID, dataset_id=trusted_context.dataset_id)
    if trusted_context.table != "ai_chat_messages" or type(parsed) is not dict:
        raise PolicyError("ingest_canonical_invalid")
    conversations, messages = parsed.get("conversations"), parsed.get("messages")
    if type(conversations) is not list or type(messages) is not list or len(messages) > MAX_MESSAGES:
        raise PolicyError("ingest_canonical_invalid")
    # The enrolled node has initialized canonical schemas; this writer never migrates.
    for table, columns in (("ai_chat_messages", _MESSAGE_COLUMNS), ("ai_chat_conversations", _CONVERSATION_COLUMNS)):
        if not set(columns) <= {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
            raise PolicyError("ingest_canonical_schema_unsupported")
    parents = {item["conversation_id"] for item in conversations}
    if len(parents) != len(conversations) or len({item["message_id"] for item in messages}) != len(messages):
        raise PolicyError("ingest_canonical_collision")
    for item in conversations:
        if conn.execute("SELECT 1 FROM ai_chat_conversations WHERE conversation_id=?", (item["conversation_id"],)).fetchone():
            raise PolicyError("ingest_canonical_collision")
    for item in messages:
        if item["conversation_id"] not in parents or conn.execute(
                "SELECT 1 FROM ai_chat_messages WHERE message_id=?", (item["message_id"],)).fetchone():
            raise PolicyError("ingest_canonical_collision")
    # This marker only locates the durable proof; readers validate that proof
    # and its current enrollment, never this JSON by itself.
    metadata_json = json.dumps({"topos_owner_ingest": {"version": "owner-attested-snapshot/v1",
        "enrollment_id": trusted_context.enrollment_id, "job_id": trusted_context.job_id}}, sort_keys=True)
    trusted_context.require_batch(conn)
    trusted_context.assert_current(conn, source_id=SOURCE_ID, dataset_id=trusted_context.dataset_id)
    now = datetime.now(timezone.utc).isoformat()
    for item in conversations:
        values = {**item, "owner_user_id": trusted_context.owner_id, "source_id": SOURCE_ID, "ingested_at": now}
        conn.execute(f"INSERT INTO ai_chat_conversations ({','.join(_CONVERSATION_COLUMNS)}) VALUES "
                     f"({','.join('?' for _ in _CONVERSATION_COLUMNS)})", [values[key] for key in _CONVERSATION_COLUMNS])
    for item in messages:
        values = {**item, "metadata_json": metadata_json, "source_id": SOURCE_ID, "ingested_at": now}
        conn.execute(f"INSERT INTO ai_chat_messages ({','.join(_MESSAGE_COLUMNS)}) VALUES "
                     f"({','.join('?' for _ in _MESSAGE_COLUMNS)})", [values[key] for key in _MESSAGE_COLUMNS])
        trusted_context.record_insert(conn, item["message_id"])
    return {"messages_created": len(messages), "conversations_created": len(conversations), "historical_skipped": 0}


async def run_chatgpt_snapshot_job(service: Any, conn_factory: Callable[[], Any], job_id: str) -> Dict[str, Any]:
    """Run one separately enrolled ChatGPT job, exactly as the iMessage runner runs its own.

    Claim and failure receipts use service CAS; canonical writes, links and
    completion share ``ctx.batch``, so a completion failure rolls back every row.
    """
    def run() -> Dict[str, Any]:
        conn, context = None, None
        try:
            conn = conn_factory()
            if conn is None:
                _reject("snapshot_database_unavailable")
            context = service.claim(conn, job_id, source_id=SOURCE_ID)
            data = service.snapshot_bytes(conn, context)
            service.assert_current(conn, context, source_id=SOURCE_ID, dataset_id=context.dataset_id)
            parsed = parse_chatgpt_snapshot(data, now=datetime.now(timezone.utc))
            with context.batch(conn):
                service.assert_current(conn, context, source_id=SOURCE_ID, dataset_id=context.dataset_id)
                counts = write_trusted_ai_chat_batch(conn, parsed, trusted_context=context)
                processed = len(parsed["messages"])
                result: Dict[str, Any] = {"status": "ok", "messages_processed": processed}
                for field in ("messages_created", "conversations_created", "historical_skipped"):
                    value = counts.get(field)
                    if type(value) is not int or not 0 <= value <= processed:
                        _reject("snapshot_result_invalid")
                    result[field] = value
                service.finish(conn, context, result)
            return result
        except Exception as error:
            reason = error.reason_code if isinstance(error, SnapshotRejected) else "snapshot_job_unavailable"
            if conn is not None and context is not None:
                try:
                    service.fail(conn, context, reason)
                except Exception:
                    # A stale worker must not overwrite the newer claim's state.
                    pass
            return {"status": "error", "reason_code": reason}
        finally:
            if conn is not None:
                conn.close()

    return await asyncio.to_thread(run)
