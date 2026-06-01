from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class EnrichmentRef:
    record_id: str


class EnrichmentStore:
    def write(self, result: Dict[str, str]) -> EnrichmentRef:
        raise NotImplementedError


class RawEnrichmentStore(EnrichmentStore):
    """Store for raw enrichment results."""
