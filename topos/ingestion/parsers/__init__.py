"""Parser registry."""

from .base import Parser
from .browser_parser import BrowserParser, BrowserEventsParser
from .calendar_parser import CalendarParser
from .chatgpt_parser import ChatGPTParser
from .demo_file_parsers import (
    DemoBrowserFileParser,
    DemoCalendarParser,
    DemoContactsParser,
    DemoFinancialParser,
    JournalTimeLogFileParser,
    DemoJournalParser,
    DemoMessengerParser,
    DemoPlacesParser,
    DemoProfileParser,
)
from .grok_parser import GrokParser
from .messenger_parser import ImessageParser, SignalParser
from .voxterm_parser import VoxtermTranscriptParser

PARSER_REGISTRY = {
    "chatgpt.conversation.v1": ChatGPTParser,
    "chatgpt.conversation.v2": ChatGPTParser,  # Same parser, flattened records match v1 format
    "grok.conversation.v1": GrokParser,
    "calendar.events.v1": CalendarParser,
    "browser.visits.v1": BrowserParser,  # Sprint 3: Browser plugin visits
    "managed.file.browser_history_dem.v1": BrowserParser,  # Hosted browser demo source alias
    "browser.events.v1": BrowserEventsParser,  # Clicks, highlights, star_page, VIDEO_PLAY
    "imessage.messages.v1": ImessageParser,  # Sprint 02: Messenger ingestion
    "signal.messages.v1": SignalParser,
    "demo.messenger.v1": DemoMessengerParser,
    "demo.calendar.v1": DemoCalendarParser,
    "demo.journal.v1": DemoJournalParser,
    "demo.profile.v1": DemoProfileParser,
    "demo.financial.v1": DemoFinancialParser,
    "demo.browser.v1": DemoBrowserFileParser,
    "demo.places.v1": DemoPlacesParser,
    "demo.contacts.v1": DemoContactsParser,
    "journal.time_log.v1": JournalTimeLogFileParser,
    "grow.time_log.v1": JournalTimeLogFileParser,
    "voxterm.transcript.v1": VoxtermTranscriptParser,
}

__all__ = [
    "Parser",
    "BrowserParser",
    "BrowserEventsParser",
    "ChatGPTParser",
    "GrokParser",
    "CalendarParser",
    "ImessageParser",
    "SignalParser",
    "DemoBrowserFileParser",
    "DemoCalendarParser",
    "DemoContactsParser",
    "DemoFinancialParser",
    "JournalTimeLogFileParser",
    "DemoJournalParser",
    "DemoMessengerParser",
    "DemoPlacesParser",
    "DemoProfileParser",
    "VoxtermTranscriptParser",
    "PARSER_REGISTRY",
]
