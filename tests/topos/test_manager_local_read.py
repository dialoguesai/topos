"""The seam where a path on disk becomes bytes for the parser.

``_read_file_bytes`` is the only place ingestion turns a local file into a
stream, which makes it the only place that has to understand that a user points
at what the export site gave them -- an archive or a folder -- rather than at
the one member inside it we can read.

It used to call ``read_bytes()``. That was safe only because the control plane
had already lifted the JSON out of the archive before the node ever saw it. A
local import points this straight at whatever is on disk, and an export archive
runs to gigabytes.
"""

from __future__ import annotations

import json
import zipfile

import pytest

from topos.ingestion.local_exports import CONVERSATIONS_MEMBER, LocalExportError
from topos.ingestion.manager import _read_file_bytes

CONVERSATIONS = [{"title": "a conversation", "mapping": {}}]


async def _collect(path):
    return b"".join([chunk async for chunk in _read_file_bytes(path)])


@pytest.mark.asyncio
async def test_reads_a_plain_json_file_unchanged(tmp_path):
    # The path every existing caller takes. It must not have moved.
    plain = tmp_path / CONVERSATIONS_MEMBER
    plain.write_text(json.dumps(CONVERSATIONS))
    assert json.loads(await _collect(plain)) == CONVERSATIONS


@pytest.mark.asyncio
async def test_lifts_conversations_out_of_an_archive(tmp_path):
    archive = tmp_path / "chatgpt-export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(CONVERSATIONS_MEMBER, json.dumps(CONVERSATIONS))
        zf.writestr("chat.html", "<html></html>")
        zf.writestr("file-0-image.png", b"\x89PNG" + b"\x00" * 2048)
    assert json.loads(await _collect(archive)) == CONVERSATIONS


@pytest.mark.asyncio
async def test_lifts_conversations_out_of_a_folder(tmp_path):
    folder = tmp_path / "chatgpt-export"
    folder.mkdir()
    (folder / CONVERSATIONS_MEMBER).write_text(json.dumps(CONVERSATIONS))
    (folder / "chat.html").write_text("<html></html>")
    assert json.loads(await _collect(folder)) == CONVERSATIONS


@pytest.mark.asyncio
async def test_reads_a_file_larger_than_one_chunk(tmp_path):
    # Chunking is the point; a payload spanning several reads must reassemble.
    big = [{"title": f"conversation {i}", "mapping": {}} for i in range(30000)]
    plain = tmp_path / CONVERSATIONS_MEMBER
    plain.write_text(json.dumps(big))
    assert plain.stat().st_size > (1 << 20)
    assert json.loads(await _collect(plain)) == big


@pytest.mark.asyncio
async def test_an_archive_with_nothing_to_read_fails_before_ingesting(tmp_path):
    # Better here, with a message naming the fix, than as a parse error several
    # confusing minutes into a job.
    archive = tmp_path / "photos.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("beach.png", b"\x89PNG")
    with pytest.raises(LocalExportError):
        await _collect(archive)


@pytest.mark.asyncio
async def test_the_archive_is_left_exactly_as_it_was(tmp_path):
    """Not a file store: reading an export must not write anything at all."""
    archive = tmp_path / "chatgpt-export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(CONVERSATIONS_MEMBER, json.dumps(CONVERSATIONS))
        zf.writestr("file-0-image.png", b"\x89PNG" + b"\x00" * 2048)
    before = archive.read_bytes()
    await _collect(archive)
    assert archive.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == [archive.name]
