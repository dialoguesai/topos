"""Session-wide detector: which tests open the OWNER'S real database for writing.

``tests/conftest.py`` already has ``_no_live_db_guard``, which pins
``TOPOS_DATABASE_PATH`` at a per-test tmp file. That guard protects exactly one
resolution path — code that asks *settings* where the database is. It cannot see
a module that computed the path itself:

    LIVE_DB_PATH = Path(os.environ.get("TOPOS_DATABASE_PATH",
                                       Path.home() / ".topos" / "database.db"))
    adapters = AdapterFactory.create("local_database", db_path=LIVE_DB_PATH)

That constant is evaluated at COLLECTION time, when the env var is still unset,
so it freezes to the real home path before any fixture runs; the explicit
``db_path=`` then walks straight past the guard into
``sqlite3.connect(str(db_path))`` (storage/adapters/factory.py). Five modules do
this. On 2026-08-19 a single ``pytest tests/`` run inserted 71 rows into the
owner's ``query_artifacts`` that way.

So this watches the one thing every path has in common — ``sqlite3.connect`` —
and records, per test, any connect that would open owner data READ-WRITE.
``mode=ro`` and ``immutable=1`` URIs are not recorded: reading the owner's data
is what the live evals are for, and it changes nothing on disk.

WHAT COUNTS AS OWNER DATA is snapshotted at import, before any test can
monkeypatch ``HOME`` or ``Path.home`` — a test that repoints HOME at ``tmp_path``
must be measured against the real locations, not against its own sandbox. The
flip side is the only blind spot worth naming: a symlink planted inside a tmp
dir that points into ``~/.topos`` is resolved but not hinted (see ``_HINTS``),
so it would go unrecorded. Nothing in this suite does that.
"""

from __future__ import annotations

import os
import platform
import sqlite3
import threading
import traceback
import urllib.parse
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional

#: Resolved at import: the developer's actual home, not whatever a test claims.
_REAL_HOME = Path(os.path.expanduser("~"))

#: Substrings that make a connect worth a ``realpath`` syscall. The full suite
#: opens sqlite thousands of times and almost none of them are candidates;
#: without this pre-filter the guard would tax every one of them.
_HINTS = (".topos", "ToposEngine", "topos-control-plane")


def _norm(path) -> str:
    return os.path.realpath(os.path.expanduser(str(path)))


def _snapshot_owner_data_locations() -> tuple[frozenset, frozenset]:
    """(directory roots, individual files) holding owner data on THIS machine.

    The roots entry is a directory, not a file, on purpose: ``~/.topos`` holds
    the active database at its top level AND one per archived profile under
    ``profiles/`` (topos/storage/db/paths.py). Watching the tree covers every
    profile without this module having to re-implement profile resolution.

    The files entries are the pre-profile locations ``resolve_active_database``
    can still adopt from — a machine that has never had a profile serves one of
    those, so a test that writes there writes owner data just the same.
    """
    roots = {_REAL_HOME / ".topos"}
    files = {_REAL_HOME / ".topos_engine" / "database.db"}
    if platform.system() == "Darwin":
        files.add(
            _REAL_HOME / "Library" / "Application Support" / "ToposEngine" / "database.db"
        )
    xdg = os.getenv("XDG_DATA_HOME")
    data_dir = Path(xdg) if xdg else _REAL_HOME / ".local" / "share"
    files.add(data_dir / "topos-control-plane" / "database.db")
    return frozenset(_norm(p) for p in roots), frozenset(_norm(p) for p in files)


_WATCHED_ROOTS, _WATCHED_FILES = _snapshot_owner_data_locations()

#: Extra paths a test can arm temporarily (see :func:`watching`) so the guard
#: itself is testable without anyone having to touch the real database.
_EXTRA_WATCHED_FILES: set = set()


@dataclass(frozen=True)
class LiveDbOpen:
    """One read-write connect against owner data, and who did it."""

    nodeid: str
    path: str
    origin: str  # repo frames, innermost first, "\n      "-joined
    thread: str

    def __str__(self) -> str:  # pragma: no cover - formatting only
        where = f" on thread {self.thread}" if self.thread != "MainThread" else ""
        return (
            f"{self.nodeid}{where}\n    opened {self.path} read-write\n"
            f"    at {self.origin}"
        )


_LOCK = threading.Lock()
_OPENS: List[LiveDbOpen] = []

#: Set before every test by the conftest hook. Opens that happen during
#: collection (module-scope connects) or after the last test are attributed to
#: these sentinels rather than silently to whichever test ran nearby.
_CURRENT_NODEID = "<collection>"

#: Node ids allowed to reach owner data because they opted in by marker
#: (live / e2e / qq_eval). Recorded, reported, but not treated as a failure —
#: a lane you have to ask for by name is a decision, not an accident.
_OPT_IN_NODEIDS: set = set()

_ORIGINAL_CONNECT = None


def _database_and_uri(args, kwargs):
    """Pull ``(database, uri)`` out of a ``sqlite3.connect`` call.

    ``uri`` is the 8th positional parameter; nothing in this repo passes it that
    way, but reading it positionally costs one comparison and removes the
    question.
    """
    database = kwargs["database"] if "database" in kwargs else (args[0] if args else None)
    if "uri" in kwargs:
        uri = bool(kwargs["uri"])
    else:
        uri = bool(args[7]) if len(args) > 7 else False
    return database, uri


def owner_data_write_target(database, uri: bool = False) -> Optional[str]:
    """The owner-data file this connect would open READ-WRITE, or None.

    None for ``:memory:``, for anything outside the watched locations, and for
    URIs that cannot write: ``mode=ro``, ``mode=memory``, ``immutable=1``.
    """
    if isinstance(database, bytes):
        database = database.decode("utf-8", "replace")
    if isinstance(database, os.PathLike):
        database = os.fspath(database)
    if not isinstance(database, str) or database in ("", ":memory:"):
        return None

    if uri and database.startswith("file:"):
        split = urllib.parse.urlsplit(database)
        params = urllib.parse.parse_qs(split.query)
        if (params.get("mode") or [""])[0] in ("ro", "memory"):
            return None
        if (params.get("immutable") or [""])[0] in ("1", "true", "yes"):
            return None
        database = urllib.parse.unquote(split.path)
        if not database or database == ":memory:":
            return None

    # The hint pre-filter is a speed optimisation over the suite's thousands of
    # tmp-file connects, so it must not apply to a path a test armed explicitly
    # (:func:`watching`) — those are deliberately outside ~/.topos.
    if not _EXTRA_WATCHED_FILES and not any(hint in database for hint in _HINTS):
        return None

    real = _norm(database)
    if real in _WATCHED_FILES or real in _EXTRA_WATCHED_FILES:
        return real
    for root in _WATCHED_ROOTS:
        if real == root or real.startswith(root + os.sep):
            return real
    return None


#: Frames of blame to keep. One is enough to fail a gate but not to FIX one:
#: the immediate caller is whoever called connect, rarely whoever decided to.
_ORIGIN_FRAMES = 6


def _origin() -> str:
    """Innermost repo frames, so the chain that DECIDED to connect is visible."""
    here = os.path.abspath(__file__)
    picked = []
    for frame in reversed(traceback.extract_stack()[:-2]):
        if frame.filename == here or "sqlite3" in frame.filename:
            continue
        picked.append(f"{frame.filename}:{frame.lineno} in {frame.name}")
        if len(picked) >= _ORIGIN_FRAMES:
            break
    return "\n      ".join(picked) if picked else "<unknown>"


def install() -> None:
    """Wrap ``sqlite3.connect`` for the rest of the process. Idempotent.

    Both ``sqlite3.connect`` and ``sqlite3.dbapi2.connect`` are rebound because
    they are the same object exported under two names. Call this BEFORE the
    conftest imports any topos module: a module that had already bound the
    original by value would otherwise be invisible to the guard. (Nothing in
    this repo does ``from sqlite3 import connect``, checked — this is belt and
    braces for the one that eventually will.)
    """
    global _ORIGINAL_CONNECT
    if _ORIGINAL_CONNECT is not None:
        return
    _ORIGINAL_CONNECT = sqlite3.connect

    def _guarded_connect(*args, **kwargs):
        try:
            database, uri = _database_and_uri(args, kwargs)
            hit = owner_data_write_target(database, uri)
        except Exception:  # noqa: BLE001 - a guard must never break a connect
            hit = None
        if hit is not None:
            with _LOCK:
                _OPENS.append(
                    LiveDbOpen(
                        _CURRENT_NODEID, hit, _origin(), threading.current_thread().name
                    )
                )
            if os.getenv("TOPOS_TEST_DB_GUARD_STRICT"):
                # One frame of blame is enough to fail a gate, but not to FIX
                # one: the caller recorded above is whoever called connect, not
                # whoever decided to. Raising here puts the whole chain in the
                # traceback. Opt-in, because a run that raises mid-suite tells
                # you about one violation and hides the rest.
                raise RuntimeError(f"live-db guard: read-write open of {hit}")
        return _ORIGINAL_CONNECT(*args, **kwargs)

    sqlite3.connect = _guarded_connect
    sqlite3.dbapi2.connect = _guarded_connect


def is_installed() -> bool:
    return _ORIGINAL_CONNECT is not None


def set_current_test(nodeid: str) -> None:
    global _CURRENT_NODEID
    _CURRENT_NODEID = nodeid


def mark_opt_in(nodeid: str) -> None:
    _OPT_IN_NODEIDS.add(nodeid)


def opens() -> List[LiveDbOpen]:
    with _LOCK:
        return list(_OPENS)


def violations() -> List[LiveDbOpen]:
    """Opens by tests that never asked for owner data. These red the run."""
    return [o for o in opens() if o.nodeid not in _OPT_IN_NODEIDS]


def opted_in_opens() -> List[LiveDbOpen]:
    return [o for o in opens() if o.nodeid in _OPT_IN_NODEIDS]


class _Capture:
    """Live view of what the guard recorded inside a :func:`watching` block."""

    def __init__(self, start: int) -> None:
        self._start = start

    def captured(self) -> List[LiveDbOpen]:
        with _LOCK:
            return list(_OPENS[self._start :])


@contextmanager
def watching(path) -> Iterator[_Capture]:
    """Arm ``path`` as owner data for the duration of the block.

    Exists so the guard can be proved against a throwaway file instead of the
    real database — a self-test that has to touch ``~/.topos`` to prove itself
    is the thing this module was written to stop. Whatever it records inside the
    block is dropped on exit, so a passing self-test never reds the session.
    """
    real = _norm(path)
    _EXTRA_WATCHED_FILES.add(real)
    with _LOCK:
        start = len(_OPENS)
    try:
        yield _Capture(start)
    finally:
        _EXTRA_WATCHED_FILES.discard(real)
        with _LOCK:
            del _OPENS[start:]
