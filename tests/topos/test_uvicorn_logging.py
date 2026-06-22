from __future__ import annotations

import logging
import logging.config
import re

from uvicorn.config import LOGGING_CONFIG

from topos.core.logging import ColorFormatter, align_uvicorn_loggers, configure_logging, get_uvicorn_log_config


def test_get_uvicorn_log_config_routes_access_logs_to_root():
    config = get_uvicorn_log_config()
    access = config["loggers"]["uvicorn.access"]  # type: ignore[index]
    assert access["handlers"] == []
    assert access["propagate"] is True


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
