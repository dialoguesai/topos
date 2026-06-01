"""Event models for Topos."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class Event:
    name: str
    payload: Dict[str, Any]
