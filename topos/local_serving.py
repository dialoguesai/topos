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
   ``TOPOS_LOCAL_MODEL_RELEASE_AFTER_S`` sets the wait, and 0 turns the watchdog off.

This module counts requests and sends unloads. It reads no prompt and logs no content, only model names, contexts,
counts and seconds.
"""
from __future__ import annotations

import asyncio
import contextlib
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
#: A node request that has waited this long for a model the host has not loaded is blocked. 0: no watchdog.
WAIT_SECONDS = float(_env_int("TOPOS_LOCAL_MODEL_RELEASE_AFTER_S", 30))
#: How often the watchdog looks. It asks the host for its loaded models only while a request is past WAIT_SECONDS.
PERIOD_SECONDS = 5.0
#: A model released is not released again for this long, so a host that keeps reloading it is not fought.
COOLDOWN_SECONDS = 60.0
#: Characters per token when estimating whether a prompt needs more than CONTEXT. Low on purpose (JSON, names and
#: other languages run below 4), so a prompt near the line keeps the context its caller asked for.
_CHARS_PER_TOKEN = 3

_lock = threading.Lock()
_ids = itertools.count(1)
_in_flight: Dict[int, Tuple[str, Optional[int], float]] = {}
_released_at: Dict[str, float] = {}
#: lowered_context: requests that asked for more than CONTEXT and did not need it (home chat's 81,920).
_stats: Dict[str, Any] = {"releases": 0, "last_release": None, "lowered_context": 0, "kept_larger_context": 0}


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
def request(body: Dict[str, Any]):
    """Prepare a local request and count it in flight until it ends, however it ends. Usable around a blocking call
    and around an ``await``: it only counts."""
    body = prepare(body)
    if _is_unload(body):
        yield body
        return
    token = next(_ids)
    with _lock:
        _in_flight[token] = (model_key(body.get("model") or ""), (body.get("options") or {}).get("num_ctx"),
                             time.monotonic())
    try:
        yield body
    finally:
        with _lock:
            _in_flight.pop(token, None)


async def post_async(http, url: str, *, json: Dict[str, Any], **kwargs):
    """``http.post(url, json=...)`` on an async client, through ``request``."""
    with request(json) as body:
        return await http.post(url, json=body, **kwargs)


def post(http, url: str, *, json: Dict[str, Any], **kwargs):
    """``http.post(url, json=...)`` on a blocking client, through ``request``."""
    with request(json) as body:
        return http.post(url, json=body, **kwargs)


def in_flight() -> List[Tuple[str, Optional[int], float]]:
    with _lock:
        return list(_in_flight.values())


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
    now = time.monotonic()
    requests = in_flight()
    if not any(now - started >= WAIT_SECONDS for _, _, started in requests):
        return []
    response = await client.get(base_url + "/api/ps", timeout=5)
    loaded = (response.json() or {}).get("models") or []
    with _lock:
        released_at = dict(_released_at)
    names, waited = release_blocked(loaded, requests, now, released_at=released_at)
    for name in names:
        await client.post(base_url + "/api/generate", json={"model": name, "keep_alive": 0}, timeout=10)
        with _lock:
            _released_at[model_key(name)] = now
            _stats["releases"] += 1
            _stats["last_release"] = {"model": name, "at": time.time(), "waited_s": round(waited, 1)}
        _log.warning("local model host blocked %.0f s behind an idle model; unloaded %s", waited, name)
    return names


async def watchdog(base_url: str, *, period: float = PERIOD_SECONDS) -> None:
    """Run ``release_blocked`` against the host every ``period`` seconds, for as long as the node runs."""
    if WAIT_SECONDS <= 0:
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
        stats = dict(_stats)
    return {
        "context": CONTEXT or None,
        "release_after_s": WAIT_SECONDS or None,
        "in_flight": len(requests),
        "longest_in_flight_s": round(max((now - s for _, _, s in requests), default=0.0), 1),
        **stats,
    }
