"""BL-12: a CSV import keeps the line breaks inside a quoted field (a journal entry with paragraphs).

The parser split the decoded file with ``str.splitlines`` before the csv module saw it, so a quoted field was cut at
each of its line breaks: the entry kept its first paragraph and the rest became broken rows of their own.
``splitlines`` also split on characters a CSV never ends a row with (U+2028, a form feed). The csv module is now given
a file object opened with ``newline=""``, as its documentation requires.

protects: a quoted field keeps every character, its LF, CRLF and blank lines included, however the file arrives in
chunks; rows still end at LF or CRLF; a line separator inside an unquoted field ends nothing; and the owner's journal
import stores the whole entry. Every entry here is invented.
"""
from __future__ import annotations

import asyncio

import pytest

from topos.ingestion.parser import parse_csv_stream, parse_file

# tests/ingestion is not a package: sibling test modules import by their own name.
from test_ai_chat_writer_class import _start_ingestion, captured_jobs, conn  # noqa: F401  (fixtures)
from test_canonical_writer_class import _rows
from test_first_party_capture_continuity import _import_message, _owner_import
from test_journal_push_provenance import _install

PARAGRAPHS = "Walked to the market early.\n\nAfter lunch the rain came back,\nso the shed waited."
CRLF_FIELD = "First line of the note.\r\nSecond line of the note."
CSV = ("entry_id,entry_at,content\r\n"
       f'j-1,2026-09-01T10:00:00,"{PARAGRAPHS}"\r\n'
       f'j-2,2026-09-02T10:00:00,"{CRLF_FIELD}"\n'
       "j-3,2026-09-03T10:00:00,One line with a line separator   inside it\n"
       'j-4,2026-09-04T10:00:00,"A quoted ""word"" and a comma, kept"\n').encode("utf-8")


async def _chunks(data: bytes, size: int):
    for start in range(0, len(data), size):
        yield data[start:start + size]


async def _parsed(data: bytes, size: int) -> list:
    return [row async for row in parse_csv_stream(_chunks(data, size))]


@pytest.mark.parametrize("size", [1, 7, 64, 100_000])
def test_a_quoted_field_keeps_its_line_breaks_however_the_file_arrives(size):
    rows = asyncio.run(_parsed(CSV, size))
    assert [row["entry_id"] for row in rows] == ["j-1", "j-2", "j-3", "j-4"]
    assert rows[0]["content"] == PARAGRAPHS
    assert rows[1]["content"] == CRLF_FIELD
    assert rows[2]["content"] == "One line with a line separator   inside it"
    assert rows[3]["content"] == 'A quoted "word" and a comma, kept'
    assert all(set(row) == {"entry_id", "entry_at", "content"} for row in rows)


def test_parse_file_reads_csv_the_same_way():
    rows = asyncio.run(_collect(parse_file(_chunks(CSV, 13), "csv")))
    assert [row["content"] for row in rows][:2] == [PARAGRAPHS, CRLF_FIELD]


async def _collect(iterator) -> list:
    return [row async for row in iterator]


@pytest.mark.asyncio
async def test_the_owners_journal_import_stores_the_whole_entry(conn, captured_jobs, tmp_path, monkeypatch):
    source = "demo_journal_file"
    _install(conn, source=source)
    body = ("entry_id,entry_at,content\n" f'j-1,2026-09-01T10:00:00,"{PARAGRAPHS}"\n').encode("utf-8")
    message = _owner_import(_import_message("req-bl12", source, "demo.journal.v1", "csv", body))
    await _start_ingestion(message, captured_jobs, conn, tmp_path, monkeypatch)
    stored = [row for row in _rows(conn, "journal_entries") if row["source_id"] == source]
    assert len(stored) == 1
    assert stored[0]["content"] == PARAGRAPHS
