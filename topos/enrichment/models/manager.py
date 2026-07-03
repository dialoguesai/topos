from __future__ import annotations

from typing import Any, List, Optional

from ..engine.model_cache import ModelSlot, get_model_cache


class ModelManager:
    """Facade over Engine ModelCache for enrichment registry integration."""

    def __init__(self) -> None:
        self._cache = get_model_cache()

    def resident_slots(self) -> List[str]:
        return self._cache.resident_slots()

    def evict_slot(self, slot: ModelSlot) -> None:
        self._cache.evict_slot(slot)

    def trim_to_budget(self) -> None:
        self._cache.trim_to_budget()

    def acquire(
        self,
        slot: ModelSlot,
        model_id: str,
        loader,
    ) -> tuple[Any, bool]:
        return self._cache.acquire(slot, model_id, loader)

    @property
    def max_resident(self) -> int:
        return self._cache.max_resident

    @property
    def evictions_total(self) -> int:
        return self._cache.evictions_total
