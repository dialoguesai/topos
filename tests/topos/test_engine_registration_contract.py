from __future__ import annotations

from topos.engine.registration import (
    CAPABILITIES_SCHEMA_VERSION,
    build_engine_capabilities,
    build_engine_heartbeat_message,
    build_engine_register_message,
)


def test_build_engine_capabilities_contract_shape():
    payload = build_engine_capabilities()
    assert payload["schema_version"] in (CAPABILITIES_SCHEMA_VERSION, "v1", "v2")
    assert isinstance(payload["providers"], list)
    assert isinstance(payload["models"], list)
    assert "supports_filtering" in payload
    assert "supports_sanitization" in payload
    assert isinstance(payload.get("operations"), list)
    assert payload.get("runtime_profile", {}).get("id")
    assert "limits" in payload
    assert "transport" in payload


def test_register_message_contains_id_type_payload():
    msg = build_engine_register_message()
    assert isinstance(msg["id"], str) and msg["id"]
    assert msg["type"] == "engine_register"
    assert msg["payload"]["status"] == "connected"
    assert "capabilities" in msg["payload"]


def test_heartbeat_message_contains_id_type_payload():
    msg = build_engine_heartbeat_message()
    assert isinstance(msg["id"], str) and msg["id"]
    assert msg["type"] == "engine_heartbeat"
    assert msg["payload"]["status"] == "connected"
