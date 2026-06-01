from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Dict

from .mappers.base import CanonicalRecord


def deterministic_id(namespace: str, value: str) -> str:
    seed = f"{namespace}:{value}".encode("utf-8")
    return hashlib.sha256(seed).hexdigest()


@dataclass
class CanonicalResolver:
    """Resolves canonical records and handles collisions (stub)."""

    def resolve_message(self, payload: Dict[str, str]) -> CanonicalRecord:
        content = payload.get("content", "")
        source_id = payload.get("source_id", "unknown")
        message_id = deterministic_id(source_id, content)
        return CanonicalRecord(record_id=message_id, payload=payload)
