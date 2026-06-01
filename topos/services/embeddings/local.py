from __future__ import annotations

from typing import List


class LocalEmbeddingsService:
    async def embed(self, texts: List[str]) -> List[List[float]]:
        _ = texts
        raise NotImplementedError("Local embeddings not implemented yet")
