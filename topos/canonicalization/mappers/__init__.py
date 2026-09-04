"""Canonical mapper registry."""

from .base import CanonicalMapper
from .browser_activity_mapper import BrowserActivityCanonicalMapper
from .chatgpt_mapper import ChatGPTCanonicalMapper
from .demo_mappers import (
    DemoCalendarMapper,
    DemoContactsMapper,
    DemoFinancialMapper,
    JournalTimeLogMapper,
    DemoJournalMapper,
    DemoMessengerMapper,
    DemoPlacesMapper,
    DemoProfileMapper,
)
from .documents_mapper import DocumentsCanonicalMapper
from .github_activity_mapper import GithubActivityCanonicalMapper
from .google_calendar_mapper import GoogleCalendarMapper
from .grok_mapper import GrokCanonicalMapper
from .messenger_mapper import ImessageCanonicalMapper, SignalCanonicalMapper, VoxtermTranscriptCanonicalMapper
from .transcript_mapper import TranscriptCanonicalMapper

MAPPER_REGISTRY = {
    "chatgpt": ChatGPTCanonicalMapper,
    "grok": GrokCanonicalMapper,
    "imessage": ImessageCanonicalMapper,
    "signal": SignalCanonicalMapper,
    "voxterm_transcript": VoxtermTranscriptCanonicalMapper,
    "browser_activity": BrowserActivityCanonicalMapper,
    "github_activity": GithubActivityCanonicalMapper,
    "demo_messenger": DemoMessengerMapper,
    "demo_calendar": DemoCalendarMapper,
    "demo_journal": DemoJournalMapper,
    "demo_profile": DemoProfileMapper,
    "demo_financial": DemoFinancialMapper,
    "demo_places": DemoPlacesMapper,
    "demo_contacts": DemoContactsMapper,
    "journal_time_log": JournalTimeLogMapper,
    "documents": DocumentsCanonicalMapper,
    "google_calendar": GoogleCalendarMapper,
    "transcript": TranscriptCanonicalMapper,
}

__all__ = [
    "BrowserActivityCanonicalMapper",
    "CanonicalMapper",
    "ChatGPTCanonicalMapper",
    "DocumentsCanonicalMapper",
    "GithubActivityCanonicalMapper",
    "GoogleCalendarMapper",
    "GrokCanonicalMapper",
    "ImessageCanonicalMapper",
    "SignalCanonicalMapper",
    "TranscriptCanonicalMapper",
    "MAPPER_REGISTRY",
]
