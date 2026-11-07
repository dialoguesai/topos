"""How the node uses its local model host: one context per model, and no request left behind an idle model.

Measured on the owner's node on 2 Nov (1.5.39, Ollama 0.40.13, a 36 GiB host with a 28.1 GiB Metal budget):

- Ollama loads a model at the context a request asks for and reloads it when another request asks for a different
  one. The node's callers asked for five: home chat's model pack 81,920, the share writer 8,192 or 12,288 and its
  judge 8,192 in the same job, derivation 8,192, entity maintenance 4,096, and verification nothing (the host's
  32,768). So the same model was loaded and reloaded under itself.
- With the 27B loaded at 81,920 (14.7 GiB resident, predicted at 26.5 GiB), a request for any other model, or for the
  27B at another context, waited until the 27B's keep_alive (home chat's 30 minutes) ran out. With the node idle, the
  loaded 27B answered in 2.7 s while llama3.2 got nothing in 60 s. One unload (``keep_alive: 0``) released the queue
  at once, and llama3.2 then answered in 2.5 s. This is the 42 and 48 minute stalls of the 1.5.38 stress runs. It is
  also why every failed verify_claim on 1.5.39 ended ``deadline_exceeded`` at ``model_call``.
- At 32,768, every switch between the three models loaded in 3 to 22 s, and the scheduler evicted normally (27B
  11.4 GiB, 9B 8.4 GiB, llama3.2 5.6 GiB). What the host leaves for models depends on the owner's other apps: about
  17 to 20 GiB that day, so two models fit together and three do not.

So two rules, each with its own switch:

1. ``prepare`` / ``request``: every local request to a model is sent at that model's one context, whatever its caller
   asked: the first of ``STEPS`` (8,192, 16,384) or ``CONTEXT`` (32,768 on a host of 32 GiB or more, see
   ``_default_context``; ``TOPOS_LOCAL_MODEL_CONTEXT``, where 0 leaves each caller's own) that fits the prompt and
   reply. A model moves up a step when a request needs more and never down while the node runs, so it is reloaded at
   most once per step. A request that asked for more than ``CONTEXT`` keeps it, but only when its prompt and reply
   need more. A context that fits never truncates, so a caller's 4,096 or 81,920 changes no reply. It only stops the
   reload.
2. ``release_blocked``, run by ``watchdog``: once a node request has waited ``WAIT_SECONDS`` for a model the host has
   not loaded, unload each loaded model that no node request is using. That is the eviction the host's scheduler
   failed to make. A model a node request is generating on is never unloaded. A client outside the node (an app
   calling the host itself) can lose its loaded model this way; it pays one reload, never an error.
   ``TOPOS_LOCAL_MODEL_RELEASE_AFTER_S`` sets the wait (15 s), and 0 turns the watchdog off.

The candidate stress run of 3 Nov (topos#250) showed the second half of the problem. The host runs one request at a
time per model (``n_slots = 1``) and never swaps out a model with work queued on it. Home chat and routines kept two
or three 27B requests queued, each 60 to 120 s, so the 27B was never idle for the watchdog. Verification's llama3.2
and the share writer's 9B then waited until their own deadlines (120 s, 100 s) and failed, 11 of 18 and 4 of 4.
Each request the node abandoned stayed queued on the host and was dropped only when it finally ran (two chat
requests ended 499 after 8m20s and 8m30s). So the node now does its own scheduling, one more rule:

3. ``_admit``: the node sends at most ``SLOTS`` requests per model at a time (the host's own one), and holds the rest
   itself, in priority order (``LIVE``, ``UNWATCHED``, ``BACKGROUND``). While a node request has waited ``SWITCH_AFTER``
   for a model the host has not loaded, the node sends nothing new to any other model, so the model it is stuck
   behind finishes the one request it is running, goes idle and is unloaded. The waiting request is then served,
   and the held ones go next. A waiting request holds only work of its own priority or lower: a routine waiting never
   holds the owner's chat or an answer. A hold for a switch ends at ``HOLD_LIMIT``; a hold for the model's own slot at
   ``SLOT_LIMIT``. Both end at the limit the caller passes for its own deadline, and then the request is not sent at
   all: once less than ``SEND_MIN_SECONDS`` of that limit is left, the caller gets ``HoldExpired``, a
   ``TimeoutError``, and the host gets nothing. The node keeps
   moving through its models as its requests need them; it never pins one.
   ``TOPOS_LOCAL_MODEL_SLOTS`` sets the per-model limit (0: no limit and no hold).

The second candidate's stress run (3 Nov) passed verification and sharing under load and left one gate: home chat's
median, 213 s for 200. The 27B served both the owner's live chat and routines in one slot, oldest first: 5 of its 11
requests were routines (2,800 to 3,200 prompt tokens, about 30 s of prompt reading each before a token), which nobody
watches arrive, and a live chat waited behind them up to 120 s. Then the 120 s limit sent held requests to the host
anyway, where they queue oldest first again. So, 4: priority within a model (``LIVE`` for the owner's own and answers,
``UNWATCHED`` for generation nobody watches stream, marked with ``unwatched``; ``BACKGROUND`` for work
``answer_gate.background_work`` marks), and a slot hold that lasts until the caller's own deadline.

The released 1.5.40 run of 3 Nov, with about 2 GiB of another app open, showed what one context for every model
costs. Before loading a model the host predicts its memory at the context asked for (the 27B 16.4 GiB, llama3.2 6.1
at 32,768) and unloads others when that does not fit what is free. All three models had fit together; with 2 GiB less
free they no longer did, and the host made 10 evictions in 15 minutes, each switch 30 to 46 s. Yet, by the host's
log that day, llama3.2's prompts never passed 1,560 tokens and the 27B's never 8,981 (the log does not name the model,
so each request was assigned to one by its reading speed on that host: an estimate, used only to size the steps).
Hence the steps in rule 1, decided per request from its own prompt and reply length and its model's name, never from
speed: llama3.2 stays at 8,192 (2.9 GiB instead of 5.6, measured by loading it) and the 27B at 16,384 unless a request
needs more.

The steps' own run (3 Nov, after a reboot, the same 2 GiB app open) had no evictions and no switch holds, yet macOS
swap went from 0 to 13.5 GiB in 20 minutes, the 27B's runs slowed (186 s at most, 138 before) and home chat's median
reached 233 s. The host's log showed where the memory went. The 9B runs on the host's MLX runner, which keeps a prefix
cache. That cache grew the runner from 8.4 GiB at load to 13.5 GiB (14.9 at peak) while it served at 8,192, and it
matched about 5 tokens per prompt, so it saved almost nothing. The runner is not bounded by ``num_ctx``, and the
host predicts a model's memory only when it loads one, so it kept loading the others into memory the 9B then grew
into. ``/api/ps`` does report the grown size (12.7 GB mid-run). So, 5:

5. ``grown_models``, run by ``watchdog`` every ``GROWTH_PERIOD``: a loaded model that has grown ``GROWTH_LIMIT`` past
   the smallest size the host reported for it at its context is unloaded. New requests to it are held from then on,
   so the one it is running finishes, and it is unloaded as soon as no node request is on it. Its next request
   reloads it at its load size (the 9B in about 8 s). The first version waited for the model to go idle by itself.
   On the live node, background work kept the 9B busy, and it grew 6.9 GiB in 50 s (to 15.2 GiB) before it did. The
   runner grew about 0.2 GiB per request, 2 GiB in one to two minutes of steady use. This reads only the sizes the host reports; a llama-server model never grows, so only a growing runner
   trips it. ``TOPOS_LOCAL_MODEL_RECYCLE_GROWTH_MIB`` sets the limit (2,048), and 0 turns it off.

This module counts requests and sends unloads. It reads no prompt and logs no content, only model names, contexts,
counts and seconds. With the node's opt-in diagnostics on (docs/OBSERVABILITY.md), each request is also a
``topos.local_model.request`` span with the same: model, context, whether it is background, how long the node held
it and why (``slot``, ``switch`` or ``none``), and the error class if it failed.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import itertools
import logging
import os
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

_log = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


def _default_context() -> int:
    """32,768 on a host with 32 GiB of memory or more, where it is also the host's own default (a request with no
    context is served at it, so sending it changes nothing for callers that never sent one). On a smaller host the
    host's default is lower and 32,768 might not fit beside the owner's apps, so callers keep their own there.
    ``OLLAMA_CONTEXT_LENGTH``, when the node can see it, is the host's chosen default and wins."""
    host = _env_int("OLLAMA_CONTEXT_LENGTH", 0)
    if host:
        return host
    try:
        memory = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (ValueError, OSError, AttributeError):
        return 0
    return 32768 if memory >= 32 * 2**30 else 0


#: The largest context a local request is served at, unless its prompt needs more. 0: callers keep their own.
CONTEXT = _env_int("TOPOS_LOCAL_MODEL_CONTEXT", _default_context())
#: The contexts a model moves up through, below CONTEXT. A model starts at the first that fits its request and moves
#: up only when a request needs more, never down while the node runs, so it is reloaded at most once per step and
#: never under itself. Measured on 3 Nov: llama3.2 takes 2.9 GiB at 8,192 and 5.6 at 32,768, and the host predicts the
#: 27B at 16.4 GiB at 32,768 (about 0.2 GiB less per 1,000 tokens). 0 in TOPOS_LOCAL_MODEL_CONTEXT_STEPS: every model
#: at CONTEXT.
STEPS = (8192, 16384) if _env_int("TOPOS_LOCAL_MODEL_CONTEXT_STEPS", 1) else ()
#: Tokens kept for the reply when a request does not say how long it may be (home chat's ran to 1,200 on 3 Nov).
_REPLY_TOKENS = 2048
#: A node request that has waited this long for a model the host has not loaded is blocked: the watchdog unloads the
#: idle models in its way. 0: no watchdog. A load the host makes on its own takes 3 to 22 s and shows in /api/ps
#: only once it is done, so a shorter wait would unload a model the host was about to keep beside the new one.
WAIT_SECONDS = float(_env_int("TOPOS_LOCAL_MODEL_RELEASE_AFTER_S", 15))
#: A node request that has waited this long for a model the host has not loaded holds new requests to other models.
SWITCH_AFTER = 3.0
#: Requests sent to one model at a time: the host's own, 1 unless OLLAMA_NUM_PARALLEL says otherwise. 0: no limit.
SLOTS = _env_int("TOPOS_LOCAL_MODEL_SLOTS", _env_int("OLLAMA_NUM_PARALLEL", 1))
#: No request is held for a model switch longer than this. An answer job ends by 110 s and a verification by 120 s.
HOLD_LIMIT = 120.0
#: No request is held for its model's own slot longer than this, unless its caller passes a shorter limit. Sending it
#: earlier would not serve it sooner (the host runs one at a time); it would only queue it where it cannot be ordered.
#: Long enough for a queue of home chat replies (300 s each at most), short enough that a count that never came down
#: cannot stop a model for good.
SLOT_LIMIT = 600.0
#: A request is sent only while at least this much of its caller's own limit is left. Past that the node does not
#: send it, and the caller gets ``HoldExpired``. Before this rule, a request held for its whole limit was sent anyway
#: with this much as its timeout. In the 1.5.45 stress run (7 Nov), two of the node's 25 s background calls to the 9B
#: were held 25.0 and 25.1 s for its slot and sent that way. Both timed out on the way, and the host logged each as a
#: 500 on /api/chat, its code for a client that hung up. No node call to the 9B on the owner's host answered in under
#: 0.71 s (4 to 7 Nov), so a request with less than this left could not have been answered.
SEND_MIN_SECONDS = 0.5
#: Priorities, lowest first served first. LIVE: the owner's own requests and answers. UNWATCHED: generation nobody
#: watches stream (a routine's step). BACKGROUND: work the node marked as background.
LIVE, UNWATCHED, BACKGROUND = 0, 1, 2
_PRIORITY_NAMES = {LIVE: "live", UNWATCHED: "unwatched", BACKGROUND: "background"}
#: How often a held request looks again.
POLL_SECONDS = 0.1
#: How often the watchdog looks. It asks the host for its loaded models only while a request is past SWITCH_AFTER.
PERIOD_SECONDS = 2.0
#: A model released is not released again for this long, so a host that keeps reloading it (an app calling the host
#: itself) is not fought. Short: moving between models is the node's normal work.
COOLDOWN_SECONDS = 15.0
#: A /api/ps reading older than this is not trusted to say what is loaded: nothing is held on it.
_LOADED_FRESH_SECONDS = 3 * PERIOD_SECONDS
#: A loaded model that has grown this far past its load size is unloaded once no node request is on it. 3 Nov: the
#: 9B's runner grew 5.1 GiB in 35 minutes of load, so about one reload of 8 s every 14 minutes. 0: never.
GROWTH_LIMIT = _env_int("TOPOS_LOCAL_MODEL_RECYCLE_GROWTH_MIB", 2048) * 2**20
#: How often the watchdog reads the host's model sizes while GROWTH_LIMIT is on (one /api/ps; the host's own app
#: reads it every 2 s).
GROWTH_PERIOD = 10.0
#: Characters per token when estimating whether a prompt needs more than CONTEXT. Low on purpose (JSON, names and
#: other languages run below 4), so a prompt near the line keeps the context its caller asked for.
_CHARS_PER_TOKEN = 3

_lock = threading.Lock()
_ids = itertools.count(1)
#: Requests the node has sent: (model, context, sent at, priority).
_in_flight: Dict[int, Tuple[str, Optional[int], float, int]] = {}
#: Requests the node holds: (model, priority, arrived).
_held: Dict[int, Tuple[str, int, float]] = {}
_unwatched: contextvars.ContextVar = contextvars.ContextVar("a_local_request_nobody_watches", default=False)
_released_at: Dict[str, float] = {}
#: The host's loaded models as the watchdog last read them, and when: [(model, context)], monotonic.
_loaded: Tuple[List[Tuple[str, Optional[int]]], float] = ([], float("-inf"))
#: Each model's context step so far: it only moves up.
_contexts: Dict[str, int] = {}
#: The smallest size the host has reported for each loaded (model, context), in bytes: its load size.
_sizes: Dict[Tuple[str, Optional[int]], int] = {}
#: Models being unloaded for growth: nothing new is sent to them until the unload returns.
_recycling: set = set()
#: Models past GROWTH_LIMIT waiting for the request they are running to end: (name, bytes grown, held since).
_draining: Dict[str, Tuple[str, int, float]] = {}
#: When the watchdog last read the host's sizes (monotonic).
_sized_at = float("-inf")
#: lowered_context: requests that asked for more than CONTEXT and did not need it (home chat's 81,920).
#: raised_context: times a model moved up a step. switch_holds: requests held so a request waiting for another model
#: could be served; held_s_max their longest. recycles: models unloaded for growing past GROWTH_LIMIT.
#: not_sent: requests whose hold used up their caller's limit, so the node never sent them (HoldExpired).
_stats: Dict[str, Any] = {"releases": 0, "last_release": None, "lowered_context": 0, "kept_larger_context": 0,
                          "raised_context": 0, "switch_holds": 0, "held_s_max": 0.0, "recycles": 0,
                          "last_recycle": None, "not_sent": 0}


class HoldExpired(TimeoutError):
    """A local request the node did not send: while it was held, less than ``SEND_MIN_SECONDS`` of its caller's own
    limit was left. Nothing reached the host. A ``TimeoutError``, as the call would have ended had it been sent."""


def model_key(name: str) -> str:
    """The host's name for a model: ``llama3.2`` and ``llama3.2:latest`` are one model."""
    name = (name or "").strip()
    return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"


def _prompt_chars(body: Dict[str, Any]) -> int:
    chars = len(body.get("prompt") or "") + len(body.get("system") or "")
    for message in body.get("messages") or ():
        if isinstance(message, dict):
            chars += len(message.get("content") or "")
    return chars


def serving_context(requested: Optional[int], *, prompt_chars: int, num_predict: Optional[int],
                    model: Optional[str] = None) -> Optional[int]:
    """The context to send: the model's step, moved up to the first that fits this request's prompt and reply; or
    the caller's own, when it asked for more than CONTEXT and its prompt and reply need more. With no model named,
    CONTEXT."""
    if CONTEXT <= 0:
        return requested
    need = prompt_chars // _CHARS_PER_TOKEN + int(num_predict or _REPLY_TOKENS)
    if requested and requested > CONTEXT and need > CONTEXT:
        return int(requested)
    if not model:
        return CONTEXT
    step = next((s for s in STEPS if need <= s < CONTEXT), CONTEXT)
    key = model_key(model)
    with _lock:
        current = _contexts.get(key)
        if current is None or step > current:
            _contexts[key] = step
            if current is not None:
                _stats["raised_context"] += 1
                _log.info("local model %s moves up to context %d (was %d)", key, step, current)
        return _contexts[key]


def prepare(body: Dict[str, Any]) -> Dict[str, Any]:
    """The request body with its ``options.num_ctx`` set to the serving context. An unload (no prompt, keep_alive 0)
    passes through untouched: the host unloads by name whatever context the model was loaded at."""
    if _is_unload(body) or CONTEXT <= 0:
        return body
    options = dict(body.get("options") or {})
    requested = options.get("num_ctx")
    context = serving_context(requested, prompt_chars=_prompt_chars(body), num_predict=options.get("num_predict"),
                              model=body.get("model"))
    if requested and requested > CONTEXT:
        with _lock:
            _stats["kept_larger_context" if context == requested else "lowered_context"] += 1
    options["num_ctx"] = context
    return {**body, "options": options}


def _is_unload(body: Dict[str, Any]) -> bool:
    """A request that only unloads: keep_alive 0 and no prompt (the engine's unload sends a single space)."""
    return (body.get("keep_alive") in (0, "0", "0s", "0m") and not (body.get("prompt") or "").strip()
            and not body.get("messages"))


@contextlib.contextmanager
def unwatched():
    """Mark the local requests made inside (and in every task it creates) as ``UNWATCHED``: generation nobody watches
    stream, which waits for the owner's live requests and answers on the same model."""
    token = _unwatched.set(True)
    try:
        yield
    finally:
        _unwatched.reset(token)


def _priority() -> int:
    """``BACKGROUND`` for work the node marked (``answer_gate.background_work``), ``UNWATCHED`` inside ``unwatched``,
    else ``LIVE``."""
    try:
        from .permissions_v2 import answer_gate
        if answer_gate.is_background():
            return BACKGROUND
    except Exception:  # noqa: BLE001 -- unmarked work is not background
        pass
    return UNWATCHED if _unwatched.get() else LIVE


def _on_a_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


@contextlib.contextmanager
def _span(ticket: "_Ticket"):
    """The request's diagnostics span; nothing when the node's diagnostics are off or unavailable."""
    try:
        from opentelemetry.trace import SpanKind
        from .observability.tracing import start_span
    except Exception:  # noqa: BLE001 -- diagnostics never fail a request
        yield None
        return
    with start_span("topos.local_model.request", kind=SpanKind.CLIENT, attributes={
            "topos.local_model.name": ticket.model, "topos.local_model.context": int(ticket.context or 0),
            "topos.local_model.background": ticket.priority == BACKGROUND,
            "topos.local_model.priority": _PRIORITY_NAMES[ticket.priority]}) as span:
        yield span


def _measure(kind: str, name: str, value: float, unit: str, attributes: Dict[str, Any]) -> None:
    try:
        from .observability.metrics import measure
    except Exception:  # noqa: BLE001
        return
    measure(kind, name, value, unit=unit, attributes=attributes)


def _waiters(now: float) -> List[Tuple[str, bool]]:
    """(model, priority) of each sent request that has waited SWITCH_AFTER for a model the host has not loaded,
    by the watchdog's last reading. Empty while that reading is stale: then nothing is known to wait, so nothing is
    held for a switch."""
    loaded, read_at = _loaded
    if now - read_at > _LOADED_FRESH_SECONDS:
        return []
    return [(model, priority) for model, context, sent, priority in _in_flight.values()
            if now - sent >= SWITCH_AFTER and not any(_matches(model, context, {"name": name, "context_length": c})
                                                     for name, c in loaded)]


def _admit(token: int, now: float, limit: Optional[float], *, waited: bool = True) -> Tuple[bool, bool]:
    """Whether a held request may be sent now, and whether a model switch is what holds it. Call with ``_lock``.
    ``limit`` is the caller's own (None: none). A request the node has already held (``waited``) is not sent once
    less than SEND_MIN_SECONDS of its limit is left: raises ``HoldExpired``. One it has not held yet goes as its
    caller sent it. The hold for a slot also ends at SLOT_LIMIT, for a switch at HOLD_LIMIT, and the request is then
    sent."""
    model, priority, arrived = _held[token]
    held = now - arrived
    if SLOTS <= 0:
        return True, False
    if waited and limit is not None and limit - held < SEND_MIN_SECONDS:
        raise HoldExpired(f"held {held:.1f} s of a {limit:.1f} s limit")
    if held >= SLOT_LIMIT:
        return True, False
    if model in _recycling or sum(1 for m, _, _, _ in _in_flight.values() if m == model) >= SLOTS:
        return False, False
    # One model's requests go in priority order, each priority by arrival.
    if any(m == model and (p, a, t) < (priority, arrived, token) for t, (m, p, a) in _held.items() if t != token):
        return False, False
    if held < HOLD_LIMIT:
        for waiting, waiting_priority in _waiters(now):
            if waiting != model and priority >= waiting_priority:
                return False, True
    return True, False


class _Ticket:
    """One local request through admission: held, sent, ended."""

    def __init__(self, body: Dict[str, Any], limit: Optional[float]):
        self.body = prepare(body)
        self.unload = _is_unload(self.body)
        self.limit = None if limit is None else max(0.0, float(limit))
        self.token = next(_ids)
        self.model = model_key(self.body.get("model") or "")
        self.context = (self.body.get("options") or {}).get("num_ctx")
        self.priority = _priority()
        self.arrived = time.monotonic()
        self.switch = False
        #: Whether the node has held it at least once: only then is it the node's hold that can use up its limit.
        self.waited = False
        self.sent: Optional[float] = None

    def hold(self) -> None:
        if not self.unload:
            with _lock:
                _held[self.token] = (self.model, self.priority, self.arrived)

    def try_send(self) -> bool:
        if self.unload:
            return True
        now = time.monotonic()
        held = now - self.arrived
        with _lock:
            try:
                admitted, switch = _admit(self.token, now, self.limit, waited=self.waited)
            except HoldExpired:
                # Out of its caller's time: no longer held, never sent.
                _held.pop(self.token, None)
                _stats["not_sent"] += 1
                admitted, expired = False, True
            else:
                expired = False
                self.switch = self.switch or switch
                self.waited = self.waited or not admitted
            if admitted:
                _held.pop(self.token, None)
                _in_flight[self.token] = (self.model, self.context, now, self.priority)
                self.sent = now
                if self.switch:
                    _stats["switch_holds"] += 1
                _stats["held_s_max"] = max(_stats["held_s_max"], round(held, 1))
        if expired:
            _log.info("local model %s: a request held %.1f s had %.1f s of its caller's %.1f s left; not sent",
                      self.model, held, max(0.0, self.limit - held), self.limit)
            raise HoldExpired(f"held {held:.1f} s of a {self.limit:.1f} s limit")
        if admitted and self.switch and held >= 1.0:
            _log.info("local model switch: held a request to %s %.1f s", self.model, held)
        return admitted

    def held_for(self) -> str:
        if self.switch:
            return "switch"
        return "slot" if self.sent is not None and self.sent - self.arrived >= 0.05 else "none"

    def opened(self, span) -> None:
        """After admission: how long the node held the request, and why."""
        if self.unload:
            return
        held = (self.sent or time.monotonic()) - self.arrived
        if span is not None:
            try:
                span.set_attribute("topos.local_model.held_s", round(held, 3))
                span.set_attribute("topos.local_model.held_for", self.held_for())
            except Exception:  # noqa: BLE001
                pass
        _measure("histogram", "topos.local_model.hold.duration", held, "s",
                 {"topos.local_model.name": self.model, "topos.local_model.held_for": self.held_for()})

    def closed(self, error: Optional[BaseException]) -> None:
        if self.unload or self.sent is None:
            return
        _measure("histogram", "topos.local_model.request.duration", time.monotonic() - self.sent, "s",
                 {"topos.local_model.name": self.model,
                  "topos.local_model.outcome": "ok" if error is None else type(error).__name__})

    def end(self) -> None:
        with _lock:
            _held.pop(self.token, None)
            _in_flight.pop(self.token, None)


@contextlib.contextmanager
def request(body: Dict[str, Any], *, limit: Optional[float] = None):
    """Prepare a local request, hold it until the node may send it (``_admit``), and count it in flight until it
    ends, however it ends. For a blocking call. On a thread that runs an event loop it is never held, because a wait
    there would stop that loop; it is still counted. ``limit`` caps the hold (see ``_admit`` for the defaults): a caller
    with a deadline passes what is left of it, and takes the time held off its own timeout. Raises ``HoldExpired``,
    without entering the body, once less than ``SEND_MIN_SECONDS`` of ``limit`` is left: nothing is sent."""
    ticket = _Ticket(body, limit)
    ticket.hold()
    error = None
    try:
        with _span(ticket) as span:
            if not _on_a_loop():
                while not ticket.try_send():
                    time.sleep(POLL_SECONDS)
            else:
                with _lock:
                    _held.pop(ticket.token, None)
                    if not ticket.unload:
                        ticket.sent = time.monotonic()
                        _in_flight[ticket.token] = (ticket.model, ticket.context, ticket.sent, ticket.priority)
            ticket.opened(span)
            try:
                yield ticket.body
            except BaseException as exc:
                error = exc
                raise
    finally:
        ticket.closed(error)
        ticket.end()


@contextlib.asynccontextmanager
async def request_async(body: Dict[str, Any], *, limit: Optional[float] = None):
    """``request`` for a coroutine: the hold awaits, so it never stops the loop."""
    ticket = _Ticket(body, limit)
    ticket.hold()
    error = None
    try:
        with _span(ticket) as span:
            while not ticket.try_send():
                await asyncio.sleep(POLL_SECONDS)
            ticket.opened(span)
            try:
                yield ticket.body
            except BaseException as exc:
                error = exc
                raise
    finally:
        ticket.closed(error)
        ticket.end()


def _deadline(kwargs: Dict[str, Any]) -> Optional[float]:
    timeout = kwargs.get("timeout")
    return float(timeout) if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) else None


def _rest(kwargs: Dict[str, Any], limit: Optional[float], started: float) -> Dict[str, Any]:
    """The call's own timeout less the time the node held it, so a hold never stretches the caller's deadline. A
    request is admitted only with SEND_MIN_SECONDS of its limit left, so the floor only covers the moment between."""
    if limit is None:
        return kwargs
    return {**kwargs, "timeout": max(SEND_MIN_SECONDS, limit - (time.monotonic() - started))}


async def post_async(http, url: str, *, json: Dict[str, Any], **kwargs):
    """``http.post(url, json=...)`` on an async client, through ``request_async``. A number ``timeout`` is also the
    hold's limit."""
    limit, started = _deadline(kwargs), time.monotonic()
    async with request_async(json, limit=limit) as body:
        return await http.post(url, json=body, **_rest(kwargs, limit, started))


def post(http, url: str, *, json: Dict[str, Any], **kwargs):
    """``http.post(url, json=...)`` on a blocking client, through ``request``. A number ``timeout`` is also the hold's
    limit."""
    limit, started = _deadline(kwargs), time.monotonic()
    with request(json, limit=limit) as body:
        return http.post(url, json=body, **_rest(kwargs, limit, started))


def in_flight() -> List[Tuple[str, Optional[int], float]]:
    """(model, context, sent at) of each request the node has sent and not seen end."""
    with _lock:
        return [(model, context, sent) for model, context, sent, _ in _in_flight.values()]


def _matches(model: str, context: Optional[int], loaded: Dict[str, Any]) -> bool:
    if model_key(str(loaded.get("name") or loaded.get("model") or "")) != model:
        return False
    return context is None or not loaded.get("context_length") or int(loaded["context_length"]) == int(context)


def release_blocked(loaded: Iterable[Dict[str, Any]], requests: Iterable[Tuple[str, Optional[int], float]],
                    now: float, *, wait_seconds: float = None, released_at: Dict[str, float] = None,
                    cooldown: float = COOLDOWN_SECONDS) -> Tuple[List[str], float]:
    """The loaded models to unload now, and the longest wait that called for it.

    ``loaded`` is the host's ``/api/ps`` list; ``requests`` the node's in-flight (model, context, started). A request
    waits for a load when no loaded model matches its model and context. Once one has waited ``wait_seconds``, every
    loaded model that no in-flight request matches is unloaded, except one released within ``cooldown``."""
    wait_seconds = WAIT_SECONDS if wait_seconds is None else wait_seconds
    released_at = {} if released_at is None else released_at
    loaded, requests = list(loaded), list(requests)
    waiting = [now - started for model, context, started in requests
               if now - started >= wait_seconds and not any(_matches(model, context, m) for m in loaded)]
    if not waiting:
        return [], 0.0
    idle = []
    for entry in loaded:
        name = str(entry.get("name") or entry.get("model") or "")
        if any(_matches(model, context, entry) for model, context, _ in requests):
            continue
        if now - released_at.get(model_key(name), float("-inf")) < cooldown:
            continue
        idle.append(name)
    return idle, max(waiting)


def _size_key(entry: Dict[str, Any]) -> Tuple[str, Optional[int]]:
    context = entry.get("context_length")
    return model_key(str(entry.get("name") or entry.get("model") or "")), int(context) if context else None


def grown_models(loaded: Iterable[Dict[str, Any]], sizes: Dict[Tuple[str, Optional[int]], int], *,
                 limit: int = None) -> List[Tuple[str, int]]:
    """The loaded models to unload for growth, each with how far it grew, in bytes. Updates ``sizes`` in place.

    ``loaded`` is the host's ``/api/ps`` list; ``sizes`` the smallest size seen for each (model, context) while
    loaded, which is its load size. A (model, context) the host no longer lists is forgotten, so a reload starts from
    its new size. A model reloaded at another context is another entry, so moving up a step is never read as growth."""
    limit = GROWTH_LIMIT if limit is None else limit
    loaded = list(loaded)
    present = set()
    grown = []
    for entry in loaded:
        size = entry.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            continue
        key = _size_key(entry)
        present.add(key)
        base = sizes[key] = min(size, sizes.get(key, size))
        if limit > 0 and size - base >= limit:
            grown.append((str(entry.get("name") or entry.get("model") or ""), size - base))
    for key in [k for k in sizes if k not in present]:
        del sizes[key]
    return grown


def _mark_grown(loaded: List[Dict[str, Any]], now: float) -> None:
    """Hold new requests to each model ``grown_models`` names, so the one it is running finishes and it goes idle."""
    with _lock:
        for name, grew in grown_models(loaded, _sizes):
            key = model_key(name)
            if key not in _recycling:
                _recycling.add(key)
                _draining[key] = (name, grew, now)


async def _unload_drained(client, base_url: str, now: float) -> List[str]:
    """Unload each held model no node request is on any more. Checked and held under one lock, so nothing is sent to
    it between that check and its unload. A model still busy after ``HOLD_LIMIT`` is let go unrecycled: its sizes
    are read again and it is held again only if it is still past the limit."""
    unload = []
    with _lock:
        busy = {m for m, _, _, _ in _in_flight.values()}
        for key, (name, grew, since) in list(_draining.items()):
            if key not in busy:
                unload.append((name, grew))
            elif now - since >= HOLD_LIMIT:
                _recycling.discard(key)
                del _draining[key]
    names = []
    for name, grew in unload:
        key = model_key(name)
        try:
            await client.post(base_url + "/api/generate", json={"model": name, "keep_alive": 0}, timeout=10)
        finally:
            with _lock:
                _recycling.discard(key)
                _draining.pop(key, None)
                _released_at[key] = now
        with _lock:
            for size_key in [k for k in _sizes if k[0] == key]:
                del _sizes[size_key]
            _stats["recycles"] += 1
            _stats["last_recycle"] = {"model": name, "at": time.time(), "grew_mib": grew // 2**20}
        names.append(name)
        _measure("counter", "topos.local_model.recycles", 1, "{unload}", {"topos.local_model.name": key})
        _log.info("local model %s grew %.1f GiB past its load size; unloaded so it reloads at that size", key,
                  grew / 2**30)
    return names


async def _check_once(client, base_url: str) -> List[str]:
    global _loaded, _sized_at
    now = time.monotonic()
    recycled = await _unload_drained(client, base_url, now) if _draining else []
    requests = in_flight()
    blocked = any(now - started >= SWITCH_AFTER for _, _, started in requests)
    sizing = GROWTH_LIMIT > 0 and now - _sized_at >= GROWTH_PERIOD
    if not blocked and not sizing:
        return recycled
    response = await client.get(base_url + "/api/ps", timeout=5)
    loaded = (response.json() or {}).get("models") or []
    now = time.monotonic()
    with _lock:
        _loaded = ([(model_key(str(m.get("name") or m.get("model") or "")), m.get("context_length")) for m in loaded],
                   now)
        released_at = dict(_released_at)
    if GROWTH_LIMIT > 0:
        _sized_at = now
        _mark_grown(loaded, now)
        recycled += await _unload_drained(client, base_url, now)
    if WAIT_SECONDS <= 0 or not blocked:
        return recycled
    names, waited = release_blocked([m for m in loaded if str(m.get("name") or m.get("model") or "") not in recycled],
                                    in_flight(), now, released_at=released_at)
    for name in names:
        await client.post(base_url + "/api/generate", json={"model": name, "keep_alive": 0}, timeout=10)
        with _lock:
            _released_at[model_key(name)] = now
            _stats["releases"] += 1
            _stats["last_release"] = {"model": name, "at": time.time(), "waited_s": round(waited, 1)}
        _measure("counter", "topos.local_model.releases", 1, "{unload}", {"topos.local_model.name": model_key(name)})
        _log.warning("local model host blocked %.0f s behind an idle model; unloaded %s", waited, name)
    return recycled + names


async def watchdog(base_url: str, *, period: float = PERIOD_SECONDS) -> None:
    """Run ``release_blocked`` and ``grown_models`` against the host every ``period`` seconds, for as long as the
    node runs."""
    if WAIT_SECONDS <= 0 and SLOTS <= 0 and GROWTH_LIMIT <= 0:
        return
    import httpx

    base_url = base_url.rstrip("/")
    async with httpx.AsyncClient() as client:
        while True:
            await asyncio.sleep(period)
            try:
                await _check_once(client, base_url)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- a host that cannot be read is left alone
                _log.debug("local model watchdog skipped a check: %s", type(exc).__name__)


def status() -> Dict[str, Any]:
    """For the shell status: the serving context, the watchdog's wait, and what it has done. Counts only."""
    now = time.monotonic()
    with _lock:
        requests = list(_in_flight.values())
        held = list(_held.values())
        stats = dict(_stats)
        contexts = dict(_contexts)
    return {
        "context": CONTEXT or None,
        "contexts": contexts,
        "release_after_s": WAIT_SECONDS or None,
        "slots": SLOTS or None,
        "recycle_growth_mib": GROWTH_LIMIT // 2**20 or None,
        "in_flight": len(requests),
        "longest_in_flight_s": round(max((now - s for _, _, s, _ in requests), default=0.0), 1),
        "held": len(held),
        "longest_held_s": round(max((now - a for _, _, a in held), default=0.0), 1),
        **stats,
    }
