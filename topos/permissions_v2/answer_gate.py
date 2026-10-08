"""The answer model is also the labelling model (A2A-4 Q4): background assessments yield to answers.

An answers-mode ask is one call of the pinned local model with a person waiting for it. The node's background
assessments (the catch-up's message labels, the browsing-interest labels and their second tries) call the same
model on the same host, back to back, so an ask queues behind them on the host (BL-147: 17 s p50 live, about 3 s of
it the model's own work). Q4's rule, which 1.5.0 counted but never applied: no assessment call starts while an
answer job is queued or running; a call already running is not interrupted.

This module only counts and waits. It imports nothing of the node, so any model caller may use it. The wait is
bounded by an answer's own deadline (`answer_release.MAX_END_SECONDS`), so a count that never came down cannot
stop the labelling for good. Waiting changes when an assessment runs, never what it decides or what is shared.
"""
from __future__ import annotations

import asyncio
import threading
import time

# An answer job ends, with or without a body, by its deadline (answer_release.MAX_END_SECONDS, 110 s from acceptance).
LIMIT_SECONDS = 110.0
POLL_SECONDS = 0.25

_lock = threading.Lock()
_active = 0


def active() -> bool:
    """An answer job is queued or running in this process."""
    with _lock:
        return _active > 0


def delta(change: int) -> None:
    global _active
    with _lock:
        _active += change


async def yield_to_answers(*, limit: float = LIMIT_SECONDS, poll: float = POLL_SECONDS, clock=time.monotonic) -> float:
    """Wait, before an assessment call starts, until no answer job is queued or running, or `limit` seconds passed.

    Returns the seconds waited (0.0 when no answer was in hand)."""
    if not active():
        return 0.0
    started = clock()
    while active() and clock() - started < limit:
        await asyncio.sleep(poll)
    return clock() - started
