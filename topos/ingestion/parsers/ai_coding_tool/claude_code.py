"""Claude Code transcript parser: validates §2.2 records read by ``claude_code_reader``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .base import AICodingToolParser

SCHEMA_ID = "claude_code.transcript.v1"


@dataclass
class ClaudeCodeTranscriptParser(AICodingToolParser):
    dataset_id: str
    _schema_id: str = SCHEMA_ID
    tool: ClassVar[str] = "claude_code"
