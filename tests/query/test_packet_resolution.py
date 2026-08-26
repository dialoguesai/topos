"""Packet resolution (PLAN_DERIVATION_LAYER.md, owner decision 2026-08-25).

The privacy claims are the tests: floors hold, caches isolate, downgrades expire,
and scores_only is byte-compatible with the pre-feature packet.
"""
import json
import sqlite3

import pytest

from topos.config.settings import (
    ENGINE_CONFIG_KEY_PACKET_RESOLUTION,
    resolve_packet_resolution,
    settings,
)
from topos.core.handlers.common import set_engine_config_value
from topos.query.fingerprint import compute_retrieval_fingerprint
from topos.query.inference import build_inference_context_packet
from topos.query.packet_resolution import (
    RESOLUTIONS,
    effective_packet_resolution,
    primary_binding_locality,
    resolution_order,
)
from topos.query.session_utils import build_cache_key


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE engine_config (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    yield c
    c.close()


# ---------------------------------------------------------------- resolver
def test_default_is_scores_only(conn):
    assert resolve_packet_resolution(settings, conn) == "scores_only"


def test_engine_config_overrides_default(conn):
    set_engine_config_value(conn, ENGINE_CONFIG_KEY_PACKET_RESOLUTION, "facts_all")
    assert resolve_packet_resolution(settings, conn) == "facts_all"


def test_invalid_stored_value_falls_back(conn):
    set_engine_config_value(conn, ENGINE_CONFIG_KEY_PACKET_RESOLUTION, "everything!!")
    assert resolve_packet_resolution(settings, conn) == "scores_only"


# ---------------------------------------------------------------- floors
def test_non_owner_floor_beats_owner_setting(conn):
    set_engine_config_value(conn, ENGINE_CONFIG_KEY_PACKET_RESOLUTION, "facts_all")
    info = effective_packet_resolution(conn, requester_id="grant:maya", disclosure_tier="default_disclosure")
    assert info["effective"] == "scores_only"
    assert info["reason"] == "non_owner_floor"
    assert info["setting"] == "facts_all"  # the setting is reported, the floor still holds


def test_grantee_tier_alone_triggers_floor(conn):
    set_engine_config_value(conn, ENGINE_CONFIG_KEY_PACKET_RESOLUTION, "facts")
    info = effective_packet_resolution(conn, requester_id="owner", disclosure_tier="default_disclosure")
    assert info["effective"] == "scores_only" and info["reason"] == "non_owner_floor"


def test_hosted_binding_floor(conn, monkeypatch):
    set_engine_config_value(conn, ENGINE_CONFIG_KEY_PACKET_RESOLUTION, "facts")
    monkeypatch.setattr(settings, "topos_engine_service_url", "https://engine.example.com")
    info = effective_packet_resolution(conn, requester_id="owner", disclosure_tier="owner_raw")
    assert info["effective"] == "scores_only"
    assert info["reason"] == "hosted_binding"
    assert info["local"] is False


def test_owner_local_active(conn, monkeypatch):
    set_engine_config_value(conn, ENGINE_CONFIG_KEY_PACKET_RESOLUTION, "facts")
    monkeypatch.setattr(settings, "topos_engine_service_url", None)
    info = effective_packet_resolution(conn, requester_id="owner", disclosure_tier="owner_raw")
    assert info["effective"] == "facts" and info["reason"] == "active"


def test_locality_flags_remote_engine_url(monkeypatch):
    monkeypatch.setattr(settings, "topos_engine_service_url", "https://x.example")
    assert primary_binding_locality(None)["local"] is False
    monkeypatch.setattr(settings, "topos_engine_service_url", None)
    assert primary_binding_locality(None)["local"] is True


# ---------------------------------------------------------------- cache isolation
def test_fingerprint_and_cache_key_isolate_resolutions():
    base = dict(scope_id="health:read", access_mode="inference")
    fps = {compute_retrieval_fingerprint(**base, packet_resolution=r) for r in RESOLUTIONS}
    assert len(fps) == 3, "each resolution must be its own fingerprint"
    keys = {build_cache_key(**base, intent_hash="abc", packet_resolution=r) for r in RESOLUTIONS}
    assert len(keys) == 3
    # legacy compatibility: scores_only keeps the pre-feature 3-part key
    assert build_cache_key(**base, intent_hash="abc") == "health:read:inference:abc"
    assert build_cache_key(**base, intent_hash="abc", packet_resolution="scores_only") == "health:read:inference:abc"


def test_resolution_order_total():
    assert resolution_order("scores_only") < resolution_order("facts") < resolution_order("facts_all")
    assert resolution_order("garbage") == 0  # unknown values sort as the strictest


# ---------------------------------------------------------------- packet content
_FACT_ITEM = {
    "retrieval_source": "fact", "summary_text": "owner takes metformin", "relevance_score": 0.9,
    "predicate": "health.medication", "value": "metformin", "valid_from": "2026-07-02",
    "altitude": "stated", "pack": "health.physical",
}


def test_scores_only_packet_has_no_facts_block():
    out = build_inference_context_packet({"scores": [dict(_FACT_ITEM)]}, packet_resolution="scores_only")
    assert '"facts"' not in out["context"]


def test_facts_packet_carries_structured_block():
    out = build_inference_context_packet({"scores": [dict(_FACT_ITEM)]}, packet_resolution="facts")
    ctx = json.loads(out["context"])
    assert ctx["facts"][0]["predicate"] == "health.medication"
    assert ctx["facts"][0]["value"] == "metformin"
    assert ctx["facts"][0]["valid_from"] == "2026-07-02"


def test_facts_block_survives_truncation_first():
    # facts are placed before scores: a tight budget must cut scores, not facts
    big = {"scores": [dict(_FACT_ITEM)] + [{"relevance_score": 0.5, "note": "x" * 50}] * 60}
    out = build_inference_context_packet(big, max_chars=900, packet_resolution="facts")
    assert out["truncated"] is True
    assert '"facts"' in out["context"][:400]
