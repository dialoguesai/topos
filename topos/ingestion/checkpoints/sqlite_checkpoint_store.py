"""SQLite-backed checkpoint store for ingestion (e.g. iMessage/Signal sync)."""

from __future__ import annotations

import json
import logging
from typing import Optional

from .checkpoint_store import CheckpointStore, IngestionCheckpoint
from ...storage.db.write_gate import commit_connection, with_db_write

logger = logging.getLogger("topos.ingestion.checkpoints.sqlite")

TABLE = "ingestion_checkpoints"


def ensure_table(conn) -> None:
    """Create ingestion_checkpoints table if not exists."""
    # DDL takes SQLite's write lock at execute time — gate it with the commit.
    with with_db_write():
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS {TABLE} (
                dataset_id TEXT NOT NULL,
                schema_id TEXT NOT NULL,
                last_record_id TEXT NOT NULL,
                metadata_json TEXT,
                updated_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (dataset_id, schema_id)
            )
        """)
        commit_connection(conn)


class SqliteCheckpointStore(CheckpointStore):
    """Persist checkpoints to SQLite (same DB as app)."""

    def __init__(self, conn) -> None:
        self.conn = conn
        if conn:
            ensure_table(conn)

    def get_checkpoint(self, dataset_id: str, schema_id: str) -> Optional[IngestionCheckpoint]:
        if not self.conn:
            return None
        try:
            ensure_table(self.conn)
            row = self.conn.execute(
                f"SELECT last_record_id, metadata_json FROM {TABLE} WHERE dataset_id = ? AND schema_id = ?",
                (dataset_id, schema_id),
            ).fetchone()
            if not row:
                return None
            last_record_id, metadata_json = row
            metadata = {}
            if metadata_json:
                try:
                    metadata = json.loads(metadata_json)
                except (TypeError, json.JSONDecodeError):
                    pass
            return IngestionCheckpoint(
                dataset_id=dataset_id,
                schema_id=schema_id,
                last_record_id=last_record_id or "0",
                metadata=metadata,
            )
        except Exception as e:
            logger.warning("get_checkpoint failed: %s", e)
            return None

    def save_checkpoint(self, checkpoint: IngestionCheckpoint) -> None:
        if not self.conn:
            return
        try:
            ensure_table(self.conn)
            metadata_json = json.dumps(checkpoint.metadata, ensure_ascii=False) if checkpoint.metadata else None
            with with_db_write():
                self.conn.execute(
                    f"""
                    INSERT OR REPLACE INTO {TABLE} (dataset_id, schema_id, last_record_id, metadata_json, updated_at)
                    VALUES (?, ?, ?, ?, datetime('now'))
                    """,
                    (checkpoint.dataset_id, checkpoint.schema_id, checkpoint.last_record_id, metadata_json),
                )
                commit_connection(self.conn)
        except Exception as e:
            logger.warning("save_checkpoint failed: %s", e)
