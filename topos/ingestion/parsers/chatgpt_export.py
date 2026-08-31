"""ChatGPT export reader: conversation tree → owner-attributed turn records.

Replaces ``chatgpt_conversation_flattener`` (PLAN_CHATGPT_IMPORT.md §1). That
reader emitted one record per *node in the mapping tree*, which on a real export
means 35% blank rows, the model's chain-of-thought stored as speech, and every
abandoned regeneration kept beside the turn that replaced it.

Three ideas separate this module from the old one:

1. **A turn is not a node.** ChatGPT's ``mapping`` is the full edit history: every
   regenerated answer and every superseded prompt is still in there. The
   conversation the user actually had is the ancestry of ``current_node``.
2. **Extraction correctness and inclusion policy are different questions.**
   ``extract_content`` reads every content type from the field the export
   actually writes — including the three the old reader got wrong. What is then
   allowed to *become a turn* is a separate, reported decision.
3. **Nothing is dropped silently.** Every exclusion increments a
   :class:`DropLedger` reason, so an import can state what it left behind.

Pure stdlib: no engine imports, no I/O. The ingestion parser and the report
script both call it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

logger = logging.getLogger("topos.ingestion.parser.chatgpt_export")

# Content types that carry something a human said or was shown as prose.
TURN_CONTENT_TYPES = frozenset({"text", "multimodal_text"})
# Content types that are a tool being driven, not speech. Extractable, and
# promoted to turns only under ``include_tool_output``.
TOOL_CONTENT_TYPES = frozenset({"code", "execution_output"})
# Model scaffolding. Never a turn under any option: chain-of-thought summaries
# and "Thought for 9 seconds" are not addressed to anyone.
SCAFFOLD_CONTENT_TYPES = frozenset({"thoughts", "reasoning_recap"})

OWNER_ROLE = "user"
ASSISTANT_ROLE = "assistant"

BRANCH_ACTIVE = "active"
BRANCH_ALTERNATE = "alternate"
# No usable ``current_node``: every node is treated as on-path rather than
# silently dropping the conversation.
BRANCH_UNKNOWN = "unknown"

# Drop reasons (stable strings — the report and the UI receipt key off them).
# Every message node in a kept conversation is either emitted or counted under
# exactly one node reason, so ``message_nodes == turns_emitted +
# dropped_nodes_total`` is a checkable invariant.
DROP_ALTERNATE_BRANCH = "alternate_branch"
DROP_SYSTEM = "system_message"
DROP_HIDDEN = "hidden_from_conversation"
DROP_SCAFFOLD = "model_scaffolding"
DROP_TOOL_OUTPUT = "tool_output"
DROP_EMPTY = "empty_content"
DROP_OUT_OF_WINDOW = "outside_date_window"
DROP_DO_NOT_REMEMBER = "marked_do_not_remember"

# Drops decided for a whole conversation before any node is read. Counted apart
# from node drops so ``message_nodes == turns_emitted + dropped_nodes`` stays a
# checkable invariant rather than mixing two units in one total.
CONVERSATION_DROP_REASONS = frozenset({DROP_OUT_OF_WINDOW, DROP_DO_NOT_REMEMBER})
DROP_UNKNOWN_ROLE = "unknown_role"


@dataclass(frozen=True)
class ExportOptions:
    """Ingest-time policy for one import.

    ``date_from``/``date_to`` are epoch seconds, both inclusive, and are applied
    at *conversation* granularity: a conversation whose last activity falls in
    the window comes in whole. Filtering individual messages would tear a thread
    in half and leave answers without their questions.
    """

    date_from: Optional[float] = None
    date_to: Optional[float] = None
    include_alternate_branches: bool = False
    include_tool_output: bool = False
    include_system: bool = False
    # ChatGPT lets a user mark a chat as not-to-be-remembered. Honouring that
    # declaration is the default; it is the user's own instruction about their
    # own data, and it costs nothing to read.
    respect_do_not_remember: bool = True

    @classmethod
    def from_payload(cls, payload: Optional[Dict[str, Any]]) -> "ExportOptions":
        """Build from the ``ingest_options`` blob carried on the job payload."""
        if not isinstance(payload, dict):
            return cls()
        return cls(
            date_from=_as_epoch(payload.get("date_from")),
            date_to=_as_epoch(payload.get("date_to")),
            include_alternate_branches=bool(payload.get("include_alternate_branches")),
            include_tool_output=bool(payload.get("include_tool_output")),
            include_system=bool(payload.get("include_system")),
            respect_do_not_remember=bool(payload.get("respect_do_not_remember", True)),
        )


@dataclass
class DropLedger:
    """Why records did not become turns, and how much was read."""

    dropped: Dict[str, int] = field(default_factory=dict)
    conversations_seen: int = 0
    conversations_kept: int = 0
    message_nodes: int = 0
    turns_emitted: int = 0

    def drop(self, reason: str, count: int = 1) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + count

    def as_dict(self) -> Dict[str, Any]:
        node_drops = {k: v for k, v in self.dropped.items() if k not in CONVERSATION_DROP_REASONS}
        conversation_drops = {k: v for k, v in self.dropped.items() if k in CONVERSATION_DROP_REASONS}
        return {
            "conversations_seen": self.conversations_seen,
            "conversations_kept": self.conversations_kept,
            "message_nodes": self.message_nodes,
            "turns_emitted": self.turns_emitted,
            "dropped": dict(sorted(self.dropped.items())),
            "dropped_nodes": dict(sorted(node_drops.items())),
            "dropped_conversations": dict(sorted(conversation_drops.items())),
            "dropped_nodes_total": sum(node_drops.values()),
            "dropped_conversations_total": sum(conversation_drops.values()),
        }


def _as_epoch(value: Any) -> Optional[float]:
    """Accept epoch seconds, epoch millis, or an ISO-8601 date/datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # Anything past ~2001 in millis is far beyond any plausible epoch-second
        # timestamp, so the magnitude is a safe discriminator.
        return float(value) / 1000.0 if float(value) > 1e11 else float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text) if text.replace(".", "", 1).isdigit() else _iso_to_epoch(text)
    except (TypeError, ValueError):
        logger.warning("Unparseable date bound in ingest options: %r", value)
        return None


def _iso_to_epoch(text: str) -> float:
    from datetime import datetime, timezone

    normalized = text.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# ---------------------------------------------------------------------------
# Content extraction
# ---------------------------------------------------------------------------


def extract_content(content_obj: Any) -> Tuple[str, List[Dict[str, Any]]]:
    """Return ``(text, assets)`` for one message content object.

    Each branch reads the field the export actually writes. The reader this
    replaces asked for ``reasoning_recap``, ``code`` and ``output``; the export
    writes ``content``, ``text`` and ``text``, so all three returned "" and were
    stored as blank rows.
    """
    if not isinstance(content_obj, dict):
        return "", []
    content_type = str(content_obj.get("content_type") or "text")

    if content_type == "text":
        return _join_parts(content_obj.get("parts")), []

    if content_type == "multimodal_text":
        return _join_parts(content_obj.get("parts")), _assets_from_parts(content_obj.get("parts"))

    if content_type == "code":
        code = _as_text(content_obj.get("text"))
        if not code:
            return _join_parts(content_obj.get("parts")), []
        language = str(content_obj.get("language") or "").strip()
        fence = language if language and language != "unknown" else ""
        return f"```{fence}\n{code}\n```", []

    if content_type == "execution_output":
        return _as_text(content_obj.get("text")) or _join_parts(content_obj.get("parts")), []

    if content_type == "reasoning_recap":
        return _as_text(content_obj.get("content")) or _join_parts(content_obj.get("parts")), []

    if content_type == "thoughts":
        thoughts = content_obj.get("thoughts")
        if isinstance(thoughts, list):
            chunks: List[str] = []
            for thought in thoughts:
                if isinstance(thought, dict):
                    # summary and content are different fields; the old reader
                    # took one *or* the other and duplicated the summary.
                    body = _as_text(thought.get("content")) or _as_text(thought.get("summary"))
                    if body:
                        chunks.append(body)
                elif isinstance(thought, str) and thought.strip():
                    chunks.append(thought.strip())
            return "\n\n".join(chunks), []
        return _as_text(thoughts), []

    # Unknown/new content type: try the generic shapes rather than guessing.
    for key in ("text", "content", "parts"):
        if key in content_obj:
            value = content_obj[key]
            text = _join_parts(value) if isinstance(value, list) else _as_text(value)
            if text:
                logger.debug("Unknown content_type %r read via %r", content_type, key)
                return text, []
    return "", []


def _as_text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    text = str(value)
    return text if text.strip() else ""


def _join_parts(parts: Any) -> str:
    if isinstance(parts, str):
        return parts if parts.strip() else ""
    if not isinstance(parts, list):
        return ""
    chunks = [part.strip() for part in parts if isinstance(part, str) and part.strip()]
    return "\n\n".join(chunks)


def _assets_from_parts(parts: Any) -> List[Dict[str, Any]]:
    """Non-text multimodal parts: image/audio pointers the message carried."""
    if not isinstance(parts, list):
        return []
    assets: List[Dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        asset: Dict[str, Any] = {
            "kind": str(part.get("content_type") or "asset"),
            "pointer": str(part.get("asset_pointer") or ""),
        }
        for key in ("width", "height", "size_bytes"):
            if part.get(key) is not None:
                asset[key] = part[key]
        assets.append(asset)
    return assets


# ---------------------------------------------------------------------------
# Tree traversal
# ---------------------------------------------------------------------------


def active_path_ids(conversation: Dict[str, Any]) -> Optional[List[str]]:
    """Node ids from root to ``current_node``, or None when unresolvable.

    None (not an empty list) is the "no usable pointer" signal, so the caller
    can fall back to the whole tree instead of dropping the conversation.
    """
    mapping = conversation.get("mapping")
    if not isinstance(mapping, dict) or not mapping:
        return None
    cursor = conversation.get("current_node")
    if not isinstance(cursor, str) or cursor not in mapping:
        return None
    path: List[str] = []
    seen: set[str] = set()
    while isinstance(cursor, str) and cursor in mapping and cursor not in seen:
        seen.add(cursor)
        path.append(cursor)
        cursor = mapping[cursor].get("parent")
    path.reverse()
    return path


def _ordered_node_ids(conversation: Dict[str, Any]) -> List[str]:
    """Depth-first order over the whole tree, roots first — stable per file."""
    mapping = conversation.get("mapping") or {}
    roots = [
        node_id
        for node_id, node in mapping.items()
        if not isinstance(node, dict) or not node.get("parent") or node.get("parent") not in mapping
    ]
    ordered: List[str] = []
    seen: set[str] = set()

    def walk(node_id: str) -> None:
        if node_id in seen or node_id not in mapping:
            return
        seen.add(node_id)
        ordered.append(node_id)
        node = mapping.get(node_id)
        children = node.get("children") if isinstance(node, dict) else None
        for child in children or []:
            if isinstance(child, str):
                walk(child)

    for root in roots:
        walk(root)
    # Orphans whose parent chain left the mapping still belong to the export.
    for node_id in mapping:
        if node_id not in seen:
            ordered.append(node_id)
    return ordered


# ---------------------------------------------------------------------------
# Windowing
# ---------------------------------------------------------------------------


def conversation_activity(conversation: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """``(created, last_active)`` in epoch seconds, from declared columns."""
    created = _as_epoch(conversation.get("create_time"))
    updated = _as_epoch(conversation.get("update_time"))
    if created is None and updated is None:
        # No declared stamp: fall back to the messages' own times so a
        # conversation is never dropped for a missing envelope field.
        times = [
            _as_epoch((node.get("message") or {}).get("create_time"))
            for node in (conversation.get("mapping") or {}).values()
            if isinstance(node, dict) and isinstance(node.get("message"), dict)
        ]
        stamps = [t for t in times if t is not None]
        if stamps:
            return min(stamps), max(stamps)
    return created, (updated if updated is not None else created)


def conversation_in_window(conversation: Dict[str, Any], options: ExportOptions) -> bool:
    """Inclusion is on last activity, so a long thread the user returned to
    inside the window comes in whole rather than half."""
    if options.date_from is None and options.date_to is None:
        return True
    created, last_active = conversation_activity(conversation)
    stamp = last_active if last_active is not None else created
    if stamp is None:
        # Undated conversation: keep it. Silently dropping unstamped records is
        # how corpora quietly lose their oldest material.
        return True
    if options.date_from is not None and stamp < options.date_from:
        return False
    return not (options.date_to is not None and stamp > options.date_to)


# ---------------------------------------------------------------------------
# Turn emission
# ---------------------------------------------------------------------------


def _conversation_facets(conversation: Dict[str, Any]) -> Dict[str, Any]:
    """Declared columns that describe the whole conversation.

    These ride on every turn's ``_metadata`` because the canonical mapper copies
    ``_metadata`` into ``metadata_json``; the conversation row is populated from
    the same block downstream.
    """
    created, updated = conversation_activity(conversation)
    facets: Dict[str, Any] = {
        "conversation_title": conversation.get("title"),
        "conversation_created_at": created,
        "conversation_updated_at": updated,
        "model_slug": conversation.get("default_model_slug"),
        "gizmo_id": conversation.get("gizmo_id"),
        "memory_scope": conversation.get("memory_scope"),
        "is_starred": conversation.get("is_starred"),
        "is_archived": conversation.get("is_archived"),
        "project_id": conversation.get("conversation_template_id"),
    }
    return {key: value for key, value in facets.items() if value is not None}


def _message_facets(message: Dict[str, Any]) -> Dict[str, Any]:
    """Declared per-message columns worth keeping: what was cited, searched,
    attached, and whether the owner spoke it rather than typed it."""
    metadata = message.get("metadata")
    if not isinstance(metadata, dict):
        return {}
    facets: Dict[str, Any] = {}

    urls: List[str] = []
    for citation in metadata.get("citations") or []:
        if isinstance(citation, dict):
            url = (citation.get("metadata") or {}).get("url")
            if isinstance(url, str) and url:
                urls.append(url)
    for group in metadata.get("search_result_groups") or []:
        if not isinstance(group, dict):
            continue
        for entry in group.get("entries") or []:
            if isinstance(entry, dict) and isinstance(entry.get("url"), str) and entry["url"]:
                urls.append(entry["url"])
    if urls:
        facets["citation_urls"] = list(dict.fromkeys(urls))

    queries = [
        query["q"]
        for query in metadata.get("search_queries") or []
        if isinstance(query, dict) and isinstance(query.get("q"), str) and query["q"].strip()
    ]
    if queries:
        facets["search_queries"] = queries

    attachments = [
        {
            "name": item.get("name"),
            "mime_type": item.get("mime_type"),
            "size": item.get("size"),
        }
        for item in metadata.get("attachments") or []
        if isinstance(item, dict)
    ]
    if attachments:
        facets["attachments"] = attachments

    if metadata.get("dictation"):
        facets["is_dictated"] = True
    if isinstance(metadata.get("model_slug"), str) and metadata["model_slug"]:
        facets["model_slug"] = metadata["model_slug"]
    if isinstance(metadata.get("canvas"), dict):
        canvas = metadata["canvas"]
        facets["canvas"] = {
            "textdoc_id": canvas.get("textdoc_id"),
            "title": canvas.get("title"),
            "version": canvas.get("version"),
        }
    return facets


# Facets that are lists and should union across carried-forward messages; every
# other facet is a scalar the emitting turn owns.
_LIST_FACETS = ("citation_urls", "search_queries", "attachments")


def _merge_facets(carried: Dict[str, Any], own: Dict[str, Any]) -> Dict[str, Any]:
    """Fold facets harvested from dropped nodes into the turn that follows them.

    A web search is three nodes: the user asks, a tool call runs the query, the
    assistant answers. The middle node is not a turn — but it is where the export
    declares the query and half the citations. Dropping the node dropped the
    declaration with it, which is why the search queries measured zero on a
    corpus that contains 91 of them.
    """
    if not carried:
        return own
    merged = dict(carried)
    for key, value in own.items():
        if key in _LIST_FACETS and isinstance(value, list) and isinstance(merged.get(key), list):
            seen = {json.dumps(v, sort_keys=True) if isinstance(v, (dict, list)) else v for v in merged[key]}
            for item in value:
                token = json.dumps(item, sort_keys=True) if isinstance(item, (dict, list)) else item
                if token not in seen:
                    seen.add(token)
                    merged[key].append(item)
        else:
            merged[key] = value
    return merged


def iter_turns(
    conversation: Dict[str, Any],
    options: Optional[ExportOptions] = None,
    ledger: Optional[DropLedger] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield flat turn records for one conversation.

    Record shape stays compatible with ``chatgpt.conversation.v1``/``v2``
    (``id``/``thread_id``/``role``/``content``/``created_at``) so
    :class:`ChatGPTParser` and the canonical mapper are unchanged.
    """
    options = options or ExportOptions()
    ledger = ledger if ledger is not None else DropLedger()

    mapping = conversation.get("mapping")
    if not isinstance(mapping, dict) or not mapping:
        return

    ledger.message_nodes += sum(
        1
        for node in mapping.values()
        if isinstance(node, dict) and isinstance(node.get("message"), dict)
    )

    conversation_id = str(conversation.get("conversation_id") or conversation.get("id") or "")
    conversation_created = conversation_activity(conversation)[0]
    conv_facets = _conversation_facets(conversation)

    path = active_path_ids(conversation)
    if path is None:
        node_ids = _ordered_node_ids(conversation)
        branch_of = dict.fromkeys(node_ids, BRANCH_UNKNOWN)
    else:
        on_path = set(path)
        node_ids = path if not options.include_alternate_branches else _ordered_node_ids(conversation)
        branch_of = {
            node_id: (BRANCH_ACTIVE if node_id in on_path else BRANCH_ALTERNATE)
            for node_id in node_ids
        }
        if not options.include_alternate_branches:
            skipped = sum(
                1
                for node_id, node in mapping.items()
                if node_id not in on_path and isinstance(node, dict) and isinstance(node.get("message"), dict)
            )
            if skipped:
                ledger.drop(DROP_ALTERNATE_BRANCH, skipped)

    turn_index = 0
    # Declared facets from nodes that are not turns (tool calls, hidden
    # scaffolding) ride forward to the next turn they belong to.
    carried: Dict[str, Any] = {}
    for node_id in node_ids:
        node = mapping.get(node_id)
        if not isinstance(node, dict):
            continue
        message = node.get("message")
        if not isinstance(message, dict):
            continue  # structural node (root/placeholder), not data

        author = message.get("author") if isinstance(message.get("author"), dict) else {}
        role = str(author.get("role") or "").strip().lower()
        content_obj = message.get("content") if isinstance(message.get("content"), dict) else {}
        content_type = str(content_obj.get("content_type") or "text")
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}

        if metadata.get("is_visually_hidden_from_conversation"):
            carried = _merge_facets(carried, _message_facets(message))
            ledger.drop(DROP_HIDDEN)
            continue
        if role == "system" and not options.include_system:
            ledger.drop(DROP_SYSTEM)
            continue
        if content_type in SCAFFOLD_CONTENT_TYPES:
            carried = _merge_facets(carried, _message_facets(message))
            ledger.drop(DROP_SCAFFOLD)
            continue

        is_tool_shaped = role == "tool" or content_type in TOOL_CONTENT_TYPES
        if is_tool_shaped and not options.include_tool_output:
            carried = _merge_facets(carried, _message_facets(message))
            ledger.drop(DROP_TOOL_OUTPUT)
            continue

        if role == "user":
            emitted_role = OWNER_ROLE
        elif role in ("assistant", "tool", "system"):
            emitted_role = ASSISTANT_ROLE
        else:
            ledger.drop(DROP_UNKNOWN_ROLE)
            continue

        text, assets = extract_content(content_obj)
        if not text.strip():
            carried = _merge_facets(carried, _message_facets(message))
            # The single rule the old reader was missing: a row with no content
            # is not a turn, whatever its content_type claims.
            ledger.drop(DROP_EMPTY)
            continue

        created_at = _as_epoch(message.get("create_time"))
        if created_at is None:
            created_at = conversation_created

        record_metadata: Dict[str, Any] = {
            **conv_facets,
            "node_id": node_id,
            "parent_id": node.get("parent"),
            "content_type": content_type,
            "original_role": role,
            "author_name": author.get("name"),
            "branch": branch_of.get(node_id, BRANCH_ACTIVE),
            "turn_index": turn_index,
        }
        if assets:
            record_metadata["assets"] = assets
        record_metadata.update(_merge_facets(carried, _message_facets(message)))
        carried = {}
        record_metadata = {k: v for k, v in record_metadata.items() if v is not None}

        turn_index += 1
        ledger.turns_emitted += 1
        yield {
            "id": str(message.get("id") or node_id),
            "thread_id": conversation_id,
            "role": emitted_role,
            "content": text,
            "created_at": created_at,
            "_metadata": record_metadata,
        }


def iter_export(
    payload: Any,
    options: Optional[ExportOptions] = None,
    ledger: Optional[DropLedger] = None,
) -> Iterator[Dict[str, Any]]:
    """Yield turn records for a whole export (array of conversations, or one)."""
    options = options or ExportOptions()
    ledger = ledger if ledger is not None else DropLedger()

    conversations: Iterable[Any]
    if isinstance(payload, dict):
        conversations = [payload]
    elif isinstance(payload, list):
        conversations = payload
    else:
        return

    for conversation in conversations:
        if not is_conversation(conversation):
            continue
        ledger.conversations_seen += 1
        if options.respect_do_not_remember and conversation.get("is_do_not_remember") is True:
            ledger.drop(DROP_DO_NOT_REMEMBER)
            continue
        if not conversation_in_window(conversation, options):
            ledger.drop(DROP_OUT_OF_WINDOW)
            continue
        ledger.conversations_kept += 1
        try:
            yield from iter_turns(conversation, options, ledger)
        except Exception as exc:  # noqa: BLE001 — one bad thread must not end the import
            logger.exception(
                "Failed to read conversation %s: %s",
                conversation.get("conversation_id") or conversation.get("id"),
                exc,
            )
            continue


def is_conversation(record: Any) -> bool:
    """True for a ChatGPT conversation object (mapping tree + an id)."""
    return (
        isinstance(record, dict)
        and isinstance(record.get("mapping"), dict)
        and bool(record.get("conversation_id") or record.get("id"))
    )
