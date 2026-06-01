"""Source connector registry."""

from .base import SourceConnector
from .calendar import CalendarSourceConnector
from .chatgpt import ChatGPTSourceConnector
from .grok import GrokSourceConnector

SOURCE_REGISTRY = {
    "chatgpt": ChatGPTSourceConnector,
    "grok": GrokSourceConnector,
    "calendar": CalendarSourceConnector,
}

__all__ = [
    "SourceConnector",
    "ChatGPTSourceConnector",
    "GrokSourceConnector",
    "CalendarSourceConnector",
    "SOURCE_REGISTRY",
]
