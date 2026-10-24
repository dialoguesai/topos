"""The answer model is also the labelling model (A2A-4 Q4): background assessments yield to answers.

An answers-mode ask is one call of the pinned local model with a person waiting for it. The node's background
assessments (the catch-up's message labels, the browsing-interest labels and their second tries) call the same
model on the same host, back to back, so an ask queues behind them on the host (BL-147: 17 s p50 live, about 3 s of
it the model's own work). Q4's rule, which 1.5.0 counted but never applied: no assessment call starts while an
answer job is queued or running; a call already running is not interrupted.

This module only counts and waits. It imports nothing of the node, so any model caller may use it. The wait is
bounded by an answer's own deadline (`answer_release.MAX_END_SECONDS`), so a count that never came down cannot
stop the labelling for good. Waiting changes when an assessment runs, never what it decides or what is shared.

NR166 R4a: the same rule for the node's other background model calls. The three assessment callers above were
the only ones that waited; ingestion's enrichment jobs, fact derivation and whatever else a pipeline job sends to
the local model host did not, and the host serves what it is sent in the order it was sent. Those calls go through
clients the owner's own chat also uses (`engine/backends/ollama.py`, `services/llm/openai.py`), and an owner's
request must never wait for a recipient's. So the work is marked where it begins (`background_work`: a pipeline
job, an enrichment run, a derivation extraction) and the client waits only inside that mark
(`yield_if_background`, `yield_to_answers_if_background`). The mark is a context variable: it follows the
coroutines the work awaits and the worker threads it starts with ``asyncio.to_thread``, and nothing else.

The bound is per call and the same 110 s: an answer job ends by then, so one wait always outlasts one job, and
under asks arriving back to back a background call still starts at least every 110 s. A call that is not a
coroutine waits in its own thread; on a thread that runs an event loop it does not wait at all, because a wait
there would stop that loop.

NR179 G1 (`ANSWER_HOLDS_BACKGROUND`, off by default, bound and unbound: the owner's decision D2). The same rule for
background starts that are not model calls: a search-index build the owner-change queue or the refresh loop's
restore starts, and an automatic graph rebuild (`hold_background_start`, called where each one starts). Seen on the
owner's node on 15 Oct: every answer job of 100 s or more ran while graph rebuilds ran and an index build held the
write gate for 44.5 s. A build already running is never stopped; only a new start waits, by the same 110 s bound.
Nothing waits inside an answer job (`answer_job`: the job counts itself, so a start it made would wait for its own
end), in the owner's own work, on a thread that runs an event loop, or while the thread holds the node's write gate
(an answer job needs the gate, so a start waiting with it held would hold the job up in turn). A wait ends at once
when the node stops (the runtime generation it began under is told to stop). Off: nothing waits.

G1 holds STARTS only. A graph-rebuild subprocess already running, and a build already holding the write gate (the
44.5 s hold seen on 15 Oct is the publish step at the end of a running build), are not stopped or shortened by it.

Kept questions Wave 2, lane OFF2N (``lanes/OFF2_CONTRACT.md`` §D, §E; the owner's ruling OD-13: background pauses
for the owner's own chat). Until now background work yielded only to share answers, never to the owner. The
control-plane client now counts the owner's own frames in flight here (``owner_frame_began`` / ``owner_frame_ended``,
raised when a frame ``owner_lane.is_owner_frame`` accepts is admitted, lowered when its task ends, however it ends),
with the monotonic time the last one ended. It classifies while the owner lane's switch is on, whatever the owner's
``while_using`` says (the idleness readiness reports needs it under "Share evenly" too); with the lane off nothing is
counted. The yield rule: every background wait above also waits while an owner frame is in flight, when
``BACKGROUND_YIELDS_TO_OWNER`` is on (the default; off is the kill switch) and the owner's ``while_using`` is
``owner_first`` (``away_config``; "Share evenly" turns it off live). Same shape as the answers' wait: polled, no
preemption, the same 110 s bound per wait, and a start's wait still ends when the node stops (a stop also ends the
client's frames, which lowers the count). It never applies to the owner's own work (``owners_own_work``), inside an
answer job (``answer_job``), or inside an owner frame's own handling (``owner_frame``, marked by the client): an owner
frame must never wait on itself, and recipients' answers are scheduled by the owner lane, not by this rule. A frame
that only claims to be the owner's (forged, lifted, retyped, unstamped) is not counted, so it can never make
background work wait. Starts (``hold_background_start``) wait for the owner whatever ``ANSWER_HOLDS_BACKGROUND`` says:
that switch is G1's, for answer jobs.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import logging
import threading
import time

# An answer job ends, with or without a body, by its deadline (answer_release.MAX_END_SECONDS, 110 s from acceptance).
LIMIT_SECONDS = 110.0
POLL_SECONDS = 0.25

#: A wait at least this long is written to the log: its seconds, and whether the limit ended it. Nothing else.
LOG_SECONDS = 1.0

_log = logging.getLogger(__name__)
_lock = threading.Lock()
_active = 0
# True: background work. None: the owner's own request, which never waits. False: not marked either way.
_background: contextvars.ContextVar = contextvars.ContextVar("a_background_model_caller", default=False)
# NR179 G1: True inside a share's answer job (`answer_job`).
_answer_job: contextvars.ContextVar = contextvars.ContextVar("a_share_answer_job", default=False)
# OFF2N: the owner's own frames in flight, and when (time.monotonic) the last one ended; None: none has yet.
_owner_frames = 0
_owner_ended_at: float | None = None
_owner_counted = False   # the client classifies owner frames (the owner lane's switch was on when it was built)
# OFF2N: True inside an owner frame's own handling (`owner_frame`): nothing in it waits for the owner.
_owner_frame: contextvars.ContextVar = contextvars.ContextVar("an_owner_frame", default=False)


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
    OFF2N: and, under the yield rule, no owner frame is in flight (module docstring).

    Returns the seconds waited (0.0 when no answer was in hand)."""
    if not _held():
        return 0.0
    started = clock()
    while _held() and clock() - started < limit:
        await asyncio.sleep(poll)
    return clock() - started


# -- OFF2N: the owner's own frames, and the yield rule (Wave 2 of kept questions) ---------------------------------

def owner_frames_counted(on: bool = True) -> None:
    """The control-plane client classifies the owner's frames (its owner lane is on): owner idleness can be told."""
    global _owner_counted
    with _lock:
        _owner_counted = bool(on)


def counts_owner_frames() -> bool:
    with _lock:
        return _owner_counted


def owner_frame_began() -> None:
    global _owner_frames
    with _lock:
        _owner_frames += 1


def owner_frame_ended(clock=time.monotonic) -> None:
    """One owner frame ended, however it ended: never below zero."""
    global _owner_frames, _owner_ended_at
    with _lock:
        _owner_frames = max(0, _owner_frames - 1)
        _owner_ended_at = clock()


def owner_frames() -> int:
    """The owner's own frames in flight now (admitted, waiting for a slot or handled)."""
    with _lock:
        return _owner_frames


def owner_idle_seconds(*, since: float, cap: int = 3600, clock=time.monotonic) -> int:
    """Readiness's ``owner_idle_seconds`` (contract §C): 0 while an owner frame is in flight, else the whole seconds
    since the last one ended, or since ``since`` (the monotonic time the node started) when none has; at most ``cap``."""
    with _lock:
        frames, ended = _owner_frames, _owner_ended_at
    if frames > 0:
        return 0
    return max(0, min(int(cap), int(clock() - (since if ended is None else ended))))


def yields_to_owner() -> bool:
    """The yield rule is on: ``BACKGROUND_YIELDS_TO_OWNER`` (the kill switch) and the owner's ``owner_first``."""
    from . import away_config, switches
    try:
        return switches.on(switches.BACKGROUND_YIELDS_TO_OWNER) and not away_config.shares_evenly()
    except Exception:  # noqa: BLE001 -- what cannot be read never makes background work wait
        return False


def _owner_holds() -> bool:
    """Background work here waits for the owner now: an owner frame is in flight, the yield rule is on, and this is
    none of the owner's own work, an answer job, or an owner frame's own handling."""
    if _background.get() is None or _answer_job.get() is True or _owner_frame.get() is True:
        return False
    with _lock:
        if _owner_frames <= 0:
            return False
    return yields_to_owner()


def _held() -> bool:
    """A background model call waits now: an answer job is in hand, or the owner holds it (``_owner_holds``)."""
    return active() or _owner_holds()


def within_owner_frame(coroutine):
    """The client's wrapper for an owner frame's handler: nothing in it (its awaits, its ``asyncio.to_thread``
    workers) waits for the owner. A coroutine that runs ``coroutine`` with the mark set."""
    async def marked():
        token = _owner_frame.set(True)
        try:
            return await coroutine
        finally:
            _owner_frame.reset(token)
    return marked()


def in_owner_frame() -> bool:
    return _owner_frame.get() is True


@contextlib.contextmanager
def owner_frame():
    """Mark what runs inside (and every task it creates) as an owner frame's own handling (``within_owner_frame``)."""
    token = _owner_frame.set(True)
    try:
        yield
    finally:
        _owner_frame.reset(token)


# -- NR166 R4a: background work that is not an assessment --------------------------------------------------------

@contextlib.contextmanager
def background_work():
    """Mark what runs inside as background work: its model calls wait for an answer job (see the module docstring)."""
    token = _background.set(True)
    try:
        yield
    finally:
        _background.reset(token)


def in_background(coroutine_function):
    """A coroutine function whose every call is background work."""
    @functools.wraps(coroutine_function)
    async def marked(*args, **kwargs):
        with background_work():
            return await coroutine_function(*args, **kwargs)
    return marked


@contextlib.contextmanager
def owners_own_work():
    """Mark what runs inside as the owner's own request: nothing in it waits, even at a call site that waits by
    default (`yield_unless_the_owners`)."""
    token = _background.set(None)
    try:
        yield
    finally:
        _background.reset(token)


def is_background() -> bool:
    return _background.get() is True


def yield_unless_the_owners(**how) -> float:
    """For a caller that is background work wherever it is called from, except where the owner asked for it
    (`owners_own_work`): wait for answers, in a worker thread. On a thread that runs an event loop: no wait."""
    if _background.get() is None or not _held() or _on_a_loop():
        return 0.0
    return wait_for_answers(**how)


def _on_a_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _note(waited: float, limit: float, what: str = "background model call", for_whom: str = "an answer job") -> float:
    if waited >= LOG_SECONDS:
        _log.info("%s waited %.1f s for %s%s", what, waited, for_whom,
                  " (the limit; it starts anyway)" if waited >= limit else "")
    return waited


class _Whom:
    """What a wait waited for, as the log line names it: an answer job, the owner's request, or both."""

    def __init__(self):
        self.answers = self.owner = False

    def saw(self, answers: bool, owner: bool) -> bool:
        self.answers, self.owner = self.answers or answers, self.owner or owner
        return answers or owner

    def __str__(self) -> str:
        if self.owner and not self.answers:
            return "the owner's own request"
        return "an answer job or the owner's own request" if self.owner else "an answer job"


def wait_for_answers(*, limit: float = LIMIT_SECONDS, poll: float = POLL_SECONDS, clock=time.monotonic,
                     sleep=time.sleep) -> float:
    """`yield_to_answers` for a caller that is not a coroutine: it waits in its own thread. Seconds waited."""
    whom = _Whom()
    if not whom.saw(active(), _owner_holds()):
        return 0.0
    started = clock()
    while whom.saw(active(), _owner_holds()) and clock() - started < limit:
        sleep(poll)
    return _note(clock() - started, limit, for_whom=str(whom))


def yield_if_background(**how) -> float:
    """Before a model call that is not a coroutine: wait for answers when this is background work, in a worker
    thread. Not background, or on a thread that runs an event loop: no wait."""
    if _background.get() is not True or not _held() or _on_a_loop():
        return 0.0
    return wait_for_answers(**how)


async def yield_to_answers_if_background(**how) -> float:
    """Before a model call in a coroutine: wait for answers when this is background work. Otherwise no wait."""
    if _background.get() is not True or not _held():
        return 0.0
    limit = how.get("limit", LIMIT_SECONDS)
    owner = _owner_holds()
    return _note(await yield_to_answers(**how), limit,
                 for_whom="the owner's own request" if owner and not active() else "an answer job")


# -- NR179 G1: background starts that are not model calls ----------------------------------------------------------

@contextlib.contextmanager
def answer_job():
    """Mark what runs inside as a share's answer job itself: no start in it waits for answers (it would wait for
    itself). The mark follows the job's awaited coroutines and its `asyncio.to_thread` workers."""
    token = _answer_job.set(True)
    try:
        yield
    finally:
        _answer_job.reset(token)


def in_answer_job() -> bool:
    return _answer_job.get() is True


def holds_background() -> bool:
    """`ANSWER_HOLDS_BACKGROUND` (NR179 G1): off by default, bound and unbound."""
    from . import switches
    return switches.on(switches.ANSWER_HOLDS_BACKGROUND)


def _holds_the_write_gate() -> bool:
    """Whether this thread holds the node's write gate. Unknown is held: then nothing waits."""
    try:
        from topos.storage.db.write_gate import db_write_lock
        return bool(db_write_lock()._is_owned())
    except Exception:  # noqa: BLE001
        return True


def hold_background_start(*, only_if=None, limit: float | None = None, poll: float | None = None,
                          clock=time.monotonic, sleep=None) -> float:
    """NR179 G1: before a background start that is not a model call (a search-index build, an automatic graph
    rebuild), wait until no answer job is queued or running, or `limit` seconds (110) passed; then it starts.

    No wait at all: the switch off, no answer job, the owner's own work, inside an answer job, on a thread that runs
    an event loop, while this thread holds the write gate, or when `only_if()` (asked last, so only when a wait
    would follow) is false or cannot say. The wait ends early when the node stops: the runtime generation current
    when it began is told to stop (`topos.runtime_shutdown`), which also wakes its sleep. Returns the seconds waited;
    a wait of a second or more is logged with its seconds only.

    OFF2N: it also waits while an owner frame is in flight under the yield rule (module docstring), whatever
    ``ANSWER_HOLDS_BACKGROUND`` says; the same rules, bound and stop."""
    answers = holds_background()
    whom = _Whom()
    if not whom.saw(answers and active(), _owner_holds()):
        return 0.0
    if _background.get() is None or _answer_job.get() is True or _on_a_loop() or _holds_the_write_gate():
        return 0.0
    try:
        if only_if is not None and not only_if():
            return 0.0
    except Exception:  # noqa: BLE001 -- a start that cannot say whether it waits does not wait
        return 0.0
    limit = LIMIT_SECONDS if limit is None else limit
    poll = POLL_SECONDS if poll is None else poll
    from topos import runtime_shutdown
    run = runtime_shutdown.current_generation()
    sleep = run.wait if sleep is None else sleep
    started = clock()
    while (whom.saw(answers and active(), _owner_holds()) and clock() - started < limit
           and not run.is_stop_requested()):
        sleep(poll)
    return _note(clock() - started, limit, "background build", str(whom))
