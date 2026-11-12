"""One owner-confirmed fact-pack backfill run, worked through by the node itself.

The app's backfill control is one HTTP request per batch, held open for the whole batch
(`POST /v1/signal/derivation/packs/{pack_id}/backfill`, `surfaces.run_pack_backfill`). That
shape cannot carry a long history from a terminal: the owner's confirmation is one token, good
for one request and for at most 600 s (`uds.OWNER_CONFIRM_MAX_LIFE_S`), while one batch of 200
records takes over an hour on the local models and a full history needs hundreds of batches.

So the yes authorises a RUN, not a request. `start_run` takes the plan the owner was shown (the
packs, the batch size, one batch each or until done, how far back) and the node works through it on its own
thread: packs one after another, batch after batch, each batch the same `run_pack_backfill` the
app's control calls. Two things start a run. The start route in `api/signal.py`, which the owner
socket's census names dangerous (`owner_door_routes`) and which checks the confirmed socket peer
itself: the owner's run (`origin` `owner`). And `auto_catchup`, only while the owner's standing
switch is on (`topos-node packs auto on`, the same kind of yes): an automatic run (`origin`
`auto`) over the records that landed since the last one. A caller follows a run through `current()`
(counts, seconds and model names only) and ends it with `stop()`.

What bounds a run:

* its packs, batch size and date window (`since`) are fixed at the yes; nothing can be added to a run;
* it reads with the extractor the owner has picked for facts (Topos Facts for the packs it knows, the
  local model for the rest; `run_pack_backfill`) and never changes that pick. If the pick cannot load,
  the run ends (`topos_facts_unavailable`) with nothing marked done;
* one run at a time, and never beside one of the app's single batches (`single_batch`), so a run
  never puts two backfills on the local models at once;
* the owner's backfill wins over an automatic run. Starting an owner run while an automatic one is
  working is not refused: the automatic run is asked to stop after the record in hand, the owner's
  run takes the slot at once and starts its first batch when the automatic one has ended
  (`waiting_for` in its snapshot until then). One of the app's single batches waits the same way
  (`yield_to_owner`). An automatic run never starts beside anything;
* it lives in this process only. A node restart ends it. Nothing restarts an owner's run: the owner
  asks again, and the rerun resumes where the last one stopped because every finished record is
  keyed in `derivation_progress`. An automatic run is started again by its own trigger;
* it stops between records on `stop()` and on node shutdown;
* between records it yields to the owner's own requests (`core.owner_requests.pace`), like the other long
  background work on the node;
* `MODEL_ERROR_STREAK` extraction calls failing in a row end it (`model_unavailable`): a failed
  record stays unmarked, so without this a run against a model that is down would spin.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence

logger = logging.getLogger("topos.derivation.backfill_runs")

#: Records per batch unless the owner says otherwise: the app control's own default.
DEFAULT_BATCH_LIMIT = 200
#: The app route's ceiling for one batch; a run takes the same.
MAX_BATCH_LIMIT = 1000
#: Extraction calls failing in a row before a run gives up on the model.
MODEL_ERROR_STREAK = 5

THREAD_NAME = "topos-pack-backfill"

RUNNING = "running"
STOPPING = "stopping"
DONE = "done"
STOPPED = "stopped"
FAILED = "failed"

#: Who started a run: the owner's yes for this run, or the owner's standing switch (`auto_catchup`).
OWNER = "owner"
AUTO = "auto"
#: How long one of the app's single batches waits for an automatic run to end after asking it to stop.
OWNER_YIELD_WAIT_S = 300.0

_COUNT_KEYS = ("processed", "assertions", "accepted", "written", "quarantined", "llm_errors")
#: Wall seconds a batch spent in extraction calls and in verifier calls, kept apart: the extractor may
#: be Topos Facts or the local model, the verifier is always a model call per assertion.
_TIME_KEYS = ("extract_s", "verify_s")
_MODEL_KEYS = ("extract_model", "verify_model")
_SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: `(pack_id, limit, should_stop, on_record, since=...) -> stats`: one batch. The default resolves the
#: worker thread's own connection; tests pass their own.
BatchRunner = Callable[..., Dict[str, Any]]


class BackfillBusy(RuntimeError):
    """A run is active (or, for a run, one of the app's single batches is in flight)."""

    code = "backfill_already_running"


class BackfillRefused(ValueError):
    """The plan cannot run as asked; `code` is the word the route answers with."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _counts(stats: Dict[str, Any]) -> Dict[str, Any]:
    """A batch's numbers: the counts as ints, the two times as seconds. Nothing else of `stats`."""
    out: Dict[str, Any] = {}
    for key in _COUNT_KEYS + _TIME_KEYS:
        value = stats.get(key)
        number = value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
        out[key] = round(float(number), 1) if key in _TIME_KEYS else int(number)
    return out


def _models(stats: Dict[str, Any]) -> Dict[str, Optional[str]]:
    return {key: (str(stats[key])[:120] if isinstance(stats.get(key), str) and stats.get(key) else None)
            for key in _MODEL_KEYS}


def validate_since(since: Any) -> Optional[str]:
    """`YYYY-MM-DD` (a real date), or None for no date limit; anything else is `BackfillRefused`."""
    if since is None or since == "":
        return None
    if not isinstance(since, str) or not _SINCE_RE.match(since):
        raise BackfillRefused("backfill_plan_invalid", "since must be a YYYY-MM-DD date")
    try:
        datetime.strptime(since, "%Y-%m-%d")
    except ValueError:
        raise BackfillRefused("backfill_plan_invalid", "since must be a YYYY-MM-DD date") from None
    return since


class BackfillRun:
    """One run's plan and progress. Counts, seconds, states, stamps and the names of the two models that
    read and checked each pack: no record text or ids."""

    def __init__(self, pack_ids: Sequence[str], limit: int, until_done: bool,
                 since: Optional[str] = None, *, origin: str = OWNER,
                 on_end: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        self.run_id = f"bfr_{uuid.uuid4().hex[:12]}"
        self.pack_ids = list(pack_ids)
        self.limit = int(limit)
        self.until_done = bool(until_done)
        self.since = since
        self.origin = AUTO if origin == AUTO else OWNER
        #: Called once with the final snapshot, on the worker thread, after the run has ended.
        self._on_end = on_end
        #: An automatic run this (owner's) run asked to stop and waits for before its first batch.
        self._waits_for: Optional["BackfillRun"] = None
        #: Set when the worker has ended and reported: what a waiting run and `yield_to_owner` wait on.
        self.ended = threading.Event()
        self.state = RUNNING
        self.reason: Optional[str] = None
        self.started_at = _now()
        self.finished_at: Optional[str] = None
        self._began = time.monotonic()
        self._ended: Optional[float] = None
        self._packs: List[Dict[str, Any]] = [
            {"pack_id": pid, "state": "waiting", "batches": [], "current": None, "error": None,
             "extract_model": None, "verify_model": None}
            for pid in self.pack_ids]
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._fail: Optional[str] = None
        self.thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ reads
    def active(self) -> bool:
        return self.state in (RUNNING, STOPPING)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            packs = [{"pack_id": p["pack_id"], "state": p["state"], "error": p["error"],
                      "extract_model": p["extract_model"], "verify_model": p["verify_model"],
                      "batches": [dict(b) for b in p["batches"]],
                      "current": dict(p["current"]) if p["current"] else None} for p in self._packs]
            end = self._ended if self._ended is not None else time.monotonic()
            totals: Dict[str, Any] = {key: 0 for key in _COUNT_KEYS + _TIME_KEYS}
            for p in packs:
                for batch in p["batches"] + ([p["current"]] if p["current"] else []):
                    for key in totals:
                        totals[key] += batch.get(key) or 0
            for key in _TIME_KEYS:
                totals[key] = round(float(totals[key]), 1)
            return {"run_id": self.run_id, "state": self.state, "reason": self.reason,
                    "origin": self.origin,
                    "waiting_for": "automatic_run" if self._waits_for is not None else None,
                    "pack_ids": list(self.pack_ids), "limit": self.limit, "until_done": self.until_done,
                    "since": self.since,
                    "started_at": self.started_at, "finished_at": self.finished_at,
                    "elapsed_s": round(end - self._began, 1), "packs": packs, "totals": totals}

    # ------------------------------------------------------------------ control
    def request_stop(self, reason: Optional[str] = None) -> None:
        """Stop after the record in hand. `reason` (a plain word) is kept when the stop is the first
        one and it does end the run early; a plain stop reads `stopped`."""
        with self._lock:
            if self.state == RUNNING:
                self.state = STOPPING
                if reason and not self.reason:
                    self.reason = reason
        self._cancel.set()

    # ------------------------------------------------------------------ the worker's writes
    def _pack(self, index: int) -> Dict[str, Any]:
        return self._packs[index]

    def _set_pack(self, index: int, **changes: Any) -> None:
        with self._lock:
            self._packs[index].update(changes)

    def _close_batch(self, index: int, batch: Dict[str, Any]) -> None:
        with self._lock:
            pack = self._packs[index]
            pack["batches"].append(batch)
            pack["current"] = None

    def _finish(self) -> None:
        with self._lock:
            for pack in self._packs:
                pack["current"] = None
                if pack["state"] == "running":
                    pack["state"] = STOPPED
            unfinished = any(pack["state"] in ("waiting", STOPPED) for pack in self._packs)
            if self._fail:
                self.state, self.reason = FAILED, self._fail
            elif self._cancel.is_set() and unfinished:
                self.state = STOPPED
                self.reason = self.reason or "stopped"
            else:
                # Every pack had its turn (a stop that arrived after the last record changes nothing).
                self.state, self.reason = DONE, None
            self.finished_at = _now()
            self._ended = time.monotonic()


# --------------------------------------------------------------------------- the one-run rule
_state_lock = threading.Lock()
_active: Optional[BackfillRun] = None
_last: Optional[BackfillRun] = None
_single_batches = 0


def current() -> Optional[Dict[str, Any]]:
    """The active run's snapshot, else the last finished one's, else None."""
    with _state_lock:
        run = _active or _last
    return run.snapshot() if run is not None else None


def busy() -> bool:
    """A run holds the slot, or one of the app's single batches is in flight."""
    with _state_lock:
        return _active is not None or _single_batches > 0


def stop_automatic(reason: str) -> bool:
    """Ask the active run to stop after the record in hand if it is an automatic one; whether it was."""
    with _state_lock:
        run = _active
    if run is None or run.origin != AUTO:
        return False
    run.request_stop(reason)
    return True


def yield_to_owner(timeout_s: float = OWNER_YIELD_WAIT_S) -> bool:
    """Before one of the app's single batches: the owner's backfill wins over an automatic run.

    An automatic run that holds the slot is asked to stop after the record in hand, and this waits
    (on the caller's worker thread, never the event loop) until it has ended, at most `timeout_s`.
    True when no automatic run holds the slot afterwards; the caller then takes `single_batch()`,
    which still refuses beside an owner's run. Does nothing when no automatic run is active."""
    with _state_lock:
        run = _active
    if run is None or run.origin != AUTO:
        return True
    run.request_stop("owner_backfill")
    return run.ended.wait(timeout_s)


def stop() -> Optional[Dict[str, Any]]:
    """Ask the active run to stop after the record in hand; its snapshot, or None when no run is active."""
    with _state_lock:
        run = _active
    if run is None:
        return None
    run.request_stop()
    return run.snapshot()


@contextlib.contextmanager
def single_batch() -> Iterator[None]:
    """Hold for one of the app's single batches: refused while a run is active, and a run is refused
    while one is held. Two single batches may overlap, as before this module."""
    global _single_batches
    with _state_lock:
        if _active is not None:
            raise BackfillBusy("a backfill run is active")
        _single_batches += 1
    try:
        yield
    finally:
        with _state_lock:
            _single_batches -= 1


def validate_plan(conn: sqlite3.Connection, pack_ids: Any, limit: Any) -> List[str]:
    """The run's pack list, in the order asked and without repeats, or `BackfillRefused`."""
    if not isinstance(pack_ids, (list, tuple)) or not pack_ids or any(not isinstance(p, str) for p in pack_ids):
        raise BackfillRefused("backfill_plan_invalid", "pack_ids must be a list of pack ids")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_BATCH_LIMIT:
        raise BackfillRefused("backfill_plan_invalid", f"limit must be 1 to {MAX_BATCH_LIMIT}")
    ordered: List[str] = []
    for pid in (p.strip() for p in pack_ids):
        if pid and pid not in ordered:
            ordered.append(pid)
    if not ordered:
        raise BackfillRefused("backfill_plan_invalid", "pack_ids must name at least one pack")
    enabled = {str(r[0]): bool(r[1]) for r in conn.execute("SELECT pack_id, enabled FROM pack_registry")}
    for pid in ordered:
        if pid not in enabled:
            raise BackfillRefused("unknown_pack", f"unknown pack {pid}")
        if not enabled[pid]:
            raise BackfillRefused("pack_not_enabled", f"pack {pid} is not enabled")
    return ordered


def start_run(pack_ids: Sequence[str], limit: int = DEFAULT_BATCH_LIMIT, until_done: bool = False, *,
              since: Optional[str] = None, run_batch: Optional[BatchRunner] = None,
              origin: str = OWNER, on_end: Optional[Callable[[Dict[str, Any]], None]] = None) -> BackfillRun:
    """Start the one run. The caller has validated the plan (`validate_plan`, `validate_since`) and
    holds the owner's yes: for this run (`origin` `owner`, the start route), or the standing one
    (`origin` `auto`, `auto_catchup` only).

    An owner's run started while an automatic run is working takes the slot now and the automatic
    run is asked to stop after the record in hand; the owner's first batch starts when it has ended.
    Everything else that finds the slot taken is `BackfillBusy`, as before."""
    global _active
    run = BackfillRun(pack_ids, limit, until_done, since, origin=origin, on_end=on_end)
    with _state_lock:
        if _single_batches:
            raise BackfillBusy("a backfill is already running")
        if _active is not None:
            if not (run.origin == OWNER and _active.origin == AUTO):
                raise BackfillBusy("a backfill is already running")
            run._waits_for = _active
        _active = run
    if run._waits_for is not None:
        run._waits_for.request_stop("owner_backfill")
    from ...runtime_shutdown import current_generation, stop_checker

    should_stop = stop_checker(current_generation(), run._cancel)
    run.thread = threading.Thread(target=_work, args=(run, run_batch or _run_batch, should_stop),
                                  name=THREAD_NAME, daemon=True)
    try:
        run.thread.start()
    except BaseException:
        with _state_lock:
            if _active is run:
                _active = None
        run.ended.set()
        raise
    logger.info("pack backfill run %s started (%s): %d packs, %d per batch, until_done=%s, since=%s%s",
                run.run_id, "automatic" if run.origin == AUTO else "owner", len(run.pack_ids), run.limit,
                run.until_done, run.since or "-",
                "; waiting for the automatic run to end" if run._waits_for is not None else "")
    return run


def _run_batch(pack_id: str, limit: int, should_stop: Callable[[], bool],
               on_record: Callable[[Dict[str, Any]], None], since: Optional[str] = None) -> Dict[str, Any]:
    from ...core.state import get_db_connection
    from .surfaces import run_pack_backfill

    conn = get_db_connection()          # this worker thread's own handle
    if conn is None:
        raise RuntimeError("database unavailable")
    return run_pack_backfill(conn, pack_id, limit, should_stop=should_stop, on_record=on_record, since=since)


def _work(run: BackfillRun, run_batch: BatchRunner, should_stop: Callable[[], bool]) -> None:
    global _active, _last
    try:
        prior = run._waits_for
        if prior is not None:
            # The automatic run was told to stop; it ends after the record in hand. Waited out even
            # when this run is itself stopped meanwhile: the slot is this run's, and letting it go
            # while the automatic run still has a model call in flight would let a third start beside it.
            prior.ended.wait()
            with run._lock:
                run._waits_for = None
        for index in range(len(run.pack_ids)):
            if should_stop():
                break
            _work_pack(run, index, run_batch, should_stop)
            if run._fail:
                break
    except BaseException:  # noqa: BLE001 -- a run must always end in a state someone can read
        # ShutdownInterrupt (a BaseException) is how a model call ends when the node is going down.
        if not should_stop():
            logger.exception("pack backfill run %s crashed", run.run_id)
            run._fail = run._fail or "internal_error"
    finally:
        if should_stop() and not run._cancel.is_set():
            # The node is going down, not the owner's stop: say which.
            run.reason = "node_stopping"
            run._cancel.set()
        run._finish()
        logger.info("pack backfill run %s ended: %s%s", run.run_id, run.state,
                    f" ({run.reason})" if run.reason else "")
        try:
            # Before the slot is released: an automatic run's end is on record before anything can
            # start the next run, so a look never reads a run that has ended as still running.
            if run._on_end is not None:
                run._on_end(run.snapshot())
        except BaseException:  # noqa: BLE001 -- bookkeeping after the run; the run's own state stands
            logger.exception("pack backfill run %s: the end callback failed", run.run_id)
        finally:
            with _state_lock:
                _last = run
                if _active is run:
                    _active = None
            run.ended.set()


def _work_pack(run: BackfillRun, index: int, run_batch: BatchRunner, should_stop: Callable[[], bool]) -> None:
    from ...core import owner_requests
    from .surfaces import BackfillDeferred

    pack_id = run.pack_ids[index]
    run._set_pack(index, state="running")
    number = 0
    while True:
        number += 1
        began = time.monotonic()
        live: Dict[str, Any] = {"index": number, **_counts({}), "elapsed_s": 0.0}
        run._set_pack(index, current=dict(live))
        streak = {"errors": 0, "seen": 0}

        def on_record(stats: Dict[str, Any], _began=began, _number=number, _streak=streak) -> None:
            counts = _counts(stats)
            failed = counts["llm_errors"] > _streak["seen"]
            _streak["seen"] = counts["llm_errors"]
            _streak["errors"] = _streak["errors"] + 1 if failed else 0
            run._set_pack(index, current={"index": _number, **counts,
                                          "elapsed_s": round(time.monotonic() - _began, 1)},
                          **{k: v for k, v in _models(stats).items() if v})
            if _streak["errors"] >= MODEL_ERROR_STREAK and not run._fail:
                run._fail = "model_unavailable"
                run._cancel.set()
            # Between records, on this worker thread and outside any write: let a pending owner frame go first.
            owner_requests.pace()

        try:
            stats = run_batch(pack_id, run.limit, should_stop, on_record, since=run.since)
        except BackfillDeferred as deferred:
            # The owner's pick for facts cannot load. Every pack would defer the same way, so the run
            # ends here; nothing was marked done, and the pick itself is the owner's to change.
            logger.warning("pack backfill run %s: deferred at %s (%s)", run.run_id, pack_id, deferred.code)
            run._set_pack(index, state=FAILED, current=None, error=deferred.code)
            run._fail = run._fail or deferred.code
            return
        except ValueError as exc:
            # run_pack_backfill's own refusals: the pack was switched off (or the owner entity went)
            # after the yes. That pack is skipped with the reason; the others still run.
            logger.warning("pack backfill run %s: %s skipped (%s)", run.run_id, pack_id, exc)
            run._set_pack(index, state=FAILED, current=None, error=_refusal_word(str(exc)))
            return
        except Exception:  # noqa: BLE001
            logger.exception("pack backfill run %s: batch %d of %s failed", run.run_id, number, pack_id)
            run._set_pack(index, state=FAILED, current=None, error="batch_failed")
            run._fail = run._fail or "batch_failed"
            return
        batch = {"index": number, **_counts(stats), "elapsed_s": round(time.monotonic() - began, 1)}
        run._close_batch(index, batch)
        run._set_pack(index, **{k: v for k, v in _models(stats).items() if v})
        if run._fail:
            run._set_pack(index, state=FAILED, error=run._fail)
            return
        if stats.get("stopped") or should_stop():
            run._set_pack(index, state=STOPPED)
            return
        if not run.until_done or batch["processed"] < run.limit:
            # A batch that came back short walked the whole window: nothing is left for this pack
            # but records whose extraction call failed, which the next run asks again.
            run._set_pack(index, state=DONE)
            return


def _refusal_word(message: str) -> str:
    if "not enabled" in message:
        return "pack_not_enabled"
    if "unknown pack" in message:
        return "unknown_pack"
    if "no owner entity" in message:
        return "no_owner_entity"
    return "refused"


# --------------------------------------------------------------------------- the count before the yes
def backfill_plan(conn: sqlite3.Connection, pack_ids: Sequence[str], since: Optional[str] = None) -> Dict[str, Any]:
    """What a backfill of these packs would do, for the owner to read before the yes.

    How many records wait for each pack, counted the way a backfill would reach them: the same
    history window, the same `since`, the pack's role gate and its own prefilter, less what
    `derivation_progress` already holds. And which model would read each pack: Topos Facts when the
    owner has picked it and its spec lists the pack, else the local extraction model; plus the
    verifier every assertion goes to. Reads only: no model is loaded or called, nothing is written
    (the one-time role-skip retirement a real batch does first is not done here, so the first count
    after an upgrade can be slightly low). The pack list of Topos Facts comes from its spec file on
    this computer; when that was never downloaded the route of a pack is `unknown` until a run
    loads it.

    A pack that is unknown or switched off is listed with `waiting: None`; a run would refuse it.
    """
    from ...config.facts_llm import facts_classifier_version
    from ...config.settings import settings as _settings
    from ...enrichment.jobs.canonical.derivation_job import _verify_mode
    from ...features.facts.llm_extract import _resolved_extraction_model  # type: ignore
    from . import topos_facts as _tf
    from .packs import load_packs
    from .registry import bundled_pack_dir
    from .surfaces import BACKFILL_HISTORY_WINDOW
    from .verify import verifier_model

    since = validate_since(since)
    asked: List[str] = []
    for pid in pack_ids:
        pid = str(pid or "").strip()
        if pid and pid not in asked:
            asked.append(pid)
    catalog = load_packs(bundled_pack_dir(), only=asked) if asked else {}
    enabled = {str(r[0]): bool(r[1]) for r in conn.execute("SELECT pack_id, enabled FROM pack_registry")}
    countable = [pid for pid in asked if enabled.get(pid) and catalog.get(pid) is not None]

    waiting, history = waiting_counts(conn, catalog, countable, since)

    llm_model = str(_resolved_extraction_model(_settings, conn) or "")
    picked = bool(facts_classifier_version(conn))
    spec = _tf.known_packs_offline() if picked else None

    def route(pid: str) -> Dict[str, Optional[str]]:
        if not picked or pid in _tf.ROUTED_TO_LLM:
            return {"extract_route": "llm", "extract_model": llm_model}
        if spec is None:
            return {"extract_route": "unknown", "extract_model": None}
        if pid in spec:
            return {"extract_route": "classifier", "extract_model": _tf.MODEL_ID}
        return {"extract_route": "llm", "extract_model": llm_model}

    packs = []
    for pid in asked:
        pack = catalog.get(pid)
        packs.append({"pack_id": pid, "title": str(getattr(pack, "title", pid) or pid) if pack else pid,
                      "known": pid in enabled and pack is not None, "enabled": bool(enabled.get(pid)),
                      "waiting": waiting.get(pid), **route(pid)})
    return {"packs": packs, "since": since, "history_window": BACKFILL_HISTORY_WINDOW,
            "history_records": sum(history.values()),
            # A table that filled the window has older records a backfill does not reach.
            "history_truncated": any(n >= BACKFILL_HISTORY_WINDOW for n in history.values()),
            "total_waiting": sum(waiting.values()),
            "extraction": {"provider": "topos_facts" if picked else "llm", "llm_model": llm_model,
                           "classifier_model": _tf.MODEL_ID if picked else None,
                           "classifier_spec_on_disk": spec is not None if picked else None},
            "verify_model": verifier_model() if _verify_mode() != "off" else None}


def waiting_counts(conn: sqlite3.Connection, catalog: Dict[str, Any], pack_ids: Sequence[str],
                   since: Optional[str], *, per_record: bool = False) -> tuple:
    """`(waiting per pack, records walked per table)`: the one counting rule, for the plan the owner
    reads and for the automatic catch-up's look before it starts anything.

    A record waits for a pack when the pack's role gate lets it in, its prefilter passes it and
    `derivation_progress` does not hold it yet. `per_record` asks the progress table once per record
    that got that far instead of loading each pack's whole key range first: the right trade for the
    few days the automatic catch-up counts, the wrong one for a full history. Reads only.

    An owner's short message (DR-62, `_iter_history`) waits only for a pack that would read it
    with its conversation, and only when it has one; it is not one of the records walked."""
    from ...enrichment.jobs.canonical.derivation_job import _context_for, _iter_history
    from ..provenance.posture import make_posture_resolver
    from .prefilter import PackPrefilter, _tokens
    from .surfaces import BACKFILL_HISTORY_WINDOW
    from .template import context_packs

    pack_ids = list(pack_ids)
    waiting = {pid: 0 for pid in pack_ids}
    history = {"journal_entries": 0, "conversation_messages": 0}
    if not pack_ids:
        return waiting, history
    filters = {pid: PackPrefilter(catalog[pid]) for pid in pack_ids}
    roles = {pid: catalog[pid].allowed_roles() for pid in pack_ids}
    prefix = {pid: f"{pid}@{catalog[pid].version}:" for pid in pack_ids}
    done = {} if per_record else {pid: _done_keys(conn, prefix[pid]) for pid in pack_ids}

    def keyed(pid: str, suffix: str) -> bool:
        if not per_record:
            return suffix in done[pid]
        return conn.execute("SELECT 1 FROM derivation_progress WHERE key = ?",
                            (prefix[pid] + suffix,)).fetchone() is not None

    ctx_packs = context_packs()
    ctx_cache: Dict[str, Any] = {"record_only": _record_only_packs(conn, pack_ids)}
    for rec in _iter_history(conn, limit=BACKFILL_HISTORY_WINDOW, posture_for=make_posture_resolver(conn),
                             since=since):
        short = bool(rec.get("short"))
        if not short:
            history[rec["table"]] = history.get(rec["table"], 0) + 1
        low = (rec["text"] or "").lower()
        tokens = _tokens(low)
        suffix = f"{rec['table']}:{rec['record_id']}"
        for pid in pack_ids:
            if short and pid not in ctx_packs:
                continue
            if rec["role"] in roles[pid] and filters[pid].matches(low, tokens) and not keyed(pid, suffix):
                if short and not _context_for(conn, catalog[pid], rec, ctx_cache):
                    continue
                waiting[pid] += 1
    return waiting, history


def _record_only_packs(conn: sqlite3.Connection, pack_ids: Sequence[str]) -> frozenset:
    """The packs a run would not read with a conversation's context: the ones Topos Facts reads,
    and, while its pack list is not on this computer, the ones it might. Loads no model."""
    from ...config.facts_llm import facts_classifier_version
    from . import topos_facts as _tf

    if not facts_classifier_version(conn):
        return frozenset()
    spec = _tf.known_packs_offline()
    return frozenset(pid for pid in pack_ids
                     if pid not in _tf.ROUTED_TO_LLM and (spec is None or pid in spec))


def _done_keys(conn: sqlite3.Connection, prefix: str) -> set:
    """The `table:record_id` tails of one pack version's progress keys (a range read on the key)."""
    upper = prefix[:-1] + chr(ord(prefix[-1]) + 1)
    return {str(r[0])[len(prefix):] for r in conn.execute(
        "SELECT key FROM derivation_progress WHERE key >= ? AND key < ?", (prefix, upper))}
