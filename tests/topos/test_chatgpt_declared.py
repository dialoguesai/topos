"""Declared entity minting for ChatGPT turns (PLAN_CHATGPT_IMPORT.md Sprint 4).

Each test pins one of the three judgements the module makes — hosts not URLs,
queries read but not minted, machine-named files refused — so a later change
that quietly reverses one shows up as that sentence failing.
"""

from __future__ import annotations

import json

import pytest

from topos.features.entities.chatgpt_declared import (
    EDGE_AUTHORED,
    EDGE_EXPOSED_TO,
    TYPE_DOCUMENT,
    TYPE_WEB_SOURCE,
    coverage,
    declared_rows,
    host_of,
    is_named_by_a_human,
)
from topos.features.entities.declared_mappings import extract_declared_entities

AT = "2025-07-20T00:00:00Z"


def turn(metadata: dict, *, source_id: str = "chatgpt_file_ingestion", as_string: bool = True):
    return {
        "message_id": "m1",
        "source_id": source_id,
        "ts": AT,
        "_table": "ai_chat_messages",
        "metadata_json": json.dumps(metadata) if as_string else metadata,
    }


def rows_for(metadata: dict, **kw):
    return declared_rows(turn(metadata, **kw), record_id="m1", event_at=AT)


# --------------------------------------------------------------------------
# host_of
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://cloud.google.com/iam/docs/roles", "cloud.google.com"),
        ("https://www.stackoverflow.com/q/1", "stackoverflow.com"),
        ("http://docs.python.org:8080/3/library", "docs.python.org"),
        ("cloud.google.com/iam", "cloud.google.com"),
        ("https://localhost:3000/x", ""),
        ("https://127.0.0.1/x", ""),
        ("https://example.com/x", ""),
        ("not a url", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_host_of_names_the_source_not_the_page(url, expected):
    assert host_of(url) == expected


def test_subdomains_stay_distinct():
    """Reading Google Cloud's docs is not the same as reading google.com."""
    assert host_of("https://cloud.google.com/a") != host_of("https://google.com/a")


# --------------------------------------------------------------------------
# Filenames
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("proposal-v3.pdf", True),
        ("ChatGPT Image Jul 20, 2025.png", True),
        ("dialogues_logo_cleaned_multi.png", True),
        ("bb2b7a54-d935-4ac1-82d2-af0ff5df204c.png", False),
        ("a1b2c3d4e5f60718293a4b5c6d7e8f90.png", False),
        ("x.png", False),
        ("", False),
    ],
)
def test_only_files_a_person_named_become_documents(name, expected):
    assert is_named_by_a_human(name) is expected


# --------------------------------------------------------------------------
# Minting
# --------------------------------------------------------------------------


def test_cited_urls_mint_one_node_per_host():
    rows = rows_for({"citation_urls": [
        "https://cloud.google.com/iam/docs/roles",
        "https://cloud.google.com/iam/docs/service-accounts",
        "https://www.cloud.google.com/other",
        "https://stackoverflow.com/q/1",
    ]})
    hosts = [r["entity_text"] for r in rows if r["entity_type"] == TYPE_WEB_SOURCE]
    assert hosts == ["cloud.google.com", "stackoverflow.com"]


def test_the_url_survives_as_evidence_even_though_the_host_is_the_node():
    rows = rows_for({"citation_urls": ["https://cloud.google.com/iam/docs/roles"]})
    assert rows[0]["entity_text"] == "cloud.google.com"
    assert "https://cloud.google.com/iam/docs/roles" in rows[0]["surface_detail"]


def test_a_cited_page_is_exposure_never_authorship():
    """The provenance layer must not read a page the model fetched as the
    owner's own words."""
    rows = rows_for({"citation_urls": ["https://stackoverflow.com/q/1"]})
    assert rows[0]["self_edge"] == EDGE_EXPOSED_TO
    assert rows[0]["self_edge"] != EDGE_AUTHORED


def test_search_queries_are_read_onto_the_evidence_but_never_minted():
    rows = rows_for({
        "citation_urls": ["https://cloud.google.com/iam"],
        "search_queries": ["gcp iam roles for compute instances"],
    })
    assert [r["entity_type"] for r in rows] == [TYPE_WEB_SOURCE]
    assert "gcp iam roles for compute instances" in rows[0]["surface_detail"]


def test_a_query_with_no_citations_mints_nothing():
    assert rows_for({"search_queries": ["how do I do a thing"]}) == []


def test_canvas_documents_are_authored_by_the_owner():
    rows = rows_for({"canvas": {"title": "IAM notes", "textdoc_id": "td1", "version": 2}})
    assert rows[0]["entity_type"] == TYPE_DOCUMENT
    assert rows[0]["entity_text"] == "IAM notes"
    assert rows[0]["self_edge"] == EDGE_AUTHORED


def test_a_canvas_with_no_title_mints_nothing():
    assert rows_for({"canvas": {"textdoc_id": "td1"}}) == []


def test_machine_named_attachments_are_refused():
    rows = rows_for({"attachments": [
        {"name": "proposal-v3.pdf"},
        {"name": "bb2b7a54-d935-4ac1-82d2-af0ff5df204c.png"},
    ]})
    assert [r["entity_text"] for r in rows] == ["proposal-v3.pdf"]


def test_declared_rows_carry_the_declared_contract():
    row = rows_for({"citation_urls": ["https://stackoverflow.com/q/1"]})[0]
    assert row["provider"] == "declared"      # spine keeps the type verbatim
    assert row["confidence"] == 1.0           # no model, nothing to discount
    assert row["canonical_table"] == "ai_chat_messages"
    assert row["record_id"] == "m1" and row["event_at"] == AT


def test_metadata_is_read_as_a_dict_or_a_json_string():
    payload = {"citation_urls": ["https://stackoverflow.com/q/1"]}
    assert rows_for(payload, as_string=True) == rows_for(payload, as_string=False)


def test_a_turn_with_no_declared_metadata_mints_nothing():
    assert declared_rows({"message_id": "m1", "source_id": "chatgpt_file_ingestion"}, record_id="m1") == []
    assert rows_for({}) == []


def test_nothing_is_minted_without_a_record_id():
    assert declared_rows(turn({"citation_urls": ["https://a.com/x"]}), record_id="") == []


# --------------------------------------------------------------------------
# Registry wiring — the entities job reaches this through extract_declared_entities
# --------------------------------------------------------------------------


def test_the_entities_job_entry_point_dispatches_to_this_producer():
    rows = extract_declared_entities(
        turn({"citation_urls": ["https://cloud.google.com/iam"], "canvas": {"title": "Notes"}}),
        record_id="m1",
        event_at=AT,
    )
    assert {r["entity_type"] for r in rows} == {TYPE_WEB_SOURCE, TYPE_DOCUMENT}


def test_other_sources_are_untouched():
    rows = extract_declared_entities(
        turn({"citation_urls": ["https://cloud.google.com/iam"]}, source_id="imessage"),
        record_id="m1",
        event_at=AT,
    )
    assert rows == []


# --------------------------------------------------------------------------
# Coverage — the before/after number
# --------------------------------------------------------------------------


def test_coverage_counts_what_would_be_minted_and_what_was_refused():
    records = [
        turn({"citation_urls": ["https://a.com/1", "https://a.com/2", "https://b.com/1"],
              "search_queries": ["q one", "q two"]}),
        turn({"canvas": {"title": "Notes"},
              "attachments": [{"name": "deck.pdf"}, {"name": "bb2b7a54-d935-4ac1-82d2-af0ff5df204c.png"}]}),
    ]
    assert coverage(records) == {
        "citation_urls": 3,
        "web_sources": 2,
        "documents": 2,
        "search_queries_read": 2,
        "attachments_skipped_machine_named": 1,
    }
