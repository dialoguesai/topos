"""A2.1 / D-002: selector unauthorized respects populated accessible_entity_ids."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from topos.query.pipeline import _selector_unauthorized

pytestmark = [pytest.mark.check("C-quality-selector-entity-grant")]


def test_selector_unauthorized_when_allow_list_empty() -> None:
    manifest = SimpleNamespace(accessible_entity_ids=[])
    with patch(
        "topos.features.entities.linking.link_query_entities",
        return_value=[{"entity_id": "ent_maya", "entity_type": "person"}],
    ):
        assert _selector_unauthorized(object(), "what did Maya say?", manifest) is True


def test_selector_authorized_when_entity_on_grant() -> None:
    manifest = SimpleNamespace(accessible_entity_ids=["ent_maya"])
    with patch(
        "topos.features.entities.linking.link_query_entities",
        return_value=[{"entity_id": "ent_maya", "entity_type": "person"}],
    ):
        assert _selector_unauthorized(object(), "meeting with Maya", manifest) is False


def test_selector_unauthorized_when_named_person_not_on_grant() -> None:
    manifest = SimpleNamespace(accessible_entity_ids=["ent_maya"])
    with patch(
        "topos.features.entities.linking.link_query_entities",
        return_value=[{"entity_id": "ent_alex", "entity_type": "person"}],
    ):
        assert _selector_unauthorized(object(), "prep with Alex", manifest) is True


def test_non_person_entities_are_not_selectors() -> None:
    manifest = SimpleNamespace(accessible_entity_ids=[])
    with patch(
        "topos.features.entities.linking.link_query_entities",
        return_value=[{"entity_id": "ent_austin", "entity_type": "place"}],
    ):
        assert _selector_unauthorized(object(), "weather in Austin", manifest) is False
