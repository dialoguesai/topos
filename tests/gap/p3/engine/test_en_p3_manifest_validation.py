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


def test_resolve_manifest_rejects_ceiling_escalation() -> None:
    with pytest.raises(ManifestValidationError) as exc_info:
        resolve_scope_manifest(
            "health:read",
            client_manifest={"access_mode_ceiling": "inference"},
        )
    assert exc_info.value.code == "ceiling_escalation"


def test_resolve_manifest_rejects_legacy_scope() -> None:
    with pytest.raises(ManifestValidationError) as exc_info:
        resolve_scope_manifest("aiMessages:read")
    assert exc_info.value.code == "legacy_scope"
