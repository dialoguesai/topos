"""YouTube archive / session transcript parser.

Accepts either a ``yt_transcript_archive`` v2 object or an already-shaped
``transcript.session.v1`` payload. Connector-supplied role fields
(``participation_mode``, ``is_self``, ``is_from_self``, ``is_owner``,
``actor_role``) are dropped — transcripts are ambient unless the owner later
tags them. A ``participants`` list is kept as name hints only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from ..sources.base import RawRecord
from ..validation.base import ValidationResult
from .base import NormalizedRecord, Parser

_DROPPED_ROLE_KEYS = (
    "participation_mode",
    "is_self",
    "is_from_self",
    "is_owner",
    "actor_role",
)

# YouTube lines overlap by ~2s; a gap above this is a real pause.
STITCH_GAP_SEC = 1.0
# Hard brakes so unpunctuated auto-captions do not become one blob.
MAX_UTTERANCE_SEC = 18.0
MAX_UTTERANCE_CHARS = 360
MAX_UTTERANCE_LINES = 6

_SENTENCE_END_RE = re.compile(r"""[.!?…]["')\]]*\s*$""")
_SOUND_MARKER_RE = re.compile(
    r"^\[(?:music|applause|laughter|sound|inaudible|silence|cheering)[^\]]*\]$",
    re.IGNORECASE,
)


def _youtube_video_id(url_or_id: str) -> str:
    raw = str(url_or_id or "").strip()
    if not raw:
        return ""
    if len(raw) == 11 and "/" not in raw and " " not in raw:
        return raw
    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    if "youtu.be" in host:
        return parsed.path.lstrip("/").split("/")[0]
    if "youtube" in host:
        qs = parse_qs(parsed.query)
        if qs.get("v"):
            return str(qs["v"][0])
        parts = [p for p in parsed.path.split("/") if p]
        if parts and parts[0] in ("embed", "shorts", "live", "v") and len(parts) > 1:
            return parts[1]
    return ""


def _strip_role_fields(obj: Any) -> Any:
    if isinstance(obj, list):
        return [_strip_role_fields(item) for item in obj]
    if not isinstance(obj, dict):
        return obj
    cleaned = {k: _strip_role_fields(v) for k, v in obj.items() if k not in _DROPPED_ROLE_KEYS}
    return cleaned


def _asr_from_meta(meta: Dict[str, Any], explicit_model: Any, explicit_quality: Any) -> tuple[str, str, int]:
    is_generated = meta.get("is_generated")
    if explicit_quality in ("human", "generated", "unknown"):
        quality = str(explicit_quality)
    elif is_generated is True:
        quality = "generated"
    elif is_generated is False:
        quality = "human"
    else:
        quality = "unknown"
    model = str(explicit_model or "").strip()
    if not model:
        if quality == "generated":
            model = "youtube-auto"
        elif quality == "human":
            model = "youtube-manual"
        else:
            model = ""
    generated_int = 1 if quality == "generated" or is_generated is True else 0
    if quality == "unknown" and is_generated is None:
        generated_int = 0
    return model, quality, generated_int


def _items_from_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    for key in ("items", "segments", "captions"):
        raw = payload.get(key)
        if isinstance(raw, list):
            return [item for item in raw if isinstance(item, dict)]
    return []


def _item_text(item: Dict[str, Any]) -> str:
    return str(item.get("text") or item.get("content") or item.get("caption") or "").strip()


def _item_start(item: Dict[str, Any]) -> float:
    raw = item.get("start")
    if raw is None:
        raw = item.get("start_sec")
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def _item_duration(item: Dict[str, Any]) -> float:
    raw = item.get("duration")
    if raw is None:
        raw = item.get("duration_sec")
    try:
        return float(raw) if raw is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _item_speaker(item: Dict[str, Any]) -> str:
    return str(
        item.get("speaker_label") or item.get("speaker") or item.get("speaker_id") or ""
    ).strip()


def _ends_sentence(text: str) -> bool:
    return bool(_SENTENCE_END_RE.search(text))


def _is_sound_marker(text: str) -> bool:
    return bool(_SOUND_MARKER_RE.match(text.strip()))


def _join_caption_text(left: str, right: str) -> str:
    left = left.rstrip()
    right = right.lstrip()
    if not left:
        return right
    if not right:
        return left
    if right[0] in ",.;:!?":
        return f"{left}{right}"
    return f"{left} {right}"


def stitch_caption_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Coalesce timed caption fragments into utterance-sized items.

    YouTube archives arrive as overlapping one-clause lines. Downstream
    co-occurrence is same-record only, so ``generation of OpenAI`` +
    ``called Astra`` must become one item here — before canonicalize —
    or they never share a row. This is caption-format specific; do not
    move it into the entity graph rebuild.
    """
    cleaned = [item for item in items if isinstance(item, dict) and _item_text(item)]
    if not cleaned:
        return []

    def _open(item: Dict[str, Any]) -> Dict[str, Any]:
        start = _item_start(item)
        duration = _item_duration(item)
        return {
            "first": dict(item),
            "text": _item_text(item),
            "start": start,
            "end": start + duration,
            "speaker": _item_speaker(item),
            "lines": 1,
        }

    def _can_extend(group: Dict[str, Any], item: Dict[str, Any]) -> bool:
        nxt = _item_text(item)
        if not nxt or _is_sound_marker(nxt) or _is_sound_marker(group["text"]):
            return False
        if _ends_sentence(group["text"]):
            return False
        nxt_speaker = _item_speaker(item)
        if group["speaker"] and nxt_speaker and group["speaker"] != nxt_speaker:
            return False
        nxt_start = _item_start(item)
        if nxt_start - group["end"] > STITCH_GAP_SEC:
            return False
        nxt_end = nxt_start + _item_duration(item)
        span = max(nxt_end, group["end"]) - group["start"]
        if span > MAX_UTTERANCE_SEC:
            return False
        joined = _join_caption_text(group["text"], nxt)
        if len(joined) > MAX_UTTERANCE_CHARS:
            return False
        if group["lines"] >= MAX_UTTERANCE_LINES:
            return False
        return True

    def _extend(group: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
        nxt_start = _item_start(item)
        nxt_end = nxt_start + _item_duration(item)
        speaker = group["speaker"] or _item_speaker(item)
        return {
            **group,
            "text": _join_caption_text(group["text"], _item_text(item)),
            "end": max(group["end"], nxt_end),
            "speaker": speaker,
            "lines": group["lines"] + 1,
        }

    def _close(group: Dict[str, Any]) -> Dict[str, Any]:
        first = dict(group["first"])
        first["text"] = group["text"]
        first["start"] = group["start"]
        first["duration"] = max(0.0, group["end"] - group["start"])
        first.pop("start_sec", None)
        first.pop("duration_sec", None)
        first.pop("content", None)
        first.pop("caption", None)
        if group["speaker"]:
            first["speaker_label"] = group["speaker"]
        first["stitched_lines"] = group["lines"]
        return first

    out: List[Dict[str, Any]] = []
    current = _open(cleaned[0])
    for item in cleaned[1:]:
        if _can_extend(current, item):
            current = _extend(current, item)
        else:
            out.append(_close(current))
            current = _open(item)
    out.append(_close(current))
    return out


def shape_transcript_session(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a YouTube archive or session payload; drop connector role fields."""
    src = _strip_role_fields(dict(payload))
    if not isinstance(src, dict):
        src = {}
    meta = src.get("transcript_meta") if isinstance(src.get("transcript_meta"), dict) else {}
    video = src.get("video") if isinstance(src.get("video"), dict) else {}

    video_id = str(src.get("video_id") or video.get("video_id") or "").strip()
    origin_url = str(
        src.get("origin_url") or video.get("canonical_url") or src.get("url") or ""
    ).strip()
    if not video_id:
        video_id = _youtube_video_id(origin_url)

    transcript_id = str(src.get("transcript_id") or "").strip()
    if not transcript_id and video_id:
        transcript_id = f"yt:{video_id}"
    if not transcript_id:
        transcript_id = str(src.get("id") or src.get("record_id") or "").strip()

    title = str(
        src.get("title") or video.get("title") or video_id or transcript_id or ""
    ).strip()

    origin_kind = str(src.get("origin_kind") or "").strip()
    if not origin_kind:
        if str(src.get("schema") or "") == "yt_transcript_archive" or video_id or "youtu" in origin_url:
            origin_kind = "youtube"
        else:
            origin_kind = "other"

    started_at = str(
        src.get("started_at")
        or video.get("published_at")
        or src.get("fetched_at")
        or ""
    ).strip() or None

    duration = src.get("duration_sec")
    if duration is None:
        duration = meta.get("total_duration_sec")
    if duration is None:
        duration = video.get("duration_sec")

    language_code = str(
        src.get("language_code") or meta.get("language_code") or ""
    ).strip() or None

    asr_model, asr_quality, is_generated = _asr_from_meta(
        meta, src.get("asr_model"), src.get("asr_quality")
    )

    participants: List[Dict[str, Any]] = []
    raw_participants = src.get("participants")
    if isinstance(raw_participants, list):
        for person in raw_participants:
            if isinstance(person, str) and person.strip():
                participants.append({"name": person.strip()})
            elif isinstance(person, dict):
                name = str(person.get("name") or person.get("display_name") or "").strip()
                if name:
                    participants.append({"name": name})

    items = stitch_caption_items(_items_from_payload(src))
    if transcript_id:
        seen_ids: set[str] = set()
        for item in items:
            if item.get("segment_id"):
                seen_ids.add(str(item["segment_id"]))
                continue
            start_ms = int(round(_item_start(item) * 1000))
            segment_id = f"{transcript_id}:{start_ms}"
            suffix = 0
            while segment_id in seen_ids:
                suffix += 1
                segment_id = f"{transcript_id}:{start_ms}:{suffix}"
            item["segment_id"] = segment_id
            seen_ids.add(segment_id)

    shaped: Dict[str, Any] = {
        "transcript_id": transcript_id,
        "title": title or None,
        "origin_url": origin_url or None,
        "origin_kind": origin_kind,
        "started_at": started_at,
        "duration_sec": duration,
        "language_code": language_code,
        "asr_model": asr_model or None,
        "asr_quality": asr_quality,
        "is_generated": is_generated,
        "media_ref": src.get("media_ref"),
        "participants": participants,
        "items": items,
        "record_id": transcript_id,
    }
    if video_id:
        shaped["video_id"] = video_id
    return {k: v for k, v in shaped.items() if v is not None}


@dataclass
class TranscriptSessionParser(Parser):
    dataset_id: str
    _schema_id: str = "transcript.session.v1"

    def parse(self, raw: RawRecord) -> NormalizedRecord:
        payload = raw.payload if isinstance(raw.payload, dict) else {}
        shaped = shape_transcript_session(payload)
        record_id = str(shaped.get("transcript_id") or raw.record_id)
        shaped["transcript_id"] = record_id
        shaped["record_id"] = record_id
        shaped["dataset_id"] = self.dataset_id
        return NormalizedRecord(record_id=record_id, payload=shaped)

    def validate(self, record: RawRecord) -> ValidationResult:
        payload = record.payload
        if not isinstance(payload, dict):
            return ValidationResult(is_valid=False, errors=["Record must be a dict"], metadata={})
        has_id = bool(
            str(
                payload.get("transcript_id")
                or payload.get("video_id")
                or payload.get("origin_url")
                or payload.get("id")
                or ""
            ).strip()
        )
        items = _items_from_payload(payload)
        if not has_id:
            return ValidationResult(
                is_valid=False,
                errors=["Missing required field: transcript_id, video_id, or origin_url"],
                metadata={},
            )
        if not items:
            return ValidationResult(
                is_valid=False, errors=["Missing required field: items"], metadata={}
            )
        return ValidationResult(is_valid=True, errors=[], metadata={})

    def schema_id(self) -> str:
        return self._schema_id
