"""iMessage reader: copy chat.db to temp (or open read-only), query messages since checkpoint.

Requires macOS and Full Disk Access for ~/Library/Messages/chat.db.
Uses chunked copy to support chat.db larger than ~2GB (avoids errno 84 EOVERFLOW from sendfile).
"""

from __future__ import annotations

import errno
import logging
import os
import plistlib
import re
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

logger = logging.getLogger("topos.ingestion.sources.imessage_reader")

# Mac epoch: seconds between 2001-01-01 and 1970-01-01
MAC_EPOCH_OFFSET = 978307200

DEFAULT_CHAT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"

# Chunk size for copy (avoids sendfile/stat overflow on files > ~2GB)
COPY_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB


def get_chat_db_path() -> Path:
    """Return path to iMessage chat.db (macOS)."""
    return Path(os.environ.get("IMESSAGE_CHAT_DB", str(DEFAULT_CHAT_DB_PATH)))


def mac_epoch_to_unix(mac_date: Optional[int]) -> Optional[float]:
    """Convert iMessage date to Unix timestamp.

    Apple message.date can be stored in seconds, milliseconds, microseconds, or
    nanoseconds since 2001-01-01 depending on OS/version/export path. Normalize
    to seconds before adding MAC epoch offset.
    """
    if mac_date is None:
        return None
    value = float(mac_date)
    abs_value = abs(value)
    # Heuristics by magnitude:
    # - seconds since 2001: ~1e9
    # - milliseconds:       ~1e12
    # - microseconds:       ~1e15
    # - nanoseconds:        ~1e18
    if abs_value >= 1e17:
        value = value / 1_000_000_000.0
    elif abs_value >= 1e14:
        value = value / 1_000_000.0
    elif abs_value >= 1e11:
        value = value / 1_000.0
    return value + MAC_EPOCH_OFFSET


def _copy_large_file(src: Path, dst: str, show_progress: bool = True) -> None:
    """Copy file in chunks using os.open/os.read/os.write only, to avoid EOVERFLOW (errno 84) on any system.
    Optional progress bar when size is available (stat may raise 84 on large files; we catch and skip bar).
    """
    total_size: Optional[int] = None
    if show_progress:
        try:
            total_size = src.stat().st_size
        except OSError as e:
            if getattr(e, "errno", None) == errno.EOVERFLOW:
                logger.debug("chat.db size overflow (EOVERFLOW), copying without progress bar")
            total_size = None

    pbar = None
    if show_progress and total_size is not None and total_size > 0:
        from topos.enrichment.progress_bar import ProgressBar
        pbar = ProgressBar(total=total_size, desc="Copying chat.db", width=40)
        pbar.__enter__()

    fd_in = fd_out = None
    try:
        try:
            fd_in = os.open(str(src), os.O_RDONLY)
        except OSError as e:
            if getattr(e, "errno", None) == errno.EOVERFLOW:
                logger.warning(
                    "EOVERFLOW opening source chat.db (file may be too large for this system): path=%s",
                    src,
                )
            raise
        fd_out = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        while True:
            chunk = os.read(fd_in, COPY_CHUNK_SIZE)
            if not chunk:
                break
            os.write(fd_out, chunk)
            if pbar is not None:
                pbar.update(len(chunk))
    finally:
        if fd_in is not None:
            try:
                os.close(fd_in)
            except OSError:
                pass
        if fd_out is not None:
            try:
                os.close(fd_out)
            except OSError:
                pass
        if pbar is not None:
            pbar.close()


def _normalize_sender_id(value: Any) -> Optional[str]:
    """Normalize sender identity from handle.id for storage."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _extract_text_from_plist(obj: Any) -> list[str]:
    """Recursively pull likely text values from parsed plist structures."""
    out: list[str] = []
    if isinstance(obj, str):
        s = " ".join(obj.split()).strip()
        if s and any(ch.isalpha() for ch in s):
            out.append(s)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            # Skip obviously structural keys.
            if isinstance(k, str) and k in {
                "$archiver", "$version", "$objects", "$top", "$class",
                "NS.keys", "NS.objects",
            }:
                continue
            out.extend(_extract_text_from_plist(v))
    elif isinstance(obj, (list, tuple, set)):
        for item in obj:
            out.extend(_extract_text_from_plist(item))
    return out


def _looks_like_archive_noise(s: str) -> bool:
    low = s.lower()
    return (
        low.startswith("ns.")
        or "nskeyedarchiver" in low
        or "nsdictionary" in low
        or "nsmutablestring" in low
        or "nsnumber" in low
        or "attribute" in low
        or low in {"bplist00", "$objects", "$top", "$version", "$archiver"}
    )


def _extract_utf8_text_candidates(raw: bytes) -> list[str]:
    """Extract likely human text from UTF-8 byte payloads only.

    We intentionally avoid utf-16/latin blind decoding to prevent fake CJK
    gibberish from archive bytes interpreted with wrong encodings.
    """
    try:
        decoded = raw.decode("utf-8", errors="ignore")
    except Exception:
        return []
    candidates: list[str] = []
    for match in re.findall(r"[^\x00-\x1F]{4,}", decoded):
        s = " ".join(match.split()).strip()
        if not s:
            continue
        if _looks_like_archive_noise(s):
            continue
        # Keep likely natural-language strings; avoid purely symbolic fragments.
        alpha = sum(1 for ch in s if ch.isalpha())
        if alpha < 3:
            continue
        candidates.append(s)
    return candidates


def _extract_text_from_attributed_body(value: Any) -> Optional[str]:
    """Best-effort extraction of human text from iMessage attributedBody blobs.

    Important: do NOT decode arbitrary bytes as plain text. That produces
    garbage strings (often CJK-looking) when archive bytes are interpreted with
    the wrong encoding.
    """
    if value is None:
        return None
    if isinstance(value, str):
        s = " ".join(value.split()).strip()
        return s or None
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(value)
    candidates: list[str] = []

    # Many modern iMessage attributedBody fields are keyed archive plists.
    if raw.startswith(b"bplist00"):
        try:
            plist_obj = plistlib.loads(raw)
            candidates.extend(_extract_text_from_plist(plist_obj))
        except Exception:
            pass

    # Some attributedBody payloads are not bplist but still contain UTF-8 text.
    if not candidates:
        candidates.extend(_extract_utf8_text_candidates(raw))
    if not candidates:
        return None

    # Filter out noisy/internal archive strings and pick best candidate.
    filtered = [s for s in candidates if not _looks_like_archive_noise(s)]
    if not filtered:
        return None
    best = max(filtered, key=len)
    best = best.replace("\ufffc", "").strip()
    return best or None


def _build_content_from_row(row: Dict[str, Any]) -> Optional[str]:
    """Build content string for iMessage rows, including non-text message forms."""
    text = (row.get("text") or "").strip()
    if text:
        return text

    attributed_text = _extract_text_from_attributed_body(row.get("attributed_body"))
    if attributed_text:
        return attributed_text

    subject = (row.get("subject") or "").strip()
    if subject:
        return subject

    # Handle tapbacks / reaction-style records where text is empty.
    associated_guid = row.get("associated_message_guid")
    associated_type = row.get("associated_message_type")
    if associated_guid:
        return f"[reaction:{associated_type}]"

    if row.get("cache_has_attachments"):
        return "[attachment]"

    item_type = row.get("item_type")
    if item_type not in (None, 0, "0"):
        return f"[system_event:item_type={item_type}]"

    return None


def _extract_imessage_context(row: Dict[str, Any]) -> Dict[str, Any]:
    """Extract unified reply/system context for canonical mapping."""
    metadata: Dict[str, Any] = {}
    reply_to_message_id: Optional[str] = None
    message_type = "message"
    event_type: Optional[str] = None

    thread_originator_guid = row.get("thread_originator_guid")
    if thread_originator_guid:
        reply_to_message_id = str(thread_originator_guid)
        metadata["thread_originator_guid"] = str(thread_originator_guid)
    if row.get("thread_originator_part") is not None:
        metadata["thread_originator_part"] = row.get("thread_originator_part")

    associated_guid = row.get("associated_message_guid")
    associated_type = row.get("associated_message_type")
    if associated_guid:
        metadata["associated_message_guid"] = str(associated_guid)
    if associated_type is not None:
        metadata["associated_message_type"] = associated_type

    item_type = row.get("item_type")
    if item_type not in (None, 0, "0"):
        message_type = "system"
        event_type = f"imessage_item_type:{item_type}"
        metadata["item_type"] = item_type

    group_action_type = row.get("group_action_type")
    if group_action_type not in (None, 0, "0"):
        message_type = "system"
        event_type = f"imessage_group_action:{group_action_type}"
        metadata["group_action_type"] = group_action_type

    if row.get("message_guid"):
        metadata["message_guid"] = str(row.get("message_guid"))
    if row.get("chat_guid"):
        metadata["chat_guid"] = str(row.get("chat_guid"))
    if row.get("chat_identifier"):
        metadata["chat_identifier"] = str(row.get("chat_identifier"))

    result: Dict[str, Any] = {
        "message_type": message_type,
        "event_type": event_type,
    }
    if reply_to_message_id:
        result["reply_to_message_id"] = reply_to_message_id
    if metadata:
        result["_metadata"] = metadata
    return result


def read_imessage_rows(
    last_rowid: Optional[str] = None,
    chat_db_path: Optional[Path] = None,
    batch_size: int = 5000,
    start_unix: Optional[float] = None,
) -> Iterator[Dict[str, Any]]:
    """
    Copy chat.db to a temp file (chunked to support >2GB), query messages with ROWID > last_rowid, yield rows as dicts.
    Each row has: id (imessage:ROWID), thread_id (str chat_id), content (text), created_at (Unix ts), role (user/other from is_from_me).
    """
    path = chat_db_path or get_chat_db_path()
    if not path.exists():
        raise FileNotFoundError(f"chat.db not found at {path}; Full Disk Access may be required")
    copy_path = None
    try:
        fd, copy_path = tempfile.mkstemp(suffix=".db", prefix="topos_imessage_")
        os.close(fd)
        try:
            _copy_large_file(path, copy_path)
        except OSError as e:
            if getattr(e, "errno", None) == errno.EOVERFLOW:
                try:
                    _copy_large_file(path, copy_path, show_progress=False)
                except (OSError, PermissionError) as retry_e:
                    raise PermissionError(f"Cannot copy chat.db: {retry_e}. Full Disk Access may be required.") from retry_e
            else:
                raise PermissionError(f"Cannot copy chat.db: {e}. Full Disk Access may be required.") from e
        except PermissionError as e:
            raise
    except Exception:
        if copy_path and os.path.exists(copy_path):
            try:
                os.unlink(copy_path)
            except OSError:
                pass
        raise
    try:
        try:
            conn = sqlite3.connect(copy_path)
        except OSError as e:
            if getattr(e, "errno", None) == errno.EOVERFLOW:
                logger.warning(
                    "EOVERFLOW opening copied chat.db with SQLite (copied file may be too large): %s",
                    copy_path,
                )
            raise
        conn.row_factory = sqlite3.Row
        try:
            message_columns = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(message)").fetchall()
                if r["name"]
            }
            chat_columns = {
                str(r["name"])
                for r in conn.execute("PRAGMA table_info(chat)").fetchall()
                if r["name"]
            }

            def _message_col_or_null(column: str, alias: str) -> str:
                if column in message_columns:
                    return f"message.{column} AS {alias}"
                return f"NULL AS {alias}"

            def _chat_col_or_null(column: str, alias: str) -> str:
                if column in chat_columns:
                    return f"chat.{column} AS {alias}"
                return f"NULL AS {alias}"

            last = 0
            if last_rowid:
                # last_record_id may be "imessage:12345" or "12345"
                raw = last_rowid.split(":")[-1]
                try:
                    last = int(raw)
                except ValueError:
                    pass
            mac_start_seconds = None
            if start_unix is not None:
                mac_start_seconds = float(start_unix) - MAC_EPOCH_OFFSET
            # Include non-text message forms too; content is synthesized when text is absent.
            query = f"""
                SELECT message.ROWID AS rowid,
                       message.text AS text,
                       message.subject AS subject,
                       message.attributedBody AS attributed_body,
                       message.associated_message_guid AS associated_message_guid,
                       message.associated_message_type AS associated_message_type,
                       message.cache_has_attachments AS cache_has_attachments,
                       message.item_type AS item_type,
                       {_message_col_or_null("group_action_type", "group_action_type")},
                       {_message_col_or_null("thread_originator_guid", "thread_originator_guid")},
                       {_message_col_or_null("thread_originator_part", "thread_originator_part")},
                       {_message_col_or_null("guid", "message_guid")},
                       message.date AS date,
                       message.handle_id AS handle_id,
                       message.is_from_me AS is_from_me,
                       handle.id AS sender_id,
                       chat.ROWID AS chat_id,
                       {_chat_col_or_null("guid", "chat_guid")},
                       {_chat_col_or_null("chat_identifier", "chat_identifier")}
                FROM message
                JOIN chat_message_join ON message.ROWID = chat_message_join.message_id
                JOIN chat ON chat.ROWID = chat_message_join.chat_id
                LEFT JOIN handle ON handle.ROWID = message.handle_id
                WHERE message.ROWID > ?
                  AND (
                    ? IS NULL
                    OR (
                      CASE
                        WHEN abs(message.date) >= 100000000000000000 THEN (message.date / 1000000000.0)
                        WHEN abs(message.date) >= 100000000000000 THEN (message.date / 1000000.0)
                        WHEN abs(message.date) >= 100000000000 THEN (message.date / 1000.0)
                        ELSE (message.date * 1.0)
                      END
                    ) >= ?
                  )
                ORDER BY message.ROWID
                LIMIT ?
            """
            cursor = conn.execute(query, (last, mac_start_seconds, mac_start_seconds, batch_size))
            for row in cursor:
                r = dict(row)
                rowid = r["rowid"]
                content = _build_content_from_row(r)
                if not content:
                    continue
                mac_date = r.get("date")
                unix_ts = mac_epoch_to_unix(mac_date) if mac_date is not None else None
                is_from_me = r.get("is_from_me", 0)
                role = "user" if is_from_me else "other"
                context = _extract_imessage_context(r)
                if is_from_me:
                    sender_id = "self"
                else:
                    sender_id = _normalize_sender_id(r.get("sender_id")) or f"unknown:{r.get('handle_id')}"
                out = {
                    "id": f"imessage:{rowid}",
                    "thread_id": str(r.get("chat_id", "")),
                    "content": content,
                    "created_at": unix_ts,
                    "role": role,
                    "sender_id": sender_id,
                    "ROWID": rowid,
                }
                if context.get("reply_to_message_id"):
                    out["reply_to_message_id"] = context["reply_to_message_id"]
                if context.get("message_type"):
                    out["message_type"] = context["message_type"]
                if context.get("event_type"):
                    out["event_type"] = context["event_type"]
                if context.get("_metadata"):
                    out["_metadata"] = context["_metadata"]
                yield out
        finally:
            conn.close()
    finally:
        try:
            os.unlink(copy_path)
        except OSError:
            pass


def read_imessage_rows_list(
    last_rowid: Optional[str] = None,
    chat_db_path: Optional[Path] = None,
    batch_size: int = 5000,
    start_unix: Optional[float] = None,
) -> list[Dict[str, Any]]:
    """Convenience: consume iterator into a list."""
    return list(
        read_imessage_rows(
            last_rowid=last_rowid,
            chat_db_path=chat_db_path,
            batch_size=batch_size,
            start_unix=start_unix,
        )
    )
