"""Canonical tables for human-to-human conversations (messenger ingestion).

Stores thread metadata in `conversations` and message rows in `conversation_messages`.
Used when canonical_group_id="conversations" (e.g. iMessage, Signal). Not ai_chat_*.
"""

from __future__ import annotations

import json
import logging
import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("topos.storage.canonical.conversations_tables")

CONVERSATIONS_TABLE = "conversations"
CONVERSATION_MESSAGES_TABLE = "conversation_messages"
CONTACTS_TABLE = "contacts"
CONTACT_IDENTIFIERS_TABLE = "contact_identifiers"
CONVERSATION_PARTICIPANTS_TABLE = "conversation_participants"


def _message_timestamp_unix_for_sort(event_at: Optional[str], created_at: Optional[str]) -> float:
    """
    Parse message time for chronological ordering (larger = more recent).

    SQLite ``ORDER BY event_at DESC`` on TEXT can mis-order values (mixed ISO shapes,
    epoch strings, empty event_at with valid created_at). Callers fetch a bounded set
    and re-sort in Python using this key.
    """
    for raw in (event_at, created_at):
        if raw is None:
            continue
        s = str(raw).strip()
        if not s:
            continue
        try:
            iso = s
            if iso.endswith("Z"):
                iso = iso[:-1] + "+00:00"
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass
        try:
            norm = s[:19].replace("T", " ")
            dt = datetime.strptime(norm, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            pass
        if s.isdigit() and len(s) >= 10:
            try:
                val = int(s)
                if val > 10_000_000_000:
                    val //= 1000
                return float(val)
            except ValueError:
                pass
    return 0.0


def ensure_conversations_table(conn) -> None:
    """Create conversations table (thread metadata) if not exists."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {CONVERSATIONS_TABLE} (
            conversation_id TEXT NOT NULL,
            dataset_id TEXT NOT NULL,
            source_id TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (conversation_id, dataset_id)
        )
    """)
    conn.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_{CONVERSATIONS_TABLE}_dataset_id
        ON {CONVERSATIONS_TABLE}(dataset_id)
    """)
    conn.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_{CONVERSATIONS_TABLE}_source_id
        ON {CONVERSATIONS_TABLE}(source_id)
    """)
    conn.commit()


def ensure_conversation_messages_table(conn) -> None:
    """Create conversation_messages table (message rows) if not exists. Stage 9: event_at, is_from_self."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {CONVERSATION_MESSAGES_TABLE} (
            message_id TEXT NOT NULL PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            dataset_id TEXT NOT NULL,
            sender_type TEXT,
            sender_id TEXT,
            reply_to_message_id TEXT,
            message_type TEXT,
            event_type TEXT,
            content TEXT,
            event_at TEXT NOT NULL,
            source_id TEXT NOT NULL,
            metadata_json TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            is_from_self INTEGER DEFAULT 0,
            owner_user_id TEXT
        )
    """)
    conn.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_{CONVERSATION_MESSAGES_TABLE}_conversation_id
        ON {CONVERSATION_MESSAGES_TABLE}(conversation_id)
    """)
    conn.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_{CONVERSATION_MESSAGES_TABLE}_dataset_id
        ON {CONVERSATION_MESSAGES_TABLE}(dataset_id)
    """)
    conn.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_{CONVERSATION_MESSAGES_TABLE}_source_id
        ON {CONVERSATION_MESSAGES_TABLE}(source_id)
    """)
    conn.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_{CONVERSATION_MESSAGES_TABLE}_event_at
        ON {CONVERSATION_MESSAGES_TABLE}(event_at)
    """)
    conn.commit()


def ensure_contacts_table(conn) -> None:
    """Create canonical contacts table if not exists."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {CONTACTS_TABLE} (
            contact_id TEXT NOT NULL PRIMARY KEY,
            dataset_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            display_name TEXT,
            known_usernames_json TEXT,
            is_self INTEGER NOT NULL DEFAULT 0,
            last_import_source TEXT,
            last_import_run_id TEXT,
            last_imported_at TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_{CONTACTS_TABLE}_dataset_source
        ON {CONTACTS_TABLE}(dataset_id, source_id)
    """)
    conn.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_{CONTACTS_TABLE}_is_self
        ON {CONTACTS_TABLE}(is_self)
    """)
    conn.commit()


def ensure_contact_identifiers_table(conn) -> None:
    """Create table mapping contact identifiers (phone/email/service ids)."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {CONTACT_IDENTIFIERS_TABLE} (
            dataset_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            identifier TEXT NOT NULL,
            identifier_type TEXT NOT NULL,
            contact_id TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (dataset_id, source_id, identifier)
        )
    """)
    conn.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_{CONTACT_IDENTIFIERS_TABLE}_contact
        ON {CONTACT_IDENTIFIERS_TABLE}(contact_id)
    """)
    conn.commit()


def ensure_conversation_participants_table(conn) -> None:
    """Create conversation <-> participant relationship table."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {CONVERSATION_PARTICIPANTS_TABLE} (
            conversation_id TEXT NOT NULL,
            dataset_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            contact_id TEXT NOT NULL,
            role TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (conversation_id, dataset_id, source_id, contact_id)
        )
    """)
    conn.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_{CONVERSATION_PARTICIPANTS_TABLE}_dataset_source
        ON {CONVERSATION_PARTICIPANTS_TABLE}(dataset_id, source_id)
    """)
    conn.commit()


def _ensure_signal_identity_columns(conn) -> None:
    """Add is_from_self and owner_user_id for Signal identity. Stage 9: is_from_self (was from_self). Idempotent."""
    for col, typ in (("is_from_self", "INTEGER DEFAULT 0"), ("owner_user_id", "TEXT")):
        try:
            conn.execute(f"ALTER TABLE {CONVERSATION_MESSAGES_TABLE} ADD COLUMN {col} {typ}")
            conn.commit()
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                logger.debug("Signal identity column %s: %s", col, e)
            conn.rollback()


def _ensure_reply_and_event_columns(conn) -> None:
    """Add unified reply/system columns for messenger sources. Idempotent."""
    for col, typ in (
        ("reply_to_message_id", "TEXT"),
        ("message_type", "TEXT"),
        ("event_type", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE {CONVERSATION_MESSAGES_TABLE} ADD COLUMN {col} {typ}")
            conn.commit()
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                logger.debug("Messenger context column %s: %s", col, e)
            conn.rollback()


def _ensure_contact_provenance_columns(conn) -> None:
    """Add contact import/profile columns. Idempotent."""
    for col, typ in (
        ("known_usernames_json", "TEXT"),
        ("last_import_source", "TEXT"),
        ("last_import_run_id", "TEXT"),
        ("last_imported_at", "TEXT"),
    ):
        try:
            conn.execute(f"ALTER TABLE {CONTACTS_TABLE} ADD COLUMN {col} {typ}")
            conn.commit()
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                logger.debug("Contact provenance column %s: %s", col, e)
            conn.rollback()


def _ensure_contact_sharing_policy_column(conn) -> None:
    """Stage 11: JSON policy for name_visibility / row_visibility per contact."""
    try:
        conn.execute(f"ALTER TABLE {CONTACTS_TABLE} ADD COLUMN sharing_policy_json TEXT")
        conn.commit()
    except Exception as e:
        if "duplicate column" not in str(e).lower():
            logger.debug("Contact sharing_policy_json column: %s", e)
        conn.rollback()


def ensure_all_tables(conn) -> None:
    """Ensure both conversations and conversation_messages tables exist.
    Stage 9 column renames run at engine startup (app.py) to avoid blocking the event loop during requests.
    """
    ensure_conversations_table(conn)
    ensure_conversation_messages_table(conn)
    ensure_contacts_table(conn)
    ensure_contact_identifiers_table(conn)
    ensure_conversation_participants_table(conn)
    _ensure_reply_and_event_columns(conn)
    _ensure_signal_identity_columns(conn)
    _ensure_contact_provenance_columns(conn)
    _ensure_contact_sharing_policy_column(conn)


class ConversationsTablesManager:
    """Writes to conversations and conversation_messages only (canonical_group_id=conversations)."""

    def __init__(self, conn) -> None:
        self.conn = conn

    def ensure_tables(self) -> None:
        """Create conversations and conversation_messages tables if not exist."""
        if self.conn:
            ensure_all_tables(self.conn)

    def upsert_conversation(
        self,
        conversation_id: str,
        dataset_id: str,
        source_id: Optional[str] = None,
    ) -> None:
        """Insert or replace one row in conversations."""
        if not self.conn:
            return
        self.ensure_tables()
        self.conn.execute(f"""
            INSERT OR REPLACE INTO {CONVERSATIONS_TABLE}
            (conversation_id, dataset_id, source_id, created_at, updated_at)
            VALUES (?, ?, ?, datetime('now'), datetime('now'))
        """, (conversation_id, dataset_id, source_id or ""))
        self.conn.commit()

    def upsert_message_batch(
        self,
        records: List[Dict[str, Any]],
        dataset_id: str,
        source_id: str,
    ) -> Dict[str, int]:
        """
        Upsert messages into conversation_messages and ensure parent rows in conversations.
        Each record must have: message_id, thread_id or conversation_id, ts, sender_type, content.
        Optional: sender_id, _metadata, from_self (0/1), owner_user_id (for Signal identity).
        """
        if not self.conn or not records:
            return {"messages_created": 0, "conversations_created": 0}
        self.ensure_tables()

        def _normalize_identifier_type(identifier: str) -> str:
            low = (identifier or "").lower()
            if low.startswith("+") or low.replace("-", "").replace(" ", "").isdigit():
                return "phone"
            if "@" in low:
                return "email"
            if ":" in low or len(low) > 24:
                return "service_id"
            return "handle"

        def _merge_known_usernames_json(
            existing_json: Optional[str],
            candidates: List[str],
        ) -> Optional[str]:
            norm: List[str] = []
            seen: set[str] = set()
            try:
                existing = json.loads(existing_json or "[]")
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []
            for raw in list(existing) + list(candidates):
                val = str(raw or "").strip()
                if not val or len(val) > 128:
                    continue
                low = val.lower()
                if low in seen:
                    continue
                seen.add(low)
                norm.append(val)
            return json.dumps(norm, ensure_ascii=False) if norm else None

        def _extract_display_name(rec: Dict[str, Any]) -> Optional[str]:
            metadata = rec.get("_metadata")
            if isinstance(metadata, dict):
                for key in (
                    "sender_name",
                    "display_name",
                    "contact_name",
                    "profileName",
                    "profile_name",
                    "quoteAuthor",
                ):
                    val = metadata.get(key)
                    if isinstance(val, str) and val.strip():
                        return val.strip()
            return None

        def _extract_username_candidates(rec: Dict[str, Any], sender_identifier_type: str) -> List[str]:
            out: List[str] = []
            sender_id = str(rec.get("sender_id") or "").strip()
            if sender_id and sender_identifier_type in {"handle", "service_id"} and "@" not in sender_id:
                out.append(sender_id)
            metadata = rec.get("_metadata")
            if isinstance(metadata, dict):
                for key in ("username", "handle", "profileName", "profile_name"):
                    val = metadata.get(key)
                    if isinstance(val, str) and val.strip():
                        out.append(val.strip())
            return out

        def _upsert_contact(rec: Dict[str, Any]) -> Optional[str]:
            sender_id = str(rec.get("sender_id") or "").strip()
            if not sender_id:
                return None
            sender_identifier_type = _normalize_identifier_type(sender_id)
            is_self = 1 if (rec.get("from_self") is True or rec.get("is_from_self") is True or sender_id.lower() == "self") else 0
            row = self.conn.execute(
                f"""
                SELECT contact_id
                FROM {CONTACT_IDENTIFIERS_TABLE}
                WHERE dataset_id = ?
                  AND identifier = ?
                  AND source_id IN (?, '*')
                ORDER BY CASE WHEN source_id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (dataset_id, sender_id, source_id, source_id),
            ).fetchone()
            if row and row[0]:
                contact_id = str(row[0])
            else:
                key = f"{sender_identifier_type}:{sender_id}"
                digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
                contact_id = f"{dataset_id}:contact:{digest}"
            display_name = _extract_display_name(rec)
            usernames_json = _merge_known_usernames_json(
                (
                    self.conn.execute(
                        f"SELECT known_usernames_json FROM {CONTACTS_TABLE} WHERE contact_id = ? LIMIT 1",
                        (contact_id,),
                    ).fetchone() or [None]
                )[0],
                _extract_username_candidates(rec, sender_identifier_type),
            )
            self.conn.execute(
                f"""
                INSERT INTO {CONTACTS_TABLE}
                (contact_id, dataset_id, source_id, display_name, known_usernames_json, is_self, last_import_source, last_import_run_id, last_imported_at, created_at, updated_at)
                VALUES (?, ?, 'global', ?, ?, ?, NULL, NULL, NULL, datetime('now'), datetime('now'))
                ON CONFLICT(contact_id) DO UPDATE SET
                    display_name = COALESCE(excluded.display_name, {CONTACTS_TABLE}.display_name),
                    known_usernames_json = COALESCE(excluded.known_usernames_json, {CONTACTS_TABLE}.known_usernames_json),
                    is_self = CASE WHEN excluded.is_self = 1 THEN 1 ELSE {CONTACTS_TABLE}.is_self END,
                    updated_at = datetime('now')
                """,
                (contact_id, dataset_id, display_name, usernames_json, is_self),
            )
            self.conn.execute(
                f"""
                INSERT INTO {CONTACT_IDENTIFIERS_TABLE}
                (dataset_id, source_id, identifier, identifier_type, contact_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                ON CONFLICT(dataset_id, source_id, identifier) DO UPDATE SET
                    contact_id = excluded.contact_id,
                    updated_at = datetime('now')
                """,
                (
                    dataset_id,
                    source_id,
                    sender_id,
                    sender_identifier_type,
                    contact_id,
                ),
            )
            return contact_id

        conversations_created = 0
        seen_conversation_ids: set[tuple[str, str]] = set()
        participants_seen: set[tuple[str, str]] = set()
        for rec in records:
            conversation_id = (
                str(rec.get("conversation_id") or rec.get("thread_id") or dataset_id)
            )
            key = (conversation_id, dataset_id)
            if key not in seen_conversation_ids:
                self.upsert_conversation(conversation_id, dataset_id, source_id)
                seen_conversation_ids.add(key)
                conversations_created += 1
            contact_id = _upsert_contact(rec)
            if contact_id:
                part_key = (conversation_id, contact_id)
                if part_key not in participants_seen:
                    self.conn.execute(
                        f"""
                        INSERT INTO {CONVERSATION_PARTICIPANTS_TABLE}
                        (conversation_id, dataset_id, source_id, contact_id, role, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                        ON CONFLICT(conversation_id, dataset_id, source_id, contact_id) DO UPDATE SET
                            role = COALESCE(excluded.role, {CONVERSATION_PARTICIPANTS_TABLE}.role),
                            updated_at = datetime('now')
                        """,
                        (
                            conversation_id,
                            dataset_id,
                            source_id,
                            contact_id,
                            "self" if (rec.get("from_self") or rec.get("is_from_self")) else "participant",
                        ),
                    )
                    participants_seen.add(part_key)
        for rec in records:
            message_id = str(rec.get("message_id") or "")
            if not message_id:
                continue
            conversation_id = (
                str(rec.get("conversation_id") or rec.get("thread_id") or dataset_id)
            )
            event_at = rec.get("event_at") or rec.get("ts") or ""
            sender_type = rec.get("sender_type")
            sender_id = rec.get("sender_id")
            reply_to_message_id = rec.get("reply_to_message_id")
            message_type = rec.get("message_type")
            event_type = rec.get("event_type")
            content = rec.get("content")
            metadata_json = None
            if "_metadata" in rec:
                metadata_json = json.dumps(rec["_metadata"], ensure_ascii=False)
            is_from_self = 1 if (rec.get("is_from_self") is True or rec.get("from_self") is True) else 0
            owner_user_id = rec.get("owner_user_id")
            self.conn.execute(f"""
                INSERT OR REPLACE INTO {CONVERSATION_MESSAGES_TABLE}
                (message_id, conversation_id, dataset_id, sender_type, sender_id, reply_to_message_id, message_type, event_type, content, event_at, source_id, metadata_json, is_from_self, owner_user_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """, (
                message_id,
                conversation_id,
                dataset_id,
                sender_type,
                sender_id,
                reply_to_message_id,
                message_type,
                event_type,
                content,
                event_at,
                source_id,
                metadata_json,
                is_from_self,
                owner_user_id,
            ))
        self.conn.commit()
        logger.debug(
            "[PIPELINE:CONVERSATIONS] Wrote %d messages to %s, %d conversation rows",
            len(records),
            CONVERSATION_MESSAGES_TABLE,
            len(seen_conversation_ids),
        )
        return {"messages_created": len(records), "conversations_created": conversations_created}

    def list_contacts(
        self,
        *,
        dataset_id: str,
        source_id: str,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """List contacts with primary identifier and message counts for a source."""
        if not self.conn:
            return []
        self.ensure_tables()
        rows = self.conn.execute(
            f"""
            SELECT c.contact_id,
                   c.display_name,
                   c.known_usernames_json,
                   c.sharing_policy_json,
                   c.is_self,
                   c.last_import_source,
                   c.last_import_run_id,
                   c.last_imported_at,
                   ci.identifier AS primary_identifier,
                   ci.identifier_type AS primary_identifier_type,
                   COUNT(m.message_id) AS message_count
            FROM {CONTACTS_TABLE} c
            LEFT JOIN {CONTACT_IDENTIFIERS_TABLE} ci
              ON ci.contact_id = c.contact_id
             AND ci.dataset_id = c.dataset_id
             AND ci.source_id IN (?, '*')
            LEFT JOIN {CONVERSATION_MESSAGES_TABLE} m
              ON m.dataset_id = c.dataset_id
             AND m.source_id = ?
             AND m.sender_id = ci.identifier
            WHERE c.dataset_id = ?
            GROUP BY c.contact_id, c.display_name, c.known_usernames_json, c.sharing_policy_json, c.is_self, c.last_import_source, c.last_import_run_id, c.last_imported_at, ci.identifier, ci.identifier_type
            ORDER BY c.is_self DESC, message_count DESC, c.updated_at DESC
            LIMIT ?
            """,
            (source_id, source_id, dataset_id, int(limit)),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            try:
                usernames = json.loads(row[2] or "[]")
                if not isinstance(usernames, list):
                    usernames = []
            except Exception:
                usernames = []
            pol_raw = row[3]
            try:
                sharing_policy = json.loads(pol_raw) if pol_raw else {}
                if not isinstance(sharing_policy, dict):
                    sharing_policy = {}
            except Exception:
                sharing_policy = {}
            out.append(
                {
                    "contact_id": row[0],
                    "display_name": row[1],
                    "known_usernames": usernames,
                    "sharing_policy": sharing_policy,
                    "is_self": bool(row[4]),
                    "last_import_source": row[5],
                    "last_import_run_id": row[6],
                    "last_imported_at": row[7],
                    "identifier": row[8],
                    "identifier_type": row[9],
                    "message_count": int(row[10] or 0),
                }
            )
        return out

    def get_contact_message_samples(
        self,
        *,
        dataset_id: str,
        source_id: str,
        identifier: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Return the most recent sample messages for identifier (newest first)."""
        if not self.conn or not identifier:
            return []
        lim = max(1, int(limit))
        # Pull a wider candidate set: string ORDER BY can disagree with true time order.
        cap = min(500, max(60, lim * 25))
        rows = self.conn.execute(
            f"""
            SELECT message_id, content, event_at, conversation_id, created_at
            FROM {CONVERSATION_MESSAGES_TABLE}
            WHERE dataset_id = ?
              AND source_id = ?
              AND sender_id = ?
            ORDER BY event_at DESC, created_at DESC, message_id DESC
            LIMIT ?
            """,
            (dataset_id, source_id, identifier, cap),
        ).fetchall()
        sorted_rows = sorted(
            rows,
            key=lambda r: (
                _message_timestamp_unix_for_sort(
                    str(r[2]) if r[2] is not None else None,
                    str(r[4]) if len(r) > 4 and r[4] is not None else None,
                ),
                str(r[0] or ""),
            ),
            reverse=True,
        )[:lim]
        return [
            {
                "message_id": row[0],
                "content": row[1],
                "event_at": row[2],
                "conversation_id": row[3],
            }
            for row in sorted_rows
        ]

    def get_contact_conversation_thread_previews(
        self,
        *,
        dataset_id: str,
        source_id: str,
        profile_identifier: str,
        max_conversations: int = 8,
        messages_per_conversation: int = 45,
    ) -> List[Dict[str, Any]]:
        """Recent slices of full threads where ``profile_identifier`` sent at least one message.

        Each block lists messages from all participants in that conversation (not only the profile
        contact), ordered newest→oldest within the window (the ``messages_per_conversation`` most
        recent rows in the thread).
        """
        if not self.conn or not profile_identifier:
            return []
        self.ensure_tables()
        mc = max(1, int(max_conversations))
        mpc = max(1, int(messages_per_conversation))

        rows = self.conn.execute(
            f"""
            SELECT conversation_id, event_at, created_at
            FROM {CONVERSATION_MESSAGES_TABLE}
            WHERE dataset_id = ?
              AND source_id = ?
              AND sender_id = ?
            """,
            (dataset_id, source_id, profile_identifier),
        ).fetchall()
        best_ts: Dict[str, float] = {}
        for conv_id, event_at, created_at in rows:
            cid = str(conv_id or "").strip()
            if not cid:
                continue
            ts = _message_timestamp_unix_for_sort(
                str(event_at) if event_at is not None else None,
                str(created_at) if created_at is not None else None,
            )
            best_ts[cid] = max(best_ts.get(cid, 0.0), ts)
        sorted_conv_ids = sorted(best_ts.keys(), key=lambda c: best_ts[c], reverse=True)[:mc]

        out: List[Dict[str, Any]] = []
        for cid in sorted_conv_ids:
            all_rows = self.conn.execute(
                f"""
                SELECT message_id, content, event_at, conversation_id, created_at, sender_id, is_from_self
                FROM {CONVERSATION_MESSAGES_TABLE}
                WHERE dataset_id = ?
                  AND source_id = ?
                  AND conversation_id = ?
                """,
                (dataset_id, source_id, cid),
            ).fetchall()
            sorted_all = sorted(
                all_rows,
                key=lambda r: (
                    _message_timestamp_unix_for_sort(
                        str(r[2]) if r[2] is not None else None,
                        str(r[4]) if len(r) > 4 and r[4] is not None else None,
                    ),
                    str(r[0] or ""),
                ),
            )
            window = sorted_all[-mpc:] if len(sorted_all) > mpc else sorted_all
            messages: List[Dict[str, Any]] = []
            for r in reversed(window):
                is_self = r[6]
                messages.append(
                    {
                        "message_id": r[0],
                        "content": r[1],
                        "event_at": r[2],
                        "conversation_id": r[3],
                        "created_at": r[4],
                        "sender_id": r[5],
                        "is_from_self": bool(is_self) if is_self is not None else False,
                    }
                )
            out.append({"conversation_id": cid, "messages": messages})
        return out

    def update_contact_display_name(
        self,
        *,
        dataset_id: str,
        source_id: str,
        contact_id: str,
        display_name: Optional[str],
    ) -> None:
        """Set/clear display name for contact."""
        if not self.conn:
            return
        self.conn.execute(
            f"""
            UPDATE {CONTACTS_TABLE}
            SET display_name = ?, last_import_source = 'manual_edit', updated_at = datetime('now')
            WHERE dataset_id = ? AND contact_id = ?
            """,
            ((display_name or None), dataset_id, contact_id),
        )
        self.conn.commit()

    def update_contact_sharing_policy(
        self,
        *,
        dataset_id: str,
        contact_id: str,
        sharing_policy: Optional[Dict[str, Any]],
    ) -> None:
        """Stage 11: set sharing_policy_json (name_visibility, row_visibility)."""
        if not self.conn:
            return
        self.ensure_tables()
        payload = json.dumps(sharing_policy or {}) if sharing_policy else None
        self.conn.execute(
            f"""
            UPDATE {CONTACTS_TABLE}
            SET sharing_policy_json = ?, updated_at = datetime('now')
            WHERE dataset_id = ? AND contact_id = ?
            """,
            (payload, dataset_id, contact_id),
        )
        self.conn.commit()

    def auto_resolve_contact_names(
        self,
        *,
        dataset_id: str,
        source_id: str,
    ) -> int:
        """Best-effort fill display_name from message metadata."""
        if not self.conn:
            return 0
        candidates = self.conn.execute(
            f"""
            SELECT sender_id, metadata_json
            FROM {CONVERSATION_MESSAGES_TABLE}
            WHERE dataset_id = ?
              AND source_id = ?
              AND sender_id IS NOT NULL
              AND sender_id != ''
              AND metadata_json IS NOT NULL
            ORDER BY event_at DESC
            """,
            (dataset_id, source_id),
        ).fetchall()
        updated = 0
        seen: set[str] = set()
        for sender_id, metadata_json in candidates:
            sid = str(sender_id or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            try:
                md = json.loads(metadata_json or "{}")
            except Exception:
                continue
            display_name = None
            if isinstance(md, dict):
                for key in (
                    "sender_name",
                    "display_name",
                    "contact_name",
                    "profileName",
                    "profile_name",
                    "quoteAuthor",
                ):
                    val = md.get(key)
                    if isinstance(val, str) and val.strip():
                        display_name = val.strip()
                        break
            if not display_name:
                continue
            row = self.conn.execute(
                f"""
                SELECT contact_id
                FROM {CONTACT_IDENTIFIERS_TABLE}
                WHERE dataset_id = ?
                  AND identifier = ?
                  AND source_id IN (?, '*')
                ORDER BY CASE WHEN source_id = ? THEN 0 ELSE 1 END
                LIMIT 1
                """,
                (dataset_id, sid, source_id, source_id),
            ).fetchone()
            if not row or not row[0]:
                continue
            contact_id = str(row[0])
            cursor = self.conn.execute(
                f"""
                UPDATE {CONTACTS_TABLE}
                SET display_name = COALESCE(display_name, ?),
                    last_import_source = CASE WHEN display_name IS NULL THEN 'auto_resolve' ELSE last_import_source END,
                    updated_at = datetime('now')
                WHERE dataset_id = ? AND contact_id = ?
                """,
                (display_name, dataset_id, contact_id),
            )
            if int(cursor.rowcount or 0) > 0:
                updated += 1
        if updated:
            self.conn.commit()
        return updated

    def import_contacts_batch(
        self,
        *,
        dataset_id: str,
        contacts: List[Dict[str, Any]],
        source_id: Optional[str] = None,
        target_sources: Optional[List[str]] = None,
        import_source: Optional[str] = None,
        import_run_id: Optional[str] = None,
    ) -> Dict[str, int]:
        """
        Upsert external contacts into canonical contacts tables.
        Does not overwrite existing non-null display names.
        """
        if not self.conn or not contacts:
            return {"contacts_upserted": 0, "identifiers_upserted": 0}
        self.ensure_tables()
        import_source_value = str(import_source or "").strip() or None
        import_run_id_value = str(import_run_id or "").strip() or None
        scoped_sources = sorted(
            {
                s
                for s in ([str(source_id or "").strip()] + [str(s or "").strip() for s in (target_sources or [])])
                if s
            }
        )

        def _normalize_identifier_type(identifier: str, explicit_type: Optional[str]) -> str:
            if explicit_type and str(explicit_type).strip():
                return str(explicit_type).strip().lower()
            low = (identifier or "").lower()
            if low.startswith("+") or low.replace("-", "").replace(" ", "").isdigit():
                return "phone"
            if "@" in low:
                return "email"
            if ":" in low or len(low) > 24:
                return "service_id"
            return "handle"

        def _merge_known_usernames_json(existing_json: Optional[str], candidates: List[str]) -> Optional[str]:
            merged: List[str] = []
            seen: set[str] = set()
            try:
                existing = json.loads(existing_json or "[]")
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []
            for raw in list(existing) + list(candidates):
                val = str(raw or "").strip()
                if not val or len(val) > 128:
                    continue
                low = val.lower()
                if low in seen:
                    continue
                seen.add(low)
                merged.append(val)
            return json.dumps(merged, ensure_ascii=False) if merged else None

        contacts_upserted = 0
        identifiers_upserted = 0
        for rec in contacts:
            identifiers_raw = rec.get("identifiers") or []
            pairs: List[tuple[str, str]] = []
            for item in identifiers_raw:
                if isinstance(item, dict):
                    identifier = str(item.get("identifier") or "").strip()
                    itype = _normalize_identifier_type(identifier, item.get("type"))
                else:
                    identifier = str(item or "").strip()
                    itype = _normalize_identifier_type(identifier, None)
                if identifier:
                    pairs.append((identifier, itype))
            if not pairs:
                continue

            # Prefer existing contact mapping for any known identifier.
            contact_id = None
            for identifier, _ in pairs:
                row = self.conn.execute(
                    f"""
                    SELECT contact_id FROM {CONTACT_IDENTIFIERS_TABLE}
                    WHERE dataset_id = ? AND identifier = ?
                    LIMIT 1
                    """,
                    (dataset_id, identifier),
                ).fetchone()
                if row and row[0]:
                    contact_id = str(row[0])
                    break
            if not contact_id:
                key = "|".join(sorted({f"{t}:{i}" for i, t in pairs}))
                digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]
                contact_id = f"{dataset_id}:contact:import:{digest}"
            display_name = rec.get("display_name")
            if display_name is not None:
                display_name = str(display_name).strip() or None
            username_candidates = [
                i for i, t in pairs
                if t in {"handle", "service_id"} and "@" not in i and len(i) <= 128
            ]
            existing_profile = self.conn.execute(
                f"""
                SELECT known_usernames_json
                FROM {CONTACTS_TABLE}
                WHERE contact_id = ?
                LIMIT 1
                """,
                (contact_id,),
            ).fetchone()
            usernames_json = _merge_known_usernames_json(
                existing_profile[0] if existing_profile else None,
                username_candidates,
            )

            self.conn.execute(
                f"""
                INSERT INTO {CONTACTS_TABLE}
                (
                    contact_id,
                    dataset_id,
                    source_id,
                    display_name,
                    known_usernames_json,
                    is_self,
                    last_import_source,
                    last_import_run_id,
                    last_imported_at,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, 'global', ?, ?, 0, ?, ?, CASE WHEN ? IS NOT NULL THEN datetime('now') ELSE NULL END, datetime('now'), datetime('now'))
                ON CONFLICT(contact_id) DO UPDATE SET
                    display_name = COALESCE({CONTACTS_TABLE}.display_name, excluded.display_name),
                    known_usernames_json = COALESCE(excluded.known_usernames_json, {CONTACTS_TABLE}.known_usernames_json),
                    last_import_source = COALESCE(excluded.last_import_source, {CONTACTS_TABLE}.last_import_source),
                    last_import_run_id = COALESCE(excluded.last_import_run_id, {CONTACTS_TABLE}.last_import_run_id),
                    last_imported_at = CASE
                        WHEN excluded.last_import_source IS NOT NULL THEN datetime('now')
                        ELSE {CONTACTS_TABLE}.last_imported_at
                    END,
                    updated_at = datetime('now')
                """,
                (
                    contact_id,
                    dataset_id,
                    display_name,
                    usernames_json,
                    import_source_value,
                    import_run_id_value,
                    import_source_value,
                ),
            )
            contacts_upserted += 1

            identifier_scopes = ["*"] + scoped_sources
            for identifier, identifier_type in pairs:
                for scope_source_id in identifier_scopes:
                    self.conn.execute(
                        f"""
                        INSERT INTO {CONTACT_IDENTIFIERS_TABLE}
                        (dataset_id, source_id, identifier, identifier_type, contact_id, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))
                        ON CONFLICT(dataset_id, source_id, identifier) DO UPDATE SET
                            identifier_type = COALESCE({CONTACT_IDENTIFIERS_TABLE}.identifier_type, excluded.identifier_type),
                            contact_id = {CONTACT_IDENTIFIERS_TABLE}.contact_id,
                            updated_at = datetime('now')
                        """,
                        (dataset_id, scope_source_id, identifier, identifier_type, contact_id),
                    )
                    identifiers_upserted += 1

        self.conn.commit()
        return {"contacts_upserted": contacts_upserted, "identifiers_upserted": identifiers_upserted}
