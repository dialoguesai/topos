"""Query pipeline types (Phase 3 runtime)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Literal, Optional

AccessMode = Literal["raw", "summary", "inference"]

MODE_RANK = {"raw": 0, "summary": 1, "inference": 2}


class RetrievalError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class RetrievalRequest:
    manifest: Any
    access_mode: AccessMode
    filter_manifest: Optional[Dict[str, Any]] = None
    field_transforms: Optional[List[Any]] = None
    skip_retrieval: bool = False


@dataclass
class RetrievalBundle:
    context_packet: Dict[str, Any] = field(default_factory=dict)
    stores_touched: List[str] = field(default_factory=list)
    record_counts: Dict[str, int] = field(default_factory=dict)
    error: Optional[str] = None


@dataclass
class FilteredContext:
    context_packet: Dict[str, Any] = field(default_factory=dict)
    filters_applied: List[str] = field(default_factory=list)


@dataclass
class PublicResult:
    payload: Dict[str, Any] = field(default_factory=dict)
    strategy: str = "direct"

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.payload)


@dataclass
class QueryTurn:
    query_text: str
    scope_id: str
    access_mode: AccessMode
    intent_hash: str = ""
    requester_id: str = ""


@dataclass
class ClassificationResult:
    outcome: Any
    cache_key: Optional[str] = None
    deny_reason: Optional[str] = None


FORBIDDEN_ARTIFACT_KEYS = frozenset(
    {"evidence", "source_rows", "retrieval_context", "prompt", "vector", "embedding", "vectors"}
)

FORBIDDEN_INFERENCE_PUBLIC_KEYS = frozenset({"evidence", "source_rows", "retrieval_context"})
