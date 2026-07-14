import pytest

from topos.api.enrichment import _list_source_enrichments_core
from topos.sources.definitions import (
    SOURCE_KIND_DERIVED,
    SOURCE_KIND_INGESTION,
    SOURCE_KIND_SCOPE_MANIFEST,
    source_capabilities_from_payload,
    source_kind_from_payload,
    with_source_capabilities,
)
from topos.sources.registry import CANONICAL_ADDRESS_BOOK, get_sources_by_scope
from topos.sources.install_service import InstallRecord, _validate_source_contract


def test_runtime_source_capabilities_are_inferred_for_legacy_definitions() -> None:
    definition = {
        "source_id": "browser_visits",
        "source_type": "ui_stream",
        "schema_id": "browser_visits",
        "parser_id": "browser_visits",
    }
    assert source_kind_from_payload(definition) == SOURCE_KIND_INGESTION
    assert source_capabilities_from_payload(definition) == {
        "supports_ingestion": True,
        "supports_enrichment_metadata": True,
        "supports_generic_scrub": True,
        "supports_uninstall": True,
    }


def test_incomplete_legacy_install_is_scope_manifest_and_deny_by_default() -> None:
    annotated = with_source_capabilities(
        {
            "source_id": "global",
            "display_name": "Address Book (merged)",
            "default_scope_id": "contacts",
        }
    )
    assert annotated["source_kind"] == SOURCE_KIND_SCOPE_MANIFEST
    assert not any(annotated["source_capabilities"].values())


def test_canonical_address_book_is_an_explicit_derived_capability() -> None:
    serialized = CANONICAL_ADDRESS_BOOK.to_dict()
    assert serialized["source_kind"] == SOURCE_KIND_DERIVED
    assert not any(serialized["source_capabilities"].values())
    assert _list_source_enrichments_core(CANONICAL_ADDRESS_BOOK.source_id)[
        "enrichments"
    ] == []
    assert CANONICAL_ADDRESS_BOOK.source_id in get_sources_by_scope(
        "relationship_context:read"
    )


def test_install_status_serializes_engine_capabilities() -> None:
    record = InstallRecord(
        install_id="install-1",
        scope={},
        source_id="browser_visits",
        version_id=None,
        status="active",
        is_active=True,
        source_definition_json={
            "source_id": "browser_visits",
            "source_type": "ui_stream",
            "schema_id": "browser_visits",
            "parser_id": "browser_visits",
        },
        source_version_row_json=None,
        failure_reason=None,
        created_at="",
        updated_at="",
    )

    definition = record.to_dict()["source_definition_json"]
    assert definition["source_kind"] == SOURCE_KIND_INGESTION
    assert definition["source_capabilities"]["supports_enrichment_metadata"] is True


def test_runtime_install_cannot_claim_derived_source_kind() -> None:
    with pytest.raises(ValueError, match="source_kind='ingestion'"):
        _validate_source_contract(
            {
                "source_id": "not-a-derived-capability",
                "source_type": "ui_stream",
                "source_kind": "derived",
                "schema_id": "custom.v1",
                "parser_id": "custom.v1",
            }
        )

