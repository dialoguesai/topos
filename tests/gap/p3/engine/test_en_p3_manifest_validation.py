"""GT-EN-P3: Engine-side manifest validation from scope registry."""

import pytest

from topos.query.manifest_validation import ManifestValidationError, resolve_scope_manifest

pytestmark = pytest.mark.gap


def test_resolve_manifest_uses_registry_not_client_tables() -> None:
    manifest = resolve_scope_manifest(
        "messages:read",
        client_manifest={
            "scope_id": "messages:read",
            "canonical_tables": ["secret_table"],
            "access_mode_ceiling": "raw",
        },
    )
    assert manifest.scope_id == "messages:read"
    assert "conversation_messages" in manifest.canonical_tables
    assert "secret_table" not in manifest.canonical_tables


def test_resolve_manifest_ceiling_from_supported_objects() -> None:
    manifest = resolve_scope_manifest("messages:read")
    assert manifest.access_mode_ceiling == "raw"


def test_resolve_manifest_applies_grant_ceiling() -> None:
    manifest = resolve_scope_manifest(
        "messages:read",
        filter_manifest={"filter_manifest": {"access_mode_ceiling": "summary"}},
    )
    assert manifest.access_mode_ceiling == "summary"


def test_resolve_manifest_rejects_legacy_scope() -> None:
    with pytest.raises(ManifestValidationError) as exc_info:
        resolve_scope_manifest("aiMessages:read")
    assert exc_info.value.code == "legacy_scope"


def test_resolve_manifest_applies_scope_table_allowlist() -> None:
    manifest = resolve_scope_manifest(
        "contacts:resolve",
        filter_manifest={"scope_table_allowlist": {"contacts:resolve": ["contacts"]}},
    )
    assert manifest.canonical_tables == ["contacts"]


@pytest.mark.check("C-quality-selector-entity-grant")
def test_resolve_manifest_maps_accessible_entity_ids_from_grant_filters() -> None:
    """D-002 / A2.1: grant sibling accessible_entity_ids populate the selector allow-list."""
    manifest = resolve_scope_manifest(
        "messages:read",
        filter_manifest={
            "filter_manifest": {"access_mode_ceiling": "summary"},
            "accessible_entity_ids": ["ent_maya", "ent_alex", "ent_maya"],
            "accessible_entity_cohorts": ["none"],
        },
    )
    assert manifest.accessible_entity_ids == ["ent_maya", "ent_alex"]
    assert manifest.accessible_entity_cohorts == ["none"]
    assert manifest.entity_selector_policy_active is True


@pytest.mark.check("C-quality-selector-entity-grant")
def test_resolve_manifest_unknown_cohort_does_not_widen_access() -> None:
    """Cohorts are accepted for audit / A8 aggregate permit but do not add entity ids (C1)."""
    manifest = resolve_scope_manifest(
        "messages:read",
        filter_manifest={"accessible_entity_cohorts": ["contacts", "calendar_attendees"]},
    )
    assert manifest.accessible_entity_ids == []
    assert manifest.accessible_entity_cohorts == ["contacts", "calendar_attendees"]
    assert manifest.entity_selector_policy_active is True


@pytest.mark.check("C-quality-selector-entity-grant")
def test_resolve_manifest_missing_entity_policy_is_inactive() -> None:
    """Legacy grants without entity keys stay unrestricted under default-ON enforcement."""
    manifest = resolve_scope_manifest(
        "messages:read",
        filter_manifest={"filter_manifest": {"access_mode_ceiling": "summary"}},
    )
    assert manifest.entity_selector_policy_active is False
    assert manifest.accessible_entity_ids == []


@pytest.mark.check("C-quality-selector-entity-grant")
def test_resolve_manifest_empty_entity_ids_activates_deny_all() -> None:
    manifest = resolve_scope_manifest(
        "messages:read",
        filter_manifest={"accessible_entity_ids": []},
    )
    assert manifest.entity_selector_policy_active is True
    assert manifest.accessible_entity_ids == []
