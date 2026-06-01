"""Signal Desktop DB reader: open SQLCipher DB, query messages since checkpoint.

Requires pysqlcipher3 (pip install pysqlcipher3). Key from ~/Library/Application Support/Signal/config.json.
"""

from __future__ import annotations

import base64
import importlib
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("topos.ingestion.sources.signal_reader")

DEFAULT_SIGNAL_DIR = Path.home() / "Library" / "Application Support" / "Signal"
DEFAULT_DB_PATH = DEFAULT_SIGNAL_DIR / "sql" / "db.sqlite"
DEFAULT_CONFIG_PATH = DEFAULT_SIGNAL_DIR / "config.json"


def get_signal_paths() -> tuple[Path, Path]:
    """Return (config_path, db_path). Override with env SIGNAL_CONFIG_PATH, SIGNAL_DB_PATH."""
    config = Path(os.environ.get("SIGNAL_CONFIG_PATH", str(DEFAULT_CONFIG_PATH)))
    db = Path(os.environ.get("SIGNAL_DB_PATH", str(DEFAULT_DB_PATH)))
    return config, db


def _normalize_hex_key(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if stripped.startswith("0x"):
        stripped = stripped[2:]
    if stripped.startswith("x'") and stripped.endswith("'") and len(stripped) >= 4:
        stripped = stripped[2:-1]
    if re.fullmatch(r"[0-9a-fA-F]+", stripped):
        return stripped
    return None


def _get_macos_safe_storage_password() -> Optional[str]:
    """Best-effort retrieval of Signal Safe Storage password from Keychain."""
    if sys.platform != "darwin":
        return None
    services = [
        "Signal Safe Storage",
        "Signal",
    ]
    for service in services:
        try:
            proc = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-w"],
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                return proc.stdout.strip()
        except Exception:
            continue
    return None


def _decrypt_signal_encrypted_key(encrypted_key: str) -> Optional[str]:
    """Decrypt Electron safeStorage encryptedKey on macOS.

    Signal Desktop stores encryptedKey via Electron safeStorage. On macOS, this
    can be decrypted with the "Signal Safe Storage" keychain secret.
    """
    if sys.platform != "darwin":
        return None

    safe_storage_password = _get_macos_safe_storage_password()
    if not safe_storage_password:
        logger.warning("Signal encryptedKey present but Safe Storage password was not found in Keychain")
        return None

    raw: Optional[bytes] = None
    try:
        raw = base64.b64decode(encrypted_key)
    except Exception:
        try:
            raw = bytes.fromhex(encrypted_key)
        except Exception:
            raw = None
    if not raw:
        logger.warning("Signal encryptedKey format is not base64/hex-decodable")
        return None

    if raw.startswith(b"v10"):
        raw = raw[3:]
    if not raw:
        return None

    try:
        backends_mod = importlib.import_module("cryptography.hazmat.backends")
        primitives_mod = importlib.import_module("cryptography.hazmat.primitives")
        ciphers_mod = importlib.import_module("cryptography.hazmat.primitives.ciphers")
        pbkdf2_mod = importlib.import_module("cryptography.hazmat.primitives.kdf.pbkdf2")
        default_backend = getattr(backends_mod, "default_backend")
        hashes = getattr(primitives_mod, "hashes")
        Cipher = getattr(ciphers_mod, "Cipher")
        algorithms = getattr(ciphers_mod, "algorithms")
        modes = getattr(ciphers_mod, "modes")
        PBKDF2HMAC = getattr(pbkdf2_mod, "PBKDF2HMAC")
    except Exception as e:
        logger.warning("cryptography import failed for Signal encryptedKey decrypt: %s", e)
        return None

    try:
        # Electron/Chromium OSCrypt compatibility (macOS).
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA1(),
            length=16,
            salt=b"saltysalt",
            iterations=1003,
            backend=default_backend(),
        )
        aes_key = kdf.derive(safe_storage_password.encode("utf-8"))
        cipher = Cipher(algorithms.AES(aes_key), modes.CBC(b" " * 16), backend=default_backend())
        decryptor = cipher.decryptor()
        plaintext = decryptor.update(raw) + decryptor.finalize()
    except Exception as e:
        logger.warning("Signal encryptedKey decryption failed: %s", e)
        return None

    # PKCS#7 unpadding
    if plaintext:
        pad_len = plaintext[-1]
        if 1 <= pad_len <= 16 and plaintext.endswith(bytes([pad_len]) * pad_len):
            plaintext = plaintext[:-pad_len]

    # First try direct text forms.
    text = plaintext.decode("utf-8", errors="ignore").strip().strip("\x00")
    normalized = _normalize_hex_key(text)
    if normalized:
        return normalized

    # Some builds may store binary key material; fall back to hex-encoding bytes.
    binary_hex = plaintext.hex()
    if binary_hex:
        return binary_hex
    return None


def get_signal_key(config_path: Optional[Path] = None, preferred_hex_key: Optional[str] = None) -> Optional[str]:
    """Read raw SQLCipher key from Signal config.json.

    Note: `encryptedKey` is not a raw SQLCipher key and cannot be used directly.
    """
    if isinstance(preferred_hex_key, str) and preferred_hex_key.strip():
        normalized_preferred_key = _normalize_hex_key(preferred_hex_key)
        if normalized_preferred_key:
            return normalized_preferred_key
        logger.warning("Preferred Signal sync key was provided but is not hex-formatted")

    env_key = os.environ.get("SIGNAL_KEY_HEX") or os.environ.get("SIGNAL_SQLCIPHER_KEY")
    if isinstance(env_key, str) and env_key.strip():
        normalized_env_key = _normalize_hex_key(env_key)
        if normalized_env_key:
            return normalized_env_key
        logger.warning("SIGNAL_KEY_HEX/SIGNAL_SQLCIPHER_KEY is set but is not hex-formatted")

    config_path = config_path or get_signal_paths()[0]
    if not config_path.exists():
        return None
    try:
        with open(config_path, encoding="utf-8") as f:
            data = json.load(f)
        key = _normalize_hex_key(data.get("key"))
        if key:
            return key
        if isinstance(data.get("key"), str):
            logger.warning("Signal config key exists but is not hex-formatted")

        encrypted_key = data.get("encryptedKey")
        if isinstance(encrypted_key, str) and encrypted_key.strip():
            decrypted = _decrypt_signal_encrypted_key(encrypted_key.strip())
            if decrypted:
                logger.info("Signal encryptedKey decrypted via Keychain")
                return decrypted
            logger.warning("Signal config has encryptedKey but decryption failed")
        return None
    except Exception as e:
        logger.warning("get_signal_key failed: %s", e)
        return None


def _normalize_signal_ts_seconds(value: Any) -> Optional[float]:
    """Normalize Signal timestamp values to Unix seconds."""
    if value is None:
        return None
    try:
        ts = float(value)
    except Exception:
        return None
    abs_ts = abs(ts)
    if abs_ts >= 1e17:
        ts = ts / 1_000_000_000.0
    elif abs_ts >= 1e14:
        ts = ts / 1_000_000.0
    elif abs_ts >= 1e11:
        ts = ts / 1_000.0
    return ts


def _normalize_signal_sender_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _safe_json_loads(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_reply_from_signal_json(payload: Dict[str, Any]) -> tuple[Optional[str], Dict[str, Any]]:
    """Extract reply linkage from Signal JSON payload shape variants."""
    metadata: Dict[str, Any] = {}
    reply_to: Optional[str] = None

    for key in ("replyToMessageId", "reply_to_message_id", "quotedMessageId", "quoteId"):
        if payload.get(key) is not None:
            reply_to = str(payload.get(key))
            metadata[key] = payload.get(key)
            break

    quote = payload.get("quote")
    if isinstance(quote, dict):
        metadata["quote"] = quote
        if reply_to is None:
            for key in ("id", "messageId", "message_id", "targetMessageId"):
                if quote.get(key) is not None:
                    reply_to = str(quote.get(key))
                    break

    story_ctx = payload.get("storyReplyContext")
    if isinstance(story_ctx, dict):
        metadata["storyReplyContext"] = story_ctx
        if reply_to is None:
            for key in ("messageId", "message_id", "targetMessageId"):
                if story_ctx.get(key) is not None:
                    reply_to = str(story_ctx.get(key))
                    break

    return reply_to, metadata


def read_signal_rows(
    last_record_id: Optional[str] = None,
    config_path: Optional[Path] = None,
    db_path: Optional[Path] = None,
    my_phone_number: Optional[str] = None,
    batch_size: int = 5000,
    start_unix: Optional[float] = None,
    signal_key_hex: Optional[str] = None,
) -> list[Dict[str, Any]]:
    """
    Open Signal SQLCipher DB and return message rows since last_record_id.
    Each row: id (signal:{id}), thread_id (conversationId), content (body), created_at (Unix), role (user/other from type), ROWID/id.
    """
    try:
        from pysqlcipher3 import dbapi2 as sqlcipher
    except ImportError as e:
        raise ImportError(
            "pysqlcipher3 required for Signal sync. Install with: pip install pysqlcipher3"
        ) from e

    config_path, db_path = config_path or get_signal_paths()[0], db_path or get_signal_paths()[1]
    if not db_path.exists():
        raise FileNotFoundError(f"Signal DB not found at {db_path}")
    key = get_signal_key(config_path, preferred_hex_key=signal_key_hex)
    if not key:
        raise ValueError(
            "Signal SQLCipher key unavailable. Could not resolve raw key from config.json "
            "or macOS Keychain. Workaround: set SIGNAL_KEY_HEX to a raw SQLCipher hex key."
        )

    key_hex_expr = f"x'{key}'"

    conn = None
    last_open_error: Optional[str] = None
    # Different Signal versions/DBs can require different compatibility modes.
    for compat in (4, 3):
        try:
            candidate = sqlcipher.connect(str(db_path))
            if compat is not None:
                candidate.execute(f"PRAGMA cipher_compatibility = {compat}")
            candidate.execute(f'PRAGMA key = "{key_hex_expr}"')
            candidate.execute("SELECT count(*) FROM sqlite_master")
            conn = candidate
            break
        except Exception as e:
            last_open_error = str(e)
            try:
                candidate.close()
            except Exception:
                pass
            continue

    if conn is None:
        raise ValueError(
            f"Unable to open Signal DB at {db_path} with available SQLCipher settings: "
            f"{last_open_error or 'unknown error'}"
        )

    conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))
    try:
        # Signal Desktop schema varies by version; detect available columns first.
        table_info_rows = conn.execute("PRAGMA table_info(messages)").fetchall()
        available_columns = {str(row.get("name") or "") for row in table_info_rows}

        if "id" not in available_columns or "body" not in available_columns or "sent_at" not in available_columns:
            raise ValueError(
                "Signal messages table is missing required columns (id/body/sent_at). "
                f"Available columns: {sorted(c for c in available_columns if c)}"
            )

        conversation_col = "conversationId" if "conversationId" in available_columns else (
            "conversation_id" if "conversation_id" in available_columns else None
        )
        if conversation_col is None:
            raise ValueError(
                "Signal messages table is missing conversation column (conversationId/conversation_id). "
                f"Available columns: {sorted(c for c in available_columns if c)}"
            )

        sender_cols = [c for c in ("sourceServiceId", "sourceUuid", "source") if c in available_columns]
        sender_select = ", " + ", ".join(sender_cols) if sender_cols else ""
        reply_cols = [
            c
            for c in (
                "quoteId",
                "quotedMessageId",
                "replyToMessageId",
                "reply_to_message_id",
                "quoteAuthorAci",
                "quoteAuthorUuid",
                "quoteAuthor",
                "quoteText",
                "quoteBody",
                "storyReplyContext",
            )
            if c in available_columns
        ]
        reply_select = ", " + ", ".join(reply_cols) if reply_cols else ""
        system_cols = [
            c
            for c in (
                "groupV2Change",
                "groupUpdate",
                "groupChange",
                "callId",
                "callHistoryDetails",
                "expiresTimer",
                "expirationStartTimestamp",
                "isErased",
                "isViewOnce",
                "isStory",
            )
            if c in available_columns
        ]
        system_select = ", " + ", ".join(system_cols) if system_cols else ""
        json_cols = [c for c in ("json", "messageJson", "payload_json") if c in available_columns]
        json_select = ", " + ", ".join(json_cols) if json_cols else ""

        last_ts: float = 0.0
        if last_record_id:
            parts = last_record_id.split(":")
            if len(parts) >= 3 and parts[0] == "signal":
                try:
                    last_ts = float(parts[2])
                except ValueError:
                    pass
        # Query-side normalization converts sent_at to milliseconds.
        last_ts_ms = float(last_ts) * 1000.0

        start_ms: Optional[int] = None
        if start_unix is not None:
            try:
                start_ms = int(float(start_unix) * 1000.0)
            except Exception:
                start_ms = None

        # Signal Desktop: read only columns that exist in this schema variant.
        normalized_sent_at_expr = """
            CASE
                WHEN abs(sent_at) >= 100000000000000000 THEN (sent_at / 1000000.0)
                WHEN abs(sent_at) >= 100000000000000 THEN (sent_at / 1000.0)
                WHEN abs(sent_at) >= 100000000000 THEN (sent_at * 1.0)
                ELSE (sent_at * 1000.0)
            END
        """

        query = f"""
            SELECT id, body, sent_at, type, {conversation_col} AS conversation_id{sender_select}{reply_select}{system_select}{json_select}
            FROM messages
            WHERE ({normalized_sent_at_expr}) > ?
              AND (
                    ? IS NULL
                    OR ({normalized_sent_at_expr}) >= ?
                  )
            ORDER BY sent_at
            LIMIT ?
        """
        cursor = conn.execute(query, (last_ts_ms, start_ms, start_ms, batch_size))
        rows = cursor.fetchall()
        out = []
        for r in rows:
            msg_id = r.get("id") or r.get("rowid")
            sent_at_seconds = _normalize_signal_ts_seconds(r.get("sent_at"))
            sent_at = sent_at_seconds if sent_at_seconds is not None else 0
            msg_type = (r.get("type") or "").lower()
            role = "user" if msg_type == "outgoing" else "other"
            message_type = "system" if msg_type not in {"outgoing", "incoming"} else "message"
            event_type = f"signal_type:{msg_type}" if message_type == "system" and msg_type else None
            sender_id = _normalize_signal_sender_id(next((r.get(c) for c in sender_cols if r.get(c)), None))
            if role == "user":
                sender_id = "self"
            if not sender_id:
                sender_id = f"unknown:{msg_id}"
            reply_to_message_id = next(
                (
                    str(r.get(c))
                    for c in ("quoteId", "quotedMessageId", "replyToMessageId", "reply_to_message_id")
                    if c in reply_cols and r.get(c)
                ),
                None,
            )
            content = (r.get("body") or "").strip()
            if not content and message_type == "system":
                content = f"[system_event:{msg_type or 'signal'}]"

            metadata: Dict[str, Any] = {}
            for c in reply_cols:
                if r.get(c) is not None:
                    metadata[c] = r.get(c)
            for c in system_cols:
                if r.get(c) is not None:
                    metadata[c] = r.get(c)

            # Many Signal Desktop builds keep reply context in JSON payload instead of dedicated columns.
            json_payload = None
            for c in json_cols:
                parsed = _safe_json_loads(r.get(c))
                if parsed:
                    json_payload = parsed
                    break
            if json_payload:
                json_reply_to, json_reply_meta = _extract_reply_from_signal_json(json_payload)
                if reply_to_message_id is None and json_reply_to is not None:
                    reply_to_message_id = json_reply_to
                metadata.update(json_reply_meta)
            row_out = {
                "id": f"signal:{msg_id}:{int(sent_at)}",
                "thread_id": str(r.get("conversation_id") or ""),
                "content": content,
                "created_at": sent_at,
                "role": role,
                "sender_id": sender_id,
                "message_type": message_type,
                "event_type": event_type,
                "reply_to_message_id": reply_to_message_id,
                "ROWID": msg_id,
                "sent_at": sent_at,
            }
            if metadata:
                row_out["_metadata"] = metadata
            out.append(row_out)
        return out
    finally:
        conn.close()
