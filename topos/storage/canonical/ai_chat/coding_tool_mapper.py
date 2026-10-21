"""Canonical mapper for the AI coding-tool transcripts (CONTRACTS §2.1).

One mapper for the three sources and one conversation-id rule:
``<tool>:<session_id>`` (``claude_code:<session>``), the tool named by the
source the row is written under — never the source id
(``claude_code_sessions:``), another tool's prefix, or ``chatgpt:``. A stored id
written any other way is put right by ``coding_conversation_repair``.
The reader's ``_metadata`` rides into ``metadata_json`` — minus any path facet,
because ``metadata_json`` reaches grantees raw (security finding 14); the
project facet goes to the owner-only ``ai_chat_message_facets`` table (§2.6).

Registered from ``ingestion/parsers/ai_coding_tool/__init__.py`` through the
existing :func:`register_mapper`; nothing else in this package is edited.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .mapper import CanonicalMapper, register_mapper
from .model import CanonicalAIChatMessage

MAPPER_ID = "ai_coding_tool"
CONVERSATION_PREFIXES = ("claude_code", "codex", "cursor")
TOOL_BY_SOURCE_ID = {
    "claude_code_sessions": "claude_code",
    "codex_sessions": "codex",
    "cursor_sessions": "cursor",
}
# Every prefix a coding writer has stored: the tools, the source ids (a runtime-install shim bound to the first
# coding source a process installed, 1.5.13 and the 1.5.14 candidate) and the mapper id (a record naming no tool).
WRITTEN_PREFIXES = frozenset(CONVERSATION_PREFIXES) | frozenset(TOOL_BY_SOURCE_ID) | {MAPPER_ID}

# Keys that never reach metadata_json, whatever a reader put in ``_metadata``.
EXCLUDED_METADATA_KEYS = frozenset({
    "cwd",
    "path",
    "paths",
    "file_path",
    "project_path",
    "repo_path",
    "transcript_path",
    "session_path",
    "root_path",
    "gitBranch",
    "git_branch",
    "branch",
    "hostname",
    "project_key",
})


class CodingToolToAIChatMapper(CanonicalMapper):
    """Maps §2.2 staging records (from any of the three parsers) to the canonical AI chat model."""

    def map_to_canonical(
        self,
        staging_record: Dict[str, Any],
        source: str = MAPPER_ID,
    ) -> List[CanonicalAIChatMessage]:
        conversation_id = self.extract_conversation_id(staging_record)
        actual_source_id = staging_record.get("source_id") or source
        metadata: Dict[str, Any] = {
            "thread_id": staging_record.get("thread_id"),
            "original_source": source,
        }
        extra = staging_record.get("_metadata")
        if isinstance(extra, dict):
            metadata.update({k: v for k, v in extra.items() if k not in EXCLUDED_METADATA_KEYS})
        message = CanonicalAIChatMessage(
            message_id=staging_record.get("message_id", ""),
            conversation_id=conversation_id,
            sender_type=staging_record.get("sender_type", ""),
            sender_id=None,
            ts=staging_record.get("ts", ""),
            content=staging_record.get("content") or "",
            content_rendered=None,
            metadata_json=metadata,
            seq=0,
            source_id=actual_source_id,
        )
        return [message]

    def extract_conversation_id(self, staging_record: Dict[str, Any]) -> str:
        tool = _tool_of(staging_record)
        thread_id = staging_record.get("thread_id")
        if thread_id:
            return coding_conversation_id(tool, thread_id)
        return coding_conversation_id(tool, staging_record.get("dataset_id", ""))


def coding_conversation_id(tool: str, session_id: str) -> str:
    """The conversation id of one coding session: ``<tool>:<session_id>``."""
    return f"{tool}:{session_id}"


def canonical_coding_conversation_id(source_id: str, conversation_id: str) -> Optional[str]:
    """The id a stored coding-source conversation should have, or None for a shape no coding writer produced."""
    tool = TOOL_BY_SOURCE_ID.get(source_id)
    prefix, sep, session_id = str(conversation_id or "").partition(":")
    if tool is None or not sep or not session_id or prefix not in WRITTEN_PREFIXES:
        return None
    return coding_conversation_id(tool, session_id)


def _tool_of(staging_record: Dict[str, Any]) -> str:
    tool = TOOL_BY_SOURCE_ID.get(str(staging_record.get("source_id") or ""))
    if tool:
        return tool
    extra = staging_record.get("_metadata")
    if isinstance(extra, dict) and extra.get("tool") in CONVERSATION_PREFIXES:
        return str(extra["tool"])
    message_id = str(staging_record.get("message_id") or "")
    prefix = message_id.split(":", 1)[0]
    if prefix in CONVERSATION_PREFIXES:
        return prefix
    return MAPPER_ID


def register_coding_tool_mapper() -> None:
    register_mapper(MAPPER_ID, CodingToolToAIChatMapper)
