"""P2.1 (PLAN_PROVENANCE_SPLIT): the browser activity mapper carries hostname and
the owner-selected highlight span into canonical.

The v1 mapper dropped hostname and content entirely. The v2 mapper:
  * carries hostname onto the canonical row + metadata_json;
  * for highlight/selection events, sets activity_type='browser_highlight',
    pulls the selected span into content AND metadata_json.highlight;
  * never promotes page_excerpt (the page author's words) to the highlight key;
  * leaves plain visits/clicks unchanged (activity_type='visit'/'click', no
    highlight span).
"""

from __future__ import annotations

import json

from topos.canonicalization.mappers.browser_activity_mapper import (
    BrowserActivityCanonicalMapper,
)
from topos.ingestion.parsers.base import NormalizedRecord


def _map(payload: dict) -> dict:
    mapper = BrowserActivityCanonicalMapper()
    norm = NormalizedRecord(record_id=str(payload.get("id") or "r1"), payload=payload)
    return mapper.map(norm).payload


def test_highlight_event_carries_span_and_activity_type() -> None:
    out = _map(
        {
            "id": "e1",
            "event_type": "highlight",
            "url": "https://example.com/fermentation",
            "title": "Fermentation Methods Deep Dive",
            "hostname": "example.com",
            "content": "the copper still method halves fermentation time",
            "visited_at": "2026-07-01T10:00:00Z",
        }
    )
    assert out["activity_type"] == "browser_highlight"
    assert out["content"] == "the copper still method halves fermentation time"
    assert out["hostname"] == "example.com"
    assert out["metadata_json"]["highlight"] == "the copper still method halves fermentation time"
    assert out["metadata_json"]["hostname"] == "example.com"


def test_highlight_span_wrapped_as_json_selected_text() -> None:
    # The plugin sometimes sends the span wrapped as {"selectedText": …}.
    out = _map(
        {
            "id": "e2",
            "event_type": "selection",
            "url": "https://example.com/a",
            "hostname": "example.com",
            "content": json.dumps({"selectedText": "copper still method", "note": "x"}),
        }
    )
    assert out["activity_type"] == "browser_highlight"
    assert out["content"] == "copper still method"
    assert out["metadata_json"]["highlight"] == "copper still method"


def test_visit_event_unchanged_no_highlight() -> None:
    out = _map(
        {
            "id": "v1",
            "event_type": "visit",
            "url": "https://example.com/page",
            "title": "A Page",
            "hostname": "example.com",
            "visited_at": "2026-07-01T10:00:00Z",
        }
    )
    assert out["activity_type"] == "visit"
    assert out["content"] is None
    assert out["hostname"] == "example.com"
    assert "highlight" not in out["metadata_json"]


def test_click_event_with_pointer_dict_content_has_no_span() -> None:
    # A click's content is pointer coordinates, not a span — never a highlight.
    out = _map(
        {
            "id": "c1",
            "event_type": "click",
            "url": "https://example.com/page",
            "hostname": "example.com",
            "content": {"x": 10, "y": 20, "element": "button.primary"},
        }
    )
    assert out["activity_type"] == "click"
    assert out["content"] is None
    assert "highlight" not in out["metadata_json"]


def test_missing_hostname_derived_from_url() -> None:
    # BT6 (PLAN_ATTENTION_TRIAGE.md): plugin batches that send url without
    # hostname get it derived, so downstream vocab never goes empty.
    out = _map({"id": "n1", "event_type": "visit", "url": "https://x/y"})
    assert out["hostname"] == "x"
    assert out["metadata_json"]["hostname"] == "x"


def test_missing_hostname_and_url_leaves_key_absent() -> None:
    out = _map({"id": "n2", "event_type": "visit"})
    assert out["hostname"] is None
    assert "hostname" not in out["metadata_json"]


def test_map_many_defaults_to_single_record() -> None:
    """Single-lane mappers keep working through the base map_many() default
    (dual-lane hook added for github_activity): one record, no table override."""
    mapper = BrowserActivityCanonicalMapper()
    norm = NormalizedRecord(
        record_id="v9", payload={"id": "v9", "event_type": "visit", "url": "https://x/y"}
    )
    records = mapper.map_many(norm)
    assert len(records) == 1
    assert records[0].table is None
    assert records[0].payload == mapper.map(norm).payload
