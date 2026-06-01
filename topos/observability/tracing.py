"""Tracing stubs for Topos."""

from __future__ import annotations


class Span:
    def __init__(self, name: str):
        self.name = name

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        _ = (exc_type, exc, tb)


def start_span(name: str) -> Span:
    return Span(name)
