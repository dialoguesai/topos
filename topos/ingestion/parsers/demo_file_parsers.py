"""CSV file parsers for signal-dimension demo harness sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from ..journal_time_log_normalize import (
    normalize_time_log_payload as _normalize_time_log,
    time_log_payload_has_start_time,
)
from ..sources.base import RawRecord
from ..validation.base import ValidationResult
from .base import NormalizedRecord, Parser


def _require_fields(payload: Dict[str, Any], fields: List[str]) -> List[str]:
    return [f"Missing required field: {name}" for name in fields if not str(payload.get(name) or "").strip()]


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y"}


@dataclass
class DemoCsvParser(Parser):
    """Generic CSV row parser with schema-specific normalizer."""

    dataset_id: str
    _schema_id: str
    required_fields: List[str]
    normalize: Callable[[Dict[str, Any], str, str], Dict[str, Any]]

    def parse(self, raw: RawRecord) -> NormalizedRecord:
        normalized = self.normalize(dict(raw.payload), raw.record_id, self.dataset_id)
        record_id = str(normalized.get("record_id") or normalized.get("message_id") or raw.record_id)
        return NormalizedRecord(record_id=record_id, payload=normalized)

    def validate(self, record: RawRecord) -> ValidationResult:
        if not isinstance(record.payload, dict):
            return ValidationResult(is_valid=False, errors=["Record must be a dict"], metadata={})
        errors = _require_fields(record.payload, self.required_fields)
        return ValidationResult(is_valid=not errors, errors=errors, metadata={})

    def schema_id(self) -> str:
        return self._schema_id


def _normalize_messenger(payload: Dict[str, Any], record_id: str, dataset_id: str) -> Dict[str, Any]:
    is_self = _coerce_bool(payload.get("is_from_self"))
    sender_id = str(payload.get("sender_id") or ("self" if is_self else record_id))
    return {
        "message_id": str(payload.get("message_id") or record_id),
        "dataset_id": dataset_id,
        "thread_id": str(payload.get("conversation_id") or ""),
        "conversation_id": str(payload.get("conversation_id") or ""),
        "sender_id": sender_id,
        "sender_type": "human",
        "is_from_self": is_self,
        "ts": str(payload.get("event_at") or ""),
        "content": str(payload.get("content") or ""),
        "_metadata": {"sender_name": payload.get("sender_name")},
    }


def _normalize_calendar(payload: Dict[str, Any], record_id: str, dataset_id: str) -> Dict[str, Any]:
    event_id = str(payload.get("event_id") or record_id)
    return {
        "record_id": event_id,
        "event_id": event_id,
        "dataset_id": dataset_id,
        "title": str(payload.get("title") or ""),
        "starts_at": str(payload.get("starts_at") or ""),
        "ends_at": str(payload.get("ends_at") or ""),
        "location": str(payload.get("location") or ""),
        "attendees": str(payload.get("attendees") or ""),
        "is_busy": _coerce_bool(payload.get("is_busy", True)),
    }


def _normalize_journal(payload: Dict[str, Any], record_id: str, dataset_id: str) -> Dict[str, Any]:
    entry_id = str(payload.get("entry_id") or record_id)
    return {
        "record_id": entry_id,
        "entry_id": entry_id,
        "dataset_id": dataset_id,
        "entry_at": str(payload.get("entry_at") or ""),
        "mood_tag": str(payload.get("mood_tag") or ""),
        "category": str(payload.get("category") or ""),
        "content": str(payload.get("content") or ""),
    }


def _normalize_profile(payload: Dict[str, Any], record_id: str, dataset_id: str) -> Dict[str, Any]:
    rid = str(payload.get("record_id") or record_id)
    return {
        "record_id": rid,
        "dataset_id": dataset_id,
        "record_type": str(payload.get("record_type") or ""),
        "title": str(payload.get("title") or ""),
        "organization": str(payload.get("organization") or ""),
        "start_date": str(payload.get("start_date") or ""),
        "end_date": str(payload.get("end_date") or ""),
        "description": str(payload.get("description") or ""),
    }


def _normalize_financial(payload: Dict[str, Any], record_id: str, dataset_id: str) -> Dict[str, Any]:
    tid = str(payload.get("transaction_id") or record_id)
    amount_raw = payload.get("amount")
    try:
        amount = float(amount_raw)
    except (TypeError, ValueError):
        amount = 0.0
    return {
        "record_id": tid,
        "transaction_id": tid,
        "dataset_id": dataset_id,
        "account_type": str(payload.get("account_type") or ""),
        "account_name": str(payload.get("account_name") or ""),
        "posted_at": str(payload.get("posted_at") or ""),
        "amount": amount,
        "currency": str(payload.get("currency") or "USD"),
        "category": str(payload.get("category") or ""),
        "description": str(payload.get("description") or ""),
    }


def _normalize_browser(payload: Dict[str, Any], record_id: str, dataset_id: str) -> Dict[str, Any]:
    event_id = str(payload.get("event_id") or record_id)
    visited_at = str(payload.get("visited_at") or "")
    return {
        "record_id": event_id,
        "id": event_id,
        "dataset_id": dataset_id,
        "url": str(payload.get("url") or ""),
        "title": str(payload.get("title") or ""),
        "visited_at": visited_at,
        "event_type": str(payload.get("activity_type") or "visit"),
    }


def _normalize_places(payload: Dict[str, Any], record_id: str, dataset_id: str) -> Dict[str, Any]:
    event_id = str(payload.get("event_id") or record_id)
    return {
        "record_id": event_id,
        "event_id": event_id,
        "dataset_id": dataset_id,
        "place_name": str(payload.get("place_name") or ""),
        "city": str(payload.get("city") or ""),
        "region": str(payload.get("region") or ""),
        "country": str(payload.get("country") or ""),
        "event_at": str(payload.get("event_at") or ""),
        "event_type": str(payload.get("event_type") or ""),
    }


def _normalize_contacts(payload: Dict[str, Any], record_id: str, dataset_id: str) -> Dict[str, Any]:
    contact_id = str(payload.get("contact_id") or record_id)
    return {
        "record_id": contact_id,
        "contact_id": contact_id,
        "dataset_id": dataset_id,
        "display_name": str(payload.get("display_name") or ""),
        "identifier": str(payload.get("identifier") or ""),
        "identifier_type": str(payload.get("identifier_type") or "email"),
    }


def demo_parser_factory(schema_id: str, dataset_id: str) -> Optional[DemoCsvParser]:
    specs: Dict[str, tuple[List[str], Callable[..., Dict[str, Any]]]] = {
        "demo.messenger.v1": (["message_id", "conversation_id", "content"], _normalize_messenger),
        "demo.calendar.v1": (["event_id", "title", "starts_at"], _normalize_calendar),
        "demo.journal.v1": (["entry_id", "content"], _normalize_journal),
        "demo.profile.v1": (["record_id", "record_type", "title"], _normalize_profile),
        "demo.financial.v1": (["transaction_id", "account_type", "amount"], _normalize_financial),
        "demo.browser.v1": (["event_id", "url", "visited_at"], _normalize_browser),
        "demo.places.v1": (["event_id", "place_name", "event_at"], _normalize_places),
        "demo.contacts.v1": (["contact_id", "display_name", "identifier"], _normalize_contacts),
        "journal.time_log.v1": ([], _normalize_time_log),
    }
    spec = specs.get(schema_id)
    if not spec:
        return None
    required, normalizer = spec
    return DemoCsvParser(
        dataset_id=dataset_id,
        _schema_id=schema_id,
        required_fields=required,
        normalize=normalizer,
    )


@dataclass
class DemoMessengerParser(DemoCsvParser):
    def __init__(self, dataset_id: str, **_kwargs) -> None:
        super().__init__(
            dataset_id=dataset_id,
            _schema_id="demo.messenger.v1",
            required_fields=["message_id", "conversation_id", "content"],
            normalize=_normalize_messenger,
        )


@dataclass
class DemoCalendarParser(DemoCsvParser):
    def __init__(self, dataset_id: str, **_kwargs) -> None:
        super().__init__(
            dataset_id=dataset_id,
            _schema_id="demo.calendar.v1",
            required_fields=["event_id", "title", "starts_at"],
            normalize=_normalize_calendar,
        )


@dataclass
class DemoJournalParser(DemoCsvParser):
    def __init__(self, dataset_id: str, **_kwargs) -> None:
        super().__init__(
            dataset_id=dataset_id,
            _schema_id="demo.journal.v1",
            required_fields=["entry_id", "content"],
            normalize=_normalize_journal,
        )


@dataclass
class DemoProfileParser(DemoCsvParser):
    def __init__(self, dataset_id: str, **_kwargs) -> None:
        super().__init__(
            dataset_id=dataset_id,
            _schema_id="demo.profile.v1",
            required_fields=["record_id", "record_type", "title"],
            normalize=_normalize_profile,
        )


@dataclass
class DemoFinancialParser(DemoCsvParser):
    def __init__(self, dataset_id: str, **_kwargs) -> None:
        super().__init__(
            dataset_id=dataset_id,
            _schema_id="demo.financial.v1",
            required_fields=["transaction_id", "account_type", "amount"],
            normalize=_normalize_financial,
        )


@dataclass
class DemoBrowserFileParser(DemoCsvParser):
    def __init__(self, dataset_id: str, **_kwargs) -> None:
        super().__init__(
            dataset_id=dataset_id,
            _schema_id="demo.browser.v1",
            required_fields=["event_id", "url", "visited_at"],
            normalize=_normalize_browser,
        )


@dataclass
class DemoPlacesParser(DemoCsvParser):
    def __init__(self, dataset_id: str, **_kwargs) -> None:
        super().__init__(
            dataset_id=dataset_id,
            _schema_id="demo.places.v1",
            required_fields=["event_id", "place_name", "event_at"],
            normalize=_normalize_places,
        )


@dataclass
class DemoEmailParser(DemoCsvParser):
    def __init__(self, dataset_id: str, **_kwargs) -> None:
        super().__init__(
            dataset_id=dataset_id,
            _schema_id="demo.messenger.v1",
            required_fields=["message_id", "conversation_id", "content"],
            normalize=_normalize_messenger,
        )


@dataclass
class JournalTimeLogFileParser(DemoCsvParser):
    def __init__(self, dataset_id: str, **_kwargs) -> None:
        super().__init__(
            dataset_id=dataset_id,
            _schema_id="journal.time_log.v1",
            required_fields=[],
            normalize=_normalize_time_log,
        )

    def validate(self, record: RawRecord) -> ValidationResult:
        if not isinstance(record.payload, dict):
            return ValidationResult(is_valid=False, errors=["Record must be a dict"], metadata={})
        errors = _require_fields(record.payload, self.required_fields)
        if not time_log_payload_has_start_time(record.payload):
            errors.append("Missing required field: starts_at (or legacy startDate)")
        return ValidationResult(is_valid=not errors, errors=errors, metadata={})


@dataclass
class DemoContactsParser(DemoCsvParser):
    def __init__(self, dataset_id: str, **_kwargs) -> None:
        super().__init__(
            dataset_id=dataset_id,
            _schema_id="demo.contacts.v1",
            required_fields=["contact_id", "display_name", "identifier"],
            normalize=_normalize_contacts,
        )
