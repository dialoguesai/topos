"""What ``owner_agent`` reads, under ruling D1 (b) (AI-tool on-ramp, CONTRACTS §1.3; lane L1).

protects: a special-class scope resolves to the literal ``scores_only`` for the agent with reason
``owner_agent_special_class``; every other scope resolves to the owner's own dial with reason
``owner_agent_ruling`` and the hosted-binding interlock is skipped for this class (the ruling overrides it);
the disclosure tier for the agent is an explicit branch — ``owner_raw`` for a normal scope even when the
requester id would read as a grantee, ``default_disclosure`` for a special-class scope even when the payload asks
for raw — and the grantee flag still wins.
"""
from __future__ import annotations

import sqlite3

import pytest

from topos.config.settings import ENGINE_CONFIG_KEY_PACKET_RESOLUTION
from topos.core.handlers.common import set_engine_config_value
from topos.disclosure.tier import resolve_disclosure_tier
from topos.principal import Principal
from topos.query import packet_resolution as pr
from topos.query.packet_resolution import effective_packet_resolution

pytestmark = pytest.mark.public

AGENT = Principal(cls="owner_agent", channel="uds", client_id="agent-claude-code")
HOSTED = {"local": False, "provider": "openai", "model": "gpt", "remote_engine_url": False}
LOCAL = {"local": True, "provider": "ollama", "model": "m", "remote_engine_url": False}


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE engine_config (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    yield c
    c.close()


def _resolve(conn, scope_id, *, tier="owner_raw"):
    return effective_packet_resolution(
        conn, requester_id="mcp", disclosure_tier=tier, owner_id="", principal=AGENT, scope_id=scope_id,
    )


# ---------------------------------------------------------- the special set
def test_the_special_class_scope_set_is_the_contracts():
    from topos.query.packet_resolution import SPECIAL_CLASS_SCOPES, is_special_class_scope

    assert SPECIAL_CLASS_SCOPES == frozenset({"health:read", "facts_sensitive:read"})
    assert is_special_class_scope("health:read") is True
    assert is_special_class_scope("facts_sensitive:read") is True
    assert is_special_class_scope("messages:read") is False
    assert is_special_class_scope("ai_conversations:read") is False
    assert is_special_class_scope("") is False


def test_a_registry_entry_with_a_health_or_beliefs_dimension_is_special(monkeypatch):
    from topos.query import packet_resolution as module

    entries = {
        "mind:read": {"scope_id": "mind:read", "primary_dimensions": ["Beliefs", "Profile"]},
        "body:read": {"scope_id": "body:read", "primary_dimensions": ["Health"]},
        "work_context:read": {"scope_id": "work_context:read", "primary_dimensions": ["Work"]},
    }
    monkeypatch.setattr(module, "_scope_entry", lambda sid: entries.get(sid))
    assert module.is_special_class_scope("mind:read") is True
    assert module.is_special_class_scope("body:read") is True
    assert module.is_special_class_scope("work_context:read") is False


# ---------------------------------------------------------- packet resolution
@pytest.mark.parametrize("scope_id", ["health:read", "facts_sensitive:read"])
def test_a_special_class_scope_resolves_scores_only_with_its_own_reason(conn, monkeypatch, scope_id):
    set_engine_config_value(conn, ENGINE_CONFIG_KEY_PACKET_RESOLUTION, "facts_all")
    monkeypatch.setattr(pr, "primary_binding_locality", lambda _c: dict(LOCAL))
    info = _resolve(conn, scope_id)
    assert info["effective"] == "scores_only"
    assert info["reason"] == "owner_agent_special_class"
    assert info["setting"] == "facts_all"
    assert info["principal_cls"] == "owner_agent"


@pytest.mark.parametrize("setting", ["facts", "facts_all"])
def test_a_normal_scope_resolves_to_the_owners_dial_even_when_the_binding_is_hosted(conn, monkeypatch, setting):
    """The interlock that floors owner_app to scores_only on a hosted model is skipped for this class:
    the agent's model is always hosted, and the ruling is that the owner's dial applies."""
    set_engine_config_value(conn, ENGINE_CONFIG_KEY_PACKET_RESOLUTION, setting)
    monkeypatch.setattr(pr, "primary_binding_locality", lambda _c: dict(HOSTED))
    info = _resolve(conn, "messages:read")
    assert info["effective"] == setting
    assert info["reason"] == "owner_agent_ruling"
    assert info["local"] is False
    # Control: the owner's app on the same hosted binding is still floored (unchanged).
    app = effective_packet_resolution(
        conn, requester_id="mcp", disclosure_tier="owner_raw", owner_id="",
        principal=Principal(cls="owner_app", channel="uds"), scope_id="messages:read",
    )
    assert app["effective"] == "scores_only" and app["reason"] == "hosted_binding"


def test_the_owners_scores_only_dial_is_the_agents_too(conn, monkeypatch):
    set_engine_config_value(conn, ENGINE_CONFIG_KEY_PACKET_RESOLUTION, "scores_only")
    monkeypatch.setattr(pr, "primary_binding_locality", lambda _c: dict(LOCAL))
    info = _resolve(conn, "messages:read")
    assert info["effective"] == "scores_only" and info["reason"] == "owner_agent_ruling"


def test_a_default_disclosure_tier_still_floors_the_agent(conn, monkeypatch):
    """The tier leg stays the independent guard, as for owner_app."""
    set_engine_config_value(conn, ENGINE_CONFIG_KEY_PACKET_RESOLUTION, "facts_all")
    monkeypatch.setattr(pr, "primary_binding_locality", lambda _c: dict(LOCAL))
    info = _resolve(conn, "messages:read", tier="default_disclosure")
    assert info["effective"] == "scores_only" and info["reason"] == "non_owner_floor"


RELAYED = Principal(cls="owner_agent", channel="cp_relay", client_id="agent-claude-code")


def test_the_word_on_any_other_channel_is_floored_like_a_third_party(conn, monkeypatch):
    """Minted on the socket only (rev-1 security 19): a stamp or a forwarder naming the class gets the
    third-party floor, even with the owner's own ids in the payload and the owner's dial at facts_all."""
    set_engine_config_value(conn, ENGINE_CONFIG_KEY_PACKET_RESOLUTION, "facts_all")
    monkeypatch.setattr(pr, "primary_binding_locality", lambda _c: dict(LOCAL))
    info = effective_packet_resolution(
        conn, requester_id="the-owner", disclosure_tier="owner_raw", owner_id="the-owner",
        principal=RELAYED, scope_id="messages:read",
    )
    assert info["effective"] == "scores_only"
    assert info["reason"] == "principal_floor"
    tier = resolve_disclosure_tier(
        requester_id="the-owner", owner_id="the-owner", explicit_tier="owner_raw", principal=RELAYED,
        scope_id="messages:read",
    )
    assert tier == "default_disclosure"


# ---------------------------------------------------------- disclosure tier
def test_owner_agent_tier_is_owner_raw_for_a_normal_scope_never_the_requester_heuristic():
    tier = resolve_disclosure_tier(
        requester_id="grant:someone-else", owner_id="the-owner", principal=AGENT, scope_id="messages:read",
    )
    assert tier == "owner_raw"


def test_owner_agent_tier_is_default_disclosure_for_a_special_scope_even_when_asked_raw():
    tier = resolve_disclosure_tier(
        requester_id="the-owner", owner_id="the-owner", explicit_tier="owner_raw", principal=AGENT,
        scope_id="health:read",
    )
    assert tier == "default_disclosure"


def test_owner_agent_tier_honours_an_explicit_downgrade_and_the_grantee_flag():
    assert resolve_disclosure_tier(
        principal=AGENT, scope_id="messages:read", explicit_tier="default_disclosure",
    ) == "default_disclosure"
    assert resolve_disclosure_tier(
        principal=AGENT, scope_id="messages:read", is_grantee_request=True,
    ) == "default_disclosure"
