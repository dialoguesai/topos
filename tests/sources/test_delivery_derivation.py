"""Delivery derivation, serialization round-trips, and old-JSON rehydration (CONNECTOR_SPEC.md §3)."""

from __future__ import annotations

import pytest

from topos.sources.definitions import (
    DELIVERY_CLIENT_PUSH,
    DELIVERY_LOCAL_SYNC,
    DELIVERY_OWNER_UPLOAD,
    DataSourceDefinition,
    accepts_app_ingest,
    definition_from_payload,
    derive_delivery,
    is_file_delivery,
    source_type_for_delivery,
)


def _minimal(**overrides):
    base = {
        "source_id": "test_source",
        "display_name": "Test Source",
        "source_type": "file",
        "schema_id": "test.v1",
        "parser_id": "test.v1",
    }
    base.update(overrides)
    return DataSourceDefinition(**base)


def test_derive_delivery_covers_all_legacy_source_types():
    assert derive_delivery("file") == DELIVERY_OWNER_UPLOAD
    assert derive_delivery("local_sync") == DELIVERY_LOCAL_SYNC
    assert derive_delivery("ui_stream") == DELIVERY_CLIENT_PUSH
    assert derive_delivery("stub") is None
    assert derive_delivery("") is None


def test_source_type_back_derivation():
    assert source_type_for_delivery("owner_upload") == "file"
    assert source_type_for_delivery("owner_ui") == "ui_stream"
    assert source_type_for_delivery("client_push") == "ui_stream"
    assert source_type_for_delivery("local_sync") == "local_sync"
    assert source_type_for_delivery(None) is None


def test_definition_derives_delivery_when_unset():
    assert _minimal(source_type="file").delivery == DELIVERY_OWNER_UPLOAD
    assert _minimal(source_type="ui_stream").delivery == DELIVERY_CLIENT_PUSH
    assert _minimal(source_type="local_sync").delivery == DELIVERY_LOCAL_SYNC
    assert _minimal(source_type="stub").delivery is None


def test_explicit_delivery_wins_and_invalid_rejected():
    defn = _minimal(source_type="ui_stream", delivery="owner_ui")
    assert defn.delivery == "owner_ui"
    with pytest.raises(ValueError, match="delivery"):
        _minimal(delivery="teleport")


def test_to_dict_emits_both_source_type_and_delivery():
    payload = _minimal(source_type="ui_stream").to_dict()
    assert payload["source_type"] == "ui_stream"
    assert payload["delivery"] == DELIVERY_CLIENT_PUSH


def test_round_trip_preserves_delivery():
    original = _minimal(source_type="ui_stream", delivery="owner_ui")
    rehydrated = definition_from_payload(original.to_dict())
    assert rehydrated.delivery == "owner_ui"
    assert rehydrated.source_type == "ui_stream"


def test_old_json_without_delivery_rehydrates_with_derivation():
    payload = _minimal(source_type="file").to_dict()
    payload.pop("delivery")
    rehydrated = definition_from_payload(payload)
    assert rehydrated.delivery == DELIVERY_OWNER_UPLOAD


def test_unknown_future_keys_are_ignored_on_rehydration():
    payload = _minimal().to_dict()
    payload["some_future_field"] = {"x": 1}
    rehydrated = definition_from_payload(payload)
    assert rehydrated.source_id == "test_source"


def test_delivery_first_payload_back_derives_source_type():
    payload = _minimal().to_dict()
    payload.pop("source_type")
    payload["delivery"] = "client_push"
    rehydrated = definition_from_payload(payload)
    assert rehydrated.source_type == "ui_stream"
    assert rehydrated.delivery == "client_push"


def test_runtime_predicates():
    assert is_file_delivery(_minimal(source_type="file"))
    assert not is_file_delivery(_minimal(source_type="ui_stream"))
    assert accepts_app_ingest(_minimal(source_type="ui_stream"))
    assert accepts_app_ingest(_minimal(source_type="ui_stream", delivery="owner_ui"))
    assert not accepts_app_ingest(_minimal(source_type="file"))
    assert not accepts_app_ingest(_minimal(source_type="stub"))
