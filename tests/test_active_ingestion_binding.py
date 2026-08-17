"""Which ingestion directory does this node read?

One helper answers that — ``storage.raw.file_store.active_ingestion_base`` — and
these are the invariants it exists to hold.

The bug they were written against: the raw ingest writer wrote to
``~/.topos/ingestion``, but the three explorer handlers and the storage
breakdown searched ``~/.topos_engine/ingestion`` as well and unioned whatever
they found. ``ingestion`` is on ``profiles.MOVE_ALLOWLIST``, so it archives and
restores with a profile switch; a directory outside ``~/.topos`` therefore
belongs to no Topos at all, and listing its records told the user the active
Topos held data no Topos held. The explorer also ignored
``TOPOS_INGESTION_BASE_PATH`` outright while the writer honoured it, so setting
the override pointed reads and writes at different directories.

Everything runs against tmp_path standing in for the home directory. No live
home is read and no live ingestion directory is touched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from topos.core.handlers import database_explorer as explorer
from topos.storage.db import storage_breakdown
from topos.storage.raw.file_store import RawFileStore, active_ingestion_base

pytestmark = [pytest.mark.public]


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A home directory of our own, so no test can reach the real ~/.topos."""
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake))
    monkeypatch.delenv("TOPOS_INGESTION_BASE_PATH", raising=False)
    return fake


def _seed_jsonl(directory: Path, name: str, lines: int = 3) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text("".join(f'{{"n": {i}}}\n' for i in range(lines)), encoding="utf-8")
    return target


async def _list_files() -> list[dict]:
    response = await explorer.handle_list_jsonl_files({"id": "req-1"})
    assert response["status"] == "ok", response
    return response["payload"]["files"]


class TestOneAnswer:
    def test_writer_and_readers_resolve_to_the_same_directory(self, home):
        """The property the whole change exists for."""
        expected = home / ".topos" / "ingestion"
        assert active_ingestion_base() == expected

        # The writer creates it on construction; the readers must then agree.
        assert RawFileStore().base_path == expected
        assert explorer._ingestion_root() == expected.resolve()

    def test_env_override_is_honoured_by_every_consumer(self, home, tmp_path, monkeypatch):
        """THE inconsistency. The writer honoured TOPOS_INGESTION_BASE_PATH and
        the explorer hard-coded ~/.topos, so with the override set the app
        listed a different directory than the one being written to."""
        override = tmp_path / "elsewhere" / "ingestion"
        override.mkdir(parents=True)
        monkeypatch.setenv("TOPOS_INGESTION_BASE_PATH", str(override))

        assert active_ingestion_base() == override
        assert RawFileStore().base_path == override
        assert explorer._ingestion_root() == override.resolve()

    @pytest.mark.asyncio
    async def test_listing_follows_the_override(self, home, tmp_path, monkeypatch):
        override = tmp_path / "elsewhere" / "ingestion"
        _seed_jsonl(override, "dataset.jsonl")
        monkeypatch.setenv("TOPOS_INGESTION_BASE_PATH", str(override))

        files = await _list_files()

        assert [f["file_name"] for f in files] == ["dataset.jsonl"]
        assert files[0]["line_count"] == 3


class TestNoLegacyFallback:
    """A pre-profile directory belongs to no Topos. It is not read, at all."""

    @pytest.mark.asyncio
    async def test_legacy_directory_is_not_listed(self, home):
        _seed_jsonl(home / ".topos" / "ingestion", "mine.jsonl")
        _seed_jsonl(home / ".topos_engine" / "ingestion", "stray.jsonl")

        files = await _list_files()

        assert [f["file_name"] for f in files] == ["mine.jsonl"]

    @pytest.mark.asyncio
    async def test_legacy_file_cannot_be_deleted(self, home):
        (home / ".topos" / "ingestion").mkdir(parents=True)
        stray = _seed_jsonl(home / ".topos_engine" / "ingestion", "stray.jsonl")

        response = await explorer.handle_delete_jsonl_file(
            {"id": "req-1", "payload": {"file_path": str(stray)}}
        )

        assert response["status"] == "error"
        assert "outside allowed ingestion directories" in response["error"]
        assert stray.is_file(), "a refused delete must not delete"

    @pytest.mark.asyncio
    async def test_legacy_file_cannot_be_read(self, home):
        (home / ".topos" / "ingestion").mkdir(parents=True)
        stray = _seed_jsonl(home / ".topos_engine" / "ingestion", "stray.jsonl")

        response = await explorer.handle_read_jsonl_file(
            {"id": "req-1", "payload": {"file_path": str(stray)}}
        )

        assert response["status"] == "error"
        assert "outside allowed ingestion directories" in response["error"]

    def test_legacy_directory_is_not_counted_as_this_topos_storage(self, home):
        _seed_jsonl(home / ".topos" / "ingestion", "mine.jsonl")
        (home / ".topos" / "ingestion" / "mine.jsonl").write_bytes(b"m" * 1000)
        (home / ".topos_engine" / "ingestion").mkdir(parents=True)
        (home / ".topos_engine" / "ingestion" / "stray.jsonl").write_bytes(b"s" * 9000)

        assert storage_breakdown.raw_ingestion_size_bytes() == 1000


class TestAccessScope:
    @pytest.mark.asyncio
    async def test_path_outside_the_root_is_refused(self, home, tmp_path):
        (home / ".topos" / "ingestion").mkdir(parents=True)
        outsider = _seed_jsonl(tmp_path / "somewhere", "other.jsonl")

        response = await explorer.handle_delete_jsonl_file(
            {"id": "req-1", "payload": {"file_path": str(outsider)}}
        )

        assert response["status"] == "error"
        assert outsider.is_file()

    @pytest.mark.asyncio
    async def test_missing_root_denies_rather_than_widens(self, home):
        """No ingestion directory yet: nothing is listed and nothing is reachable
        — the absence must not fall through to a broader scope."""
        assert explorer._ingestion_root() is None
        assert await _list_files() == []

        response = await explorer.handle_delete_jsonl_file(
            {"id": "req-1", "payload": {"file_path": str(home / ".topos" / "ingestion" / "x.jsonl")}}
        )
        assert response["status"] == "error"

    @pytest.mark.asyncio
    async def test_a_file_inside_the_root_is_still_deletable(self, home):
        """The scope narrowed; it did not close."""
        mine = _seed_jsonl(home / ".topos" / "ingestion" / "dataset", "mine.jsonl")

        response = await explorer.handle_delete_jsonl_file(
            {"id": "req-1", "payload": {"file_path": str(mine)}}
        )

        assert response["status"] == "ok", response
        assert not mine.exists()
