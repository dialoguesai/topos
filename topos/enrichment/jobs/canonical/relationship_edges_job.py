from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..base import BaseEnrichmentJob

logger = logging.getLogger("topos.enrichment.jobs.relationship_edges")


class RelationshipEdgesJob(BaseEnrichmentJob):
    """Rules-only co-occurrence / message-frequency edges (no LLM)."""

    def get_derived_table(self) -> str:
        return "relationship_edges"

    def get_job_name(self) -> str:
        return "relationship_edges"

    async def enrich(
        self,
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        pairs: Counter[Tuple[str, str]] = Counter()
        source_id = None
        for msg in canonical_messages:
            source_id = msg.get("source_id") or source_id
            conv = str(msg.get("conversation_id") or msg.get("thread_id") or "default")
            sender = str(msg.get("sender_id") or msg.get("contact_id") or msg.get("from") or "").strip()
            if not sender:
                sender_type = str(msg.get("sender_type") or "user").strip().lower() or "user"
                sender = f"{sender_type}:{conv.split(':')[-1][:16]}"
            pairs[(sender, conv)] += 1
        results: List[Dict[str, Any]] = []
        for (src, dst), weight in pairs.items():
            if src == dst:
                continue
            results.append(
                {
                    "src_node_id": f"contact:{src}",
                    "dst_node_id": f"conversation:{dst}",
                    "edge_type": "message_frequency",
                    "weight": float(weight),
                    "source_id": source_id,
                    "provider": "rules",
                    "model": "relationship_rules_v1",
                }
            )
        if progress_callback:
            progress_callback(len(canonical_messages), len(canonical_messages))
        return results
