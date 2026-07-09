"""P1.5: exposure-profile visibility config handlers (get/put) round-trip.

The React toggle -> CP /v1/exposure-profile-config -> these engine handlers ->
engine_config key 'exposure_profile_visible' -> the retrieval read path.
"""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

import topos.core.handlers as hub
import topos.core.handlers.config as cfg
from topos.config.settings import exposure_profile_visible
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(monkeypatch):
    c = sqlite3.connect(":memory:")
    apply_all_migrations(c)
    monkeypatch.setattr(hub, "get_db_connection", lambda: c)
    yield c
    c.close()


def test_get_defaults_to_visible(conn):
    out = asyncio.run(cfg.handle_get_exposure_profile_config({"id": "1"}))
    assert out["status"] == "ok"
    assert out["payload"]["exposure_profile_visible"] is True


def test_put_false_then_read_path_sees_it(conn):
    put = asyncio.run(
        cfg.handle_put_exposure_profile_config(
            {"id": "2", "payload": {"exposure_profile_visible": False}}
        )
    )
    assert put["status"] == "ok" and put["payload"]["exposure_profile_visible"] is False
    # the engine retrieval read path reflects the persisted toggle
    assert exposure_profile_visible(conn) is False
    # and a fresh GET agrees
    got = asyncio.run(cfg.handle_get_exposure_profile_config({"id": "3"}))
    assert got["payload"]["exposure_profile_visible"] is False


def test_put_accepts_string_truthiness_and_re_enable(conn):
    asyncio.run(cfg.handle_put_exposure_profile_config({"id": "4", "payload": {"exposure_profile_visible": False}}))
    asyncio.run(cfg.handle_put_exposure_profile_config({"id": "5", "payload": {"exposure_profile_visible": "true"}}))
    assert exposure_profile_visible(conn) is True


def test_put_missing_key_errors(conn):
    out = asyncio.run(cfg.handle_put_exposure_profile_config({"id": "6", "payload": {}}))
    assert out["status"] == "error"
