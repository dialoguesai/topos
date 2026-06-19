"""Canonical mapper registry."""

from .base import CanonicalMapper
from .browser_activity_mapper import BrowserActivityCanonicalMapper
from .chatgpt_mapper import ChatGPTCanonicalMapper
from .grok_mapper import GrokCanonicalMapper
from .messenger_mapper import ImessageCanonicalMapper, SignalCanonicalMapper

MAPPER_REGISTRY = {
    "chatgpt": ChatGPTCanonicalMapper,
    "grok": GrokCanonicalMapper,
    "imessage": ImessageCanonicalMapper,
    "signal": SignalCanonicalMapper,
    "browser_activity": BrowserActivityCanonicalMapper,
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
