"""Canonical mapper registry."""

from .base import CanonicalMapper
from .chatgpt_mapper import ChatGPTCanonicalMapper
from .grok_mapper import GrokCanonicalMapper
from .messenger_mapper import ImessageCanonicalMapper, SignalCanonicalMapper

MAPPER_REGISTRY = {
    "chatgpt": ChatGPTCanonicalMapper,
    "grok": GrokCanonicalMapper,
    "imessage": ImessageCanonicalMapper,  # Sprint 02: conversations canonical group
    "signal": SignalCanonicalMapper,
}

__all__ = [
    "CanonicalMapper",
    "ChatGPTCanonicalMapper",
    "GrokCanonicalMapper",
    "ImessageCanonicalMapper",
    "SignalCanonicalMapper",
    "MAPPER_REGISTRY",
]
