"""Canonical mapper registry."""

from .base import CanonicalMapper
from .browser_activity_mapper import BrowserActivityCanonicalMapper
from .chatgpt_mapper import ChatGPTCanonicalMapper
from .demo_mappers import (
    DemoCalendarMapper,
    DemoContactsMapper,
    DemoFinancialMapper,
    DemoGrowMapper,
    DemoJournalMapper,
    DemoMessengerMapper,
    DemoPlacesMapper,
    DemoProfileMapper,
)
from .grok_mapper import GrokCanonicalMapper
from .messenger_mapper import ImessageCanonicalMapper, SignalCanonicalMapper

MAPPER_REGISTRY = {
    "chatgpt": ChatGPTCanonicalMapper,
    "grok": GrokCanonicalMapper,
    "imessage": ImessageCanonicalMapper,
    "signal": SignalCanonicalMapper,
    "browser_activity": BrowserActivityCanonicalMapper,
    "demo_messenger": DemoMessengerMapper,
    "demo_calendar": DemoCalendarMapper,
    "demo_journal": DemoJournalMapper,
    "demo_profile": DemoProfileMapper,
    "demo_financial": DemoFinancialMapper,
    "demo_places": DemoPlacesMapper,
    "demo_contacts": DemoContactsMapper,
    "grow_time_log": DemoGrowMapper,
}

__all__ = [
    "BrowserActivityCanonicalMapper",
    "CanonicalMapper",
    "ChatGPTCanonicalMapper",
    "GrokCanonicalMapper",
    "ImessageCanonicalMapper",
    "SignalCanonicalMapper",
    "MAPPER_REGISTRY",
]
