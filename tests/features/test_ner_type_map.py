"""NER label → spine entity_type mapping (OntoNotes-18 + legacy CoNLL-4).

Two properties matter:
  * identity labels land on first-class or folded types (never silently lost);
  * value labels (dates, money, cardinals) return None — the unknown-label
    fallback must NOT bucket them into "topic", or an OntoNotes model floods
    the spine with quantities on the first backfill.
"""

from __future__ import annotations

import pytest

from topos.features.entities.resolver import map_ner_type


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        # Legacy CoNLL-2003 (dslim/bert-base-NER) — unchanged behavior.
        ("PER", "person"),
        ("ORG", "org"),
        ("LOC", "place"),
        ("MISC", "topic"),
        # OntoNotes 5 identity labels.
        ("PERSON", "person"),
        ("GPE", "place"),
        ("FAC", "place"),
        ("NORP", "org"),
        ("WORK_OF_ART", "work_of_art"),
        ("EVENT", "event"),
        ("PRODUCT", "product"),
        ("LAW", "topic"),
        ("LANGUAGE", "topic"),
    ],
)
def test_identity_labels_map(label: str, expected: str) -> None:
    assert map_ner_type(label) == expected


@pytest.mark.parametrize(
    "label",
    ["DATE", "TIME", "PERCENT", "MONEY", "QUANTITY", "ORDINAL", "CARDINAL"],
)
def test_value_labels_drop(label: str) -> None:
    assert map_ner_type(label) is None
    # Aggregated pipeline output is upper, but never rely on it.
    assert map_ner_type(label.lower()) is None


def test_unknown_and_empty_fall_back_to_topic() -> None:
    assert map_ner_type("SOMETHING_NEW") == "topic"
    assert map_ner_type("") == "topic"
    assert map_ner_type(None) == "topic"


def test_case_insensitive() -> None:
    assert map_ner_type("person") == "person"
    assert map_ner_type("Work_Of_Art") == "work_of_art"
