from __future__ import annotations

import logging
import logging.config
import os
import re

import pytest
from uvicorn.config import LOGGING_CONFIG

from topos.core.logging import (
    ColorFormatter,
    QuietPollingAccessFilter,
    align_uvicorn_loggers,
    configure_logging,
    get_uvicorn_log_config,
    suppress_ml_progress_bars,
)


def _access_record(path: str, status: int) -> logging.LogRecord:
    logger = logging.getLogger("uvicorn.access")
    return logger.makeRecord(
        logger.name,
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:50989", "GET", path, "1.1", status),
        None,
    )


def test_get_uvicorn_log_config_routes_access_logs_to_root():
    config = get_uvicorn_log_config()
    access = config["loggers"]["uvicorn.access"]  # type: ignore[index]
    assert access["handlers"] == []
    assert access["propagate"] is True
    assert access["filters"] == ["quiet_polling"]
    assert config["filters"]["quiet_polling"]["()"] == (  # type: ignore[index]
        "topos.core.logging.QuietPollingAccessFilter"
    )


@pytest.fixture(params=[logging.INFO, logging.DEBUG])
def root_at_info(request):
    """The filter must behave identically at DEBUG — topos/.env ships LOG_LEVEL=DEBUG."""
    root = logging.getLogger()
    previous = root.level
    root.setLevel(request.param)
    yield root
    root.setLevel(previous)


def test_quiet_filter_drops_successful_polls(monkeypatch, root_at_info):
    monkeypatch.delenv("TOPOS_ACCESS_LOG", raising=False)
    quiet = QuietPollingAccessFilter()

    for path in ("/healthcheck", "/device_info", "/v1/shell/status", "/v1/shell/status/"):
        assert quiet.filter(_access_record(path, 200)) is False
    assert quiet.filter(_access_record("/healthcheck?verbose=1", 200)) is False


def test_quiet_filter_keeps_failures_and_real_traffic(monkeypatch, root_at_info):
    monkeypatch.delenv("TOPOS_ACCESS_LOG", raising=False)
    quiet = QuietPollingAccessFilter()

    assert quiet.filter(_access_record("/healthcheck", 503)) is True
    assert quiet.filter(_access_record("/device_info", 401)) is True
    assert quiet.filter(_access_record("/v1/signal/briefs/profile", 200)) is True
    # Non-access records (no uvicorn arg tuple) always pass through.
    assert quiet.filter(logging.getLogger("uvicorn.error").makeRecord(
        "uvicorn.error", logging.INFO, __file__, 1, "Application startup complete.", None, None
    )) is True


def test_quiet_filter_opt_out(monkeypatch, root_at_info):
    quiet = QuietPollingAccessFilter()

    monkeypatch.setenv("TOPOS_ACCESS_LOG", "all")
    assert quiet.filter(_access_record("/healthcheck", 200)) is True


def test_align_uvicorn_loggers_attaches_quiet_filter_once(monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    configure_logging()
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.filters.clear()

    align_uvicorn_loggers()
    align_uvicorn_loggers()

    assert sum(isinstance(f, QuietPollingAccessFilter) for f in access_logger.filters) == 1


def test_align_uvicorn_loggers_after_default_uvicorn_config(monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    configure_logging()
    logging.config.dictConfig(LOGGING_CONFIG)

    access_logger = logging.getLogger("uvicorn.access")
    assert access_logger.handlers
    assert access_logger.propagate is False

    align_uvicorn_loggers()

    assert not access_logger.handlers
    assert access_logger.propagate is True
    assert access_logger.hasHandlers()


def test_uvicorn_access_log_uses_topos_color_format(monkeypatch):
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    configure_logging()
    logging.config.dictConfig(LOGGING_CONFIG)
    align_uvicorn_loggers()

    access_logger = logging.getLogger("uvicorn.access")
    handler = logging.getLogger().handlers[0]
    assert isinstance(handler.formatter, ColorFormatter)

    record = access_logger.makeRecord(
        access_logger.name,
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:55390", "GET", "/v1/signal/briefs/profile", "1.1", 200),
        None,
    )
    formatted = handler.formatter.format(record)
    plain = re.sub(r"\x1b\[[0-9;]*m", "", formatted)
    assert re.match(
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \| INFO \| uvicorn\.access: "
        r'127\.0\.0\.1:55390 - "GET /v1/signal/briefs/profile HTTP/1\.1" 200$',
        plain,
    )


def test_suppress_ml_progress_bars_disables_transformers_progress():
    suppress_ml_progress_bars()
    assert os.environ.get("HF_HUB_DISABLE_PROGRESS_BARS") == "1"
    from transformers.utils.logging import is_progress_bar_enabled

    assert not is_progress_bar_enabled()


def test_configure_logging_suppresses_ml_progress_bars(monkeypatch):
    monkeypatch.delenv("HF_HUB_DISABLE_PROGRESS_BARS", raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    configure_logging()
    assert os.environ.get("HF_HUB_DISABLE_PROGRESS_BARS") == "1"
    from transformers.utils.logging import is_progress_bar_enabled

    assert not is_progress_bar_enabled()
