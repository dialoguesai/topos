from __future__ import annotations

from typing import List


class RemoteEmbeddingsService:
    async def embed(self, texts: List[str]) -> List[List[float]]:
        _ = texts
        raise NotImplementedError("Remote embeddings not implemented yet")
