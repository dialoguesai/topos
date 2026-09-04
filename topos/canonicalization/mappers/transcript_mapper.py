"""Session-shaped transcript → transcripts / speakers / segments.

``map_many`` fans one normalized session into a header row, optional roster
rows from owner-supplied ``participants``, and one segment per caption item.

Attribution is fail-closed:
- ``participation_mode`` is always ``ambient``
- every segment is ``actor_role=ambient``, ``is_from_self=0``
- roster names never set ``is_owner`` or ``contact_id``
- unlabeled captions stay unlabeled (roster is a prior, not diarization)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from ...ingestion.parsers.base import NormalizedRecord
from .base import CanonicalMapper, CanonicalRecord, MappingMetadata

_AMBIENT = "ambient"


def _parse_started_at(raw: Any) -> Optional[datetime]:
    text = str(raw or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _event_at(started_at: Optional[datetime], start_sec: Any) -> Optional[str]:
    if started_at is None:
        return None
    try:
        offset = float(start_sec or 0)
    except (TypeError, ValueError):
        offset = 0.0
    return (started_at + timedelta(seconds=offset)).isoformat()


def _item_text(item: Dict[str, Any]) -> str:
    return str(item.get("text") or item.get("content") or item.get("caption") or "").strip()


def _item_speaker_label(item: Dict[str, Any]) -> str:
    return str(
        item.get("speaker_label") or item.get("speaker") or item.get("speaker_id") or ""
    ).strip()


@dataclass
class TranscriptCanonicalMapper(CanonicalMapper):
    version: str = "v1"

    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        header = self._header(normalized)
        return CanonicalRecord(
            record_id=str(header["transcript_id"]),
            payload=header,
            table="transcripts",
        )

    def map_many(self, normalized: NormalizedRecord) -> List[CanonicalRecord]:
        p = normalized.payload if isinstance(normalized.payload, dict) else {}
        header = self._header(normalized)
        transcript_id = str(header["transcript_id"])
        dataset_id = p.get("dataset_id")
        started = _parse_started_at(header.get("started_at"))
        out: List[CanonicalRecord] = [
            CanonicalRecord(record_id=transcript_id, payload=header, table="transcripts")
        ]

        label_to_speaker: Dict[str, str] = {}
        participants = p.get("participants") if isinstance(p.get("participants"), list) else []
        for index, person in enumerate(participants):
            if not isinstance(person, dict):
                continue
            name = str(person.get("name") or "").strip()
            if not name:
                continue
            speaker_id = f"{transcript_id}:roster:{index}"
            out.append(
                CanonicalRecord(
                    record_id=speaker_id,
                    payload={
                        "speaker_id": speaker_id,
                        "transcript_id": transcript_id,
                        "dataset_id": dataset_id,
                        "label": name,
                        "display_name": name,
                        "contact_id": None,
                        "is_owner": 0,
                        "attribution_source": "owner_roster",
                        "attribution_confidence": 1.0,
                        "source_record_id": speaker_id,
                    },
                    table="transcript_speakers",
                )
            )

        items = p.get("items") if isinstance(p.get("items"), list) else []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            text = _item_text(item)
            if not text:
                continue
            label = _item_speaker_label(item)
            speaker_id = None
            if label:
                speaker_id = label_to_speaker.get(label)
                if speaker_id is None:
                    speaker_id = f"{transcript_id}:label:{len(label_to_speaker)}"
                    label_to_speaker[label] = speaker_id
                    out.append(
                        CanonicalRecord(
                            record_id=speaker_id,
                            payload={
                                "speaker_id": speaker_id,
                                "transcript_id": transcript_id,
                                "dataset_id": dataset_id,
                                "label": label,
                                "display_name": None,
                                "contact_id": None,
                                "is_owner": 0,
                                "attribution_source": "source_label",
                                "attribution_confidence": 0.0,
                                "source_record_id": speaker_id,
                            },
                            table="transcript_speakers",
                        )
                    )
            try:
                start_sec = float(item.get("start") if item.get("start") is not None else item.get("start_sec") or 0)
            except (TypeError, ValueError):
                start_sec = 0.0
            try:
                duration_sec = item.get("duration")
                if duration_sec is None:
                    duration_sec = item.get("duration_sec")
                duration_sec = float(duration_sec) if duration_sec is not None else None
            except (TypeError, ValueError):
                duration_sec = None
            segment_id = str(item.get("segment_id") or f"{transcript_id}:{index}")
            asr_confidence = item.get("asr_confidence")
            try:
                asr_confidence = float(asr_confidence) if asr_confidence is not None else None
            except (TypeError, ValueError):
                asr_confidence = None
            out.append(
                CanonicalRecord(
                    record_id=segment_id,
                    payload={
                        "segment_id": segment_id,
                        "transcript_id": transcript_id,
                        "dataset_id": dataset_id,
                        "speaker_id": speaker_id,
                        "speaker_label": label or None,
                        "content": text,
                        "start_sec": start_sec,
                        "duration_sec": duration_sec,
                        "event_at": _event_at(started, start_sec),
                        "actor_role": _AMBIENT,
                        "is_from_self": 0,
                        "asr_confidence": asr_confidence,
                        "source_record_id": segment_id,
                    },
                    table="transcript_segments",
                )
            )
        return out

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        return MappingMetadata(source_id="transcript", mapping_version=self.version)

    def _header(self, normalized: NormalizedRecord) -> Dict[str, Any]:
        p = normalized.payload if isinstance(normalized.payload, dict) else {}
        transcript_id = str(p.get("transcript_id") or normalized.record_id)
        return {
            "transcript_id": transcript_id,
            "dataset_id": p.get("dataset_id"),
            "title": p.get("title"),
            "origin_url": p.get("origin_url"),
            "origin_kind": p.get("origin_kind") or "other",
            "started_at": p.get("started_at"),
            "ended_at": p.get("ended_at"),
            "duration_sec": p.get("duration_sec"),
            "language_code": p.get("language_code"),
            "asr_model": p.get("asr_model"),
            "asr_quality": p.get("asr_quality") or "unknown",
            "is_generated": p.get("is_generated"),
            "media_ref": p.get("media_ref"),
            "participation_mode": _AMBIENT,
            "source_record_id": transcript_id,
        }
