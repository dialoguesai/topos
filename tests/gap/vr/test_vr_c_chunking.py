"""Gap tests for chunking (Phase C)."""

from __future__ import annotations

import pytest

from topos.features.signal.chunking import chunk_text

pytestmark = pytest.mark.gap


def test_long_text_produces_multiple_chunks() -> None:
    text = "word " * 900
    chunks = chunk_text(text.strip(), max_tokens=128, overlap=16)
    assert len(chunks) > 1
    assert chunks[0].chunk_index == 0
    assert all(chunk.text for chunk in chunks)


def test_short_text_single_chunk() -> None:
    chunks = chunk_text("hello world")
    assert len(chunks) == 1
    assert chunks[0].chunk_index == 0
