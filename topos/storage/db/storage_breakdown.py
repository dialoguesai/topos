from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .paths import sqlite_on_disk_size_bytes

logger = logging.getLogger("topos.storage.db.storage_breakdown")

_EMBEDDING_TABLES = frozenset({"signal_embeddings", "message_embeddings", "signal_embeddings_vec"})
_OPLOG_TABLES = frozenset({"oplog"})
_ENRICHMENT_TABLES = frozenset(
    {
        "signal_facts",
        "signal_scores",
        "signal_tags",
        "signal_summaries",
        "signal_objects",
        "signal_dimension_profiles",
        "message_emotions",
        "message_entities",
        "message_topics",
        "message_sentiment",
        "extraction_artifacts",
        "data_health_dimension",
        "user_goals",
        "relationship_edges",
    }
)
_RECORD_PREFIXES = (
    "messages",
    "ai_chat_",
    "conversation_",
    "conversations",
    "journal_",
    "contacts",
    "contact_",
    "activity_",
    "financial_",
    "location_",
    "calendar_",
    "browser_",
    "raw_chat_messages_",
    "messenger_",
    "profile_",
    "wiki_canonical",
    "ingest_",
    "ingestion_",
    "canonical_",
    "home_chat_",
    "browserhistory_",
    "llm_",
)

_CATEGORY_LABELS: Dict[str, str] = {
    "raw_files": "Raw files",
    "vector_embeddings": "Vector embeddings",
    "messages_and_records": "Messages & records",
    "oplog": "Oplog",
    "enrichment": "Enrichment & signals",
    "system_and_other": "System & other",
}

_CATEGORY_ORDER = (
    "raw_files",
    "vector_embeddings",
    "messages_and_records",
    "oplog",
    "enrichment",
    "system_and_other",
)


def _raw_ingestion_paths() -> list[Path]:
    paths: list[Path] = []
    env_override = os.getenv("TOPOS_INGESTION_BASE_PATH")
    if env_override:
        paths.append(Path(env_override))
    paths.append(Path.home() / ".topos" / "ingestion")
    paths.append(Path.home() / ".topos_engine" / "ingestion")

    seen: set[str] = set()
    unique: list[Path] = []
    for path in paths:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _directory_size_bytes(root: Path) -> int:
    if not root.exists() or not root.is_dir():
        return 0
    total = 0
    try:
        for path in root.rglob("*"):
            if not path.is_file() or path.name.endswith(".backup"):
                continue
            try:
                total += path.stat().st_size
            except OSError as exc:
                logger.debug("Failed to stat raw file %s: %s", path, exc)
    except OSError as exc:
        logger.debug("Failed to walk raw directory %s: %s", root, exc)
    return total


def raw_ingestion_size_bytes() -> int:
    total = 0
    seen_roots: set[str] = set()
    for root in _raw_ingestion_paths():
        if not root.exists():
            continue
        key = str(root.resolve())
        if key in seen_roots:
            continue
        seen_roots.add(key)
        total += _directory_size_bytes(root)
    return total


def _index_target_table(index_name: str, known_tables: set[str]) -> str:
    if index_name in known_tables:
        return index_name
    if index_name.startswith("sqlite_autoindex_"):
        body = index_name[len("sqlite_autoindex_") :]
        for table in sorted(known_tables, key=len, reverse=True):
            if body.startswith(table):
                return table
    if index_name.startswith("idx_"):
        body = index_name[len("idx_") :]
        for table in sorted(known_tables, key=len, reverse=True):
            if body.startswith(f"{table}_") or body == table:
                return table
    return index_name


def _source_flat_table_ids() -> frozenset[str]:
    try:
        from ...sources.registry import REGISTRY
    except ImportError:
        return frozenset()
    table_ids: set[str] = set()
    for defn in REGISTRY.values():
        if not bool(getattr(defn, "pipeline_include_data_table", False)):
            continue
        tables = getattr(defn, "tables", None)
        if not isinstance(tables, list):
            continue
        for table in tables:
            if isinstance(table, dict):
                table_id = str(table.get("table_id") or "").strip()
                if table_id:
                    table_ids.add(table_id)
    return frozenset(table_ids)


def _classify_table(table_name: str) -> str:
    if table_name in _source_flat_table_ids():
        return "messages_and_records"
    if table_name in _EMBEDDING_TABLES:
        return "vector_embeddings"
    if table_name in _OPLOG_TABLES:
        return "oplog"
    if table_name in _ENRICHMENT_TABLES:
        return "enrichment"
    for prefix in _RECORD_PREFIXES:
        if table_name == prefix or table_name.startswith(prefix):
            return "messages_and_records"
    return "system_and_other"


def _sqlite_dbstat_bytes_by_category(conn: sqlite3.Connection) -> Dict[str, int]:
    try:
        rows = conn.execute("SELECT name, pgsize FROM dbstat").fetchall()
    except sqlite3.Error as exc:
        logger.debug("dbstat unavailable for storage breakdown: %s", exc)
        return {}

    known_tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    totals: Dict[str, int] = {category: 0 for category in _CATEGORY_ORDER if category != "raw_files"}
    for name, page_size in rows:
        table_name = _index_target_table(str(name), known_tables)
        category = _classify_table(table_name)
        totals[category] = totals.get(category, 0) + int(page_size or 0)
    return totals


def _scale_category_bytes(category_bytes: Dict[str, int], target_total: int) -> Dict[str, int]:
    current_total = sum(category_bytes.values())
    if current_total <= 0 or target_total <= 0:
        return category_bytes
    scale = target_total / current_total
    scaled = {key: int(value * scale) for key, value in category_bytes.items()}
    remainder = target_total - sum(scaled.values())
    if remainder > 0:
        largest = max(scaled, key=scaled.get)
        scaled[largest] += remainder
    return scaled


def _build_categories(raw_bytes: int, sqlite_categories: Dict[str, int]) -> list[Dict[str, Any]]:
    combined: Dict[str, int] = {"raw_files": raw_bytes, **sqlite_categories}
    total_bytes = sum(combined.values())
    if total_bytes <= 0:
        return []

    categories: list[Dict[str, Any]] = []
    running_percent = 0.0
    non_zero = [
        category_id
        for category_id in _CATEGORY_ORDER
        if combined.get(category_id, 0) > 0
    ]
    for index, category_id in enumerate(non_zero):
        bytes_value = int(combined.get(category_id, 0))
        if index == len(non_zero) - 1:
            percent = round(max(0.0, 100.0 - running_percent), 1)
        else:
            percent = round((bytes_value / total_bytes) * 100.0, 1)
            running_percent += percent
        categories.append(
            {
                "id": category_id,
                "label": _CATEGORY_LABELS[category_id],
                "bytes": bytes_value,
                "percent": percent,
            }
        )
    return categories


def compute_local_storage_breakdown(
    conn: sqlite3.Connection,
    db_path: Path,
) -> Optional[Dict[str, Any]]:
    sqlite_bytes = sqlite_on_disk_size_bytes(db_path)
    if sqlite_bytes is None:
        return None

    sqlite_categories = _sqlite_dbstat_bytes_by_category(conn)
    if sqlite_categories:
        sqlite_categories = _scale_category_bytes(sqlite_categories, sqlite_bytes)

    raw_bytes = raw_ingestion_size_bytes()
    categories = _build_categories(raw_bytes, sqlite_categories)
    total_bytes = sqlite_bytes + raw_bytes
    return {
        "total_bytes": total_bytes,
        "sqlite_bytes": sqlite_bytes,
        "raw_files_bytes": raw_bytes,
        "categories": categories,
    }
