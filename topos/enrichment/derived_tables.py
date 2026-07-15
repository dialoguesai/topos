"""Derived tables manager for enrichment data storage."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ..storage.db.write_gate import commit_connection
from ..utils.base_object import BaseObject

logger = logging.getLogger("topos.enrichment.derived_tables")


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
        """Ensure enrichment tables exist."""
        if not self.conn:
            return
        
        try:
            # Create message_emotions table (Stage 9: model_name, all_emotions_json)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS message_emotions (
                    message_id TEXT NOT NULL,
                    source_id TEXT,
                    emotion_label TEXT,
                    confidence REAL,
                    model_name TEXT,
                    all_emotions_json TEXT,
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
            
            # Add source_id column if it doesn't exist (migration for existing tables)
            try:
                self.conn.execute("ALTER TABLE message_emotions ADD COLUMN source_id TEXT")
            except sqlite3.OperationalError:
                # Column already exists, ignore
                pass
            
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_message_emotions_message 
                ON message_emotions(message_id)
            """)
            
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_message_emotions_label 
                ON message_emotions(emotion_label)
            """)
            
            self.conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_message_emotions_source 
                ON message_emotions(source_id)
            """)
            
            commit_connection(self.conn)
            logger.debug("%s: Ensured message_emotions table exists", self)
        except Exception as e:
            logger.error("%s: Failed to ensure enrichment tables: %s", self, e)
            if self.conn:
                self.conn.rollback()

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

            for i in range(0, len(records), batch_size):
                batch = records[i:i + batch_size]
                if use_derived:
                    values = []
                    for record in batch:
                        import json

                        all_emotions_val = record.get("all_emotions_json") or record.get("all_emotions") or []
                        all_emotions_str = (
                            json.dumps(all_emotions_val)
                            if isinstance(all_emotions_val, list)
                            else (all_emotions_val if isinstance(all_emotions_val, str) else "[]")
                        )
                        values.append(
                            (
                                record.get("message_id"),
                                record.get("source_id"),
                                record.get("emotion_label"),
                                record.get("confidence"),
                                record.get("model_name") or record.get("model"),
                                all_emotions_str,
                                extracted_at,
                            )
                        )
                    self.conn.executemany(
                        """
                        INSERT OR REPLACE INTO message_emotions (
                            message_id, source_id, emotion_label, confidence,
                            model_name, all_emotions_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        values,
                    )
                elif use_wiki:
                    import json
                    import uuid

                    for record in batch:
                        message_id = record.get("message_id") or record.get("record_id")
                        emotion_id = str(record.get("emotion_id") or uuid.uuid4())
                        payload = json.dumps({**record, "message_id": message_id})
                        self.conn.execute(
                            """
                            INSERT OR REPLACE INTO message_emotions (
                                emotion_id, record_id, source_id, model, provider, payload_json
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                emotion_id,
                                message_id,
                                record.get("source_id"),
                                record.get("model_name") or record.get("model"),
                                record.get("provider"),
                                payload,
                            ),
                        )
                else:
                    logger.warning("%s: message_emotions schema not recognized", self)
                    return 0

                commit_connection(self.conn)
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
            for record in batch:
                record_id = record.get("record_id") or record.get("message_id")
                entity_text = record.get("entity_text") or record.get("text")
                if not record_id or not entity_text:
                    continue
                entity_id = str(record.get("entity_id") or uuid.uuid4())
                payload = json.dumps({**record, "record_id": record_id, "entity_id": entity_id})
                if "payload_json" in cols:
                    self.conn.execute(
                        """
                        INSERT OR REPLACE INTO message_entities (
                            entity_id, record_id, source_id, entity_text, model, provider, payload_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            entity_id,
                            record_id,
                            record.get("source_id"),
                            entity_text,
                            record.get("model"),
                            record.get("provider"),
                            payload,
                        ),
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
            commit_connection(self.conn)
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
            for record in batch:
                record_id = record.get("record_id") or record.get("message_id")
                goal_text = record.get("goal_text") or record.get("text")
                if not goal_text:
                    continue
                goal_id = str(record.get("goal_id") or uuid.uuid4())
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO user_goals (
                        goal_id, record_id, source_id, goal_text, model, provider, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        goal_id,
                        record_id,
                        record.get("source_id"),
                        goal_text,
                        record.get("model"),
                        record.get("provider"),
                        json.dumps({**record, "goal_id": goal_id}),
                    ),
                )
                written += 1
            commit_connection(self.conn)
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
            for record in batch:
                values = build_values(record, cols)
                if values is None:
                    continue
                _insert_matching_columns(self.conn, table, cols, values)
                written += 1
            commit_connection(self.conn)
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
                for record in batch:
                    record_id = record.get("record_id") or record.get("event_id")
                    if not record_id:
                        continue
                    values.append(
                        (
                            record.get("enriched_from_table") or "activity_events",
                            record_id,
                            record.get("dataset_id"),
                            record.get("url"),
                            record.get("title"),
                            record.get("url_category") or record.get("category"),
                            record.get("url_confidence") if record.get("url_confidence") is not None else record.get("confidence"),
                            record.get("model_name") or record.get("model"),
                        )
                    )
                if not values:
                    continue
                self.conn.executemany(
                    """
                    INSERT OR REPLACE INTO browser_url_classification
                    (enriched_from_table, record_id, dataset_id, url, title, url_category, url_confidence, model_name, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                    """,
                    values,
                )
                commit_connection(self.conn)
                written += len(values)
        except Exception as exc:
            logger.error("%s: Failed to write browser_url_classification batch: %s", self, exc)
            if self.conn:
                self.conn.rollback()
        return written
