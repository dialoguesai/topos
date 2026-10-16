"""Codex transcript parser: validates §2.2 records read by ``codex_reader`` (lane L6)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .base import AICodingToolParser

SCHEMA_ID = "codex.transcript.v1"


@dataclass
class CodexTranscriptParser(AICodingToolParser):
    dataset_id: str
    _schema_id: str = SCHEMA_ID
    tool: ClassVar[str] = "codex"
