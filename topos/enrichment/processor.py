from __future__ import annotations

from typing import Any, Dict, List, Optional

from .orchestrator import EnrichmentOrchestrator


class EnrichmentProcessor:
    def __init__(self, orchestrator: Optional[EnrichmentOrchestrator] = None):
        self.orchestrator = orchestrator or EnrichmentOrchestrator()

    async def process(
        self,
        canonical_messages: List[Dict[str, Any]],
        job_names: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        return await self.orchestrator.run_canonical(canonical_messages, job_names=job_names)
