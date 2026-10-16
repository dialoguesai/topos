"""Canonical mapper for the AI coding-tool transcripts (CONTRACTS §2.1).

One mapper for the three sources; the conversation prefix comes from the
record's ``_metadata.tool`` (``claude_code:<session>``), never ``chatgpt:``.
The reader's ``_metadata`` rides into ``metadata_json`` — minus any path facet,
because ``metadata_json`` reaches grantees raw (security finding 14); the
project facet goes to the owner-only ``ai_chat_message_facets`` table (§2.6).

Registered from ``ingestion/parsers/ai_coding_tool/__init__.py`` through the
existing :func:`register_mapper`; nothing else in this package is edited.
"""

from __future__ import annotations

from typing import Any, Dict, List

from .mapper import CanonicalMapper, register_mapper
from .model import CanonicalAIChatMessage

MAPPER_ID = "ai_coding_tool"
CONVERSATION_PREFIXES = ("claude_code", "codex", "cursor")

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
            return f"{tool}:{thread_id}"
        return f"{tool}:{staging_record.get('dataset_id', '')}"


def _tool_of(staging_record: Dict[str, Any]) -> str:
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
