"""Raw tables manager for storing original payloads before canonicalization."""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Dict, Optional

logger = logging.getLogger("topos.storage.raw.raw_tables_manager")


class RawTablesManager:
    """Manages raw retention tables for storing original payloads.
    
    According to architecture, raw tables are per-connector:
    - `raw_chat_messages_{source}` for chat sources
    - `raw_{source}_events` for event sources
    """
    
    def __init__(self, conn: sqlite3.Connection):
        """Initialize with database connection."""
        self.conn = conn
    
    def get_raw_table_name(self, source_id: str, source_type: str = "chat_messages") -> str:
        """Get raw table name for a source.
        
        Args:
            source_id: Source identifier (e.g., "chatgpt", "chatgpt_ui_conversation")
            source_type: Type of data ("chat_messages", "events", etc.)
            
        Returns:
            Table name like "raw_chat_messages_chatgpt"
        """
        # Extract base source name (remove prefixes like "dev_test_")
        if source_id in ("browser_visits", "browser_events", "starred_websites"):
            base_source = source_id.replace("_", "")
        else:
            base_source = source_id
            if "_" in source_id:
                # For "chatgpt_ui_conversation", extract "chatgpt"
                parts = source_id.split("_")
                # Find the actual source name (usually after prefixes)
                for part in parts:
                    if part in ["chatgpt", "grok", "claude", "gemini"]:
                        base_source = part
                        break
                # If no known source found, use the last meaningful part
                if base_source == source_id:
                    # For "chatgpt_ui_conversation", use "chatgpt_ui_conversation"
                    # but normalize to just the source type
                    if "chatgpt" in source_id.lower():
                        base_source = "chatgpt"
                    elif "grok" in source_id.lower():
                        base_source = "grok"
                    else:
                        # Fallback: use a sanitized version
                        base_source = source_id.replace("dev_test_", "").replace("_", "")
        
        if source_type == "chat_messages":
            return f"raw_chat_messages_{base_source}"
        else:
            return f"raw_{base_source}_{source_type}"
    
    def ensure_raw_table(self, table_name: str) -> None:
        """Ensure raw table exists with proper schema.
        
        Raw tables store original payloads verbatim with:
        - source_system: Source identifier
        - source_record_id: Unique record ID within source
        - payload_json: Original payload as JSON string
        - created_at: Timestamp when record was stored
        - Uniqueness: (source_system, source_record_id)
        """
        try:
            if table_name == "raw_chat_messages_browservisits":
                self._ensure_browser_visits_raw_table(table_name)
                return
            self.conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {table_name} (
                    source_system TEXT NOT NULL,
                    source_record_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (source_system, source_record_id)
                )
            """)
            
            # Create indexes
            self.conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{table_name}_source_system 
                ON {table_name}(source_system)
            """)
            
            self.conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{table_name}_created_at 
                ON {table_name}(created_at)
            """)
            
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            logger.error("Failed to ensure raw table %s: %s", table_name, e)
            raise

    def _ensure_browser_visits_raw_table(self, table_name: str) -> None:
        """Ensure browser visits raw table uses normalized columns (no payload_json)."""
        cursor = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        table_exists = cursor.fetchone() is not None

        def _create_schema(target_name: str) -> None:
            self.conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {target_name} (
                    source_system TEXT NOT NULL,
                    source_record_id TEXT NOT NULL,
                    record_id TEXT,
                    dataset_id TEXT,
                    url TEXT,
                    visited_at TEXT,
                    title TEXT,
                    favicon_url TEXT,
                    hostname TEXT,
                    device_name TEXT,
                    tab_id INTEGER,
                    window_id INTEGER,
                    incognito INTEGER,
                    transition_type TEXT,
                    pinned INTEGER,
                    audible INTEGER,
                    muted INTEGER,
                    opener_tab_id INTEGER,
                    referred_by TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    PRIMARY KEY (source_system, source_record_id)
                )
            """)

        if not table_exists:
            _create_schema(table_name)
        else:
            existing_cols_cursor = self.conn.execute(f"PRAGMA table_info({table_name})")
            existing_cols = {row[1] for row in existing_cols_cursor.fetchall()}
            needs_migration = "payload_json" in existing_cols or "url" not in existing_cols
            if needs_migration:
                pre_count = self.conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                tmp_table = f"{table_name}__migrated"
                self.conn.execute(f"DROP TABLE IF EXISTS {tmp_table}")
                _create_schema(tmp_table)
                self.conn.execute(f"""
                    INSERT OR REPLACE INTO {tmp_table} (
                        source_system, source_record_id, record_id, dataset_id, url, visited_at, title,
                        favicon_url, hostname, device_name, tab_id, window_id, incognito, transition_type,
                        pinned, audible, muted, opener_tab_id, referred_by, created_at
                    )
                    SELECT
                        source_system,
                        source_record_id,
                        COALESCE(json_extract(payload_json, '$.record_id'), source_record_id),
                        json_extract(payload_json, '$.dataset_id'),
                        json_extract(payload_json, '$.url'),
                        json_extract(payload_json, '$.visited_at'),
                        json_extract(payload_json, '$.title'),
                        json_extract(payload_json, '$.favicon_url'),
                        json_extract(payload_json, '$.hostname'),
                        json_extract(payload_json, '$.device_name'),
                        CAST(json_extract(payload_json, '$.tab_id') AS INTEGER),
                        CAST(json_extract(payload_json, '$.window_id') AS INTEGER),
                        CASE
                            WHEN json_extract(payload_json, '$.incognito') IN (1, '1', 'true', 'TRUE') THEN 1
                            WHEN json_extract(payload_json, '$.incognito') IN (0, '0', 'false', 'FALSE') THEN 0
                            ELSE NULL
                        END,
                        json_extract(payload_json, '$.transition_type'),
                        CASE
                            WHEN json_extract(payload_json, '$.pinned') IN (1, '1', 'true', 'TRUE') THEN 1
                            WHEN json_extract(payload_json, '$.pinned') IN (0, '0', 'false', 'FALSE') THEN 0
                            ELSE NULL
                        END,
                        CASE
                            WHEN json_extract(payload_json, '$.audible') IN (1, '1', 'true', 'TRUE') THEN 1
                            WHEN json_extract(payload_json, '$.audible') IN (0, '0', 'false', 'FALSE') THEN 0
                            ELSE NULL
                        END,
                        CASE
                            WHEN json_extract(payload_json, '$.muted') IN (1, '1', 'true', 'TRUE') THEN 1
                            WHEN json_extract(payload_json, '$.muted') IN (0, '0', 'false', 'FALSE') THEN 0
                            ELSE NULL
                        END,
                        CAST(json_extract(payload_json, '$.opener_tab_id') AS INTEGER),
                        json_extract(payload_json, '$.referred_by'),
                        COALESCE(created_at, datetime('now'))
                    FROM {table_name}
                """)
                self.conn.execute(f"DROP TABLE {table_name}")
                self.conn.execute(f"ALTER TABLE {tmp_table} RENAME TO {table_name}")
                post_count = self.conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0]
                logger.info(
                    "[PIPELINE:RAW] Migrated %s to normalized schema: rows_before=%d rows_after=%d",
                    table_name,
                    pre_count,
                    post_count,
                )

        self.conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{table_name}_source_system
            ON {table_name}(source_system)
        """)
        self.conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{table_name}_created_at
            ON {table_name}(created_at)
        """)
        self.conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{table_name}_visited_at
            ON {table_name}(visited_at)
        """)
        self.conn.execute(f"""
            CREATE INDEX IF NOT EXISTS idx_{table_name}_url
            ON {table_name}(url)
        """)
        self.conn.commit()
    
    def write_raw_record(
        self,
        source_id: str,
        source_record_id: str,
        payload: Dict[str, Any],
        source_type: str = "chat_messages",
    ) -> None:
        """Write raw record to raw table.
        
        Args:
            source_id: Source identifier
            source_record_id: Unique record ID within source
            payload: Original payload dictionary
            source_type: Type of data ("chat_messages", "events", etc.)
        """
        table_name = self.get_raw_table_name(source_id, source_type)
        self.ensure_raw_table(table_name)
        
        try:
            if table_name == "raw_chat_messages_browservisits":
                self.conn.execute(f"""
                    INSERT OR REPLACE INTO {table_name}
                    (
                        source_system, source_record_id, record_id, dataset_id, url, visited_at, title,
                        favicon_url, hostname, device_name, tab_id, window_id, incognito, transition_type,
                        pinned, audible, muted, opener_tab_id, referred_by, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """, (
                    source_id,
                    source_record_id,
                    payload.get("record_id") or source_record_id,
                    payload.get("dataset_id"),
                    payload.get("url"),
                    payload.get("visited_at"),
                    payload.get("title"),
                    payload.get("favicon_url"),
                    payload.get("hostname"),
                    payload.get("device_name"),
                    payload.get("tab_id") if isinstance(payload.get("tab_id"), int) else None,
                    payload.get("window_id") if isinstance(payload.get("window_id"), int) else None,
                    1 if payload.get("incognito") is True else (0 if payload.get("incognito") is False else None),
                    payload.get("transition_type"),
                    1 if payload.get("pinned") is True else (0 if payload.get("pinned") is False else None),
                    1 if payload.get("audible") is True else (0 if payload.get("audible") is False else None),
                    1 if payload.get("muted") is True else (0 if payload.get("muted") is False else None),
                    payload.get("opener_tab_id") if isinstance(payload.get("opener_tab_id"), int) else None,
                    payload.get("referred_by"),
                ))
                self.conn.commit()
                return

            # Store payload as JSON string
            payload_json = json.dumps(payload, ensure_ascii=False)
            
            self.conn.execute(f"""
                INSERT OR REPLACE INTO {table_name} 
                (source_system, source_record_id, payload_json, created_at)
                VALUES (?, ?, ?, datetime('now'))
            """, (source_id, source_record_id, payload_json))
            
            self.conn.commit()
        except Exception as e:
            self.conn.rollback()
            logger.error(
                "[PIPELINE:RAW] Failed to store raw record: source=%s, record_id=%s, error=%s",
                source_id,
                source_record_id,
                e,
            )
            raise
