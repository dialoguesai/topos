"""A container must never be copied into the raw store.

What this guards, found on a live import: selecting a ChatGPT export .zip put a
**1.4 GB verbatim copy of the archive** into ~/.topos/ingestion under a .jsonl
name, starting with the bytes ``PK..``. The job then died with

    'utf-8' codec can't decode byte 0xb7 in position 10: invalid start byte

The decode error was how it announced itself. The copy was the defect: the local
import path exists precisely so that nothing is duplicated, and it duplicated the
largest file in the corpus.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from topos.ingestion.local_exports import CONVERSATIONS_MEMBER
from topos.ingestion.triggers.file_trigger import FileTrigger
from topos.storage.raw.file_store import RawFileStore

CONVERSATIONS = [{"title": "a conversation", "mapping": {}}]
DATASET = "ds-1"
SCHEMA = "chatgpt.conversation.v2"


@pytest.fixture
def store(tmp_path) -> RawFileStore:
    return RawFileStore(base_path=tmp_path / "ingestion")


def _export_zip(root: Path, *, images: int = 3) -> Path:
    path = root / "chatgpt-export.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(CONVERSATIONS_MEMBER, json.dumps(CONVERSATIONS))
        # The bulk of a real export, and the reason copying it is unacceptable.
        for i in range(images):
            archive.writestr(f"file-{i}.png", b"\x89PNG" + b"\x00" * 200_000)
    return path


def test_an_archive_yields_the_conversations_file_not_the_archive(tmp_path, store):
    archive = _export_zip(tmp_path)
    FileTrigger(file_store=store).create_job("j1", DATASET, SCHEMA, str(archive))

    stored = store.get_file_path(DATASET, SCHEMA)
    assert stored.exists()
    # Not a zip: the store holds readable JSON.
    assert stored.read_bytes()[:2] != b"PK"
    assert json.loads(stored.read_text()) == CONVERSATIONS


def test_the_store_does_not_grow_to_the_size_of_the_archive(tmp_path, store):
    """The number that matters. A verbatim copy put 1.4GB on disk to move 52MB."""
    archive = _export_zip(tmp_path, images=5)
    FileTrigger(file_store=store).create_job("j1", DATASET, SCHEMA, str(archive))

    stored = store.get_file_path(DATASET, SCHEMA)
    assert stored.stat().st_size < archive.stat().st_size / 4


def test_the_archive_on_disk_is_untouched(tmp_path, store):
    archive = _export_zip(tmp_path)
    before = archive.read_bytes()
    FileTrigger(file_store=store).create_job("j1", DATASET, SCHEMA, str(archive))
    assert archive.read_bytes() == before


def test_an_unzipped_folder_is_lifted_too(tmp_path, store):
    folder = tmp_path / "chatgpt-export"
    folder.mkdir()
    (folder / CONVERSATIONS_MEMBER).write_text(json.dumps(CONVERSATIONS))
    (folder / "chat.html").write_text("<html></html>" * 500)

    FileTrigger(file_store=store).create_job("j1", DATASET, SCHEMA, str(folder))
    assert json.loads(store.get_file_path(DATASET, SCHEMA).read_text()) == CONVERSATIONS


def test_a_plain_json_file_is_still_copied_verbatim(tmp_path, store):
    """The ordinary path must not change: only containers are lifted."""
    plain = tmp_path / "conversations.json"
    plain.write_text(json.dumps(CONVERSATIONS))
    FileTrigger(file_store=store).create_job("j1", DATASET, SCHEMA, str(plain))
    assert json.loads(store.get_file_path(DATASET, SCHEMA).read_text()) == CONVERSATIONS


def test_a_jsonl_file_is_still_copied_verbatim(tmp_path, store):
    rows = tmp_path / "rows.jsonl"
    rows.write_text('{"a":1}\n{"a":2}\n')
    FileTrigger(file_store=store).create_job("j1", DATASET, SCHEMA, str(rows))
    assert store.get_file_path(DATASET, SCHEMA).read_text() == '{"a":1}\n{"a":2}\n'


def test_a_large_member_streams_rather_than_loading(tmp_path, store):
    # write_stream exists so a big conversations.json is not held in memory.
    big = [{"title": f"c{i}", "mapping": {}} for i in range(20000)]
    archive = tmp_path / "big.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(CONVERSATIONS_MEMBER, json.dumps(big))

    FileTrigger(file_store=store).create_job("j1", DATASET, SCHEMA, str(archive))
    assert json.loads(store.get_file_path(DATASET, SCHEMA).read_text()) == big
