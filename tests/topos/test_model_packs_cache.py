"""Tests for the node-side model-pack cache (PLAN_MODEL_PACKS.md M2).

Covers the module's stated "done when": the resolver answers from cache with
the network unreachable; an equal revision does not rewrite the cache and a
stale one does; a cold cache returns None rather than erroring.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional

import pytest

from topos.config.model_packs import (
    ROLES,
    apply_sync_payload,
    cached_revision,
    fetch_model_packs_sync_payload,
    resolve_role_model,
    sync_model_packs_from_control_plane,
)
from topos.config.settings import Settings


def _memory_conn() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")


@pytest.fixture()
def configured_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("TOPOS_KEY", "test-engine-key-12345678901234567890123456789012")
    monkeypatch.setenv("TOPOS_CONTROL_PLANE_URL", "wss://cp.example.test/ws/engine")
    return Settings()


def _sync_payload(*, revision: int, active: str = "pack-1", roles: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "revision": revision,
        "active": active,
        "packs": [
            {
                "pack_id": active,
                "roles": roles
                or {role: {"provider": "ollama", "model": f"{role}-model:latest"} for role in ROLES},
            }
        ],
    }


# --- resolve_role_model: cache-only, never errors -----------------------------------


def test_resolve_role_model_returns_none_with_no_connection():
    assert resolve_role_model(None, "primary") is None


def test_resolve_role_model_returns_none_on_a_cold_cache():
    conn = _memory_conn()
    assert resolve_role_model(conn, "primary") is None


def test_resolve_role_model_returns_the_active_packs_binding():
    conn = _memory_conn()
    apply_sync_payload(conn, _sync_payload(revision=1))
    assert resolve_role_model(conn, "tool") == ("ollama", "tool-model:latest")


def test_resolve_role_model_returns_none_for_a_role_the_active_pack_does_not_bind():
    conn = _memory_conn()
    apply_sync_payload(conn, _sync_payload(revision=1, roles={"primary": {"provider": "ollama", "model": "m"}}))
    assert resolve_role_model(conn, "classify") is None


def test_resolve_role_model_is_case_and_whitespace_tolerant_on_role():
    conn = _memory_conn()
    apply_sync_payload(conn, _sync_payload(revision=1))
    assert resolve_role_model(conn, "  PRIMARY ") == ("ollama", "primary-model:latest")


# --- cached_revision -----------------------------------------------------------------


def test_cached_revision_is_zero_when_cold():
    assert cached_revision(_memory_conn()) == 0
    assert cached_revision(None) == 0


def test_cached_revision_reflects_the_last_applied_payload():
    conn = _memory_conn()
    apply_sync_payload(conn, _sync_payload(revision=42))
    assert cached_revision(conn) == 42


# --- apply_sync_payload: equal revision no-ops, a different one overwrites -----------


def test_apply_sync_payload_writes_on_first_sync():
    conn = _memory_conn()
    assert apply_sync_payload(conn, _sync_payload(revision=1)) is True
    assert cached_revision(conn) == 1


def test_apply_sync_payload_skips_the_write_when_revision_is_unchanged():
    conn = _memory_conn()
    apply_sync_payload(conn, _sync_payload(revision=5))
    wrote_again = apply_sync_payload(
        conn,
        _sync_payload(revision=5, roles={"primary": {"provider": "openai", "model": "should-not-apply"}}),
    )
    assert wrote_again is False
    # The stale-looking payload above must not have overwritten anything.
    assert resolve_role_model(conn, "primary") == ("ollama", "primary-model:latest")


def test_apply_sync_payload_overwrites_when_revision_changes():
    conn = _memory_conn()
    apply_sync_payload(conn, _sync_payload(revision=5))
    wrote = apply_sync_payload(
        conn,
        _sync_payload(revision=6, roles={"primary": {"provider": "openai", "model": "gpt-4o"}}),
    )
    assert wrote is True
    assert cached_revision(conn) == 6
    assert resolve_role_model(conn, "primary") == ("openai", "gpt-4o")


# --- network path: unreachable control plane leaves the cache exactly alone ----------


class _UnconfiguredSettings:
    """A minimal stand-in for the real `Settings`, without its "TOPOS_KEY is
    required" startup validator — this exercises the *runtime* not-configured
    path (e.g. dev/test operation with connectivity absent), not the
    process-startup one, which is asserted elsewhere."""

    topos_key = None
    topos_control_plane_url = "wss://cp.example.test/ws/engine"


@pytest.mark.asyncio
async def test_fetch_returns_none_when_control_plane_key_is_not_configured():
    assert await fetch_model_packs_sync_payload(_UnconfiguredSettings()) is None


@pytest.mark.asyncio
async def test_fetch_returns_none_when_the_network_is_unreachable(
    configured_settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    class _RaisingAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> "_RaisingAsyncClient":
            return self

        async def __aexit__(self, *exc_info) -> None:
            return None

        async def get(self, *args, **kwargs):
            raise ConnectionError("simulated network failure")

    import topos.config.model_packs as model_packs_module

    monkeypatch.setattr(model_packs_module.httpx, "AsyncClient", _RaisingAsyncClient)
    assert await fetch_model_packs_sync_payload(configured_settings) is None


@pytest.mark.asyncio
async def test_sync_leaves_the_cache_untouched_when_the_control_plane_is_unreachable(
    configured_settings: Settings, monkeypatch: pytest.MonkeyPatch
):
    conn = _memory_conn()
    apply_sync_payload(conn, _sync_payload(revision=3))

    async def _fail(*args, **kwargs):
        return None

    import topos.config.model_packs as model_packs_module

    monkeypatch.setattr(model_packs_module, "fetch_model_packs_sync_payload", _fail)
    wrote = await sync_model_packs_from_control_plane(configured_settings, conn)
    assert wrote is False
    # Still answers from the cache it had before the failed sync.
    assert cached_revision(conn) == 3
    assert resolve_role_model(conn, "primary") == ("ollama", "primary-model:latest")


@pytest.mark.asyncio
async def test_sync_with_no_connection_returns_false_without_touching_anything(configured_settings: Settings):
    assert await sync_model_packs_from_control_plane(configured_settings, None) is False


@pytest.mark.asyncio
async def test_sync_applies_a_reachable_payload(configured_settings: Settings, monkeypatch: pytest.MonkeyPatch):
    conn = _memory_conn()

    async def _fake_fetch(*args, **kwargs):
        return _sync_payload(revision=9)

    import topos.config.model_packs as model_packs_module

    monkeypatch.setattr(model_packs_module, "fetch_model_packs_sync_payload", _fake_fetch)
    wrote = await sync_model_packs_from_control_plane(configured_settings, conn)
    assert wrote is True
    assert cached_revision(conn) == 9
