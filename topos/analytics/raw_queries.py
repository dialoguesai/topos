from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..storage.raw.file_store import RawFileStore

logger = logging.getLogger("topos.analytics.raw_queries")


def _normalize_ts(value: Any) -> str:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    if isinstance(value, str):
        return value
    return ""


def _normalize_sender(payload: dict) -> str:
    role = (payload.get("role") or "").lower()
    if role:
        return "human" if role == "user" else role
    sender_type = payload.get("sender_type")
    return sender_type or "assistant"


def _message_from_payload(payload: dict, fallback_id: str, dataset_id: str) -> dict:
    created_at = payload.get("created_at") or payload.get("ts")
    out: Dict[str, Any] = {
        "message_id": payload.get("id") or payload.get("message_id") or fallback_id,
        "dataset_id": dataset_id,
        "sender_type": _normalize_sender(payload),
        "ts": _normalize_ts(created_at),
        "content": payload.get("content", ""),
    }
    if payload.get("source_id") is not None:
        out["source_id"] = str(payload["source_id"])
    return out


def _parse_ts_to_datetime(ts: str) -> Optional[datetime]:
    """Parse ISO-like ts string to datetime for comparison. Returns None if unparseable."""
    if not ts:
        return None
    try:
        if isinstance(ts, (int, float)):
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        s = str(ts).strip()
        if "T" in s:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _apply_filter_manifest_to_messages(
    messages: List[Dict[str, Any]],
    manifest: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Apply filter_manifest (rolling_window_days, date_range, source_filter) in Python. Stage 2b."""
    if not manifest or not isinstance(manifest, dict):
        return messages
    out: List[Dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    rolling_days: Optional[int] = None
    if manifest.get("rolling_window_days") is not None:
        try:
            rolling_days = max(0, int(manifest["rolling_window_days"]))
        except (TypeError, ValueError):
            pass
    range_start: Optional[datetime] = None
    if manifest.get("date_range_start"):
        range_start = _parse_ts_to_datetime(str(manifest["date_range_start"]))
    range_end: Optional[datetime] = None
    if manifest.get("date_range_end"):
        range_end = _parse_ts_to_datetime(str(manifest["date_range_end"]))
    source_allow: Optional[List[str]] = None
    if isinstance(manifest.get("source_filter"), list) and len(manifest["source_filter"]) > 0:
        source_allow = [str(s) for s in manifest["source_filter"]]
    for msg in messages:
        ts_str = msg.get("ts")
        dt = _parse_ts_to_datetime(ts_str) if ts_str else None
        if rolling_days is not None and dt is not None:
            if dt < now - timedelta(days=rolling_days):
                continue
        if range_start is not None and dt is not None and dt < range_start:
            continue
        if range_end is not None and dt is not None and dt > range_end:
            continue
        if source_allow is not None:
            sid = msg.get("source_id")
            if sid is not None and str(sid) not in source_allow:
                continue
        out.append(msg)
    return out


def load_raw_messages(
    *,
    dataset_id: str,
    schema_id: str,
    limit: Optional[int] = None,
    offset: int = 0,
    filter_manifest: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    file_store = RawFileStore()
    file_path = file_store.get_file_path(dataset_id, schema_id)
    logger.debug(
        "[PIPELINE:ANALYTICS] Loading raw messages: dataset_id=%s, schema_id=%s, file_path=%s, limit=%s, offset=%s",
        dataset_id,
        schema_id,
        file_path,
        limit,
        offset,
    )
    if not file_path.exists():
        logger.debug("[PIPELINE:ANALYTICS] Raw file does not exist: %s", file_path)
        return []
    messages: List[Dict[str, Any]] = []
    with Path(file_path).open("r", encoding="utf-8") as handle:
        for idx, line in enumerate(handle):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            messages.append(_message_from_payload(payload, str(idx + 1), dataset_id))
    messages = _apply_filter_manifest_to_messages(messages, filter_manifest)
    if offset:
        messages = messages[offset:]
    if limit is not None:
        messages = messages[:limit]
    logger.debug(
        "[PIPELINE:ANALYTICS] Loaded %d messages (after limit/offset)",
        len(messages),
    )
    return messages


def messages_per_day(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counts: dict[str, int] = defaultdict(int)
    for message in messages:
        ts = message.get("ts") or ""
        if ts:
            day = ts.split("T", 1)[0]
            counts[day] += 1
    return [{"day": day, "count": counts[day]} for day in sorted(counts.keys())]


def total_messages(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"total_messages": len(messages)}


def messages_by_sender(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    counts = Counter(msg.get("sender_type") or "unknown" for msg in messages)
    return [{"sender_type": sender, "count": count} for sender, count in counts.most_common()]


def avg_message_length(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not messages:
        return {"avg_length": 0.0, "min_length": 0, "max_length": 0}
    lengths = [len(msg.get("content") or "") for msg in messages]
    return {
        "avg_length": float(sum(lengths)) / len(lengths),
        "min_length": min(lengths),
        "max_length": max(lengths),
    }
