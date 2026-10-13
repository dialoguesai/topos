"""NR166 R3b: say what an event loop was doing when it stopped running. Attribution only; off by default.

The node has two loops: the app's (every owner-app read and every relayed request that is not a share door) and the
control-plane client's (the connection, its pings, the share doors' waits). One blocking call on either stalls
everything else on it, and until now the log could only say that a request was late, never what the loop was in.

With ``TOPOS_PERMISSIONS_V2_SEARCH_ATTRIBUTION`` on (and timings, which it needs) when a loop starts, that loop
stamps a clock a few times a second and one watcher thread reads the stamps. A loop whose stamp is older than
``LAG_SECONDS`` is stalled: the watcher reads that loop's thread's Python stack (``sys._current_frames``) and
writes, at most once per ``EVERY_SECONDS`` per loop:

    permission_search_timing run=<32 hex> stage=loop_lag elapsed_ms=<lag so far> corr=- t_ms=<clock>
        loop=<app|client> stalls=<n> longest_ms=<ms> frames=<k>
    permission_search_timing ... stage=loop_lag_frame elapsed_ms=0.000 ... loop=<name> depth=<i> site=<file:line:fn>
    permission_search_timing ... stage=loop_lag_end elapsed_ms=<the whole stall> ... loop=<name>

``loop_lag_end`` is written once, when the stall a ``loop_lag`` line caught ends, with its whole length.
``stalls`` and ``longest_ms`` cover the other stalls that finished since this loop's last ``loop_lag`` line: the
ones the rate limit kept quiet are still counted. A frame is a file's base name, a line number and a function
name: code, never a value. No local, argument, message, id or path is read. Innermost frame first, ``FRAMES`` at
most.

The switch is read when the loop starts. Off: no stamp, no thread, nothing written. The watcher never touches the
loop, the write gate or a database; it only reads a float and, when a loop is stalled, that thread's frames.
"""
from __future__ import annotations

import sys
import threading
import time

#: A loop that has not run for this long is stalled.
LAG_SECONDS = 1.0
#: At most one report per loop in this long.
EVERY_SECONDS = 10.0
#: How often a watched loop stamps its clock, and how often the watcher reads the stamps.
BEAT_SECONDS = 0.25
#: Innermost frames written per report.
FRAMES = 12

_lock = threading.Lock()
_watched: dict = {}      # loop name -> _Watch
_thread: threading.Thread | None = None


class _Watch:
    def __init__(self, loop, name: str, ident: int):
        self.loop, self.name, self.ident = loop, name, ident
        self.beat = time.monotonic()
        self.started = False        # the loop has run at least one beat
        self.gone = False
        self.reported_at: float | None = None
        self.stalled_from: float | None = None   # the stamp a stall in progress began at
        self.reported: float | None = None       # ... and the one a `loop_lag` line was written for
        self.stalls = 0
        self.longest = 0.0

    def tick(self) -> None:
        """On the loop: stamp the clock and ask to be called again."""
        if self.gone:
            return
        self.beat = time.monotonic()
        self.started = True
        try:
            self.loop.call_later(BEAT_SECONDS, self.tick)
        except RuntimeError:   # the loop is closed
            self.gone = True


def watch(loop, name: str) -> bool:
    """Watch `loop`, which runs (or is about to run) on the calling thread. False, and nothing started, unless
    attribution is on. Never raises: a watchdog must not stop a node from starting."""
    global _thread
    try:
        from . import search_timing
        if name not in ("app", "client") or not search_timing.attributing():
            return False
        watched = _Watch(loop, name, threading.get_ident())
        with _lock:
            previous = _watched.get(name)
            if previous is not None:
                previous.gone = True
            _watched[name] = watched
            if _thread is None or not _thread.is_alive():
                _thread = threading.Thread(target=_run, name="loop-lag-watch", daemon=True)
                _thread.start()
        loop.call_soon(watched.tick)
        return True
    except Exception:  # noqa: BLE001
        return False


def unwatch(name: str) -> None:
    with _lock:
        watched = _watched.pop(name, None)
    if watched is not None:
        watched.gone = True


def _site(frame) -> str:
    """One frame as `file:line:function`: the file's base name only."""
    code = frame.f_code
    return f"{code.co_filename.rsplit('/', 1)[-1]}:{frame.f_lineno}:{code.co_name}"


def _stack(ident: int) -> list:
    frame = sys._current_frames().get(ident)
    sites = []
    while frame is not None and len(sites) < FRAMES:
        sites.append(_site(frame))
        frame = frame.f_back
    return sites


def _check(watched: _Watch, now: float) -> None:
    beat = watched.beat
    lag = now - beat
    if watched.stalled_from is not None and beat != watched.stalled_from:
        # The stall that was in progress has ended: the loop stamped again.
        length = beat - watched.stalled_from
        if watched.stalled_from == watched.reported:
            from . import search_timing
            search_timing.SearchTiming().emit("loop_lag_end", length, loop=watched.name)
        else:
            watched.stalls += 1
            watched.longest = max(watched.longest, length)
        watched.stalled_from = watched.reported = None
    if lag <= LAG_SECONDS:
        return
    watched.stalled_from = beat
    if watched.reported == beat or (watched.reported_at is not None and now - watched.reported_at < EVERY_SECONDS):
        return
    from . import search_timing
    if not search_timing.attributing():
        return
    sites = _stack(watched.ident)
    watched.reported_at, watched.reported = now, beat
    stalls, longest = watched.stalls, watched.longest
    watched.stalls, watched.longest = 0, 0.0
    line = search_timing.SearchTiming()
    line.emit("loop_lag", lag, loop=watched.name, stalls=stalls, longest_ms=f"{longest * 1000:.3f}",
              frames=len(sites))
    for depth, site in enumerate(sites):
        line.emit("loop_lag_frame", 0.0, loop=watched.name, depth=depth, site=search_timing._token(site, 120))


def _run() -> None:
    global _thread
    while True:
        time.sleep(BEAT_SECONDS)
        try:
            with _lock:
                current = list(_watched.items())
                if not current:      # nothing left to watch: this thread ends, and `watch` starts another
                    _thread = None
                    return
            now = time.monotonic()
            for name, watched in current:
                ended = watched.gone or watched.loop.is_closed() or (watched.started and not watched.loop.is_running())
                if ended:
                    watched.gone = True
                    with _lock:
                        if _watched.get(name) is watched:
                            del _watched[name]
                    continue
                if watched.started:
                    _check(watched, now)
        except Exception:  # noqa: BLE001 -- diagnostics never take the node down
            pass

