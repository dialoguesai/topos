import pytest


@pytest.mark.asyncio
async def test_sources_endpoint_returns_registry():
    from topos.api.sources import get_sources

    data = await get_sources()
    assert "sources" in data
    source_ids = {item["source_id"] for item in data["sources"]}
    assert "chatgpt_file_ingestion" in source_ids
    assert "chatgpt_ui_conversation" in source_ids
    browser_source = next(item for item in data["sources"] if item["source_id"] == "browser_visits")
    assert browser_source["filter_tier_kind"] == "inferability"
    assert "default_filter_tiers" in browser_source
