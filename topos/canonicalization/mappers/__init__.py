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
from .github_activity_mapper import GithubActivityCanonicalMapper
from .grok_mapper import GrokCanonicalMapper
from .messenger_mapper import ImessageCanonicalMapper, SignalCanonicalMapper, VoxtermTranscriptCanonicalMapper

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
}

__all__ = [
    "BrowserActivityCanonicalMapper",
    "CanonicalMapper",
    "ChatGPTCanonicalMapper",
    "GithubActivityCanonicalMapper",
    "GrokCanonicalMapper",
    "ImessageCanonicalMapper",
    "SignalCanonicalMapper",
    "MAPPER_REGISTRY",
]
