"""Shared types for Topos."""

from __future__ import annotations

from typing import Any, Dict, TypedDict

JsonDict = Dict[str, Any]


class HealthStatus(TypedDict):
    status: str
    time: float
    cloud_connected: bool | None
