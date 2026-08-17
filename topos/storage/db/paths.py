"""Where this node's SQLite database lives — one answer, one code path.

``resolve_active_database`` is that answer. Everything that needs to know which
file the node serves (connection setup, size reporting, the CLI's ``--discover``
diagnostic, profile adoption) goes through it.

It replaced four separate searches with four different candidate lists and four
different orders. They disagreed, and the disagreement was silent: when the
active slot held no ``database.db`` — which is every newly created Topos before
its first write — the search fell through to a database left behind by a
pre-profile install. On 2026-08-17 a new Topos ran its entire session against
``~/Library/Application Support/ToposEngine/database.db``: a file no profile
owns, that no switch will ever archive, and that the node had already migrated
in place. Nothing logged it; ``lsof`` was the only way to see it.

So: a machine that uses the profile layout resolves to its slot and nothing
else, whether or not the file exists yet. Legacy locations are consulted only
on a machine that has never had a profile, and then the database is ADOPTED
(copied into the slot) rather than served where it lies — the served file
always belongs to the profile that owns it.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger("topos.storage.db.paths")

#: The active Topos lives at the top level of ~/.topos; archived ones live under
#: ``profiles/``. Defined here rather than in ``topos.profiles`` because this
#: module is the one every database consumer already imports — ``topos.profiles``
#: imports these names back out, so there is still a single spelling of each.
TOPOS_DIRNAME = ".topos"
DATABASE_FILENAME = "database.db"
PROFILES_DIRNAME = "profiles"
ACTIVE_MARKER_FILENAME = "active-profile.json"


def active_base() -> Path:
    """The directory holding the active Topos."""
    return Path.home() / TOPOS_DIRNAME


def get_database_path(base_path: Optional[str]) -> Path:
    if base_path:
        return Path(base_path)
    return active_base() / DATABASE_FILENAME


def get_config_file() -> Path:
    return get_data_directory() / "config.json"


def load_config() -> dict:
    config_file = get_config_file()
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as exc:
            logger.warning("Failed to load config from %s: %s", config_file, exc)
    return {}


def save_config(config: dict) -> None:
    config_file = get_config_file()
    config_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception as exc:
        logger.error("Failed to save config to %s: %s", config_file, exc)
        raise


def get_data_directory() -> Path:
    system = platform.system()
    if system == "Windows":
        base = Path(os.getenv("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "ToposControlPlane"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "ToposControlPlane"

    xdg_data = os.getenv("XDG_DATA_HOME")
    if xdg_data:
        return Path(xdg_data) / "topos-control-plane"
    return Path.home() / ".local" / "share" / "topos-control-plane"


# -- legacy locations -------------------------------------------------------


def legacy_database_candidates() -> list[Path]:
    """Databases from installs that predate the profile layout.

    Read for ADOPTION only. A machine that has ever had a profile never
    consults these — see the module docstring for what happened when it did.
    """
    candidates: list[Path] = []
    if platform.system() == "Darwin":
        candidates.append(
            Path.home() / "Library" / "Application Support" / "ToposEngine" / DATABASE_FILENAME
        )
    candidates.append(Path.home() / ".topos_engine" / DATABASE_FILENAME)
    candidates.append(get_data_directory() / DATABASE_FILENAME)
    return candidates


def existing_legacy_databases() -> list[Path]:
    return [p for p in legacy_database_candidates() if p.is_file()]


def newest_legacy_database() -> Optional[Path]:
    """The most recently written legacy database, if any.

    Newest rather than first-in-list: the candidate order is historical, and on
    a machine carrying several of these the one still being written to is the
    one the user means.
    """
    existing = existing_legacy_databases()
    if not existing:
        return None
    return max(existing, key=lambda p: p.stat().st_mtime)


# -- the resolver -----------------------------------------------------------


#: How ``resolve_active_database`` reached its answer. Carried into the startup
#: log line and the healthcheck so a mis-binding is visible from the outside
#: instead of requiring ``lsof``.
SOURCE_SETTINGS = "settings"  # an explicit database_path pin wins over everything
SOURCE_SLOT = "slot"  # the active profile's database, already on disk
SOURCE_NEW_SLOT = "new-slot"  # the active profile's database, not created yet
SOURCE_ADOPTED = "adopted"  # a pre-profile database copied into the slot
SOURCE_LEGACY = "legacy"  # a pre-profile database, served where it lies


@dataclass(frozen=True)
class ActiveDatabase:
    """Which SQLite file this node serves, and how that was decided."""

    path: Path
    source: str
    profile_id: Optional[str] = None
    adopted_from: Optional[Path] = None

    @property
    def in_active_slot(self) -> bool:
        return self.source in (SOURCE_SLOT, SOURCE_NEW_SLOT, SOURCE_ADOPTED)


def _active_profile_id(base: Path) -> Optional[str]:
    marker = base / ACTIVE_MARKER_FILENAME
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a missing or unreadable marker is not an error
        return None
    profile_id = payload.get("profile_id")
    return str(profile_id) if profile_id else None


def uses_profile_layout(base: Optional[Path] = None) -> bool:
    """Has this machine ever created or switched a Topos?

    Either signal is enough: the marker is written on activation, and the
    ``profiles/`` directory survives every archive. A machine with either one
    has an active slot even when that slot is empty.
    """
    base = base or active_base()
    return (base / ACTIVE_MARKER_FILENAME).exists() or (base / PROFILES_DIRNAME).is_dir()


def adopt_into_slot(legacy: Path, slot: Path) -> bool:
    """Copy a legacy database (and its sidecars) into the active slot.

    Copy, never move: the original stays as its own backup, which is what makes
    this safe to do automatically on first start.
    """
    try:
        slot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, slot)
        for suffix in ("-wal", "-shm"):
            sidecar = legacy.with_name(legacy.name + suffix)
            if sidecar.is_file():
                shutil.copy2(sidecar, slot.with_name(slot.name + suffix))
        return True
    except OSError as exc:
        logger.error("Could not adopt legacy database %s into %s: %s", legacy, slot, exc)
        return False


def _pinned_database_path() -> Optional[str]:
    """An explicitly configured database path, if there is one.

    Falls back to the environment when settings cannot be constructed. The
    resolver has to answer on an unpaired machine too: ``Settings()`` refuses
    to build without a ``TOPOS_KEY``, and ``--discover`` is precisely the
    diagnostic someone runs when their install is not working yet.
    """
    try:
        from ...config.settings import settings

        if settings.topos_database_path:
            return str(settings.topos_database_path)
        return None
    except Exception:  # noqa: BLE001 — unconfigured is not an error here
        return os.getenv("TOPOS_DATABASE_PATH") or None


def resolve_active_database(
    base: Optional[Path] = None, *, adopt: bool = False
) -> ActiveDatabase:
    """Resolve which database this node serves.

    ``adopt=True`` may copy a pre-profile database into the slot, so only
    startup passes it — the steady-state resolution that runs on almost every
    API call must be free of side effects (and of logging).
    """
    base = base or active_base()

    pinned = _pinned_database_path()
    if pinned:
        return ActiveDatabase(Path(pinned), source=SOURCE_SETTINGS)

    slot = base / DATABASE_FILENAME
    profile_id = _active_profile_id(base)

    if slot.is_file():
        return ActiveDatabase(slot, source=SOURCE_SLOT, profile_id=profile_id)

    # The slot is empty. On a machine with profiles that means a new Topos that
    # has not written yet, and the answer is still the slot — searching legacy
    # locations here is the bug this function exists to prevent.
    if uses_profile_layout(base):
        return ActiveDatabase(slot, source=SOURCE_NEW_SLOT, profile_id=profile_id)

    legacy = newest_legacy_database()
    if legacy is None:
        return ActiveDatabase(slot, source=SOURCE_NEW_SLOT, profile_id=profile_id)

    if not adopt:
        # Resolution without side effects still has to name the file the node
        # would actually serve, or the size readout and the connection would
        # disagree until the next start.
        return ActiveDatabase(legacy, source=SOURCE_LEGACY)

    if adopt_into_slot(legacy, slot):
        return ActiveDatabase(
            slot, source=SOURCE_ADOPTED, profile_id=profile_id, adopted_from=legacy
        )
    return ActiveDatabase(legacy, source=SOURCE_LEGACY)


def discover_databases() -> list[Path]:
    """The active database first, then any legacy strays — a diagnostic.

    Surfaces exactly the situation that used to be invisible: a database
    outside the active slot that some earlier install left behind.
    """
    databases: list[Path] = []
    active = resolve_active_database()
    if active.path.is_file():
        databases.append(active.path)
    for candidate in existing_legacy_databases():
        if candidate not in databases:
            databases.append(candidate)
    config = load_config()
    if "database_path" in config:
        pinned = Path(config["database_path"])
        if pinned.is_file() and pinned not in databases:
            databases.append(pinned)
    return databases


def sqlite_on_disk_size_bytes(db_path: Path) -> Optional[int]:
    """Return on-disk bytes for a SQLite file including WAL and SHM sidecars."""
    if not db_path.is_file():
        return None
    total = 0
    for suffix in ("", "-wal", "-shm"):
        candidate = db_path if not suffix else db_path.with_name(f"{db_path.name}{suffix}")
        if not candidate.is_file():
            continue
        try:
            total += candidate.stat().st_size
        except OSError as exc:
            logger.debug("Failed to stat %s: %s", candidate, exc)
    return total if total > 0 else None


def get_local_database_size_bytes(explicit_path: Optional[str] = None) -> Optional[int]:
    """Best-effort on-disk size of the active local SQLite database.

    Goes through the resolver so the number describes the database the node is
    actually serving. It used to run its own search with its own ordering, so
    the size shown in the app could describe a different file than the one
    being read.
    """
    if explicit_path:
        return sqlite_on_disk_size_bytes(Path(explicit_path))
    return sqlite_on_disk_size_bytes(resolve_active_database().path)


def validate_database(db_path: Path) -> bool:
    if not db_path.exists() or not db_path.is_file():
        return False
    try:
        import sqlite3

        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing_tables = {row[0] for row in cursor}
        conn.close()
        required_tables = {"oplog", "messages", "engine_config"}
        return required_tables.issubset(existing_tables)
    except Exception as exc:
        logger.warning("Failed to validate database %s: %s", db_path, exc)
        return False


def read_database_user_version(db_path: Path) -> Optional[int]:
    """``PRAGMA user_version`` of a database this process is not serving.

    Read-only and best effort: returns None for a file that is missing, not a
    database, or unreadable. Callers use it to decide whether a database is too
    new for this build, and "cannot tell" must never be treated as "too new".
    """
    if not db_path.is_file():
        return None
    try:
        import sqlite3

        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=1.0)
        try:
            row = conn.execute("PRAGMA user_version").fetchone()
        finally:
            conn.close()
        return int(row[0]) if row else None
    except Exception as exc:  # noqa: BLE001 — diagnostics only
        logger.debug("Could not read user_version from %s: %s", db_path, exc)
        return None
