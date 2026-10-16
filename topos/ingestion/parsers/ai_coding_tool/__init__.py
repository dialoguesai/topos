"""AI coding-tool transcript parsers (Claude Code, Codex, Cursor) — CONTRACTS §2.

Importing this package is also what registers the ``ai_coding_tool`` canonical
mapper: ``ingestion/parsers/__init__.py`` imports it, and the canonical
pipeline imports that package, so any path that can write these rows has the
mapper. ``storage/canonical/ai_chat/__init__.py`` is left untouched.
"""

from ....storage.canonical.ai_chat.coding_tool_mapper import register_coding_tool_mapper
from .base import AICodingToolParser, contract_errors
from .claude_code import ClaudeCodeTranscriptParser
from .codex import CodexTranscriptParser
from .common import Ledger, is_harness_text, make_record, scrub_text, taints
from .cursor import CursorTranscriptParser

register_coding_tool_mapper()

__all__ = [
    "AICodingToolParser",
    "ClaudeCodeTranscriptParser",
    "CodexTranscriptParser",
    "CursorTranscriptParser",
    "Ledger",
    "contract_errors",
    "is_harness_text",
    "make_record",
    "scrub_text",
    "taints",
]
