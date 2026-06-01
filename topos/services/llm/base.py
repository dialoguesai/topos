from __future__ import annotations

from typing import Dict, Protocol


class LLMService(Protocol):
    async def generate(self, payload: Dict[str, str]) -> Dict[str, str]: ...
