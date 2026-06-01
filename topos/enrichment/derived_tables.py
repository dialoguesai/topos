"""Derived tables manager for enrichment data storage."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..utils.base_object import BaseObject

logger = logging.getLogger("topos.enrichment.derived_tables")


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
        
        # If still no connection, try to create one
        if self.conn is None:
            try:
                from ..storage.db.paths import get_database_path
                from ..config.settings import settings
                
                db_path = get_database_path(settings.topos_database_path)
                if db_path.exists() or db_path.parent.exists():
                    self.conn = sqlite3.connect(str(db_path))
                    logger.debug("%s: Created database connection: %s", self, db_path)
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
            
            self.conn.commit()
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
        elif table_name == "message_topics":
            return self._write_topics_batch(enrichment_records, batch_size)
        elif table_name == "message_sentiment":
            return self._write_sentiment_batch(enrichment_records, batch_size)
        elif table_name == "message_embeddings":
            return self._write_embeddings_batch(enrichment_records, batch_size)
        else:
            logger.warning("%s: Unknown derived table: %s", self, table_name)
            return 0

    def _write_emotions_batch(
        self,
        records: List[Dict[str, Any]],
        batch_size: int,
    ) -> int:
        """Write emotion records to message_emotions table."""
        written = 0
        try:
            self._ensure_tables()
            extracted_at = datetime.now(timezone.utc).isoformat()
            
            for i in range(0, len(records), batch_size):
                batch = records[i:i + batch_size]
                
                values = []
                for record in batch:
                    import json
                    all_emotions_val = record.get("all_emotions_json") or record.get("all_emotions") or []
                    all_emotions_str = json.dumps(all_emotions_val) if isinstance(all_emotions_val, list) else (all_emotions_val if isinstance(all_emotions_val, str) else "[]")
                    values.append((
                        record.get("message_id"),
                        record.get("source_id"),
                        record.get("emotion_label"),
                        record.get("confidence"),
                        record.get("model_name") or record.get("model"),
                        all_emotions_str,
                        extracted_at,
                    ))
                
                self.conn.executemany("""
                    INSERT OR REPLACE INTO message_emotions (
                        message_id, source_id, emotion_label, confidence, model_name, all_emotions_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """, values)
                
                self.conn.commit()
                written += len(batch)
                logger.debug(
                    "[PIPELINE:ENRICHMENT] %s: Wrote batch of %d emotion records (total: %d)",
                    self,
                    len(batch),
                    written,
                )
        except Exception as e:
            if self.conn:
                self.conn.rollback()
            logger.error("[PIPELINE:ENRICHMENT] %s: Failed to write emotions batch: %s", self, e)
            raise
        
        return written

    def _write_topics_batch(
        self,
        records: List[Dict[str, Any]],
        batch_size: int,
    ) -> int:
        """Write topics records (stub)."""
        logger.debug("%s: Topics batch write not yet implemented", self)
        return 0

    def _write_sentiment_batch(
        self,
        records: List[Dict[str, Any]],
        batch_size: int,
    ) -> int:
        """Write sentiment records (stub)."""
        logger.debug("%s: Sentiment batch write not yet implemented", self)
        return 0

    def _write_embeddings_batch(
        self,
        records: List[Dict[str, Any]],
        batch_size: int,
    ) -> int:
        """Write embeddings records (stub)."""
        logger.debug("%s: Embeddings batch write not yet implemented", self)
        return 0
