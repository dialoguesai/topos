"""Find and open an export that is already on this machine.

Two jobs, both deliberately narrow.

**Discovery.** Look in a few well-known folders for something that *is* an export
and describe it well enough for a person to recognise. Matching is on shape --
does it contain a ``conversations.json``? -- and never on the name. A search for
"ChatGPT" in a downloads folder matches every image the product ever generated;
on the machine this was written for, dozens of them and not one export.

**Opening.** Hand back a stream of the one member we can read. An export archive
is overwhelmingly images by weight, none of it ingestible. We take one file out
and leave the rest where it is: nothing is copied, expanded, or stored, and the
archive on disk is not modified.

The point of both is that the bytes never move. If the file and the node are on
the same machine, an import should cost one open() and no network at all.

Callers outside this module should treat a candidate's ``handle`` as opaque. It
is a digest, not an encoding: it cannot be turned back into a path, and
:func:`resolve` only ever returns paths that are still inside a search root.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Dict, Iterable, List, Optional, Sequence

# The one member of an export we can read. Everything else in the archive --
# the rendered chat.html, the images, the small metadata files -- is ignored.
CONVERSATIONS_MEMBER = "conversations.json"

# Folder names to look in, relative to the user's home. Present on macOS and
# Windows alike; a missing one is skipped rather than being an error.
SEARCH_ROOT_NAMES = ("Downloads", "Desktop", "Documents")

# How far below a root to look. 1 means "a folder in Downloads", which covers
# the unzip-in-place case. Deeper than that and a scan of a large Documents
# tree stops being cheap, and starts being a search of someone's whole life.
MAX_DEPTH = 2

# Bounds, so a pathological folder cannot turn discovery into a hang. Every one
# of these is reported when it bites (see ``ScanResult.truncated``) rather than
# silently shortening the list.
MAX_CANDIDATES = 25
MAX_ENTRIES_SCANNED = 4000
MAX_ARCHIVE_PROBES = 40
# How many files inside one export folder we will stat to describe it.
MAX_FOLDER_ENTRIES = 5000
SCAN_DEADLINE_SECONDS = 2.5

# An export's conversations.json is never tiny. Anything smaller is a stub, a
# truncated download, or a different file wearing the same name.
MIN_PAYLOAD_BYTES = 1024

_ARCHIVE_SUFFIXES = (".zip",)


@dataclass(frozen=True)
class ExportCandidate:
    """One thing on disk that looks like an export.

    ``size_bytes`` is what sits on disk; ``payload_bytes`` is the part we will
    actually read. For an archive those differ by more than an order of
    magnitude, and showing both is what lets the import screen tell the truth
    about how little of the file it needs.
    """

    handle: str
    label: str
    kind: str  # "archive" | "folder" | "file"
    size_bytes: int
    payload_bytes: int
    ignored_files: int
    ignored_bytes: int
    modified_at: float
    path: Path

    def as_payload(self) -> dict:
        """The candidate as it crosses a wire -- everything except the path.

        The label carries the root name and the file name, which is what a
        person needs to recognise their own download. The absolute path, which
        would carry the account name and whatever else the home directory
        reveals, stays here.
        """
        return {
            "handle": self.handle,
            "label": self.label,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "payload_bytes": self.payload_bytes,
            "ignored_files": self.ignored_files,
            "ignored_bytes": self.ignored_bytes,
            "modified_at": self.modified_at,
        }


@dataclass(frozen=True)
class ScanResult:
    candidates: List[ExportCandidate]
    roots_searched: List[str]
    truncated: bool

    def as_payload(self) -> dict:
        return {
            "candidates": [c.as_payload() for c in self.candidates],
            "roots_searched": self.roots_searched,
            "truncated": self.truncated,
        }


def handle_for(path: Path) -> str:
    """A stable, opaque id for a path.

    A digest rather than an encoding: the browser can hold it and hand it back,
    but cannot read a filesystem layout out of it, and neither can anything that
    sees it in transit.
    """
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]


def default_search_roots(home: Optional[Path] = None) -> List[Path]:
    """The folders we look in, on whichever platform this is.

    Windows and macOS both put these directly under the user profile, so one
    list covers both. A root that does not exist is simply absent -- plenty of
    people have no Desktop folder.
    """
    base = home or Path.home()
    roots: List[Path] = []
    for name in SEARCH_ROOT_NAMES:
        candidate = base / name
        try:
            if candidate.is_dir():
                roots.append(candidate)
        except OSError:
            # An unreadable root (permissions, a stale network mount) is not an
            # error: it is a folder we cannot offer, and the scan goes on.
            continue
    return roots


def _archive_summary(path: Path) -> Optional[tuple[int, int, int]]:
    """``(payload_bytes, ignored_files, ignored_bytes)`` for an export archive.

    Read from the central directory only -- no member is decompressed, so this
    costs a couple of seeks even on a multi-gigabyte file. Returns None when the
    archive holds no conversations file, which is how a zip of holiday photos
    fails to look like an export.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            payload_bytes = None
            ignored_files = 0
            ignored_bytes = 0
            for info in archive.infolist():
                if info.is_dir():
                    continue
                if info.filename.rsplit("/", 1)[-1] == CONVERSATIONS_MEMBER:
                    # Shallowest wins: the export puts it at the archive root.
                    if payload_bytes is None or info.filename.count("/") == 0:
                        payload_bytes = info.file_size
                    continue
                ignored_files += 1
                ignored_bytes += info.file_size
    except (zipfile.BadZipFile, OSError, RuntimeError):
        return None
    if payload_bytes is None or payload_bytes < MIN_PAYLOAD_BYTES:
        return None
    return payload_bytes, ignored_files, ignored_bytes


def _folder_summary(path: Path) -> Optional[tuple[int, int, int]]:
    """Same shape for an unzipped export folder.

    Counts at every depth, not just the top. A real unzipped export keeps the
    conversations file at the root and puts its images in a subfolder, so
    scanning one level deep reported 136 ignored files where there were 609 --
    and the receipt built on that number would have understated, by a factor of
    four, what the user was being told we leave behind.
    """
    member = path / CONVERSATIONS_MEMBER
    try:
        if not member.is_file():
            return None
        payload_bytes = member.stat().st_size
        if payload_bytes < MIN_PAYLOAD_BYTES:
            return None
    except OSError:
        return None

    ignored_files = 0
    ignored_bytes = 0
    seen = 0
    for directory, subdirs, filenames in os.walk(path, followlinks=False):
        for filename in filenames:
            seen += 1
            if seen > MAX_FOLDER_ENTRIES:
                # Stop counting rather than stop offering: an approximate
                # "ignoring at least N" beats refusing a usable export.
                return payload_bytes, ignored_files, ignored_bytes
            if directory == str(path) and filename == CONVERSATIONS_MEMBER:
                continue
            try:
                ignored_bytes += os.stat(
                    os.path.join(directory, filename), follow_symlinks=False
                ).st_size
                ignored_files += 1
            except OSError:
                continue
    return payload_bytes, ignored_files, ignored_bytes


def _label_for(path: Path, root: Path) -> str:
    """"Downloads / chatgpt-export.zip" -- enough to recognise, no more."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = Path(path.name)
    return f"{root.name} / {relative.as_posix()}"


def _candidate_for(path: Path, root: Path) -> Optional[ExportCandidate]:
    try:
        stat = path.stat()
    except OSError:
        return None

    if path.is_dir():
        summary = _folder_summary(path)
        kind = "folder"
        size_bytes = 0
        if summary:
            size_bytes = summary[0] + summary[2]
    elif path.suffix.lower() in _ARCHIVE_SUFFIXES:
        summary = _archive_summary(path)
        kind = "archive"
        size_bytes = stat.st_size
    elif path.name == CONVERSATIONS_MEMBER:
        if stat.st_size < MIN_PAYLOAD_BYTES:
            return None
        summary = (stat.st_size, 0, 0)
        kind = "file"
        size_bytes = stat.st_size
    else:
        return None

    if not summary:
        return None

    payload_bytes, ignored_files, ignored_bytes = summary
    return ExportCandidate(
        handle=handle_for(path),
        label=_label_for(path, root),
        kind=kind,
        size_bytes=size_bytes,
        payload_bytes=payload_bytes,
        ignored_files=ignored_files,
        ignored_bytes=ignored_bytes,
        modified_at=stat.st_mtime,
        path=path,
    )


def find_exports(
    roots: Optional[Sequence[Path]] = None,
    *,
    now: Optional[float] = None,
    deadline_seconds: float = SCAN_DEADLINE_SECONDS,
) -> ScanResult:
    """Look for exports in ``roots``, newest first.

    Never follows symlinks -- a link is the one cheap way a scan bounded to
    three folders ends up reading something else entirely.
    """
    search_roots = list(roots) if roots is not None else default_search_roots()
    started = now if now is not None else time.monotonic()
    found: List[ExportCandidate] = []
    seen: set = set()
    entries_scanned = 0
    archives_probed = 0
    truncated = False

    def out_of_budget() -> bool:
        return (time.monotonic() - started) > deadline_seconds

    for root in search_roots:
        stack: List[tuple[Path, int]] = [(root, 0)]
        while stack:
            directory, depth = stack.pop()
            if out_of_budget() or entries_scanned >= MAX_ENTRIES_SCANNED:
                truncated = True
                break
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        entries_scanned += 1
                        if entries_scanned >= MAX_ENTRIES_SCANNED or out_of_budget():
                            truncated = True
                            break
                        if entry.is_symlink():
                            continue
                        path = Path(entry.path)
                        is_dir = entry.is_dir(follow_symlinks=False)
                        if is_dir and depth + 1 < MAX_DEPTH:
                            stack.append((path, depth + 1))
                        looks_like_archive = (
                            not is_dir and path.suffix.lower() in _ARCHIVE_SUFFIXES
                        )
                        if looks_like_archive:
                            if archives_probed >= MAX_ARCHIVE_PROBES:
                                truncated = True
                                continue
                            archives_probed += 1
                        elif not is_dir and entry.name != CONVERSATIONS_MEMBER:
                            continue
                        candidate = _candidate_for(path, root)
                        if candidate and candidate.handle not in seen:
                            seen.add(candidate.handle)
                            found.append(candidate)
            except OSError:
                # A folder we cannot read is a folder we cannot offer. macOS
                # gates Desktop and Documents behind a consent prompt; a refusal
                # there must narrow the results, never fail the scan.
                continue

    # An unzipped export matches twice -- once as the folder, once as the
    # conversations file inside it -- and offering both asks the user to choose
    # between two names for one thing. Keep the folder: it is what the export
    # site produced, and it is the one that knows what it is leaving behind.
    folder_paths = {c.path for c in found if c.kind == "folder"}
    found = [c for c in found if not (c.kind == "file" and c.path.parent in folder_paths)]

    found.sort(key=lambda c: c.modified_at, reverse=True)
    if len(found) > MAX_CANDIDATES:
        found = found[:MAX_CANDIDATES]
        truncated = True
    return ScanResult(
        candidates=found,
        roots_searched=[r.name for r in search_roots],
        truncated=truncated,
    )


def resolve(handle: str, roots: Optional[Sequence[Path]] = None) -> Optional[Path]:
    """Turn a handle back into a path, by finding it again.

    Deliberately a re-scan rather than a stored mapping: there is no table to go
    stale, no state to leak across users, and a handle for something that has
    since moved or been deleted simply stops resolving. It also means a handle
    can only ever name something inside a search root, which is the property
    that makes it safe to accept one over a wire.
    """
    if not handle:
        return None
    for candidate in find_exports(roots).candidates:
        if candidate.handle == handle:
            return candidate.path
    return None


class _ArchiveMemberStream:
    """A read-only stream over one archive member that also owns the archive.

    ``ZipFile.open`` decompresses lazily, so reading this never materialises the
    member -- which matters when the member is tens of megabytes inside an
    archive of more than a gigabyte. Closing it closes both.
    """

    def __init__(self, archive: zipfile.ZipFile, member: str) -> None:
        self._archive = archive
        self._stream = archive.open(member)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            self._archive.close()

    def __enter__(self) -> "_ArchiveMemberStream":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class LocalExportError(Exception):
    """The path is not something we can read conversations out of."""


def _conversations_member(archive: zipfile.ZipFile) -> Optional[str]:
    members = [
        name
        for name in archive.namelist()
        if name.rsplit("/", 1)[-1] == CONVERSATIONS_MEMBER
    ]
    if not members:
        return None
    return min(members, key=lambda name: name.count("/"))


def is_container(path: Path) -> bool:
    """True when the ingestible content lives *inside* ``path``.

    A container must never be copied as-is: it is the archive or the folder the
    export site produced, and only one member of it can be read.
    """
    try:
        if path.is_dir():
            return True
    except OSError:
        return False
    return path.suffix.lower() in _ARCHIVE_SUFFIXES


def open_ingestible(path: Path) -> BinaryIO:
    """Open the part of ``path`` we can actually read.

    A plain file opens as itself -- this is the ordinary ingestion path and must
    stay unchanged. A folder or an archive resolves to the conversations file
    inside it, so a user can point at what the export site gave them rather than
    being asked to go find one file in it.
    """
    if path.is_dir():
        member = path / CONVERSATIONS_MEMBER
        if not member.is_file():
            raise LocalExportError(
                f"No {CONVERSATIONS_MEMBER} in that folder. "
                f"Choose the folder from the export, or the {CONVERSATIONS_MEMBER} inside it."
            )
        return open(member, "rb")

    if path.suffix.lower() in _ARCHIVE_SUFFIXES:
        try:
            archive = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise LocalExportError(
                "That .zip could not be opened. Re-download the export and try again."
            ) from exc
        member = _conversations_member(archive)
        if not member:
            archive.close()
            raise LocalExportError(
                f"No {CONVERSATIONS_MEMBER} inside that archive. "
                f"Use the export .zip, or the {CONVERSATIONS_MEMBER} from inside it."
            )
        return _ArchiveMemberStream(archive, member)  # type: ignore[return-value]

    return open(path, "rb")


def describe_export(path: Path) -> Dict[str, Any]:
    """What is actually inside an export: how many conversations, and when.

    Discovery deliberately stops at the archive's central directory, which is
    cheap and says nothing about content. That left the window control unable
    to warn about the one mismatch that silently imports nothing: an export
    older than the window it is measured against. Seen twice on the same file —
    a July 2025 export against a six-month window drops all 1,543 conversations
    and completes successfully with zero records.

    So this opens the thing and reads the envelope stamps. It costs a parse of
    the conversations file (about a second for 52 MB), which is why it is
    called for ONE chosen candidate rather than for every row of a scan.
    """
    import json

    from .parsers.chatgpt_export import conversation_activity

    with open_ingestible(path) as stream:
        payload = json.loads(stream.read().decode("utf-8"))

    conversations = payload if isinstance(payload, list) else [payload]
    newest: Optional[float] = None
    oldest: Optional[float] = None
    counted = 0
    for conversation in conversations:
        if not isinstance(conversation, dict) or not isinstance(conversation.get("mapping"), dict):
            continue
        counted += 1
        created, last_active = conversation_activity(conversation)
        for stamp in (created, last_active):
            if stamp is None:
                continue
            newest = stamp if newest is None else max(newest, stamp)
            oldest = stamp if oldest is None else min(oldest, stamp)

    return {
        "conversations": counted,
        "oldest_at": oldest,
        "newest_at": newest,
        # The smallest window, in whole months from now, that reaches the most
        # recent conversation. The number the user actually needs, computed
        # where the dates are, rather than left as arithmetic on two timestamps.
        "months_to_reach_newest": (
            max(1, math.ceil((time.time() - newest) / (30.44 * 86400))) if newest else None
        ),
    }


def iter_ingestible_chunks(path: Path, chunk_size: int = 1 << 20) -> Iterable[bytes]:
    """Stream the ingestible content of ``path``.

    Streaming rather than ``read_bytes()`` is the whole point: pointing the node
    at a gigabyte-scale archive should cost a buffer, not a copy of the file in
    memory.
    """
    stream = open_ingestible(path)
    try:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            yield chunk
    finally:
        stream.close()
