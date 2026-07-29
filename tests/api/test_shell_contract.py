"""Shell contract: /v1/shell/* endpoints, CLI attach probe, tray consumption."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

pytestmark = pytest.mark.public

from topos.api import shell as shell_module
from topos.cli import commands, tray
from topos.runtime_update import UpdateInfo


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setitem(shell_module._apply_state, "applying", False)
    monkeypatch.setitem(shell_module._apply_state, "last_result", None)
    app = FastAPI()
    app.include_router(shell_module.router)
    return TestClient(app)


class TestShellStatus:
    def test_identity_and_shape(self, client, monkeypatch):
        monkeypatch.setattr(shell_module, "get_update_info", lambda: None)
        monkeypatch.delenv("TOPOS_LOG_FILE", raising=False)
        payload = client.get("/v1/shell/status").json()
        assert payload["name"] == "topos-node"
        assert payload["version"]
        assert payload["pid"] > 0
        assert payload["log_file"] is None
        assert payload["update"]["available"] is False

    def test_reports_log_file_and_update(self, client, monkeypatch, tmp_path):
        monkeypatch.setenv("TOPOS_LOG_FILE", str(tmp_path / "node.log"))
        monkeypatch.setattr(
            shell_module, "get_update_info", lambda: UpdateInfo(package_name="topos-node", installed="1.0.0", latest="1.1.0")
        )
        payload = client.get("/v1/shell/status").json()
        assert payload["log_file"] == str(tmp_path / "node.log")
        assert payload["update"] == {
            "available": True,
            "installed": "1.0.0",
            "latest": "1.1.0",
            "applying": False,
            "last_result": None,
        }


class TestShellUpdate:
    def test_no_update_available(self, client, monkeypatch):
        monkeypatch.setattr(shell_module, "get_update_info", lambda: None)
        result = client.post("/v1/shell/update").json()
        assert result == {"started": False, "reason": "no_update_available"}

    def test_applies_in_background_and_records_result(self, client, monkeypatch):
        monkeypatch.setattr(
            shell_module, "get_update_info", lambda: UpdateInfo(package_name="topos-node", installed="1.0.0", latest="1.1.0")
        )
        applied = []

        def fake_apply(package_name):
            applied.append(package_name)
            return True

        monkeypatch.setattr(shell_module, "apply_package_update", fake_apply)
        result = client.post("/v1/shell/update").json()
        assert result == {"started": True}
        import time

        deadline_status = None
        for _ in range(100):
            deadline_status = client.get("/v1/shell/status").json()["update"]
            if deadline_status["last_result"] is not None:
                break
            time.sleep(0.01)
        assert applied == [shell_module.DEFAULT_PACKAGE_NAME]
        assert deadline_status["last_result"] == "success"
        assert deadline_status["applying"] is False

    def test_rejects_non_local_clients(self, client, monkeypatch):
        monkeypatch.setattr(shell_module, "_client_is_local", lambda request: False)
        assert client.post("/v1/shell/update").status_code == 403

    def test_already_applying_not_restarted(self, client, monkeypatch):
        monkeypatch.setattr(
            shell_module, "get_update_info", lambda: UpdateInfo(package_name="topos-node", installed="1.0.0", latest="1.1.0")
        )
        monkeypatch.setitem(shell_module._apply_state, "applying", True)
        result = client.post("/v1/shell/update").json()
        assert result == {"started": False, "reason": "already_applying"}


class TestProbeRunningNode:
    class _Resp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    def test_free_port_returns_none(self, monkeypatch):
        import httpx

        def raise_connect(*args, **kwargs):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(httpx, "get", raise_connect)
        assert commands._probe_running_node("0.0.0.0", 9000) is None

    def test_running_node_returns_shell_status(self, monkeypatch):
        import httpx

        payload = {"name": "topos-node", "version": "1.3.1", "log_file": "/tmp/node.log"}
        monkeypatch.setattr(httpx, "get", lambda url, timeout: self._Resp(200, payload))
        assert commands._probe_running_node("0.0.0.0", 9000) == payload

    def test_foreign_server_returns_none(self, monkeypatch):
        import httpx

        monkeypatch.setattr(httpx, "get", lambda url, timeout: self._Resp(200, {"hello": "web"}))
        assert commands._probe_running_node("0.0.0.0", 9000) is None


class TestTrayConsumesContract:
    def _tray(self, **kwargs):
        return tray.ToposTray(
            host="0.0.0.0",
            port=9000,
            version="1.0.0",
            package_name="topos-node",
            on_quit=lambda: None,
            **kwargs,
        )

    def test_fetch_shell_status_adopts_update_and_log_path(self, monkeypatch):
        import httpx

        payload = {
            "name": "topos-node",
            "version": "1.3.2",
            "log_file": "/tmp/logs/node.log",
            "update": {
                "available": True,
                "installed": "1.3.1",
                "latest": "1.4.0",
                "applying": False,
                "last_result": None,
            },
        }
        monkeypatch.setattr(
            httpx, "get", lambda url, timeout: TestProbeRunningNode._Resp(200, payload)
        )
        t = self._tray()
        t._fetch_shell_status()
        assert t.update["available"] is True
        assert t.version == "1.3.2"
        assert t.log_path == Path("/tmp/logs/node.log")

    def test_attached_menu_quit_leaves_node_running(self):
        pytest.importorskip("pystray")
        items = [str(i.text) for i in self._tray(attached=True)._build_menu().items]
        assert "Close Tray (node keeps running)" in items
        assert "Quit Topos Node" not in items

    def test_update_menu_states(self):
        pytest.importorskip("pystray")
        t = self._tray()
        t.update = {"available": True, "latest": "1.4.0", "applying": False, "last_result": None}
        assert "Update to v1.4.0" in [str(i.text) for i in t._build_menu().items]
        t.update = {"available": True, "latest": "1.4.0", "applying": True, "last_result": None}
        assert "Installing update…" in [str(i.text) for i in t._build_menu().items]
        t.update = {"available": True, "latest": "1.4.0", "applying": False, "last_result": "success"}
        assert "Update installed — restart to finish" in [
            str(i.text) for i in t._build_menu().items
        ]
