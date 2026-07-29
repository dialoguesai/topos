"""App mode (--app): file logging via TOPOS_LOG_FILE and Show Logs tray plumbing."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

pytestmark = pytest.mark.public

from topos.cli import tray
from topos.core import logging as topos_logging


@pytest.fixture
def restore_root_logging():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    yield
    root.handlers = saved_handlers
    root.setLevel(saved_level)


class TestLogFilePath:
    def test_unset_means_stdout(self, monkeypatch):
        monkeypatch.delenv("TOPOS_LOG_FILE", raising=False)
        assert topos_logging.get_log_file_path() is None

    def test_set_expands_user(self, monkeypatch):
        monkeypatch.setenv("TOPOS_LOG_FILE", "~/.topos/logs/node.log")
        assert topos_logging.get_log_file_path() == Path.home() / ".topos" / "logs" / "node.log"

    def test_default_node_log_path(self):
        assert topos_logging.default_node_log_path() == (
            Path.home() / ".topos" / "logs" / "node.log"
        )


class TestConfigureLoggingToFile:
    def test_file_handler_writes_and_replaces_stdout(
        self, tmp_path, monkeypatch, restore_root_logging
    ):
        log_file = tmp_path / "logs" / "node.log"
        monkeypatch.setenv("TOPOS_LOG_FILE", str(log_file))
        topos_logging.configure_logging()

        root = logging.getLogger()
        assert len(root.handlers) == 1
        handler = root.handlers[0]
        assert isinstance(handler, RotatingFileHandler)
        assert handler.maxBytes == topos_logging.LOG_FILE_MAX_BYTES
        assert handler.backupCount == topos_logging.LOG_FILE_BACKUP_COUNT

        logging.getLogger("topos.test").info("hello from app mode")
        handler.flush()
        content = log_file.read_text(encoding="utf-8")
        assert "hello from app mode" in content
        assert "topos.test" in content
        assert "\x1b[" not in content  # no ANSI colors in files
        handler.close()

    def test_stdout_handler_when_env_unset(self, monkeypatch, restore_root_logging):
        monkeypatch.delenv("TOPOS_LOG_FILE", raising=False)
        topos_logging.configure_logging()
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert not isinstance(root.handlers[0], RotatingFileHandler)


class TestShowLogsMenu:
    def _tray(self, log_path):
        return tray.ToposTray(
            host="0.0.0.0",
            port=9000,
            version="1.0.0",
            package_name="topos-node",
            on_quit=lambda: None,
            log_path=log_path,
        )

    def test_menu_includes_show_logs_when_log_path_set(self, tmp_path):
        items = self._tray(tmp_path / "node.log")._menu_labels()
        assert "Show Logs" in items

    def test_menu_omits_show_logs_without_log_path(self):
        items = self._tray(None)._menu_labels()
        assert "Show Logs" not in items

    def test_open_log_viewer_darwin_uses_console(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(tray.sys, "platform", "darwin")
        monkeypatch.setattr(tray.subprocess, "Popen", lambda cmd: calls.append(cmd))
        log_path = tmp_path / "logs" / "node.log"
        tray.open_log_viewer(log_path)
        assert log_path.exists()  # touched so Console.app can open it
        assert calls == [["open", "-a", "Console", str(log_path)]]
