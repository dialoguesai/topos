from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class CanonicalConversation:
    conversation_id: str
    source_id: str
    metadata: Dict[str, str]


@dataclass(frozen=True)
class CanonicalParticipant:
    participant_id: str
    conversation_id: str
    role: str
    metadata: Dict[str, str]


@dataclass(frozen=True)
class CanonicalMessage:
    message_id: str
    conversation_id: str
    sender_type: str
    content: str
    ts: Optional[str]
    source_id: str
    content_hash: str
    metadata: Dict[str, str]
