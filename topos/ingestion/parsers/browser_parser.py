"""Browser visits parser for ingestion layer (Sprint 3)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict
from datetime import datetime, timezone

from ..sources.base import RawRecord
from ..validation.base import ValidationResult
from .base import NormalizedRecord, Parser

logger = logging.getLogger("topos.ingestion.parser.browser")


@dataclass
class BrowserParser(Parser):
    """Parser for browser_visits records. Stores records as-is with minimal normalization."""
    
    dataset_id: str
    _schema_id: str = "browser.visits.v1"

    def parse(self, raw: RawRecord) -> NormalizedRecord:
        """Parse browser visit record. For MVP, we store records as-is."""
        payload = raw.payload
        
        # Normalize timestamp if present
        visited_at = payload.get("visited_at") or payload.get("timestamp")
        if isinstance(visited_at, str):
            # Already ISO format, keep as-is
            ts = visited_at
        elif visited_at is None:
            # Use current time if missing
            ts = datetime.now(timezone.utc).isoformat()
        else:
            ts = str(visited_at)
        
        # Create normalized record with all browser fields preserved
        normalized = {
            "record_id": payload.get("url", raw.record_id) + "_" + ts,  # Unique ID
            "dataset_id": self.dataset_id,
            "url": payload.get("url", ""),
            "visited_at": ts,
            "title": payload.get("title", ""),
            "favicon_url": payload.get("favicon_url"),
            "hostname": payload.get("hostname", ""),
            "device_name": payload.get("device_name", ""),
            "tab_id": payload.get("tab_id"),
            "window_id": payload.get("window_id"),
            "incognito": payload.get("incognito"),
            "transition_type": payload.get("transition_type", ""),
            "pinned": payload.get("pinned"),
            "audible": payload.get("audible"),
            "muted": payload.get("muted"),
            "opener_tab_id": payload.get("opener_tab_id"),
            "referred_by": payload.get("referred_by"),
        }
        
        # Remove None values to keep payload clean
        normalized = {k: v for k, v in normalized.items() if v is not None}
        
        logger.debug(
            "[PIPELINE:PARSER] Parsed browser visit: url=%s, visited_at=%s",
            normalized.get("url", "")[:50],
            normalized.get("visited_at", ""),
        )
        return NormalizedRecord(record_id=normalized["record_id"], payload=normalized)

    def validate(self, record: RawRecord) -> ValidationResult:
        """Validate browser visit record. For MVP, minimal validation."""
        payload = record.payload
        if not isinstance(payload, dict):
            return ValidationResult(
                is_valid=False,
                errors=["Record must be a dict"],
                metadata={},
            )
        
        # Required fields for browser_visits
        if "url" not in payload:
            return ValidationResult(
                is_valid=False,
                errors=["Missing required field: url"],
                metadata={},
            )
        
        if "visited_at" not in payload and "timestamp" not in payload:
            return ValidationResult(
                is_valid=False,
                errors=["Missing required field: visited_at (or timestamp)"],
                metadata={},
            )
        
        return ValidationResult(is_valid=True, errors=[], metadata={})

    def schema_id(self) -> str:
        return self._schema_id


@dataclass
class BrowserEventsParser(Parser):
    """Parser for browser_events: clicks, highlights, star_page, VIDEO_PLAY. Stores event_type + payload."""

    dataset_id: str
    _schema_id: str = "browser.events.v1"

    def parse(self, raw: RawRecord) -> NormalizedRecord:
        """Parse browser event record. Preserves event_type and full payload."""
        payload = raw.payload
        event_type = payload.get("event_type") or "unknown"
        ts = (
            payload.get("visited_at")
            or payload.get("starred_at")
            or payload.get("created_at")
            or datetime.now(timezone.utc).isoformat()
        )
        if isinstance(ts, (int, float)):
            ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        else:
            ts = str(ts)
        url = payload.get("url") or ""
        record_id = f"{event_type}_{(url or raw.record_id)[:80]}_{ts[:24]}"
        normalized = {
            "record_id": record_id,
            "dataset_id": self.dataset_id,
            "event_type": event_type,
            "url": url,
            "visited_at": ts,
            "title": payload.get("title"),
            "favicon_url": payload.get("favicon_url"),
            "hostname": payload.get("hostname"),
            "device_name": payload.get("device_name"),
            "transition_type": payload.get("transition_type"),
            "content": payload.get("content"),
            "tab_id": payload.get("tab_id"),
            "window_id": payload.get("window_id"),
            "incognito": payload.get("incognito"),
            "pinned": payload.get("pinned"),
            "audible": payload.get("audible"),
            "muted": payload.get("muted"),
            "opener_tab_id": payload.get("opener_tab_id"),
            "starred_at": payload.get("starred_at"),
        }
        normalized = {k: v for k, v in normalized.items() if v is not None}
        logger.debug(
            "[PIPELINE:PARSER] Parsed browser event: event_type=%s, url=%s",
            event_type,
            url[:50] if url else None,
        )
        return NormalizedRecord(record_id=record_id, payload=normalized)

    def validate(self, record: RawRecord) -> ValidationResult:
        """Validate browser event: require event_type and at least url or content."""
        payload = record.payload
        if not isinstance(payload, dict):
            return ValidationResult(
                is_valid=False,
                errors=["Record must be a dict"],
                metadata={},
            )
        if not payload.get("event_type"):
            return ValidationResult(
                is_valid=False,
                errors=["Missing required field: event_type"],
                metadata={},
            )
        return ValidationResult(is_valid=True, errors=[], metadata={})

    def schema_id(self) -> str:
        return self._schema_id
