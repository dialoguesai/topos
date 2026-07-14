from __future__ import annotations

import pytest

from topos.config.settings import Settings


def test_log_level_defaults_to_info(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.setenv("TOPOS_KEY", "test-key")

    cfg = Settings(_env_file=None)

    assert cfg.log_level == "INFO"


def test_log_level_can_be_overridden(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("TOPOS_KEY", "test-key")

    cfg = Settings(_env_file=None)

    assert cfg.log_level == "DEBUG"


def test_cloud_runtime_defaults_to_lease_mode_without_explicit_flag(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("K_SERVICE", "topos-database")
    monkeypatch.setenv("TOPOS_CONTROL_PLANE_URL", "wss://cp.example/ws/engine")
    monkeypatch.setenv("TOPOS_KEY", "")
    monkeypatch.delenv("HOSTED_POOL_LEASE_ENABLED", raising=False)

    cfg = Settings()
    assert cfg.hosted_pool_lease_enabled is True


def test_cloud_runtime_rejects_static_key_without_break_glass(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("K_SERVICE", "topos-database")
    monkeypatch.setenv("TOPOS_CONTROL_PLANE_URL", "wss://cp.example/ws/engine")
    monkeypatch.setenv("TOPOS_KEY", "static-key-123")
    monkeypatch.setenv("HOSTED_POOL_LEASE_ENABLED", "false")
    monkeypatch.delenv("HOSTED_POOL_ALLOW_STATIC_KEY_IN_CLOUD", raising=False)

    with pytest.raises(ValueError, match="Cloud runtime requires hosted pool lease mode"):
        Settings()


def test_cloud_runtime_allows_break_glass_static_key_when_enabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("K_SERVICE", "topos-database")
    monkeypatch.setenv("TOPOS_CONTROL_PLANE_URL", "wss://cp.example/ws/engine")
    monkeypatch.setenv("TOPOS_KEY", "static-key-123")
    monkeypatch.setenv("HOSTED_POOL_LEASE_ENABLED", "false")
    monkeypatch.setenv("HOSTED_POOL_ALLOW_STATIC_KEY_IN_CLOUD", "true")

    cfg = Settings()
    assert cfg.topos_key == "static-key-123"
    assert cfg.hosted_pool_lease_enabled is False
