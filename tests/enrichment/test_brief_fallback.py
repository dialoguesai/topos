"""Tests for rules-based brief fallback and signal record normalization."""

from __future__ import annotations

from topos.enrichment.jobs.canonical.brief_fallback import (
    brief_input_text,
    prepare_signal_record,
    rules_brief_sections,
)
from topos.features.signal.brief_ontology import llm_merge_section_ids


def test_prepare_signal_record_maps_entry_id_to_message_id() -> None:
    record = prepare_signal_record(
        {
            "entry_id": "j1",
            "mood_tag": "anxious",
            "category": "wellbeing",
            "content": "Pre-investor nerves",
        }
    )
    assert record["message_id"] == "j1"
    assert "Pre-investor nerves" in record["content"]


def test_prepare_signal_record_builds_content_from_fields() -> None:
    record = prepare_signal_record(
        {
            "transaction_id": "f1",
            "description": "Payroll deposit",
            "amount": "12000",
        }
    )
    assert record["message_id"] == "f1"
    assert "Payroll deposit" in record["content"]


def test_rules_brief_sections_from_records() -> None:
    sections = rules_brief_sections(
        "wellbeing",
        [
            {"content": "Slept 7.5 hours"},
            {"content": "Morning run felt good"},
        ],
    )
    keys = llm_merge_section_ids("wellbeing")
    assert keys
    assert "Slept 7.5 hours" in sections[keys[0]]
    assert sections[keys[-1]]


def test_brief_input_text_compresses_grow_journal() -> None:
    digest = brief_input_text(
        {
            "category": "Job Applications",
            "place_name": "Timbercreek Apt",
            "content": (
                "Project: Job Applications\n\n"
                "Goal: Update resume\n\n"
                "Accomplished: Updated and sent to better-half.ai.\n\n"
                "I will see what they say.\n\n"
                "Location: Timbercreek Apt\n\n"
                "With: Solo\n\n"
                "Duration: 55 min\n\n"
                "Completed: TRUE"
            ),
        }
    )
    assert "Job Applications" in digest
    assert "Update resume" in digest
    assert "Updated and sent" in digest
    assert "Timbercreek Apt" in digest
    assert "Location:" not in digest
    assert "Duration:" not in digest
    assert "Project:" not in digest


def test_rules_brief_sections_places_travel_patterns() -> None:
    sections = rules_brief_sections(
        "places",
        [
            {"place_name": "Austin- Downtown", "category": "Chill", "content": "Project: Chill\n\nLocation: Austin- Downtown"},
            {"place_name": "NYC", "category": "Chill", "content": "Project: Chill\n\nLocation: NYC"},
            {"place_name": "Brooklyn- The Convent", "category": "Topos", "content": "Project: Topos\n\nLocation: Brooklyn- The Convent"},
        ],
    )
    travel = sections.get("travel_patterns") or ""
    assert "NYC" in travel
    assert "Brooklyn" in travel


def test_rules_brief_sections_memory_uses_compressed_bullets() -> None:
    grow_row = {
        "category": "Topos",
        "content": (
            "Project: Topos\n\n"
            "Goal: Ship investors page\n\n"
            "Accomplished: Created the investors page on the website.\n\n"
            "Location: Timbercreek Apt\n\n"
            "Duration: 68 min"
        ),
    }
    sections = rules_brief_sections("memory", [grow_row, grow_row])
    assert "Project:" not in sections["active_preoccupations"]
    assert sections["active_preoccupations"].startswith("- ")
    assert "Topos" in sections["active_preoccupations"]
