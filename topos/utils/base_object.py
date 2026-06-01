"""Base object with auto-generated instance names for better logging.

Inspired by Pipecat's BaseObject pattern, which provides automatic
instance naming like `ClassName#N` for cleaner logs.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import count
from threading import Lock
from typing import Optional

_class_counters: dict[type, count] = defaultdict(lambda: count(1))
_lock = Lock()


def _next_instance_number(cls: type) -> int:
    """Get the next instance number for a class (thread-safe)."""
    with _lock:
        return next(_class_counters[cls])


class BaseObject:
    """Base class that provides auto-generated instance names.
    
    Each instance gets a unique name like `ClassName#N` which is used
    in `__str__` for cleaner logging. This allows logs like:
    
        logger.debug(f"{self}: processing messages")
        # Output: "EnrichmentOrchestrator#1: processing messages"
    
    You can optionally provide a custom name:
    
        obj = MyClass(name="custom-name")
        str(obj)  # "custom-name"
    """
    
    def __init__(self, *, name: Optional[str] = None) -> None:
        """Initialize with optional custom name.
        
        Args:
            name: Optional custom name. If None, auto-generates `ClassName#N`
        """
        if name is None:
            n = _next_instance_number(self.__class__)
            name = f"{self.__class__.__name__}#{n}"
        self._name = name
    
    @property
    def name(self) -> str:
        """Get the instance name."""
        return self._name
    
    def __str__(self) -> str:
        """Return the instance name for logging."""
        return self.name
    
    def __repr__(self) -> str:
        """Return a representation including the name."""
        return f"<{self.__class__.__name__}(name={self.name!r})>"
