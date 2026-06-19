"""
Query session memory types (PRD §8.3–8.4).

Audit extension fields documented in package docstrings (§8.8).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class TurnOutcome(str, Enum):
    MEMORY_HIT = "memory_hit"
    LIVE_QUERY = "live_query"
    EXPAND_BOUNDARY = "expand_boundary"
    REQUALIFY = "requalify"
    DENIED = "denied"


@dataclass
class QueryArtifact:
    artifact_id: str
    session_id: str
    cache_key: str
    public_result_json: Dict[str, Any] = field(default_factory=dict)
    retrieval_fingerprint: str = ""
    game_layer_strategy: str = "direct"
    created_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class QuerySession:
    session_id: str
    requester_id: str
    intent_hash: str
    envelope_json: Dict[str, Any] = field(default_factory=dict)
    ttl_expires_at: Optional[str] = None
    artifacts: List[QueryArtifact] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["artifacts"] = [a.to_dict() if isinstance(a, QueryArtifact) else a for a in self.artifacts]
        return payload
