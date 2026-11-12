"""Settings accept a node with no key: local-only mode (topos/local_identity.py).

protects: no key and no lease is a valid local-only configuration outside a cloud
runtime; a cloud runtime still refuses it; a key or a hosted lease turns local-only off.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.public

from topos.config.settings import Settings  # noqa: E402

_CLOUD_MARKERS = ("K_SERVICE", "K_REVISION", "CLOUD_RUN_JOB")


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("TOPOS_KEY", "HOSTED_POOL_LEASE_ENABLED", *_CLOUD_MARKERS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TOPOS_CONTROL_PLANE_URL", "wss://cp.example/ws/engine")


def test_no_key_outside_the_cloud_is_local_only(clean_env):
    s = Settings(_env_file=None)
    assert not s.topos_key
    assert s.local_only is True


def test_a_key_turns_local_only_off(clean_env, monkeypatch):
    monkeypatch.setenv("TOPOS_KEY", "tpn_" + "ab" * 20)
    assert Settings(_env_file=None).local_only is False


def test_a_hosted_lease_turns_local_only_off(clean_env, monkeypatch):
    monkeypatch.setenv("HOSTED_POOL_LEASE_ENABLED", "true")
    assert Settings(_env_file=None).local_only is False


def test_a_cloud_runtime_still_needs_a_key_or_a_lease(clean_env, monkeypatch):
    monkeypatch.setenv("K_SERVICE", "topos-node")
    monkeypatch.setenv("HOSTED_POOL_LEASE_ENABLED", "false")
    with pytest.raises(ValueError, match="TOPOS_KEY is required"):
        Settings(_env_file=None)
