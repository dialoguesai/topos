from __future__ import annotations

import logging

import pytest

from topos.core.logging import ColorFormatter
from topos import runtime_update
from topos.runtime_update import (
    UpdateInfo,
    _set_update_info,
    apply_package_update,
    check_for_update,
    is_update_available,
    local_install_source,
)


@pytest.fixture(autouse=True)
def _reset_update_state(monkeypatch, tmp_path):
    # Point uv's tool directory at an empty tmp dir. Without this these tests
    # read the DEVELOPER's real uv install, and on a machine whose engine was
    # deployed from a working copy every one of them would take the
    # local-install path and fail for a reason that has nothing to do with the
    # code under test.
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "uv-tools"))
    runtime_update._local_install_logged = False
    _set_update_info(None)
    yield
    _set_update_info(None)
    runtime_update._local_install_logged = False


def _write_receipt(tmp_path, body: str) -> None:
    receipt = tmp_path / "uv-tools" / "topos-node" / "uv-receipt.toml"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(body, encoding="utf-8")


class TestLocalInstallSource:
    """An engine installed from a working copy cannot be moved by a PyPI
    release, and the updater must say so instead of trying forever."""

    def test_none_when_installed_from_the_index(self, tmp_path):
        _write_receipt(tmp_path, '[tool]\nrequirements = [{ name = "topos-node" }]\n')
        assert local_install_source() is None

    def test_none_when_there_is_no_receipt(self):
        assert local_install_source() is None

    def test_reports_the_directory_it_was_installed_from(self, tmp_path):
        _write_receipt(
            tmp_path,
            '[tool]\nrequirements = [{ name = "topos-node", '
            'directory = "/Users/x/.topos/deploy-head" }]\n',
        )
        assert local_install_source() == "/Users/x/.topos/deploy-head"

    def test_no_update_is_offered_for_a_local_install(self, tmp_path, monkeypatch):
        _write_receipt(
            tmp_path,
            '[tool]\nrequirements = [{ name = "topos-node", directory = "/tmp/head" }]\n',
        )
        monkeypatch.setattr(
            "topos.runtime_update.get_installed_package_version", lambda *a, **k: "1.3.20"
        )
        monkeypatch.setattr(
            "topos.runtime_update.get_latest_pypi_version", lambda *a, **k: "1.3.21"
        )
        # PyPI genuinely is newer; it still cannot be applied here.
        assert check_for_update() is None

    def test_apply_refuses_a_local_install_without_running_uv(self, tmp_path, monkeypatch):
        _write_receipt(
            tmp_path,
            '[tool]\nrequirements = [{ name = "topos-node", directory = "/tmp/head" }]\n',
        )

        def _never(*_args, **_kwargs):
            raise AssertionError("uv must not run for an install it cannot upgrade")

        monkeypatch.setattr("topos.runtime_update.subprocess.run", _never)
        assert apply_package_update() is False


class TestApplyReportsTheTruth:
    """`uv tool upgrade` exiting 0 is not proof anything was installed."""

    @staticmethod
    def _uv_that_succeeds(monkeypatch):
        monkeypatch.setattr("topos.runtime_update.resolve_uv_binary", lambda: "/usr/bin/uv")

        class _Result:
            returncode = 0
            stdout = "Nothing to upgrade"
            stderr = ""

        monkeypatch.setattr(
            "topos.runtime_update.subprocess.run", lambda *a, **k: _Result()
        )

    def test_false_when_the_version_did_not_move(self, tmp_path, monkeypatch):
        _write_receipt(tmp_path, '[tool]\nrequirements = [{ name = "topos-node" }]\n')
        self._uv_that_succeeds(monkeypatch)
        monkeypatch.setattr(
            "topos.runtime_update._installed_version_on_disk", lambda *a, **k: "1.3.20"
        )
        assert apply_package_update() is False

    def test_true_when_the_version_moved(self, tmp_path, monkeypatch):
        _write_receipt(tmp_path, '[tool]\nrequirements = [{ name = "topos-node" }]\n')
        self._uv_that_succeeds(monkeypatch)
        versions = iter(["1.3.20", "1.3.21"])
        monkeypatch.setattr(
            "topos.runtime_update._installed_version_on_disk", lambda *a, **k: next(versions)
        )
        assert apply_package_update() is True

    def test_success_when_the_version_cannot_be_read(self, tmp_path, monkeypatch):
        # Unknown must never be reported as failure: an unreadable dist-info is
        # not evidence the upgrade did nothing.
        _write_receipt(tmp_path, '[tool]\nrequirements = [{ name = "topos-node" }]\n')
        self._uv_that_succeeds(monkeypatch)
        monkeypatch.setattr(
            "topos.runtime_update._installed_version_on_disk", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "topos.runtime_update.get_installed_package_version", lambda *a, **k: None
        )
        assert apply_package_update() is True


def test_check_for_update_returns_info_when_newer(monkeypatch):
    monkeypatch.setattr(
        "topos.runtime_update.get_installed_package_version",
        lambda *_args, **_kwargs: "0.1.0",
    )
    monkeypatch.setattr(
        "topos.runtime_update.get_latest_pypi_version",
        lambda *_args, **_kwargs: "0.1.1",
    )

    info = check_for_update()
    assert info is not None
    assert info.installed == "0.1.0"
    assert info.latest == "0.1.1"


def test_check_for_update_skips_when_disabled(monkeypatch):
    monkeypatch.setenv("TOPOS_SKIP_UPDATE_CHECK", "true")
    monkeypatch.setattr(
        "topos.runtime_update.get_installed_package_version",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not check")),
    )

    assert check_for_update() is None


def test_color_formatter_uses_amber_timestamps_when_update_available():
    _set_update_info(UpdateInfo(package_name="topos-node", installed="0.1.0", latest="0.1.1"))
    formatter = ColorFormatter()
    record = logging.LogRecord(
        name="topos.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    formatted = formatter.format(record)
    assert ColorFormatter._TIMESTAMP_UPDATE_COLOR in formatted
    assert is_update_available()
