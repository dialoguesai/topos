from __future__ import annotations

import logging
import re

from topos.core.logging import ColorFormatter


def _plain(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


def _record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="topos.ingestion.manager",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def test_highlights_source_id_values():
    formatter = ColorFormatter()
    formatted = formatter.format(
        _record(
            "[PIPELINE:MANAGER] IngestionManager#1: Starting job processing: "
            "source_id=imessage, schema_id=ai_chat_messages"
        )
    )
    assert ColorFormatter._SOURCE_IDENTITY_VALUE_COLOR in formatted
    plain = _plain(formatted)
    assert "source_id=imessage" in plain
    assert "schema_id=ai_chat_messages" in plain


def test_highlights_source_values():
    formatter = ColorFormatter()
    formatted = formatter.format(
        _record("[PIPELINE:RAW] Stored raw payload: source=chatgpt_file_ingestion, record_id=abc")
    )
    assert ColorFormatter._SOURCE_IDENTITY_VALUE_COLOR in formatted
    assert "source=chatgpt_file_ingestion" in _plain(formatted)


def test_highlights_quoted_source_values():
    formatter = ColorFormatter()
    formatted = formatter.format(
        _record("Source enrichment backfill failed: source='demo_messenger_file' enrichment='ner'")
    )
    assert ColorFormatter._SOURCE_IDENTITY_VALUE_COLOR in formatted
    assert "source='demo_messenger_file'" in _plain(formatted)


def test_skips_log_format_placeholders():
    formatter = ColorFormatter()
    formatted = formatter.format(
        _record("[PIPELINE:MANAGER] %s: source_id=%s not found in registry")
    )
    assert ColorFormatter._SOURCE_IDENTITY_VALUE_COLOR not in formatted


def test_skips_sql_excluded_fragments():
    formatter = ColorFormatter()
    message = "upsert conflict source_id=excluded.source_id"
    formatted = formatter.format(_record(message))
    assert ColorFormatter._SOURCE_IDENTITY_VALUE_COLOR not in formatted


def test_skips_url_query_source_params():
    formatter = ColorFormatter()
    message = (
        "Skipping re-embed write for unchanged record browser:https://www.google.com/url"
        "?q=https://meet.google.com/abc&sa=D&source=editors&ust=1782853412344145"
        "&usg=AOvVaw2DY9XjprFI0exJftIWTrdN_2026-06-30T20:23:35.046Z"
    )
    formatted = formatter.format(_record(message))
    assert ColorFormatter._SOURCE_IDENTITY_VALUE_COLOR not in formatted


def test_still_highlights_source_in_pipeline_kv_context():
    formatter = ColorFormatter()
    formatted = formatter.format(
        _record(
            "[PIPELINE:RAW] Stored raw payload: source=browser_visits, record_id=https://x?source=editors"
        )
    )
    assert ColorFormatter._SOURCE_IDENTITY_VALUE_COLOR in formatted
    plain = _plain(formatted)
    assert "source=browser_visits" in plain
