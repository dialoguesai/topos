from __future__ import annotations

import pytest

from topos.__version__ import __version__
from topos.services.postgres import HostedDeviceService
import topos.core.state as state
from topos.config.settings import settings


@pytest.mark.asyncio
async def test_hosted_device_info_returns_minimal_profile(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "topos_user_id", "user-123", raising=False)
    monkeypatch.setattr(settings, "topos_default_dataset_id", "default", raising=False)
    monkeypatch.setattr(settings, "topos_database_mode", "postgres", raising=False)
    monkeypatch.setattr(settings, "enable_llm", True, raising=False)
    monkeypatch.setattr(settings, "engine_name", "Hosted Topos", raising=False)
    monkeypatch.setattr(state, "get_engine_mode", lambda: "full", raising=True)
    monkeypatch.setattr(state, "get_engine_class", lambda: "full_engine", raising=True)

    info = await HostedDeviceService().get_device_info(
        context={"owner_user_id": "user-123", "tenant_id": "abcd1234ef567890"}
    )

    assert info.engine_version == __version__
    assert info.database_mode == "postgres"
    assert info.user_id == "user-123"
    assert info.dataset_id == "user-123:default:abcd1234ef567890"
    assert info.sync_connected is False
    assert info.sync_enabled is False
    assert info.system == {}
    assert info.engine_name == "Hosted Topos"


@pytest.mark.asyncio
async def test_hosted_device_info_prefers_context_owner_over_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "topos_user_id", "settings-user", raising=False)
    monkeypatch.setattr(settings, "topos_database_mode", "postgres", raising=False)
    monkeypatch.setattr(state, "get_engine_mode", lambda: "sync", raising=True)
    monkeypatch.setattr(state, "get_engine_class", lambda: "sync_engine", raising=True)

    info = await HostedDeviceService().get_device_info(context={"owner_user_id": "pooled-user"})

    assert info.user_id == "pooled-user"
    assert info.dataset_id == "pooled-user:default"
    assert info.engine_mode == "sync"
    assert info.llm_enabled is False
