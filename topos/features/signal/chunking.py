"""Text chunking for embedding pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

MAX_CHUNKS_PER_RECORD = 32
DEFAULT_MAX_TOKENS = 512
DEFAULT_OVERLAP_TOKENS = 64
DEFAULT_STRATEGY = "sliding_window_v1"
MIN_CHUNK_CHARS = 80


@dataclass(frozen=True)
class ChunkSpec:
    chunk_index: int
    text: str
    char_start: int
    char_end: int
    token_count: int


def _estimate_tokens(text: str) -> int:
    return len(text.split())


def chunk_text(
    text: str,
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap: int = DEFAULT_OVERLAP_TOKENS,
    strategy: str = DEFAULT_STRATEGY,
) -> List[ChunkSpec]:
    raw = str(text or "").strip()
    if not raw:
        return []

    words = raw.split()
    if _estimate_tokens(raw) <= max_tokens:
        return [
            ChunkSpec(
                chunk_index=0,
                text=raw,
                char_start=0,
                char_end=len(raw),
                token_count=len(words),
            )
        ]

    chunks: List[ChunkSpec] = []
    start = 0
    chunk_index = 0
    step = max(1, max_tokens - overlap)

    while start < len(words) and chunk_index < MAX_CHUNKS_PER_RECORD:
        end = min(len(words), start + max_tokens)
        chunk_words = words[start:end]
        chunk_text_value = " ".join(chunk_words).strip()
        if len(chunk_text_value) < MIN_CHUNK_CHARS and chunks:
            chunks[-1] = ChunkSpec(
                chunk_index=chunks[-1].chunk_index,
                text=(chunks[-1].text + " " + chunk_text_value).strip(),
                char_start=chunks[-1].char_start,
                char_end=len(raw),
                token_count=_estimate_tokens(chunks[-1].text + " " + chunk_text_value),
            )
            break
        if not chunk_text_value:
            break
        char_start = raw.find(chunk_words[0]) if chunk_words else 0
        char_end = char_start + len(chunk_text_value)
        chunks.append(
            ChunkSpec(
                chunk_index=chunk_index,
                text=chunk_text_value,
                char_start=char_start,
                char_end=char_end,
                token_count=len(chunk_words),
            )
        )
        chunk_index += 1
        if end >= len(words):
            break
        start += step

    return chunks
