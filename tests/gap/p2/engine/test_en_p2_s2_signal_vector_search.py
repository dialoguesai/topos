"""Gap: vector semantic search API and adapter."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.gap


def test_in_memory_vector_search_similar() -> None:
    from topos.storage.adapters.fakes import InMemoryVectorIndex

    index = InMemoryVectorIndex()
    index.upsert(
        {
            "embedding_id": "e1",
            "record_id": "r1",
            "source_id": "chatgpt_ingestion",
            "text_preview": "cats and dogs",
            "model": "sentence-transformers/all-MiniLM-L6-v2",
        },
        vector=[1.0, 0.0, 0.0],
    )
    index.upsert(
        {
            "embedding_id": "e2",
            "record_id": "r2",
            "source_id": "chatgpt_ingestion",
            "text_preview": "orthogonal topic",
            "model": "sentence-transformers/all-MiniLM-L6-v2",
        },
        vector=[0.0, 1.0, 0.0],
    )

    page = index.search_similar([0.9, 0.1, 0.0], limit=2)
    assert page.items[0]["embedding_id"] == "e1"
    assert page.items[0]["similarity"] > page.items[1]["similarity"]


@pytest.mark.asyncio
async def test_signal_vectors_search_api(monkeypatch) -> None:
    from topos.app import app
    from topos.auth import require_api_key

    async def _fake_key():
        return "test-key"

    app.dependency_overrides[require_api_key] = _fake_key
    monkeypatch.setattr(
        "topos.api.signal.get_signal_service",
        lambda: type(
            "S",
            (),
            {
                "search_vectors": lambda self, **kw: {
                    "items": [{"embedding_id": "e1", "similarity": 0.91, "dims": 384}],
                    "total": 1,
                    "query": kw.get("query"),
                    "model": "sentence-transformers/all-MiniLM-L6-v2",
                    "limit": kw.get("limit"),
                }
            },
        )(),
    )
    try:
        from httpx import ASGITransport, AsyncClient

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(
                "/v1/signal/vectors/search",
                params={"q": "project planning"},
                headers={"Authorization": "Bearer test-key"},
            )
    finally:
        app.dependency_overrides.pop(require_api_key, None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "project planning"
    assert body["items"][0]["similarity"] == 0.91
    assert "vector" not in body["items"][0]
