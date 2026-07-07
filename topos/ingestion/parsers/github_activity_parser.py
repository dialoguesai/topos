"""GitHub activity parser for ingestion layer (remote connectors)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from ..sources.base import RawRecord
from ..validation.base import ValidationResult
from .base import NormalizedRecord, Parser

logger = logging.getLogger("topos.ingestion.parser.github_activity")


@dataclass
class GithubActivityParser(Parser):
    """Parser for github_activity records shaped like GitHub REST /users/{u}/events items."""

    dataset_id: str
    _schema_id: str = "github.activity.v1"

    def parse(self, raw: RawRecord) -> NormalizedRecord:
        """Parse GitHub event record. Preserves repo/actor/payload for the canonical mapper."""
        payload = raw.payload
        record_id = str(payload.get("id") or raw.record_id)

        # GitHub emits ISO-8601 created_at strings; tolerate epoch numbers from re-shapers.
        created_at = payload.get("created_at")
        if isinstance(created_at, (int, float)):
            ts = datetime.fromtimestamp(created_at, tz=timezone.utc).isoformat()
        elif created_at:
            ts = str(created_at)
        else:
            ts = datetime.now(timezone.utc).isoformat()

        normalized = {
            "record_id": record_id,
            "id": record_id,
            "dataset_id": self.dataset_id,
            "type": payload.get("type", ""),
            "created_at": ts,
            "public": payload.get("public"),
            "repo": payload.get("repo"),
            "actor": payload.get("actor"),
            "org": payload.get("org"),
            "payload": payload.get("payload"),
        }

        # Remove None values to keep payload clean
        normalized = {k: v for k, v in normalized.items() if v is not None}

        logger.debug(
            "[PIPELINE:PARSER] Parsed github event: type=%s, id=%s",
            normalized.get("type", ""),
            record_id[:32],
        )
        return NormalizedRecord(record_id=record_id, payload=normalized)

    def validate(self, record: RawRecord) -> ValidationResult:
        """Validate GitHub event record: require id, type, created_at; tolerate extra fields."""
        payload = record.payload
        if not isinstance(payload, dict):
            return ValidationResult(
                is_valid=False,
                errors=["Record must be a dict"],
                metadata={},
            )
        if not str(payload.get("id") or "").strip():
            return ValidationResult(
                is_valid=False,
                errors=["Missing required field: id"],
                metadata={},
            )
        if not str(payload.get("type") or "").strip():
            return ValidationResult(
                is_valid=False,
                errors=["Missing required field: type"],
                metadata={},
            )
        if not payload.get("created_at"):
            return ValidationResult(
                is_valid=False,
                errors=["Missing required field: created_at"],
                metadata={},
            )
        return ValidationResult(is_valid=True, errors=[], metadata={})

    def schema_id(self) -> str:
        return self._schema_id
