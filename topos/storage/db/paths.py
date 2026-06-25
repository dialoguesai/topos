from __future__ import annotations

import json
import logging
import os
import platform
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger("topos.storage.db.paths")


def get_database_path(base_path: Optional[str]) -> Path:
    if base_path:
        return Path(base_path)
    return Path.home() / ".topos" / "database.db"


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


def discover_databases() -> list[Path]:
    databases: list[Path] = []
    config = load_config()
    if "database_path" in config:
        db_path = Path(config["database_path"])
        if db_path.exists() and db_path.is_file():
            databases.append(db_path)

    data_dir = get_data_directory()
    default_db = data_dir / "database.db"
    if default_db.exists() and default_db not in databases:
        databases.append(default_db)

    legacy_db = Path.home() / ".topos_engine" / "database.db"
    if legacy_db.exists() and legacy_db not in databases:
        databases.append(legacy_db)

    env_path = os.getenv("TOPOS_DATABASE_PATH")
    if env_path:
        env_db = Path(env_path)
        if env_db.exists() and env_db.is_file() and env_db not in databases:
            databases.append(env_db)

    return databases


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


def migrate_legacy_database() -> Optional[Path]:
    legacy_path = Path.home() / ".topos_engine" / "database.db"
    if not legacy_path.exists():
        return None

    new_path = get_data_directory() / "database.db"
    if new_path.exists():
        logger.warning("Both legacy (%s) and new (%s) database exist. Using new location.", legacy_path, new_path)
        return new_path

    try:
        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy_path, new_path)
        config = load_config()
        config["database_path"] = str(new_path)
        save_config(config)
        logger.info("Migrated database from %s to %s", legacy_path, new_path)
        return new_path
    except Exception as exc:
        logger.error("Failed to migrate database: %s", exc)
        return None


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
    """Best-effort on-disk size of the active local SQLite database."""
    if explicit_path:
        return sqlite_on_disk_size_bytes(Path(explicit_path))

    from ...config.settings import settings

    if settings.topos_database_path:
        size = sqlite_on_disk_size_bytes(Path(settings.topos_database_path))
        if size is not None:
            return size

    candidates: list[Path] = []
    config = load_config()
    if "database_path" in config:
        candidates.append(Path(config["database_path"]))
    env_path = os.getenv("TOPOS_DATABASE_PATH")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(get_database_path(None))
    if platform.system() == "Darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "ToposEngine" / "database.db")
    candidates.append(Path.home() / ".topos_engine" / "database.db")
    candidates.append(get_data_directory() / "database.db")

    seen: set[str] = set()
    for db_path in candidates:
        key = str(db_path)
        if key in seen:
            continue
        seen.add(key)
        size = sqlite_on_disk_size_bytes(db_path)
        if size is not None:
            return size
    return None


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
