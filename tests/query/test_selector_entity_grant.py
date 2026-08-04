"""A2.1 / D-002: selector unauthorized respects populated accessible_entity_ids.

Semantics (A2.1 finish):
- entity_selector_policy_active=False → never unauthorized (legacy unrestricted)
- policy active + empty allow-list → deny any named person
- policy active + allow-list → permit only listed persons
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from topos.query.pipeline import _selector_enforcement_enabled, _selector_unauthorized

pytestmark = [pytest.mark.check("C-quality-selector-entity-grant")]


def _manifest(*, ids=None, active=True):
    return SimpleNamespace(
        accessible_entity_ids=list(ids or []),
        entity_selector_policy_active=active,
    )


def test_selector_unauthorized_when_allow_list_empty() -> None:
    manifest = _manifest(ids=[], active=True)
    with patch(
        "topos.features.entities.linking.link_query_entities",
        return_value=[{"entity_id": "ent_maya", "entity_type": "person"}],
    ):
        assert _selector_unauthorized(object(), "what did Maya say?", manifest) is True


def test_selector_authorized_when_entity_on_grant() -> None:
    manifest = _manifest(ids=["ent_maya"], active=True)
    with patch(
        "topos.features.entities.linking.link_query_entities",
        return_value=[{"entity_id": "ent_maya", "entity_type": "person"}],
    ):
        assert _selector_unauthorized(object(), "meeting with Maya", manifest) is False


def test_selector_unauthorized_when_named_person_not_on_grant() -> None:
    """A2.E2 false-permit: person off allow-list must be unauthorized."""
    manifest = _manifest(ids=["ent_maya"], active=True)
    with patch(
        "topos.features.entities.linking.link_query_entities",
        return_value=[{"entity_id": "ent_alex", "entity_type": "person"}],
    ):
        assert _selector_unauthorized(object(), "prep with Alex", manifest) is True


def test_non_person_entities_are_not_selectors() -> None:
    manifest = _manifest(ids=[], active=True)
    with patch(
        "topos.features.entities.linking.link_query_entities",
        return_value=[{"entity_id": "ent_austin", "entity_type": "place"}],
    ):
        assert _selector_unauthorized(object(), "weather in Austin", manifest) is False


def test_legacy_missing_policy_is_unrestricted() -> None:
    """Missing entity-policy keys must not lock out named people (safe default-ON)."""
    manifest = _manifest(ids=[], active=False)
    with patch(
        "topos.features.entities.linking.link_query_entities",
        return_value=[{"entity_id": "ent_maya", "entity_type": "person"}],
    ):
        assert _selector_unauthorized(object(), "what did Maya say?", manifest) is False


def test_a2_e3_access_advantage_empty_active_denies_named_person() -> None:
    """A2.E3: active empty allow-list suppresses before retrieve (access-advantage 0)."""
    manifest = _manifest(ids=[], active=True)
    with patch(
        "topos.features.entities.linking.link_query_entities",
        return_value=[{"entity_id": "ent_maya", "entity_type": "person"}],
    ):
        assert _selector_unauthorized(object(), "Maya salary?", manifest) is True


def test_selector_enforcement_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TOPOS_SELECTOR_ENFORCEMENT", raising=False)
    assert _selector_enforcement_enabled() is True
    monkeypatch.setenv("TOPOS_SELECTOR_ENFORCEMENT", "off")
    assert _selector_enforcement_enabled() is False
    monkeypatch.setenv("TOPOS_SELECTOR_ENFORCEMENT", "1")
    assert _selector_enforcement_enabled() is True


def test_fabricated_person_ask_suppresses_under_active_policy() -> None:
    """A7 / D1.3: unlinked person-shaped ask must suppress (denial≡absence)."""
    from topos.query.pipeline import _looks_like_named_person_ask

    manifest = _manifest(ids=["ent_maya"], active=True)
    with patch("topos.features.entities.linking.link_query_entities", return_value=[]):
        assert (
            _selector_unauthorized(
                object(), "Tell me everything about Zephyrine Quaddlebock", manifest
            )
            is True
        )
    assert _looks_like_named_person_ask("What has Zephyrine Quaddlebock said to me?") is True
    assert _looks_like_named_person_ask("show messages about the houseboat sale") is False
