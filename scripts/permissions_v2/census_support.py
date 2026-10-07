"""Shared pieces of the WS1 grant census (contract IF-1): read-only openers, the refusal of the
live store, the live-path alias and the no-write guards.

The census never reads the node's live stores. `census_copy.py` takes one consistent copy of
them with SQLite's online backup, and everything else here runs on that copy, from engine
code that stays unedited. Three engine behaviours would otherwise make a census on a copy
either wrong or a writer, and each is replaced for the length of one `copy_session` only:

- `EvidenceResolver._file_revision` hashes the canonical file's PATH. Every review snapshot,
  both enrollment markers and the ingest ledger embed the live node's value, so a resolver
  bound to the copy would find every review stale and every marker foreign. Inside the session
  a resolver bound to the copy reports the revision of the live path the copy was taken from
  (the node config's `canonical_database_path`). The design already states that byte copies
  under the same binding and path are not told apart (evidence.py `_file_revision`); nothing
  else about the resolver changes, and the drift test pins the original method's source.
- `EvidenceResolver._complete_lineage_keys` opens the canonical file read-write to key opaque
  facts after a read that saw some. It only ever costs time, never a candidate (its own
  docstring), so inside the session it does nothing and is counted.
- `IngestProvenanceService._publish_marker` rewrites the ingest marker when the copy's source
  clock is ahead of the marker. Inside the session the new marker is kept in memory, as the
  node keeps it after publishing, and is counted; the file is never written.

Output discipline (plan §4.2, "nothing is printed"): what reaches stdout is counts, booleans
and fixed-vocabulary codes. No content, name, term, identifier or path of the owner's data.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import stat
from dataclasses import dataclass, field
from pathlib import Path

LIVE_HOME = Path.home() / ".topos"
SIDECARS = ("-wal", "-shm", "-journal")


class CensusRefused(RuntimeError):
    """A fixed refusal code. Never carries a path, name or value."""


def _same_or_under(candidate: Path, root: Path) -> bool:
    candidate, root = Path(os.path.realpath(candidate)), Path(os.path.realpath(root))
    if candidate == root or root in candidate.parents:
        return True
    # APFS folds case and /System/Volumes/Data is a firmlink to /: compare by inode as well.
    if not root.exists():
        return False
    probe = candidate
    while True:
        try:
            if probe.exists() and os.path.samefile(probe, root):
                return True
        except OSError:
            pass
        if probe.parent == probe:
            return False
        probe = probe.parent


def refuse_live(path: Path) -> Path:
    """Refuse any path at or under the live ~/.topos (census inputs and outputs alike)."""
    if _same_or_under(Path(path), LIVE_HOME):
        raise CensusRefused("live_store_refused")
    return Path(path)


def _another_name_for_a_file_under(info: os.stat_result, root: Path) -> bool:
    """Whether the file `info` describes is also a file under `root` (a hard link into it): same device, same inode."""
    if not root.exists():
        return False
    for folder, _folders, names in os.walk(root):
        for name in names:
            try:
                other = os.lstat(os.path.join(folder, name))
            except OSError:
                continue
            if stat.S_ISREG(other.st_mode) and (other.st_dev, other.st_ino) == (info.st_dev, info.st_ino):
                return True
    return False


def refuse_a_real_database(path: Path, *, closed: bool = False) -> Path:
    """The one check for every tool here that WRITES to the database it is given: the pre-flight of the carry
    step, the upgrade matrix, the fixture builder. Returns the path, or refuses with a fixed word.

    - `live_store_refused`: the path is the real home's or under it (`refuse_live`: by path, and by the inode of
      each folder above it, so another spelling and a link to the home are caught), or the file is another name
      for a file under the real home.
    - `database_is_a_hard_link`: the file has more than one name. Whatever the other name is, a write here is a
      write there, and nothing beside THIS name (a write-ahead log, a lock) says whether a node has the other one
      open. The third re-check ran the pre-flight for real on a real database through such a "copy". A real copy
      has one name: `cp -R`, never links.
    - with `closed`, `copy_not_closed`: a write-ahead log, its index or a journal stands beside the file, so a
      process had it open or its newest rows are only in the log.
    A path where there is no file yet (a fixture to be made) passes the first check and has nothing to fail the
    others."""
    path = refuse_live(Path(path))
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return path
    if info.st_nlink != 1:
        if _another_name_for_a_file_under(info, LIVE_HOME):
            raise CensusRefused("live_store_refused")
        raise CensusRefused("database_is_a_hard_link")
    if closed and any(Path(str(path) + suffix).exists() for suffix in SIDECARS):
        raise CensusRefused("copy_not_closed")
    return path


def require_scratch_environment() -> None:
    """Engine imports resolve a database from the environment; both must point at scratch."""
    for variable in ("TOPOS_DATABASE_PATH", "TOPOS_ENV_FILE"):
        value = os.environ.get(variable, "")
        if not value or not Path(value).is_absolute():
            raise CensusRefused("scratch_environment_required")
        refuse_live(Path(value))


def ro(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    """A read-only connection. `immutable` only for closed copies (no sidecars may exist)."""
    path = Path(path)
    if immutable and any(Path(str(path) + suffix).exists() for suffix in SIDECARS):
        raise CensusRefused("copy_not_closed")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else ""), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def private_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise CensusRefused("private_directory_required")
    os.chmod(path, 0o700)
    return path


def write_private(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.chmod(path, mode)


def shred(path: Path) -> None:
    """Zero-overwrite then unlink; the census's own private files only."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    os.chmod(path, 0o600)
    with open(path, "r+b", buffering=0) as handle:
        handle.write(b"\0" * size)
        os.fsync(handle.fileno())
    path.unlink()


@dataclass
class SessionCounters:
    lineage_key_completions_skipped: int = 0
    ingest_marker_publishes_held_in_memory: int = 0
    aliased_revisions: int = 0
    extra: dict = field(default_factory=dict)


@contextlib.contextmanager
def copy_session(copy_database: Path, live_database_path: str):
    """Run engine code against a copy as if at the live path, and never write the copy.

    Restores every replaced attribute on exit, whatever happens inside.
    """
    from topos.permissions_v2 import evidence, ingest_provenance
    from topos.permissions_v2.canonical import digest

    copy_database = Path(copy_database)
    counters = SessionCounters()
    original_revision = evidence.EvidenceResolver._file_revision
    original_complete = evidence.EvidenceResolver._complete_lineage_keys
    original_publish = ingest_provenance.IngestProvenanceService._publish_marker

    def _file_revision(self):
        # Identical to EvidenceResolver._file_revision except for the path string of the copy.
        self._incarnation()
        path = str(self.path)
        if live_database_path is not None and Path(self.path) == copy_database:
            path = live_database_path
            counters.aliased_revisions += 1
        return digest({"binding": self.binding.model_dump(), "clock_id": self._clock_id, "canonical_path": path})

    def _complete_lineage_keys(self):
        counters.lineage_key_completions_skipped += 1

    def _publish_marker(self, marker):
        counters.ingest_marker_publishes_held_in_memory += 1
        if marker.get("state") == "active":
            self._marker = marker

    evidence.EvidenceResolver._file_revision = _file_revision
    evidence.EvidenceResolver._complete_lineage_keys = _complete_lineage_keys
    ingest_provenance.IngestProvenanceService._publish_marker = _publish_marker
    try:
        yield counters
    finally:
        evidence.EvidenceResolver._file_revision = original_revision
        evidence.EvidenceResolver._complete_lineage_keys = original_complete
        ingest_provenance.IngestProvenanceService._publish_marker = original_publish


def load_config(copy_root: Path) -> dict:
    import json
    return json.loads((Path(copy_root) / "permissions-v2" / "config.json").read_text())


def binding_from_config(config: dict):
    from topos.permissions_v2.evidence import EvidenceBinding
    identity = config["identity"]
    return EvidenceBinding.parse({key: identity[key] for key in EvidenceBinding.model_fields})
