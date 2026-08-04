"""In-process signal that a derivation batch is running.

The entity graph refresher is debounce-triggered on its own ``threading.Timer``
thread, with no knowledge of the pipeline. On 2026-07-30 that meant a 77-second
full rebuild fired *while* an 88-record batch was mid-derivation, and both held
the process-wide write gate in turn. The rebuild is also pointless at that
moment: the batch is still adding the very entities it would index.

This is the smallest honest coordination — a counter the pipeline raises around
a batch, so background rebuilds can defer instead of interleaving. It is
deliberately in-process only: everything it coordinates lives in one process,
and a durable lock would need reaping.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Iterator

_lock = threading.Lock()
_active = 0


@contextmanager
def derivation_in_flight() -> Iterator[None]:
    """Mark a derivation batch as running for its duration."""
    global _active
    with _lock:
        _active += 1
    try:
        yield
    finally:
        with _lock:
            _active = max(0, _active - 1)


def is_derivation_in_flight() -> bool:
    with _lock:
        return _active > 0


def active_derivations() -> int:
    with _lock:
        return _active


def reset_for_tests() -> None:
    global _active
    with _lock:
        _active = 0
