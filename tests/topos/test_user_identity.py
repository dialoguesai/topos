import sys

import pytest
from httpx import ASGITransport, AsyncClient

from topos.testing.lifespan import LifespanManager


def load_topos_app(monkeypatch, env: dict):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    state_mod = sys.modules.get("topos.core.state")
    if state_mod is not None and getattr(state_mod, "db_conn", None) is not None:
        try:
            state_mod.db_conn.close()
        except Exception:
            pass
        state_mod.db_conn = None
    for mod in (
        "topos.app",
        "topos.config.settings",
        "topos.auth",
        "topos.core.state",
        "topos.storage.user_identity",
    ):
        sys.modules.pop(mod, None)
    from topos.app import app  # noqa: E402

    return app


def test_user_identity_storage_round_trip(tmp_path):
    import sqlite3

    from topos.storage.user_identity import get_user_identity, put_user_identity

    conn = sqlite3.connect(str(tmp_path / "user_identity.db"))
    assert get_user_identity(conn, "user:default") is None

    put_user_identity(conn, "user:default", display_name="Jonny Johnson")
    assert get_user_identity(conn, "user:default") == {"display_name": "Jonny Johnson"}

    put_user_identity(conn, "user:default", display_name="Johnny")
    assert get_user_identity(conn, "user:default") == {"display_name": "Johnny"}


def test_signal_identity_storage_still_works_independently(tmp_path):
    import sqlite3

    from topos.storage.signal_identity import get_signal_identity, put_signal_identity
    from topos.storage.user_identity import put_user_identity

    conn = sqlite3.connect(str(tmp_path / "signal_identity.db"))
    put_user_identity(conn, "user:default", display_name="Jonny Johnson")
    put_signal_identity(conn, "user:default", my_phone_number="+15555550123", my_signal_id="signal-self")

    assert get_signal_identity(conn, "user:default") == {
        "my_phone_number": "+15555550123",
        "my_signal_id": "signal-self",
    }


@pytest.mark.asyncio
async def test_user_identity_api_get_and_put(monkeypatch, tmp_path):
    app = load_topos_app(
        monkeypatch,
        {
            "TOPOS_KEY": "test-key",
            "CONTROL_PLANE_URL": "",
            "DATABASE_PATH": str(tmp_path / "engine.db"),
        },
    )
    from topos.core.state import get_db_connection

    conn = get_db_connection()
    assert conn is not None
    conn.execute("DROP TABLE IF EXISTS user_identity")
    conn.commit()
    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            get_empty = await client.get(
                "/v1/user-identity",
                params={"dataset_id": "user:default"},
                headers={"Authorization": "Bearer test-key"},
            )
            assert get_empty.status_code == 200
            assert get_empty.json() == {"status": "ok", "dataset_id": "user:default", "display_name": None}

            put_resp = await client.put(
                "/v1/user-identity",
                headers={"Authorization": "Bearer test-key"},
                json={"dataset_id": "user:default", "display_name": "Jonny Johnson"},
            )
            assert put_resp.status_code == 200
            assert put_resp.json() == {
                "status": "ok",
                "dataset_id": "user:default",
                "display_name": "Jonny Johnson",
            }

            get_full = await client.get(
                "/v1/user-identity",
                params={"dataset_id": "user:default"},
                headers={"Authorization": "Bearer test-key"},
            )
            assert get_full.status_code == 200
            assert get_full.json() == {
                "status": "ok",
                "dataset_id": "user:default",
                "display_name": "Jonny Johnson",
            }


@pytest.mark.asyncio
async def test_user_identity_api_requires_dataset_id(monkeypatch, tmp_path):
    app = load_topos_app(
        monkeypatch,
        {
            "TOPOS_KEY": "test-key",
            "CONTROL_PLANE_URL": "",
            "DATABASE_PATH": str(tmp_path / "engine-missing.db"),
        },
    )
    transport = ASGITransport(app=app)
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://testserver") as client:
            resp = await client.get(
                "/v1/user-identity",
                headers={"Authorization": "Bearer test-key"},
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "error"
            assert "dataset_id" in resp.json()["error"].lower()
