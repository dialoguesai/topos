"""Dates and quantities must never become entity-graph nodes.

Live regression (2026-08-10): the /data/graph view rendered "an hour", "this
week", "four", "Mon-Wed" and "a great weekend" as first-class topic nodes, 51 of
them on one node. The mentions were labelled correctly — DATE / TIME / CARDINAL,
all already in ``_NER_DROP_LABELS`` — but the graph's second minting lane
(``fact_materializer``: topic-cluster ``related_entities`` and string-valued
fact objects) resolves BARE SURFACES with no label attached, so ``map_ner_type``
never ran and the drop list could not fire.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.entities.resolver import (
    is_valid_entity_surface,
    normalize_name,
    value_label_surfaces,
)
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.public


def _mention(conn, mention_id, surface, label, n=1):
    for i in range(n):
        conn.execute(
            "INSERT INTO message_entities (entity_id, record_id, source_id, entity_text, payload_json) "
            "VALUES (?, ?, 'src', ?, ?)",
            (
                f"{mention_id}-{i}",
                f"rec-{mention_id}-{i}",
                surface,
                json.dumps({"entity_text": surface, "entity_type": label}),
            ),
        )


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "v.db"), check_same_thread=False)
    apply_all_migrations(c)
    yield c
    c.close()


def test_value_labels_are_detected_from_the_models_own_judgment(conn):
    _mention(conn, "m1", "an hour", "TIME", n=3)
    _mention(conn, "m2", "this week", "DATE", n=2)
    _mention(conn, "m3", "four", "CARDINAL")
    _mention(conn, "m4", "Albiona Hoti", "PERSON", n=4)
    _mention(conn, "m5", "Topos", "ORG", n=2)
    conn.commit()

    values = value_label_surfaces(conn)
    assert normalize_name("an hour") in values
    assert normalize_name("this week") in values
    assert normalize_name("four") in values
    # Identities must survive — this guard must not eat the real graph.
    assert normalize_name("Albiona Hoti") not in values
    assert normalize_name("Topos") not in values


def test_a_single_mislabel_does_not_condemn_a_real_entity(conn):
    """Majority rule: "Phoenix" tagged DATE once is still the city."""
    _mention(conn, "m1", "Phoenix", "DATE")
    _mention(conn, "m2", "Phoenix", "GPE", n=5)
    conn.commit()
    assert normalize_name("Phoenix") not in value_label_surfaces(conn)


def test_surfaces_the_stopword_guard_lets_through(conn):
    """The existing guard cannot catch these — that is why the lane leaked.

    Each has at least one non-stopword token, so is_valid_entity_surface()
    passes it; only the model's label reveals it as a value.
    """
    for surface in ("an hour", "this week", "four", "Mon-Wed", "a great weekend"):
        assert is_valid_entity_surface(surface) is True, surface

    _mention(conn, "m1", "an hour", "TIME")
    _mention(conn, "m2", "this week", "DATE")
    _mention(conn, "m3", "four", "CARDINAL")
    _mention(conn, "m4", "Mon-Wed", "DATE")
    _mention(conn, "m5", "a great weekend", "DATE")
    conn.commit()
    values = value_label_surfaces(conn)
    for surface in ("an hour", "this week", "four", "Mon-Wed", "a great weekend"):
        assert normalize_name(surface) in values, surface


def test_empty_registry_yields_no_guard(conn):
    """No mentions → no opinions. Never block minting on missing evidence."""
    assert value_label_surfaces(conn) == frozenset()
