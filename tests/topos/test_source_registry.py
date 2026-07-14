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


def test_registry_delivery_matches_connector_spec():
    """Literal delivery assertion for every bundled source (CONNECTOR_SPEC.md §4)."""
    from topos.sources.registry import REGISTRY

    expected = {
        "chatgpt_file_ingestion": "owner_upload",
        "chatgpt_ui_conversation": "client_push",
        "browser_visits": "client_push",
        "browser_events": "client_push",
        "voxterm_transcripts": "client_push",
        "imessage": "local_sync",
        "signal": "local_sync",
        "calendar_stub": None,
        "canonical_address_book": None,
        "demo_messenger_file": "owner_upload",
        "demo_email_file": "owner_upload",
        "demo_calendar_file": "owner_upload",
        "demo_journal_file": "owner_upload",
        "demo_resume_file": "owner_upload",
        "demo_financial_file": "owner_upload",
        "demo_browser_file": "owner_upload",
        "demo_places_file": "owner_upload",
        "demo_contacts_file": "owner_upload",
    }
    # Subset check: runtime installs from other tests may add entries to REGISTRY.
    missing = set(expected) - set(REGISTRY)
    assert not missing, f"bundled sources missing from registry: {sorted(missing)}"
    for source_id, delivery in expected.items():
        assert REGISTRY[source_id].delivery == delivery, source_id
