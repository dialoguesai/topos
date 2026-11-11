"""Automatic catch-up for fact packs: the owner's standing switch, and the look that acts on it.

Why. A fact pack reads a new record as it arrives only for a source whose canonical lane lists the
`derivation` job, and few sources do. For every other source the facts come from a backfill, and a
backfill is one typed yes each time (`topos-node packs backfill`). So on a node where most records
come from sources without that job, facts stop keeping up the day the owner stops running the command.

What this is. One switch, off by default, that means "keep my switched-on fact packs current". Only the
owner turns it on or off (`topos-node packs auto on|off`, one typed yes on a terminal, or the app through
the same confirmed route). While it is on the node starts a catch-up run by itself, on the machinery a
confirmed backfill uses (`backfill_runs`): the same one-run slot, the same batch (`run_pack_backfill`),
the same yield to the owner's requests between records, the same progress keys.

When a run starts (`tick`, once a minute):

* the switch is on and the environment does not say off (`TOPOS_PACKS_AUTO_CATCHUP=off`);
* journal entries or messages have landed since the last automatic look. This reads the two record tables,
  not a source's lane, so it covers every source at once, records an app pushed through app ingest included;
* nothing new has landed for the quiet period (default 10 minutes), so a sync or an import in progress is
  left to finish. The node's start counts as an arrival: nothing starts in the first quiet period after it;
* the last automatic look ended at least the minimum gap ago (default 1 hour);
* no backfill run and no single batch is working. An automatic run never starts beside anything, and the
  owner's backfill wins over one that is working (`backfill_runs.start_run`, `yield_to_owner`).

What a run reads. Every pack that is switched on and has something waiting, and only recent records:
those dated on or after the day before the last automatic look that read everything it reached
(`OVERLAP_DAYS`), and never further back than `MAX_WINDOW_DAYS` however long the node was off. The first
look after the switch is turned on reads `FIRST_WINDOW_DAYS`. Older history stays a manual backfill, and
so does a record that lands late carrying a date before the window: the look counts those
(`older_records`) and says so, it does not widen the window for them, because a window widened for one
late record would also read every unread record of those days. Records already read are keyed in
`derivation_progress`, so the overlap costs a key lookup each and no model call.

What it never does. It loads no model to look: the count before a run is the plan's own counting rule,
which reads text and the progress keys and calls nothing, and a look that finds nothing waiting starts
no run. It keeps no model resident: a run uses the extractor the owner picked and the verifier setting
the node already has, exactly as a manual backfill does, and holds nothing afterwards. It never changes a
setting: the switch is written only by `set_switch`, which only the confirmed route calls.

State. Two `engine_config` rows. `packs_auto_catchup` is the owner's: on or off, and the two periods when
the owner set them. `packs_auto_catchup_state` is the node's own bookkeeping: how far the last look
reached and how the last run ended (ids, dates, state words and counts; no record text). Every write is a
gated, committed write of its own (`batched_writes`), made with nothing else open on the connection.

A node restart ends a run, as it ends any backfill run. The state survives, the record marks were not
moved by the unfinished run, and the next look starts another over the same window.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from . import backfill_runs as runs

logger = logging.getLogger("topos.derivation.auto_catchup")

#: The owner's switch (JSON: enabled, quiet_s, min_gap_s, changed_at). Written only by `set_switch`.
ENGINE_CONFIG_KEY_SWITCH = "packs_auto_catchup"
#: The node's bookkeeping (JSON: covered, base_at, last). Written only by this module's own looks and runs.
ENGINE_CONFIG_KEY_STATE = "packs_auto_catchup_state"

#: `off` (or 0 / false / no) stops every automatic look, whatever the switch says.
KILL_ENV = "TOPOS_PACKS_AUTO_CATCHUP"
#: Node-wide defaults for the two periods, in seconds; the owner's own values on the switch win.
QUIET_ENV = "TOPOS_PACKS_AUTO_QUIET_S"
MIN_GAP_ENV = "TOPOS_PACKS_AUTO_MIN_GAP_S"

DEFAULT_QUIET_S = 600
DEFAULT_MIN_GAP_S = 3600
QUIET_RANGE_S = (60, 24 * 3600)
MIN_GAP_RANGE_S = (300, 7 * 24 * 3600)
#: The first look after the switch is turned on reads this many days back.
FIRST_WINDOW_DAYS = 7
#: A look re-reads this many days before the last one, for records that carry an earlier date.
OVERLAP_DAYS = 1
#: No automatic run reads further back than this, however long the node was off.
MAX_WINDOW_DAYS = 30

TICK_SECONDS = 60.0
STARTUP_DELAY_SECONDS = 120.0

#: The two record tables a backfill walks, with the column that dates a record.
TABLES = (("journal_entries", "entry_at"), ("conversation_messages", "event_at"))

_OFF = frozenset({"0", "false", "off", "no"})
_COUNT_KEYS = ("processed", "assertions", "accepted", "written", "quarantined", "llm_errors")
_TIME_KEYS = ("extract_s", "verify_s")


class AutoRefused(ValueError):
    """The switch cannot be set as asked; `code` is the word the route answers with."""

    code = "auto_catchup_invalid"


# --------------------------------------------------------------------------- small reads
def kill_switch() -> bool:
    """The environment says no automatic looks on this node."""
    return os.environ.get(KILL_ENV, "on").strip().lower() in _OFF


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: Any) -> Optional[datetime]:
    try:
        when = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)


def _env_seconds(name: str, default: int, bounds: tuple) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return min(max(value, bounds[0]), bounds[1])


def _read_json(conn: sqlite3.Connection, key: str) -> Dict[str, Any]:
    try:
        row = conn.execute("SELECT value FROM engine_config WHERE key = ?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row or not row[0]:
        return {}
    try:
        value = json.loads(row[0])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(conn: sqlite3.Connection, key: str, value: Dict[str, Any]) -> None:
    """One gated, committed write of its own. The caller's connection has nothing open: taking the
    write gate while holding SQLite's write lock is the inversion topos#279 removed from the backfill."""
    from ...storage.db.write_gate import batched_writes

    if getattr(conn, "in_transaction", False):
        conn.commit()
    with batched_writes(conn):
        conn.execute("INSERT OR REPLACE INTO engine_config (key, value) VALUES (?, ?)",
                     (key, json.dumps(value, sort_keys=True)))


def _period(value: Any, bounds: tuple) -> Optional[int]:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) \
        and bounds[0] <= value <= bounds[1] else None


def read_switch(conn: sqlite3.Connection) -> Dict[str, Any]:
    """The owner's switch with the periods a look uses: the owner's own, else the node's defaults."""
    raw = _read_json(conn, ENGINE_CONFIG_KEY_SWITCH)
    quiet = _period(raw.get("quiet_s"), QUIET_RANGE_S)
    gap = _period(raw.get("min_gap_s"), MIN_GAP_RANGE_S)
    return {"enabled": raw.get("enabled") is True,
            "quiet_s": quiet if quiet is not None else _env_seconds(QUIET_ENV, DEFAULT_QUIET_S, QUIET_RANGE_S),
            "min_gap_s": gap if gap is not None else _env_seconds(MIN_GAP_ENV, DEFAULT_MIN_GAP_S, MIN_GAP_RANGE_S),
            "quiet_set": quiet is not None, "min_gap_set": gap is not None,
            "changed_at": raw.get("changed_at") if isinstance(raw.get("changed_at"), str) else None}


def read_state(conn: sqlite3.Connection) -> Dict[str, Any]:
    return _read_json(conn, ENGINE_CONFIG_KEY_STATE)


# --------------------------------------------------------------------------- the switch (the owner's)
def set_switch(conn: sqlite3.Connection, enabled: Any, *, quiet_s: Any = None, min_gap_s: Any = None,
               now: Optional[datetime] = None) -> Dict[str, Any]:
    """Turn the standing switch on or off. The caller holds the owner's confirmation (the route in
    `api/signal.py` checks the confirmed socket peer); nothing else calls this.

    `quiet_s` / `min_gap_s` are kept when given and left as they were when not. Turning it on from off
    forgets how far the last look reached, so the next one reads the first window again and no further:
    the yes covers recent records, not whatever piled up while the switch was off. Turning it off asks
    an automatic run that is working to stop after the record in hand."""
    if not isinstance(enabled, bool):
        raise AutoRefused("enabled must be true or false")
    for label, value, bounds in (("quiet_s", quiet_s, QUIET_RANGE_S), ("min_gap_s", min_gap_s, MIN_GAP_RANGE_S)):
        if value is not None and _period(value, bounds) is None:
            raise AutoRefused(f"{label} must be a whole number of seconds from {bounds[0]} to {bounds[1]}")
    before = _read_json(conn, ENGINE_CONFIG_KEY_SWITCH)
    was_on = before.get("enabled") is True
    switch = {"enabled": enabled,
              "quiet_s": quiet_s if quiet_s is not None else _period(before.get("quiet_s"), QUIET_RANGE_S),
              "min_gap_s": min_gap_s if min_gap_s is not None else _period(before.get("min_gap_s"), MIN_GAP_RANGE_S),
              "changed_at": _iso(now or _utcnow())}
    with _bookkeeping:
        _write_json(conn, ENGINE_CONFIG_KEY_SWITCH, switch)
        if enabled and not was_on:
            state = read_state(conn)
            if state.get("covered") is not None or state.get("base_at") is not None:
                _write_json(conn, ENGINE_CONFIG_KEY_STATE, {"last": state.get("last")})
    if not enabled and runs.stop_automatic("switched_off"):
        logger.info("automatic catch-up switched off: the run in flight stops after the record in hand")
    held = read_switch(conn)
    logger.info("automatic catch-up switched %s by the owner (quiet %ss, minimum gap %ss)",
                "on" if enabled else "off", held["quiet_s"], held["min_gap_s"])
    return describe(conn, now=now)


# --------------------------------------------------------------------------- what has landed
def read_marker(conn: sqlite3.Connection) -> Dict[str, int]:
    """The newest row of each record table (its rowid): one index step each, no text read."""
    marker: Dict[str, int] = {}
    for table, _column in TABLES:
        try:
            row = conn.execute(f"SELECT MAX(rowid) FROM {table}").fetchone()
        except sqlite3.OperationalError:
            continue        # a node that never held this kind of record
        marker[table] = int(row[0]) if row and row[0] is not None else 0
    return marker


def _fresh(marker: Dict[str, int], covered: Any) -> bool:
    """Whether rows landed past what the last look covered. With nothing covered yet, any row is new."""
    return any(rowid > _covered_rowid(covered, table) for table, rowid in marker.items())


def _covered_rowid(covered: Any, table: str) -> int:
    return int(covered.get(table) or 0) if isinstance(covered, dict) else 0


def _new_records(conn: sqlite3.Connection, covered: Any, since: str) -> tuple:
    """`(landed since the last look, of those dated before the window)`; `(None, 0)` on the first look,
    which reads its window whatever landed when."""
    if not isinstance(covered, dict):
        return None, 0
    total = older = 0
    for table, column in TABLES:
        try:
            row = conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM(CASE WHEN {column} < ? THEN 1 ELSE 0 END), 0)"
                f" FROM {table} WHERE rowid > ?", (since, _covered_rowid(covered, table))).fetchone()
        except sqlite3.OperationalError:
            continue
        total += int(row[0] or 0)
        older += int(row[1] or 0)
    return total, older


def window_start(state: Dict[str, Any], now: datetime) -> str:
    """The `since` date of the next automatic run (see the module docstring)."""
    today = now.astimezone(timezone.utc).date()
    base_at = _parse(state.get("base_at"))
    if base_at is None or not isinstance(state.get("covered"), dict):
        return (today - timedelta(days=FIRST_WINDOW_DAYS)).isoformat()
    since = base_at.astimezone(timezone.utc).date() - timedelta(days=OVERLAP_DAYS)
    return max(since, today - timedelta(days=MAX_WINDOW_DAYS)).isoformat()


def waiting_by_pack(conn: sqlite3.Connection, since: str) -> Dict[str, int]:
    """Records waiting for each switched-on pack, dated `since` or later: the plan's counting rule
    (`backfill_runs.waiting_counts`), asked per record. Reads only; no model is loaded or called."""
    from .packs import load_packs
    from .registry import bundled_pack_dir

    try:
        enabled = sorted(str(r[0]) for r in conn.execute("SELECT pack_id FROM pack_registry WHERE enabled = 1"))
    except sqlite3.OperationalError:
        return {}
    catalog = load_packs(bundled_pack_dir(), only=enabled) if enabled else {}
    countable = [pid for pid in enabled if catalog.get(pid) is not None]
    waiting, _history = runs.waiting_counts(conn, catalog, countable, since, per_record=True)
    return waiting


# --------------------------------------------------------------------------- the look
class _Watch:
    """When the record tables last grew, as this process saw it. In memory on purpose: the node's start
    is itself a reason to wait (it rebuilds indexes), so the first sight of the tables counts as a change."""

    def __init__(self) -> None:
        self.marker: Optional[Dict[str, int]] = None
        self.changed_at = 0.0

    def observe(self, marker: Dict[str, int], at: float) -> float:
        """Record what a look saw; the seconds since the tables last changed."""
        if self.marker != marker:
            self.marker, self.changed_at = dict(marker), at
        return at - self.changed_at

    def quiet_for(self, marker: Dict[str, int], at: float) -> float:
        """The same answer for a reader that must not move the watch (`describe`)."""
        return at - self.changed_at if self.marker == marker else 0.0


_watch = _Watch()
#: Orders the bookkeeping writes of a look and of its run's end, which happen on two threads.
_bookkeeping = threading.RLock()


def _reset_for_tests() -> None:
    global _watch
    _watch = _Watch()


def _arrivals(new_records: Optional[int], older: int = 0) -> str:
    if new_records is None:
        return "first look since the switch was turned on"
    return f"{new_records:,} new records since the last look" + (
        f" ({older:,} of them dated before the window: left for a manual backfill)" if older else "")


def _since_last(state: Dict[str, Any], now: datetime) -> Optional[float]:
    last = state.get("last") if isinstance(state.get("last"), dict) else {}
    ended = _parse(last.get("finished_at")) or _parse(last.get("started_at"))
    return (now - ended).total_seconds() if ended is not None else None


def tick(conn: sqlite3.Connection, *, wall: Callable[[], datetime] = _utcnow,
         clock: Callable[[], float] = time.monotonic, run_batch: Optional[runs.BatchRunner] = None,
         conn_factory: Optional[Callable[[], Any]] = None) -> Dict[str, Any]:
    """One look, on a worker thread with that thread's own connection. Starts at most one run.

    Returns `{"decision": word, ...}`: `kill_switch`, `off`, `run_active`, `no_new_records`,
    `ingest_busy`, `min_gap`, `switch_changed`, `nothing_waiting` or `started`. While the switch is off a look reads one
    row and nothing else. `wall` is the time of day (the stamps and the minimum gap), `clock` the
    monotonic one (the quiet period). `conn_factory` resolves the connection the run's end is recorded
    on, on the run's own thread (the default is that thread's node connection)."""
    if kill_switch():
        return {"decision": "kill_switch"}
    switch = read_switch(conn)
    if not switch["enabled"]:
        return {"decision": "off"}
    now = wall()
    marker = read_marker(conn)
    quiet_for = _watch.observe(marker, clock())
    if runs.busy():
        return {"decision": "run_active"}
    state = read_state(conn)
    covered = state.get("covered")
    shrunk = {table: rowid for table, rowid in marker.items() if rowid < _covered_rowid(covered, table)}
    if shrunk:
        # Rows past the mark were deleted. Lower it, or records that land on the freed rowids would
        # never count as new.
        with _bookkeeping:
            state = {**read_state(conn), "covered": {**covered, **shrunk}}
            _write_json(conn, ENGINE_CONFIG_KEY_STATE, state)
    if not _fresh(marker, state.get("covered")):
        return {"decision": "no_new_records"}
    if quiet_for < switch["quiet_s"]:
        return {"decision": "ingest_busy", "wait_s": round(switch["quiet_s"] - quiet_for, 1)}
    since_last = _since_last(state, now)
    if since_last is not None and since_last < switch["min_gap_s"]:
        return {"decision": "min_gap", "wait_s": round(switch["min_gap_s"] - since_last, 1)}

    since = window_start(state, now)
    new_records, older = _new_records(conn, state.get("covered"), since)
    waiting = waiting_by_pack(conn, since)
    pack_ids = [pid for pid, count in waiting.items() if count > 0]
    look = {"since": since, "new_records": new_records, "older_records": older, "packs": len(pack_ids),
            "waiting": sum(waiting.values()), "started_at": _iso(now)}
    if not pack_ids:
        # Nothing for any switched-on pack in what landed: the look is the whole job. No run, no model.
        with _bookkeeping:
            _write_json(conn, ENGINE_CONFIG_KEY_STATE, {
                "covered": marker, "base_at": _iso(now),
                "last": {**look, "run_id": None, "state": "nothing_waiting", "reason": None,
                         "finished_at": _iso(now), "totals": None}})
        logger.info("automatic catch-up: %s, nothing waiting for the switched-on packs among records "
                    "dated %s or later; no run, no model loaded", _arrivals(new_records, older), since)
        return {"decision": "nothing_waiting", **look}

    with _bookkeeping:
        # Counting can walk many records. Serialize the final permission check with set_switch:
        # an owner change during that work must win before a background run can acquire the slot.
        if kill_switch():
            return {"decision": "kill_switch"}
        current_switch = read_switch(conn)
        if not current_switch["enabled"]:
            return {"decision": "off"}
        if current_switch != switch:
            return {"decision": "switch_changed"}
        try:
            run = runs.start_run(pack_ids, runs.DEFAULT_BATCH_LIMIT, True, since=since, origin=runs.AUTO,
                                 run_batch=run_batch,
                                 on_end=lambda snap: _record_end(snap, marker, look, conn_factory, wall))
        except runs.BackfillBusy:
            return {"decision": "run_active"}      # something took the slot since the check above
        _write_json(conn, ENGINE_CONFIG_KEY_STATE, {
            **{k: state[k] for k in ("covered", "base_at") if k in state},
            "last": {**look, "run_id": run.run_id, "state": runs.RUNNING, "reason": None,
                     "finished_at": None, "totals": None}})
    logger.info("automatic catch-up: run %s started: %s, %d packs with %d record-pack pairs waiting "
                "among records dated %s or later", run.run_id, _arrivals(new_records, older), len(pack_ids),
                look["waiting"], since)
    return {"decision": "started", "run_id": run.run_id, **look}


def _record_end(snap: Dict[str, Any], marker: Dict[str, int], look: Dict[str, Any],
                conn_factory: Optional[Callable[[], Any]], wall: Callable[[], datetime] = _utcnow) -> None:
    """The run's end, on its worker thread, after its last batch has committed.

    A run that read everything it reached moves both marks: what landed is covered, and the next
    window starts from this run's start. A run that finished with model errors, or with a pack it
    could not run, covers what landed (so the same records do not trigger another run on their own)
    and leaves the window where it was: the next look walks it again, the read records cost a key
    lookup, the failed ones are asked again. A run that was stopped or failed moves nothing, so the
    next look does it over."""
    if conn_factory is None:
        from ...core.state import get_db_connection as conn_factory
    conn = conn_factory()
    if conn is None:
        return
    totals = snap.get("totals") if isinstance(snap.get("totals"), dict) else {}
    counts = {k: int(totals.get(k) or 0) for k in _COUNT_KEYS}
    counts.update({k: round(float(totals.get(k) or 0.0), 1) for k in _TIME_KEYS})
    done = snap.get("state") == runs.DONE
    clean = done and not counts["llm_errors"] and not any(
        p.get("state") != runs.DONE for p in snap.get("packs") or [])
    ended = wall()
    with _bookkeeping:
        # The marks as they are now, not as the look read them: the owner may have turned the switch
        # off and on again while the run worked, which forgets them on purpose (`set_switch`).
        held = read_state(conn)
        state: Dict[str, Any] = {k: held[k] for k in ("covered", "base_at") if k in held}
        if done:
            state["covered"] = marker
        if clean:
            state["base_at"] = look["started_at"]
        state["last"] = {**look, "run_id": snap.get("run_id"), "state": snap.get("state"),
                         "reason": snap.get("reason"), "finished_at": _iso(ended), "totals": counts}
        _write_json(conn, ENGINE_CONFIG_KEY_STATE, state)
    switch = read_switch(conn)
    logger.info("automatic catch-up: run %s ended: %s%s; %d processed, %d written, %d quarantined, "
                "%d model errors; extraction %.0f s, verification %.0f s; the next one may start %s",
                snap.get("run_id"), snap.get("state"), f" ({snap.get('reason')})" if snap.get("reason") else "",
                counts["processed"], counts["written"], counts["quarantined"], counts["llm_errors"],
                counts["extract_s"], counts["verify_s"],
                f"after {_iso(ended + timedelta(seconds=switch['min_gap_s']))}, once new records have landed"
                if switch["enabled"] else "when the switch is on again")


# --------------------------------------------------------------------------- what the owner reads
def describe(conn: sqlite3.Connection, *, now: Optional[datetime] = None,
             clock: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
    """The switch, the last automatic look and when the next may start. An open read: state words,
    stamps, periods and counts. Moves nothing, loads nothing."""
    now = now or _utcnow()
    switch = read_switch(conn)
    state = read_state(conn)
    last = dict(state["last"]) if isinstance(state.get("last"), dict) else None
    if last is not None and last.get("state") == runs.RUNNING:
        # The row says running. It is, when the node's run of that id is; a run that ended a moment
        # ago reads its own end; anything else is a run a node restart cut short.
        live = runs.current()
        if live is None or live.get("run_id") != last.get("run_id"):
            last["state"] = "interrupted"
        elif live.get("state") not in (runs.RUNNING, runs.STOPPING):
            last.update(state=live.get("state"), reason=live.get("reason"), finished_at=live.get("finished_at"))
    off = kill_switch()
    nxt: Dict[str, Any] = {"state": "off", "not_before": None}
    if off:
        nxt["state"] = "kill_switch"
    elif switch["enabled"]:
        marker = read_marker(conn)
        if runs.busy():
            nxt["state"] = "run_active"
        elif not _fresh(marker, state.get("covered")):
            nxt["state"] = "waiting_for_records"
        else:
            quiet_left = max(0.0, switch["quiet_s"] - _watch.quiet_for(marker, clock()))
            since_last = _since_last(state, now)
            gap_left = max(0.0, switch["min_gap_s"] - since_last) if since_last is not None else 0.0
            wait = max(quiet_left, gap_left)
            nxt["state"] = "due" if wait <= 0 else ("waiting_for_gap" if gap_left >= quiet_left
                                                    else "waiting_for_quiet")
            nxt["not_before"] = _iso(now + timedelta(seconds=wait))
    return {"enabled": switch["enabled"], "kill_switch": off, "changed_at": switch["changed_at"],
            "quiet_s": switch["quiet_s"], "min_gap_s": switch["min_gap_s"],
            "quiet_set": switch["quiet_set"], "min_gap_set": switch["min_gap_set"],
            "first_window_days": FIRST_WINDOW_DAYS, "max_window_days": MAX_WINDOW_DAYS,
            "last": last, "next": nxt}


# --------------------------------------------------------------------------- the loop
async def run_scheduler_loop(conn_factory: Callable[[], Any], *, tick_seconds: float = TICK_SECONDS,
                             startup_delay_seconds: float = STARTUP_DELAY_SECONDS) -> None:
    """Forever: wait, look on a worker thread. The coroutine only sleeps and awaits; the connection is
    resolved inside the worker (thread-local), never on the loop."""
    def _tick() -> Dict[str, Any]:
        conn = conn_factory()
        return tick(conn) if conn is not None else {"decision": "no_database"}

    await asyncio.sleep(startup_delay_seconds)
    while True:
        try:
            await asyncio.to_thread(_tick)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- the loop outlives a bad look
            logger.warning("automatic catch-up look failed: %s", type(exc).__name__, exc_info=True)
        await asyncio.sleep(tick_seconds)


def start_scheduler(spawn: Callable[..., Any], conn_factory: Callable[[], Any]) -> bool:
    """App startup: start the loop through `spawn` (the app's tracked-task helper)."""
    if kill_switch():
        logger.info("automatic catch-up for fact packs off (%s)", KILL_ENV)
        return False
    spawn(run_scheduler_loop(conn_factory), name="packs-auto-catchup")
    return True


__all__: List[str] = [
    "AutoRefused", "ENGINE_CONFIG_KEY_STATE", "ENGINE_CONFIG_KEY_SWITCH", "KILL_ENV", "MIN_GAP_ENV", "QUIET_ENV",
    "describe", "kill_switch", "read_marker", "read_state", "read_switch", "run_scheduler_loop", "set_switch",
    "start_scheduler", "tick", "waiting_by_pack", "window_start",
]
