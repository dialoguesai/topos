from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ...utils.base_object import BaseObject


@dataclass(frozen=True)
class EnrichmentResult:
    result_id: str
    payload: Dict[str, str]


class EnrichmentJob:
    def run(self, input_ref: str) -> EnrichmentResult:
        raise NotImplementedError


class BaseEnrichmentJob(BaseObject):
    """Base enrichment job for canonical messages."""

    def __init__(self, *, name: Optional[str] = None) -> None:
        """Initialize enrichment job with optional name.
        
        Args:
            name: Optional custom name. Defaults to `ClassName#N`
        """
        super().__init__(name=name)

    def get_job_name(self) -> str:
        raise NotImplementedError

    def get_derived_table(self) -> str:
        raise NotImplementedError

    async def enrich(
        self, 
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        """Enrich canonical messages.
        
        Args:
            canonical_messages: List of canonical message dictionaries
            progress_callback: Optional callback(current_count, total_count) called during processing
            
        Returns:
            List of enrichment result dictionaries
        """
        raise NotImplementedError

    def should_run(self, canonical_messages: List[Dict[str, Any]]) -> bool:
        return bool(canonical_messages)
