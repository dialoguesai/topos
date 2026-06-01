"""Parser registry."""

from .base import Parser
from .browser_parser import BrowserParser, BrowserEventsParser
from .calendar_parser import CalendarParser
from .chatgpt_parser import ChatGPTParser
from .grok_parser import GrokParser
from .messenger_parser import ImessageParser, SignalParser

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
    "PARSER_REGISTRY",
]
