"""iMessage reader: copy chat.db to temp (or open read-only), query messages since checkpoint.

Requires macOS and Full Disk Access for ~/Library/Messages/chat.db.
Uses chunked copy to support chat.db larger than ~2GB (avoids errno 84 EOVERFLOW from sendfile).
"""

from __future__ import annotations

import errno
import logging
import os
import plistlib
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

# Apple Messages "Filter Unknown Senders" lands chats in a separate inbox.
# chat.is_filtered = 2 is that bucket; message.is_spam = 1 is Apple's junk flag.
UNKNOWN_SENDER_FILTERED = 2
APPLE_SPAM_FLAG = 1

logger = logging.getLogger("topos.ingestion.sources.imessage_reader")

# Mac epoch: seconds between 2001-01-01 and 1970-01-01
MAC_EPOCH_OFFSET = 978307200

DEFAULT_CHAT_DB_PATH = Path.home() / "Library" / "Messages" / "chat.db"

# Chunk size for copy (avoids sendfile/stat overflow on files > ~2GB)
COPY_CHUNK_SIZE = 8 * 1024 * 1024  # 8 MiB

# NSArchiver typedstream header: streamer version 4, then the 11-byte
# literal "streamtyped". Everything Messages writes to attributedBody is
# either this or an NSKeyedArchiver bplist.
TYPEDSTREAM_HEADER = b"\x04\x0bstreamtyped"
# Begin-value marker, then a one-character type-encoding string "+":
# typedstream's code for a length-prefixed byte array.
TYPEDSTREAM_BYTE_ARRAY = b"\x84\x01\x2b"


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


@dataclass(frozen=True)
class ImessageReadBatch:
    """One checkpoint-sized scan of chat.db.

    ``rows`` are messages to ingest. ``max_scanned_rowid`` is the highest
    ROWID looked at, including spam and empty bodies, so the sync checkpoint
    can advance past skipped junk instead of stalling on an all-spam page.
    """

    rows: list[Dict[str, Any]]
    max_scanned_rowid: Optional[int]
    records_skipped: int
    scanned_count: int


def _as_int_flag(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def row_is_imessage_spam(row: Dict[str, Any]) -> bool:
    """True when Apple has already labelled the message or its chat as junk.

    ``chat.is_filtered = 2`` is the Unknown Senders inbox. ``message.is_spam = 1``
    is Apple's per-message junk flag. Missing columns (older chat.db) are not spam.
    """
    if _as_int_flag(row.get("is_spam")) == APPLE_SPAM_FLAG:
        return True
    if _as_int_flag(row.get("is_filtered")) == UNKNOWN_SENDER_FILTERED:
        return True
    return False


def _normalize_sender_id(value: Any) -> Optional[str]:
    """Normalize sender identity from handle.id for storage."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _normalize_decoded_text(value: str) -> Optional[str]:
    """Clean a decoded attributed-string body into storable message text.

    U+FFFC (OBJECT REPLACEMENT CHARACTER) is the placeholder Messages writes
    where an attachment, sticker or inline preview sits. It carries no text, so
    it is stripped; a body that was *only* placeholders collapses to None and
    lets the caller fall through to `[attachment]`.
    """
    text = value.replace("￼", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.strip()
    return text or None


def _read_typedstream_int(buf: bytes, pos: int) -> tuple[Optional[int], int]:
    """Read one typedstream-encoded integer; return (value, next_pos).

    Small values are a single signed byte. Larger ones carry a width tag:
    0x81 -> 2 bytes, 0x82 -> 4 bytes, 0x83 -> 8 bytes, little-endian.
    """
    if pos >= len(buf):
        return None, pos
    tag = buf[pos]
    if tag == 0x81:
        width = 2
    elif tag == 0x82:
        width = 4
    elif tag == 0x83:
        width = 8
    else:
        return int.from_bytes(buf[pos:pos + 1], "little", signed=True), pos + 1
    end = pos + 1 + width
    if end > len(buf):
        return None, pos
    return int.from_bytes(buf[pos + 1:end], "little", signed=True), end


def _decode_typedstream_attributed_string(raw: bytes) -> Optional[str]:
    """Decode the backing string of an NSArchiver ("streamtyped") blob.

    Layout, confirmed against blobs produced by Apple's own NSArchiver:

        04 0b "streamtyped" <version> ... <class chain ending in "NSString">
        95 84 01 2b <length> <length bytes of UTF-8>

    `84 01 2b` is a fresh type-encoding string of one character, "+", which is
    the typedstream code for a byte array. The FIRST such value after the
    NSString class chain is NSAttributedString's backing store; every later one
    is an attribute key (`__kIMMessagePartAttributeName` and friends), which is
    why "longest printable run" is the wrong rule and this one is not.
    """
    if not raw.startswith(TYPEDSTREAM_HEADER):
        return None
    class_at = raw.find(b"NSString")
    if class_at < 0:
        return None
    anchor = raw.find(TYPEDSTREAM_BYTE_ARRAY, class_at)
    if anchor < 0:
        return None
    length, pos = _read_typedstream_int(raw, anchor + len(TYPEDSTREAM_BYTE_ARRAY))
    if length is None or length < 0 or pos + length > len(raw):
        return None
    payload = raw[pos:pos + length]
    try:
        # Strict, and UTF-8 only. typedstream's "+" byte array is always UTF-8,
        # and a lenient or utf-16 fallback here manufactures plausible-looking
        # CJK out of archive bytes -- garbage that reads as real text.
        return _normalize_decoded_text(payload.decode("utf-8"))
    except UnicodeDecodeError:
        logger.debug("attributedBody: typedstream payload not valid UTF-8 (%d bytes)", length)
        return None


def _decode_keyed_archive_attributed_string(raw: bytes) -> Optional[str]:
    """Decode the backing string of an NSKeyedArchiver ("bplist00") blob.

    The graph is fully specified, so this resolves it rather than scraping it:
    `$top.root` -> object -> its `NSString` UID -> that object's `NS.string`.
    Scraping instead returns the class table ("Z$classnameX$classesWNSValue"),
    which is what 459 rows on the owner's node were carrying.
    """
    try:
        plist = plistlib.loads(raw)
    except Exception:
        return None
    if not isinstance(plist, dict):
        return None
    objects = plist.get("$objects")
    top = plist.get("$top")
    if not isinstance(objects, list) or not isinstance(top, dict):
        return None

    def deref(ref: Any) -> Any:
        if isinstance(ref, plistlib.UID):
            index = int(ref)
            if 0 <= index < len(objects):
                return objects[index]
            return None
        return ref

    root = deref(top.get("root"))
    if isinstance(root, str):
        return _normalize_decoded_text(root)
    if not isinstance(root, dict):
        return None
    # NSAttributedString stores its text under "NSString"; a bare archived
    # NSString stores it under "NS.string".
    for key in ("NSString", "NS.string"):
        node = deref(root.get(key))
        if isinstance(node, str):
            return _normalize_decoded_text(node)
        if isinstance(node, dict):
            inner = deref(node.get("NS.string"))
            if isinstance(inner, str):
                return _normalize_decoded_text(inner)
    return None


def _extract_text_from_attributed_body(value: Any) -> Optional[str]:
    """Decode human text from an iMessage `attributedBody` blob.

    Both shapes Apple emits are decoded structurally. Anything else returns
    None so the caller can synthesize a body (`[attachment]`, `[reaction:N]`)
    rather than storing bytes. This deliberately has no byte-scraping fallback:
    scraping is what wrote `streamtyped` into 1,283 rows, class-table crumbs
    into 459 more, and a stray length byte onto the front of 1,980 otherwise
    intact messages -- 3,722 rows, 49% of the owner's iMessage corpus, on
    2026-08-28. `scripts/audit_imessage_body_decode.py` is the count.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return _normalize_decoded_text(value)
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return None
    raw = bytes(value)
    if raw.startswith(b"bplist00"):
        return _decode_keyed_archive_attributed_string(raw)
    if raw.startswith(TYPEDSTREAM_HEADER):
        return _decode_typedstream_attributed_string(raw)
    logger.debug("attributedBody: unrecognized archive header %r", raw[:16])
    return None

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


def read_imessage_batch(
    last_rowid: Optional[str] = None,
    chat_db_path: Optional[Path] = None,
    batch_size: int = 5000,
    start_unix: Optional[float] = None,
    exclude_spam: bool = True,
) -> ImessageReadBatch:
    """Copy chat.db, scan up to ``batch_size`` messages with ROWID > last_rowid.

    When ``exclude_spam`` is true (the default), Apple-filtered unknown-sender
    chats and junk-flagged messages are counted in ``records_skipped`` and not
    returned in ``rows``.
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
        except PermissionError:
            raise
    except Exception:
        if copy_path and os.path.exists(copy_path):
            try:
                os.unlink(copy_path)
            except OSError:
                pass
        raise

    kept: list[Dict[str, Any]] = []
    skipped = 0
    scanned = 0
    max_scanned_rowid: Optional[int] = None
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
                raw = last_rowid.split(":")[-1]
                try:
                    last = int(raw)
                except ValueError:
                    pass
            mac_start_seconds = None
            if start_unix is not None:
                mac_start_seconds = float(start_unix) - MAC_EPOCH_OFFSET
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
                       {_message_col_or_null("is_spam", "is_spam")},
                       message.date AS date,
                       message.handle_id AS handle_id,
                       message.is_from_me AS is_from_me,
                       handle.id AS sender_id,
                       chat.ROWID AS chat_id,
                       {_chat_col_or_null("guid", "chat_guid")},
                       {_chat_col_or_null("chat_identifier", "chat_identifier")},
                       {_chat_col_or_null("is_filtered", "is_filtered")}
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
                scanned += 1
                if rowid is not None and (max_scanned_rowid is None or rowid > max_scanned_rowid):
                    max_scanned_rowid = rowid
                if exclude_spam and row_is_imessage_spam(r):
                    skipped += 1
                    continue
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
                kept.append(out)
        finally:
            conn.close()
    finally:
        try:
            os.unlink(copy_path)
        except OSError:
            pass
    return ImessageReadBatch(
        rows=kept,
        max_scanned_rowid=max_scanned_rowid,
        records_skipped=skipped,
        scanned_count=scanned,
    )


def read_imessage_rows(
    last_rowid: Optional[str] = None,
    chat_db_path: Optional[Path] = None,
    batch_size: int = 5000,
    start_unix: Optional[float] = None,
    exclude_spam: bool = True,
) -> Iterator[Dict[str, Any]]:
    """
    Copy chat.db to a temp file (chunked to support >2GB), query messages with ROWID > last_rowid, yield rows as dicts.
    Each row has: id (imessage:ROWID), thread_id (str chat_id), content (text), created_at (Unix ts), role (user/other from is_from_me).
    Apple-filtered spam is omitted unless ``exclude_spam`` is false.
    """
    yield from read_imessage_batch(
        last_rowid=last_rowid,
        chat_db_path=chat_db_path,
        batch_size=batch_size,
        start_unix=start_unix,
        exclude_spam=exclude_spam,
    ).rows


def read_imessage_rows_list(
    last_rowid: Optional[str] = None,
    chat_db_path: Optional[Path] = None,
    batch_size: int = 5000,
    start_unix: Optional[float] = None,
    exclude_spam: bool = True,
) -> list[Dict[str, Any]]:
    """Convenience: consume iterator into a list."""
    return list(
        read_imessage_rows(
            last_rowid=last_rowid,
            chat_db_path=chat_db_path,
            batch_size=batch_size,
            start_unix=start_unix,
            exclude_spam=exclude_spam,
        )
    )
