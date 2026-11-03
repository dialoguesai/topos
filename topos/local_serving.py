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

1. ``prepare`` / ``request``: every local request is sent at the one serving context, ``CONTEXT`` (32,768 on a host
   of 32 GiB or more, see ``_default_context``; ``TOPOS_LOCAL_MODEL_CONTEXT``, where 0 leaves each caller's own). A request that asked for more keeps it, but
   only when its prompt and output need more. A larger context never truncates more, so raising a caller's 4,096 or
   8,192 changes no reply. It only stops the reload.
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
   ``SLOT_LIMIT``; both at the limit the caller passes for its own deadline. The node keeps
   moving through its models as its requests need them; it never pins one.
   ``TOPOS_LOCAL_MODEL_SLOTS`` sets the per-model limit (0: no limit and no hold).

The second candidate's stress run (3 Nov) passed verification and sharing under load and left one gate: home chat's
median, 213 s for 200. The 27B served both the owner's live chat and routines in one slot, oldest first: 5 of its 11
requests were routines (2,800 to 3,200 prompt tokens, about 30 s of prompt reading each before a token), which nobody
watches arrive, and a live chat waited behind them up to 120 s. Then the 120 s limit sent held requests to the host
anyway, where they queue oldest first again. So, 4: priority within a model (``LIVE`` for the owner's own and answers,
``UNWATCHED`` for generation nobody watches stream, marked with ``unwatched``; ``BACKGROUND`` for work
``answer_gate.background_work`` marks), and a slot hold that lasts until the caller's own deadline.

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


#: The one context every local request is served at. 0: callers keep their own.
CONTEXT = _env_int("TOPOS_LOCAL_MODEL_CONTEXT", _default_context())
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
#: lowered_context: requests that asked for more than CONTEXT and did not need it (home chat's 81,920).
#: switch_holds: requests held so a request waiting for another model could be served; held_s_max their longest.
_stats: Dict[str, Any] = {"releases": 0, "last_release": None, "lowered_context": 0, "kept_larger_context": 0,
                          "switch_holds": 0, "held_s_max": 0.0}


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


def serving_context(requested: Optional[int], *, prompt_chars: int, num_predict: Optional[int]) -> Optional[int]:
    """The context to send: CONTEXT, unless the caller asked for more and its prompt and output need more."""
    if CONTEXT <= 0:
        return requested
    need = prompt_chars // _CHARS_PER_TOKEN + int(num_predict or 0)
    if requested and requested > CONTEXT and need > CONTEXT:
        return int(requested)
    return CONTEXT


def prepare(body: Dict[str, Any]) -> Dict[str, Any]:
    """The request body with its ``options.num_ctx`` set to the serving context. An unload (no prompt, keep_alive 0)
    passes through untouched: the host unloads by name whatever context the model was loaded at."""
    if _is_unload(body) or CONTEXT <= 0:
        return body
    options = dict(body.get("options") or {})
    requested = options.get("num_ctx")
    context = serving_context(requested, prompt_chars=_prompt_chars(body), num_predict=options.get("num_predict"))
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


def _admit(token: int, now: float, limit: Optional[float]) -> Tuple[bool, bool]:
    """Whether a held request may be sent now, and whether a model switch is what holds it. Call with ``_lock``.
    ``limit`` is the caller's own (None: none); the hold for a slot also ends at SLOT_LIMIT, for a switch at
    HOLD_LIMIT."""
    model, priority, arrived = _held[token]
    held = now - arrived
    if SLOTS <= 0 or (limit is not None and held >= limit) or held >= SLOT_LIMIT:
        return True, False
    if sum(1 for m, _, _, _ in _in_flight.values() if m == model) >= SLOTS:
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
        self.sent: Optional[float] = None

    def hold(self) -> None:
        if not self.unload:
            with _lock:
                _held[self.token] = (self.model, self.priority, self.arrived)

    def try_send(self) -> bool:
        if self.unload:
            return True
        now = time.monotonic()
        with _lock:
            admitted, switch = _admit(self.token, now, self.limit)
            self.switch = self.switch or switch
            if admitted:
                _held.pop(self.token, None)
                _in_flight[self.token] = (self.model, self.context, now, self.priority)
                self.sent = now
                held = now - self.arrived
                if self.switch:
                    _stats["switch_holds"] += 1
                _stats["held_s_max"] = max(_stats["held_s_max"], round(held, 1))
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
    with a deadline passes what is left of it, and takes the time held off its own timeout."""
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
    """The call's own timeout less the time the node held it, so a hold never stretches the caller's deadline."""
    if limit is None:
        return kwargs
    return {**kwargs, "timeout": max(0.5, limit - (time.monotonic() - started))}


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


async def _check_once(client, base_url: str) -> List[str]:
    global _loaded
    now = time.monotonic()
    requests = in_flight()
    if not any(now - started >= SWITCH_AFTER for _, _, started in requests):
        return []
    response = await client.get(base_url + "/api/ps", timeout=5)
    loaded = (response.json() or {}).get("models") or []
    now = time.monotonic()
    with _lock:
        _loaded = ([(model_key(str(m.get("name") or m.get("model") or "")), m.get("context_length")) for m in loaded],
                   now)
        released_at = dict(_released_at)
    if WAIT_SECONDS <= 0:
        return []
    names, waited = release_blocked(loaded, in_flight(), now, released_at=released_at)
    for name in names:
        await client.post(base_url + "/api/generate", json={"model": name, "keep_alive": 0}, timeout=10)
        with _lock:
            _released_at[model_key(name)] = now
            _stats["releases"] += 1
            _stats["last_release"] = {"model": name, "at": time.time(), "waited_s": round(waited, 1)}
        _measure("counter", "topos.local_model.releases", 1, "{unload}", {"topos.local_model.name": model_key(name)})
        _log.warning("local model host blocked %.0f s behind an idle model; unloaded %s", waited, name)
    return names


async def watchdog(base_url: str, *, period: float = PERIOD_SECONDS) -> None:
    """Run ``release_blocked`` against the host every ``period`` seconds, for as long as the node runs."""
    if WAIT_SECONDS <= 0 and SLOTS <= 0:
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
    return {
        "context": CONTEXT or None,
        "release_after_s": WAIT_SECONDS or None,
        "slots": SLOTS or None,
        "in_flight": len(requests),
        "longest_in_flight_s": round(max((now - s for _, _, s, _ in requests), default=0.0), 1),
        "held": len(held),
        "longest_held_s": round(max((now - a for _, _, a in held), default=0.0), 1),
        **stats,
    }
