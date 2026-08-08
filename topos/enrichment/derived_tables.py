"""Derived tables manager for enrichment data storage."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ..storage.db.write_gate import batched_writes, commit_connection, with_db_write
from ..utils.base_object import BaseObject

logger = logging.getLogger("topos.enrichment.derived_tables")

#: Connections whose enrichment DDL has already been applied, by ``id()``.
#: Bounded by the number of live connections (one per thread), not by records.
#: ``reset_ensured_tables_cache()`` exists for tests that rebuild a schema
#: underneath a reused connection id.
_ENSURED_CONNECTIONS: set[int] = set()


def reset_ensured_tables_cache() -> None:
    """Forget which connections have had enrichment DDL applied."""
    _ENSURED_CONNECTIONS.clear()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row[1]) for row in rows}
    except sqlite3.OperationalError:
        return set()


def _insert_matching_columns(
    conn: sqlite3.Connection,
    table: str,
    cols: set[str],
    values: Dict[str, Any],
) -> None:
    col_names = [k for k in values if k in cols]
    if not col_names:
        return
    placeholders = ", ".join("?" for _ in col_names)
    conn.execute(
        f"INSERT OR REPLACE INTO {table} ({', '.join(col_names)}) VALUES ({placeholders})",
        tuple(values[k] for k in col_names),
    )


def _record_spec_version(record: Dict[str, Any], job_id: str = "") -> int:
    raw = record.get("spec_version")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    from .models.mvp_defaults import job_spec_version

    return job_spec_version(job_id or str(record.get("job_id") or ""))


class DerivedTablesManager(BaseObject):
    """Manages derived tables for enrichment data."""

    def __init__(self, conn: Optional[sqlite3.Connection] = None, *, name: Optional[str] = None):
        """Initialize with optional database connection.
        
        Args:
            conn: SQLite connection. If None, will try to get from state or create new.
            name: Optional custom name. Defaults to `ClassName#N`
        """
        super().__init__(name=name)
        self.conn = conn
        if self.conn is None:
            # Try to get connection from state
            try:
                from ..core.state import db_conn
                self.conn = db_conn
            except Exception:
                pass
        
        # If still no connection, reuse the process singleton (tuned + write-gated).
        if self.conn is None:
            try:
                from ..core.state import get_db_connection

                self.conn = get_db_connection()
                if self.conn is not None:
                    logger.debug("%s: Using shared database connection", self)
            except Exception as e:
                logger.warning("%s: Could not create database connection: %s", self, e)
                self.conn = None
        
        # Ensure tables exist
        if self.conn:
            self._ensure_tables()

    def _ensure_tables(self) -> None:
        """Ensure enrichment tables exist.

        DDL is idempotent in SQL but not free: it takes a lock, and this class
        is constructed PER RECORD on the direct-ingest path. One 88-record
        GitHub import produced `DerivedTablesManager#121` through `#196`, each
        re-issuing the same CREATE statements inside the ingest loop while the
        pipeline and the graph refresher were competing for the same database.

        So remember, per connection, that the DDL has already run. Keyed by
        connection identity — a different connection (per-thread, or a test's
        in-memory handle) must still run it.
        """
        if not self.conn:
            return
        conn_key = id(self.conn)
        if conn_key in _ENSURED_CONNECTIONS:
            return

        try:
            with with_db_write():
                self._apply_enrichment_ddl()
                commit_connection(self.conn)
            _ENSURED_CONNECTIONS.add(conn_key)
            logger.debug("%s: Ensured message_emotions table exists", self)
        except Exception as e:
            # Not recorded as ensured — a later instance retries.
            logger.error("%s: Failed to ensure enrichment tables: %s", self, e)
            if self.conn:
                self.conn.rollback()

    def _apply_enrichment_ddl(self) -> None:
        # Create message_emotions table (Stage 9: model_name, all_emotions_json)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS message_emotions (
                message_id TEXT NOT NULL,
                source_id TEXT,
                emotion_label TEXT,
                confidence REAL,
                model_name TEXT,
                all_emotions_json TEXT,
                role TEXT,
                created_at TEXT NOT NULL,
                PRIMARY KEY (message_id, model_name)
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS message_embeddings (
                embedding_id TEXT PRIMARY KEY,
                record_id TEXT NOT NULL,
                message_id TEXT,
                source_id TEXT,
                model TEXT,
                provider TEXT,
                dims INTEGER,
                vector_json TEXT,
                payload_json TEXT
            )
        """)

        # Additive columns for existing tables (idempotent)
        for alter_sql in (
            "ALTER TABLE message_emotions ADD COLUMN source_id TEXT",
            "ALTER TABLE message_emotions ADD COLUMN role TEXT",
        ):
            try:
                self.conn.execute(alter_sql)
            except sqlite3.OperationalError:
                pass

        emo_cols = {
            row[1] for row in self.conn.execute("PRAGMA table_info(message_emotions)").fetchall()
        }
        if "message_id" in emo_cols:
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_message_emotions_message
                ON message_emotions(message_id)
            """)
        if "emotion_label" in emo_cols:
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_message_emotions_label
                ON message_emotions(emotion_label)
            """)
        if "source_id" in emo_cols:
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_message_emotions_source
                ON message_emotions(source_id)
            """)
        if "role" in emo_cols:
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_message_emotions_role
                ON message_emotions(role) WHERE role IS NOT NULL
            """)

    def write_enrichment_batch(
        self,
        enrichment_records: List[Dict[str, Any]],
        table_name: str,
        batch_size: int = 1000,
    ) -> int:
        """Write enrichment records to derived table in batches.
        
        Args:
            enrichment_records: List of enrichment record dicts
            table_name: Derived table name (e.g., 'message_emotions', 'message_sentiment')
            batch_size: Number of records per batch
            
        Returns:
            Number of records written
        """
        if not enrichment_records:
            return 0
        
        if not self.conn:
            logger.warning("%s: No database connection available, skipping storage of %d records", self, len(enrichment_records))
            return 0
        
        # Determine table schema based on table_name
        if table_name == "message_emotions":
            return self._write_emotions_batch(enrichment_records, batch_size)
        elif table_name == "message_entities":
            return self._write_entities_batch(enrichment_records, batch_size)
        elif table_name == "message_topics":
            return self._write_topics_batch(enrichment_records, batch_size)
        elif table_name == "user_goals":
            return self._write_goals_batch(enrichment_records, batch_size)
        elif table_name == "message_sentiment":
            return self._write_sentiment_batch(enrichment_records, batch_size)
        elif table_name == "message_embeddings":
            logger.warning(
                "message_embeddings writes are deprecated; use signal_embeddings via write_signal_records"
            )
            return 0
        elif table_name == "browser_url_classification":
            return self._write_url_classification_batch(enrichment_records, batch_size)
        else:
            logger.warning("%s: Unknown derived table: %s", self, table_name)
            return 0

    def _write_emotions_batch(
        self,
        records: List[Dict[str, Any]],
        batch_size: int,
    ) -> int:
        """Write emotion records to message_emotions (derived or wiki schema)."""
        written = 0
        if not self.conn:
            return 0
        try:
            self._ensure_tables()
            extracted_at = datetime.now(timezone.utc).isoformat()
            cols = _table_columns(self.conn, "message_emotions")
            use_derived = "message_id" in cols and "model_name" in cols
            use_wiki = "emotion_id" in cols and not use_derived

            if not use_derived and not use_wiki:
                logger.warning("%s: message_emotions schema not recognized", self)
                return 0
            for i in range(0, len(records), batch_size):
                batch = records[i:i + batch_size]
                # Hold the gate for the whole batch: each INSERT takes SQLite's
                # write lock at execute time, so writes and commit must share
                # one hold (write_gate lock-order inversion).
                with batched_writes(self.conn):
                    if use_derived:
                        for record in batch:
                            import json

                            all_emotions_val = record.get("all_emotions_json") or record.get("all_emotions") or []
                            all_emotions_str = (
                                json.dumps(all_emotions_val)
                                if isinstance(all_emotions_val, list)
                                else (all_emotions_val if isinstance(all_emotions_val, str) else "[]")
                            )
                            row = {
                                "message_id": record.get("message_id"),
                                "source_id": record.get("source_id"),
                                "emotion_label": record.get("emotion_label"),
                                "confidence": record.get("confidence"),
                                "model_name": record.get("model_name") or record.get("model"),
                                "all_emotions_json": all_emotions_str,
                                "role": record.get("role"),
                                "created_at": extracted_at,
                                "spec_version": _record_spec_version(record, "emo_27"),
                            }
                            _insert_matching_columns(self.conn, "message_emotions", cols, row)
                    else:
                        import json
                        import uuid

                        for record in batch:
                            message_id = record.get("message_id") or record.get("record_id")
                            emotion_id = str(record.get("emotion_id") or uuid.uuid4())
                            payload = json.dumps({**record, "message_id": message_id})
                            row = {
                                "emotion_id": emotion_id,
                                "record_id": message_id,
                                "source_id": record.get("source_id"),
                                "model": record.get("model_name") or record.get("model"),
                                "provider": record.get("provider"),
                                "payload_json": payload,
                                "spec_version": _record_spec_version(record, "emo_27"),
                            }
                            _insert_matching_columns(self.conn, "message_emotions", cols, row)
                written += len(batch)
        except Exception as e:
            if self.conn:
                self.conn.rollback()
            logger.error("[PIPELINE:ENRICHMENT] %s: Failed to write emotions batch: %s", self, e)
            raise

        return written

    def _write_entities_batch(
        self,
        records: List[Dict[str, Any]],
        batch_size: int,
    ) -> int:
        written = 0
        if not self.conn:
            return 0
        import json
        import uuid

        cols = _table_columns(self.conn, "message_entities")
        if not cols:
            return 0
        extracted_at = datetime.now(timezone.utc).isoformat()
        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]
            with batched_writes(self.conn):
                for record in batch:
                    record_id = record.get("record_id") or record.get("message_id")
                    entity_text = record.get("entity_text") or record.get("text")
                    if not record_id or not entity_text:
                        continue
                    entity_id = str(record.get("entity_id") or uuid.uuid4())
                    payload = json.dumps({**record, "record_id": record_id, "entity_id": entity_id})
                    if "payload_json" in cols:
                        _insert_matching_columns(
                            self.conn,
                            "message_entities",
                            cols,
                            {
                                "entity_id": entity_id,
                                "record_id": record_id,
                                "source_id": record.get("source_id"),
                                "entity_text": entity_text,
                                "model": record.get("model"),
                                "provider": record.get("provider"),
                                "payload_json": payload,
                                "spec_version": _record_spec_version(record, "entities"),
                            },
                        )
                    if "message_id" in cols:
                        try:
                            self.conn.execute(
                                "UPDATE message_entities SET message_id=? WHERE entity_id=?",
                                (record_id, entity_id),
                            )
                        except sqlite3.OperationalError:
                            pass
                    written += 1
        return written

    def _write_goals_batch(
        self,
        records: List[Dict[str, Any]],
        batch_size: int,
    ) -> int:
        if not self.conn:
            return 0
        import uuid

        cols = _table_columns(self.conn, "user_goals")
        if not cols:
            return 0
        written = 0
        for i in range(0, len(records), batch_size):
            batch = records[i:i + batch_size]
            with batched_writes(self.conn):
                for record in batch:
                    record_id = record.get("record_id") or record.get("message_id")
                    goal_text = record.get("goal_text") or record.get("text")
                    if not goal_text:
                        continue
                    goal_id = str(record.get("goal_id") or uuid.uuid4())
                    _insert_matching_columns(
                        self.conn,
                        "user_goals",
                        cols,
                        {
                            "goal_id": goal_id,
                            "record_id": record_id,
                            "source_id": record.get("source_id"),
                            "goal_text": goal_text,
                            "model": record.get("model"),
                            "provider": record.get("provider"),
                            "payload_json": json.dumps({**record, "goal_id": goal_id}),
                            "spec_version": _record_spec_version(record, "goal_extraction"),
                        },
                    )
                    written += 1
        return written

    def _write_batch_by_columns(
        self,
        table: str,
        records: List[Dict[str, Any]],
        batch_size: int,
        build_values: Callable[[Dict[str, Any], set[str]], Optional[Dict[str, Any]]],
    ) -> int:
        if not self.conn:
            return 0
        cols = _table_columns(self.conn, table)
        if not cols:
            return 0
        written = 0
        for i in range(0, len(records), batch_size):
            batch = records[i : i + batch_size]
            with batched_writes(self.conn):
                for record in batch:
                    values = build_values(record, cols)
                    if values is None:
                        continue
                    _insert_matching_columns(self.conn, table, cols, values)
                    written += 1
        return written

    def _write_topics_batch(
        self,
        records: List[Dict[str, Any]],
        batch_size: int,
    ) -> int:
        import uuid

        def build_values(record: Dict[str, Any], cols: set[str]) -> Optional[Dict[str, Any]]:
            topic = record.get("topic") or record.get("label")
            if not topic:
                return None
            record_id = record.get("record_id") or record.get("message_id")
            topic_id = str(record.get("topic_id") or uuid.uuid4())
            values = {
                "topic_id": topic_id,
                "record_id": record_id,
                "source_id": record.get("source_id"),
                "topic": topic,
                "model": record.get("model"),
                "provider": record.get("provider"),
                "payload_json": json.dumps({**record, "topic_id": topic_id}),
                "spec_version": _record_spec_version(record, "topics"),
            }
            if "message_id" in cols:
                values["message_id"] = record.get("message_id") or record_id
            return values

        return self._write_batch_by_columns("message_topics", records, batch_size, build_values)

    def _write_sentiment_batch(
        self,
        records: List[Dict[str, Any]],
        batch_size: int,
    ) -> int:
        import uuid

        def build_values(record: Dict[str, Any], cols: set[str]) -> Optional[Dict[str, Any]]:
            label = record.get("label") or record.get("sentiment")
            if label is None and record.get("score") is None:
                return None
            record_id = record.get("record_id") or record.get("message_id")
            sentiment_id = str(record.get("sentiment_id") or uuid.uuid4())
            values = {
                "sentiment_id": sentiment_id,
                "record_id": record_id,
                "source_id": record.get("source_id"),
                "label": label,
                "score": record.get("score"),
                "model": record.get("model"),
                "provider": record.get("provider"),
                "payload_json": json.dumps({**record, "sentiment_id": sentiment_id}),
                "spec_version": _record_spec_version(record, "sentiment"),
            }
            if "message_id" in cols:
                values["message_id"] = record.get("message_id") or record_id
            return values

        return self._write_batch_by_columns("message_sentiment", records, batch_size, build_values)

    def _write_embeddings_batch(
        self,
        records: List[Dict[str, Any]],
        batch_size: int,
    ) -> int:
        import uuid

        def build_values(record: Dict[str, Any], cols: set[str]) -> Optional[Dict[str, Any]]:
            record_id = record.get("record_id") or record.get("message_id")
            if not record_id:
                return None
            embedding_id = str(record.get("embedding_id") or uuid.uuid4())
            vector = record.get("vector")
            values = {
                "embedding_id": embedding_id,
                "record_id": record_id,
                "source_id": record.get("source_id"),
                "model": record.get("model"),
                "provider": record.get("provider"),
                "dims": record.get("dims") or (len(vector) if isinstance(vector, list) else None),
                "vector_json": json.dumps(vector) if vector is not None else None,
                "payload_json": json.dumps({**record, "embedding_id": embedding_id}),
                "spec_version": _record_spec_version(record, "embeddings"),
            }
            if "message_id" in cols:
                values["message_id"] = record.get("message_id") or record_id
            return values

        return self._write_batch_by_columns("message_embeddings", records, batch_size, build_values)

    def _write_url_classification_batch(
        self,
        records: List[Dict[str, Any]],
        batch_size: int,
    ) -> int:
        """Write URL classification enrichment rows keyed by canonical activity event_id."""
        if not self.conn:
            return 0
        from ..storage.raw.browser_flat_tables import ensure_browser_url_classification_table

        written = 0
        try:
            ensure_browser_url_classification_table(self.conn)
            for i in range(0, len(records), batch_size):
                batch = records[i : i + batch_size]
                values = []
                cols = _table_columns(self.conn, "browser_url_classification")
                with batched_writes(self.conn):
                    for record in batch:
                        record_id = record.get("record_id") or record.get("event_id")
                        if not record_id:
                            continue
                        row = {
                            "enriched_from_table": record.get("enriched_from_table") or "activity_events",
                            "record_id": record_id,
                            "dataset_id": record.get("dataset_id"),
                            "url": record.get("url"),
                            "title": record.get("title"),
                            "url_category": record.get("url_category") or record.get("category"),
                            "url_confidence": (
                                record.get("url_confidence")
                                if record.get("url_confidence") is not None
                                else record.get("confidence")
                            ),
                            "model_name": record.get("model_name") or record.get("model"),
                            "updated_at": "datetime('now')",
                            "spec_version": _record_spec_version(record, "url_classification"),
                        }
                        # updated_at uses SQL datetime — keep prior executemany path for that col
                        if "spec_version" in cols:
                            self.conn.execute(
                                """
                                INSERT OR REPLACE INTO browser_url_classification
                                (enriched_from_table, record_id, dataset_id, url, title,
                                 url_category, url_confidence, model_name, updated_at, spec_version)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
                                """,
                                (
                                    row["enriched_from_table"],
                                    row["record_id"],
                                    row["dataset_id"],
                                    row["url"],
                                    row["title"],
                                    row["url_category"],
                                    row["url_confidence"],
                                    row["model_name"],
                                    row["spec_version"],
                                ),
                            )
                        else:
                            self.conn.execute(
                                """
                                INSERT OR REPLACE INTO browser_url_classification
                                (enriched_from_table, record_id, dataset_id, url, title,
                                 url_category, url_confidence, model_name, updated_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                                """,
                                (
                                    row["enriched_from_table"],
                                    row["record_id"],
                                    row["dataset_id"],
                                    row["url"],
                                    row["title"],
                                    row["url_category"],
                                    row["url_confidence"],
                                    row["model_name"],
                                ),
                            )
                        values.append(1)
                written += len(values)
        except Exception as exc:
            logger.error("%s: Failed to write browser_url_classification batch: %s", self, exc)
            if self.conn:
                self.conn.rollback()
        return written
