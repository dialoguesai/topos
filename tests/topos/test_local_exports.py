"""Discovery and opening of an export that is already on the machine.

What these protect: the local path is the one that costs nothing -- no upload,
no object storage, no re-download. Every test here is guarding a property that,
if it broke, would either send bytes over a network that did not need to move,
or turn a bounded look in three folders into a search of someone's home
directory.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from topos.ingestion.local_exports import (
    CONVERSATIONS_MEMBER,
    MIN_PAYLOAD_BYTES,
    LocalExportError,
    default_search_roots,
    find_exports,
    handle_for,
    iter_ingestible_chunks,
    open_ingestible,
    resolve,
)

CONVERSATIONS = [{"title": "a conversation", "mapping": {}}]


def _conversations_bytes(padding: int = MIN_PAYLOAD_BYTES) -> bytes:
    payload = json.dumps(CONVERSATIONS) + " " * padding
    return payload.encode("utf-8")


def write_export_folder(root: Path, name: str = "chatgpt-export") -> Path:
    """An unzipped export: the conversations file beside the things we ignore."""
    folder = root / name
    folder.mkdir(parents=True)
    (folder / CONVERSATIONS_MEMBER).write_bytes(_conversations_bytes())
    (folder / "chat.html").write_bytes(b"<html></html>" * 100)
    (folder / "user.json").write_bytes(b"{}")
    return folder


def write_export_zip(root: Path, name: str = "chatgpt-export.zip", images: int = 3) -> Path:
    """An archive shaped like the real one: mostly images by weight."""
    path = root / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(CONVERSATIONS_MEMBER, _conversations_bytes())
        archive.writestr("chat.html", "<html></html>" * 200)
        for i in range(images):
            archive.writestr(f"file-{i}-image.png", b"\x89PNG" + b"\x00" * 4096)
    return path


# --------------------------------------------------------------------------
# opening: take one member, leave the rest
# --------------------------------------------------------------------------


def test_reads_conversations_out_of_an_archive_without_expanding_it(tmp_path):
    archive = write_export_zip(tmp_path)
    with open_ingestible(archive) as stream:
        loaded = json.loads(stream.read().decode("utf-8"))
    assert loaded == CONVERSATIONS
    # Nothing was written beside the archive: no temp copy, no expansion.
    assert [p.name for p in tmp_path.iterdir()] == [archive.name]


def test_reads_conversations_out_of_a_folder(tmp_path):
    folder = write_export_folder(tmp_path)
    with open_ingestible(folder) as stream:
        assert json.loads(stream.read().decode("utf-8")) == CONVERSATIONS


def test_a_plain_json_file_opens_as_itself(tmp_path):
    # The ordinary path, and the one the control plane still uses. It must not
    # change just because containers are now understood.
    plain = tmp_path / CONVERSATIONS_MEMBER
    plain.write_bytes(_conversations_bytes())
    with open_ingestible(plain) as stream:
        assert json.loads(stream.read().decode("utf-8")) == CONVERSATIONS


def test_an_archive_with_no_conversations_says_what_to_do(tmp_path):
    path = tmp_path / "holiday-photos.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("beach.png", b"\x89PNG")
    with pytest.raises(LocalExportError) as excinfo:
        open_ingestible(path)
    assert CONVERSATIONS_MEMBER in str(excinfo.value)


def test_a_corrupt_archive_says_to_download_it_again(tmp_path):
    path = tmp_path / "chatgpt-export.zip"
    path.write_bytes(b"this is not a zip file")
    with pytest.raises(LocalExportError) as excinfo:
        open_ingestible(path)
    assert "re-download" in str(excinfo.value).lower()


def test_prefers_the_shallowest_conversations_file(tmp_path):
    # The export puts it at the archive root; a nested one is someone's backup.
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"old/backup/{CONVERSATIONS_MEMBER}", json.dumps([{"title": "old"}]))
        archive.writestr(CONVERSATIONS_MEMBER, _conversations_bytes())
    with open_ingestible(path) as stream:
        assert json.loads(stream.read().decode("utf-8")) == CONVERSATIONS


def test_streams_in_chunks_rather_than_one_read(tmp_path):
    # The guard against read_bytes(): a gigabyte archive must not become a
    # gigabyte in memory. Small chunk size so the assertion is about shape.
    archive = write_export_zip(tmp_path)
    chunks = list(iter_ingestible_chunks(archive, chunk_size=64))
    assert len(chunks) > 1
    assert json.loads(b"".join(chunks).decode("utf-8")) == CONVERSATIONS


# --------------------------------------------------------------------------
# discovery: find it by shape, never by name
# --------------------------------------------------------------------------


def test_finds_an_archive_and_reports_what_it_will_ignore(tmp_path):
    write_export_zip(tmp_path, images=5)
    found = find_exports([tmp_path])
    assert len(found.candidates) == 1
    candidate = found.candidates[0]
    assert candidate.kind == "archive"
    # 5 images + chat.html; the receipt screen shows this number.
    assert candidate.ignored_files == 6
    assert candidate.ignored_bytes > candidate.payload_bytes
    assert candidate.payload_bytes > 0


def test_finds_an_unzipped_folder(tmp_path):
    write_export_folder(tmp_path)
    found = find_exports([tmp_path])
    assert [c.kind for c in found.candidates] == ["folder"]


def test_finds_a_bare_conversations_file(tmp_path):
    (tmp_path / CONVERSATIONS_MEMBER).write_bytes(_conversations_bytes())
    found = find_exports([tmp_path])
    assert [c.kind for c in found.candidates] == ["file"]


def test_ignores_files_merely_named_like_the_product(tmp_path):
    """The finding that shaped this: a real downloads folder is full of these.

    Matching on the name "ChatGPT" turns up every image the product ever
    generated -- dozens on the machine this was written for, and not one export.
    """
    for name in (
        "ChatGPT Image Mar 31, 2025, 06_03_01 AM.png",
        "ChatGPT-notes.txt",
        "chatgpt_export_readme.md",
    ):
        (tmp_path / name).write_bytes(b"x" * 8192)
    assert find_exports([tmp_path]).candidates == []


def test_an_archive_without_conversations_is_not_an_export(tmp_path):
    path = tmp_path / "chatgpt-photos.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("beach.png", b"\x89PNG" * 500)
    assert find_exports([tmp_path]).candidates == []


def test_a_stub_conversations_file_is_not_an_export(tmp_path):
    # A truncated download or a placeholder: offering it would fail at parse
    # time, several confusing minutes later.
    (tmp_path / CONVERSATIONS_MEMBER).write_bytes(b"[]")
    assert find_exports([tmp_path]).candidates == []


def test_newest_first(tmp_path):
    old = write_export_zip(tmp_path, "old.zip")
    new = write_export_zip(tmp_path, "new.zip")
    import os

    os.utime(old, (1_600_000_000, 1_600_000_000))
    os.utime(new, (1_700_000_000, 1_700_000_000))
    labels = [c.label for c in find_exports([tmp_path]).candidates]
    assert labels[0].endswith("new.zip")


def test_does_not_follow_symlinks(tmp_path):
    """A link is the cheap way a scan of three folders reads something else."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    write_export_zip(elsewhere)
    root = tmp_path / "root"
    root.mkdir()
    try:
        (root / "link").symlink_to(elsewhere, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - platform without symlinks
        pytest.skip("symlinks unavailable")
    assert find_exports([root]).candidates == []


def test_does_not_descend_past_the_depth_limit(tmp_path):
    deep = tmp_path / "one" / "two" / "three"
    deep.mkdir(parents=True)
    write_export_zip(deep)
    assert find_exports([tmp_path]).candidates == []


def test_finds_an_export_one_folder_down(tmp_path):
    # The unzip-in-place case, which is the common one.
    nested = tmp_path / "chatgpt"
    nested.mkdir()
    write_export_zip(nested)
    assert len(find_exports([tmp_path]).candidates) == 1


def test_a_missing_root_is_skipped_not_fatal(tmp_path):
    write_export_zip(tmp_path)
    found = find_exports([tmp_path, tmp_path / "no-such-folder"])
    assert len(found.candidates) == 1


def test_default_roots_are_the_three_we_documented(tmp_path):
    for name in ("Downloads", "Desktop", "Documents"):
        (tmp_path / name).mkdir()
    assert [r.name for r in default_search_roots(tmp_path)] == [
        "Downloads",
        "Desktop",
        "Documents",
    ]


def test_default_roots_omit_folders_that_do_not_exist(tmp_path):
    (tmp_path / "Downloads").mkdir()
    assert [r.name for r in default_search_roots(tmp_path)] == ["Downloads"]


# --------------------------------------------------------------------------
# handles: opaque, and only ever inside a root
# --------------------------------------------------------------------------


def test_the_payload_never_carries_the_path(tmp_path):
    """The property that lets a handle cross the relay.

    A label names the folder and the file so a person can recognise their own
    download; the absolute path, which carries the account name, stays here.
    """
    archive = write_export_zip(tmp_path)
    payload = find_exports([tmp_path]).candidates[0].as_payload()
    assert "path" not in payload
    serialised = json.dumps(payload)
    assert str(tmp_path) not in serialised
    assert archive.name in payload["label"]


def test_a_handle_resolves_back_to_its_file(tmp_path):
    archive = write_export_zip(tmp_path)
    handle = find_exports([tmp_path]).candidates[0].handle
    assert resolve(handle, [tmp_path]) == archive


def test_a_handle_for_something_outside_the_roots_does_not_resolve(tmp_path):
    """Why resolve re-scans instead of decoding: a handle can only ever name
    something the scan would have offered anyway."""
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = write_export_zip(outside, "secret.zip")
    root = tmp_path / "root"
    root.mkdir()
    write_export_zip(root)
    assert resolve(handle_for(secret), [root]) is None


def test_a_stale_handle_returns_none_rather_than_guessing(tmp_path):
    archive = write_export_zip(tmp_path)
    handle = find_exports([tmp_path]).candidates[0].handle
    archive.unlink()
    assert resolve(handle, [tmp_path]) is None


def test_handles_are_stable_across_scans(tmp_path):
    write_export_zip(tmp_path)
    first = find_exports([tmp_path]).candidates[0].handle
    second = find_exports([tmp_path]).candidates[0].handle
    assert first == second


def test_an_empty_handle_never_resolves(tmp_path):
    write_export_zip(tmp_path)
    assert resolve("", [tmp_path]) is None


def test_an_unzipped_export_is_offered_once_not_twice(tmp_path):
    """A folder matches on its own account and again through the file inside it.

    Offering both asks the user to choose between two names for one export.
    Found by the folder test, which saw ['folder', 'file'] where one export sat.
    """
    write_export_folder(tmp_path)
    candidates = find_exports([tmp_path]).candidates
    assert len(candidates) == 1
    assert candidates[0].kind == "folder"


def test_a_loose_conversations_file_still_counts_beside_a_folder(tmp_path):
    # The dedupe is parent-scoped, not a blanket "folders win".
    write_export_folder(tmp_path, "export-a")
    (tmp_path / CONVERSATIONS_MEMBER).write_bytes(_conversations_bytes())
    kinds = sorted(c.kind for c in find_exports([tmp_path]).candidates)
    assert kinds == ["file", "folder"]


def test_counts_ignored_files_at_every_depth_of_a_folder(tmp_path):
    """A real unzipped export nests its images one folder down.

    Counting only the top level reported 136 ignored files against a folder
    holding 609 -- so the receipt would have understated, fourfold, what it
    tells the user we leave behind. The receipt is the honest screen; a number
    that drifts low there is worse than no number.
    """
    folder = tmp_path / "chatgpt-export"
    (folder / "images").mkdir(parents=True)
    (folder / CONVERSATIONS_MEMBER).write_bytes(_conversations_bytes())
    (folder / "chat.html").write_bytes(b"<html></html>")
    for i in range(12):
        (folder / "images" / f"file-{i}.png").write_bytes(b"\x89PNG" + b"\x00" * 1024)

    candidate = find_exports([tmp_path]).candidates[0]
    assert candidate.ignored_files == 13  # 12 nested images + chat.html
    assert candidate.ignored_bytes > 12 * 1024
