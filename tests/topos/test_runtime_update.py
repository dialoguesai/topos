from __future__ import annotations

import logging

import pytest

from topos.core.logging import ColorFormatter
from topos.runtime_update import (
    UpdateInfo,
    _set_update_info,
    check_for_update,
    is_update_available,
)


@pytest.fixture(autouse=True)
def _reset_update_state():
    _set_update_info(None)
    yield
    _set_update_info(None)


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
