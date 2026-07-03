"""Pipeline-level model cache maintenance."""

from __future__ import annotations

import logging

logger = logging.getLogger("topos.engine.pipeline_memory")


def flush_engine_model_cache_after_pipeline() -> None:
    """Trim resident models to budget after enrichment / signal derivation."""
    try:
        from .model_cache import get_model_cache

        cache = get_model_cache()
        before = len(cache.resident_slots())
        cache.trim_to_budget()
        after = len(cache.resident_slots())
        if before != after:
            logger.info(
                "Engine model cache trimmed after pipeline: resident %d -> %d",
                before,
                after,
            )
    except Exception as exc:
        logger.debug("Pipeline model cache trim skipped: %s", exc)
