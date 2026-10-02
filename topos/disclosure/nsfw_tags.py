"""NSFW tags on the canonical tables: written by the explicit-wording rule at every write, repaired by a sweep.

The tag is three columns on ``journal_entries``, ``conversation_messages`` and ``ai_chat_messages``
(migration ``canonical_nsfw_v1``): ``content_nsfw`` (the decision: 1 withholds the row from every share),
``content_nsfw_score`` and ``content_nsfw_model`` (which tagger decided, and how). The decision is the rule's
(:mod:`topos.sanitization.explicit_wording`): its id in ``content_nsfw_model``, its tier as the score (1.0 for
unambiguous vocabulary, 0.5 for a phrase around an ambiguous word, 0.0 for no hit). No model runs anywhere here.

**Every row is tagged where it is written.** The canonical store tags each row it upserts, inside the same gated
transaction (:func:`tag_stored`), and the two owner-attested snapshot lanes tag each row they insert
(:func:`tag_inserted`). Before, only the pipeline's privacy stage tagged, and three paths wrote canonical rows
without it: the node's own messenger sync (every iMessage and Signal row it writes), the attested iMessage and
ChatGPT snapshot lanes, and any import whose privacy stage failed or was interrupted. On one owner's node 89,032 of
96,634 iMessage rows and 2,619 ChatGPT export rows had never been tagged and read as not NSFW.

**Existing rows repair themselves.** :class:`Retag` walks the three tables in bounded batches under the write gate
(:func:`run_at_startup` starts it on every boot, then re-checks on an interval), evaluates every row that has text,
and writes a row when, and only when, its tag needs to change:

* a row no tagger has decided (``content_nsfw_model`` NULL) is tagged;
* a row this rule family tagged is rewritten when the rule's version or its verdict moved;
* a row another tagger decided (the retired classifier) is rewritten only when the **decision** changes: its
  flag is cleared when the rule finds no explicit wording, or set when it does. A classifier row whose decision
  the rule confirms is left exactly as it is, score and id included, because those two columns are part of that
  row's reviewed surface and rewriting them would stale every review of it for nothing (``evidence._row_revision``
  excludes the rule's own id and score from the surface, so a rule-tagged row never has that problem).

The walk is keyed by the rule id and resumes: its state (``engine_config`` key :data:`STATE_KEY`) carries a rowid
cursor per table and is written in the same transaction as each batch, so an interrupted run continues where it
stopped and a finished run under the current rule leaves only a cheap check for rows no tagger has decided (rows a
path wrote without tagging, which the next check catches).

**A cleared flag is re-assessed without an owner pass.** A flagged row was withheld before any model was asked
about it, so a row whose flag is cleared has no machine assessment, and nothing else about it moves. Every clear of
an existing row's flag, by the sweep or at a write, adds one to a counter (``engine_config`` key
:data:`CLEARED_KEY`) in the transaction that clears it; a sweep that ends having seen the counter move raises the
state's ``generation`` once. The permissions refresh loop reads that generation as part of its proof digest
(:func:`tag_generation`), so one sweep that clears thousands of flags starts one catch-up pass, within that loop's
own model budget. A flag set needs nothing: every read path withholds a flagged row at read time.

**The switch.** ``nsfw_classifier_enabled`` (``NSFW_CLASSIFIER_ENABLED``, default on) keeps its name and now means
whether the node decides NSFW tags at all. Off: no write path tags, the sweep writes nothing, and every stored tag
stays as it is. A dry run still counts.

Counts only, here and in the log: no row id, text or matched word leaves this module.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..sanitization.explicit_wording import RULE_FAMILY, RULE_ID, TIER_PHRASE, TIER_UNAMBIGUOUS, Verdict, evaluate
from ..storage.db.write_gate import batched_writes, with_db_write
from .field_registry import CANONICAL_ID_COLUMN

logger = logging.getLogger("topos.disclosure.nsfw_tags")

TABLES = ("journal_entries", "conversation_messages", "ai_chat_messages")
TAG_COLUMNS = ("content_nsfw", "content_nsfw_score", "content_nsfw_model")
SCORES = {TIER_UNAMBIGUOUS: 1.0, TIER_PHRASE: 0.5, None: 0.0}
STATE_KEY = "nsfw_tags"
STATE_VERSION = "nsfw-tag-state/v1"
#: Flags cleared on existing rows, by any writer, ever: an integer that only grows.
CLEARED_KEY = "nsfw_tags.cleared"
#: Rows per gate hold; the pause between holds lets every other writer in.
BATCH_ROWS = 500
PAUSE_SECONDS = 0.05
#: How often a finished node re-checks for rows no tagger has decided.
RECHECK_SECONDS = 1800.0
#: Every way a row can come out of an evaluation. ``evaluated`` is their sum.
OUTCOMES = ("tagged", "cleared", "flagged", "relabelled", "unchanged", "kept_legacy_flag", "kept_legacy_clear",
            "not_written")
COUNTS = ("rows", "with_text", "evaluated", "flagged_unambiguous", "flagged_phrase", "never_tagged", "rule_tagged",
          "legacy_tagged", *OUTCOMES)

Tag = Tuple[int, float, str]
#: One write run at a time in this process (the startup sweep and an owner's run): each keeps its cursor in memory.
_WRITE_RUN = threading.Lock()


def tagging_enabled() -> bool:
    """``nsfw_classifier_enabled``: whether the node decides NSFW tags at all (module docstring)."""
    try:
        from ..config.settings import settings
    except Exception:  # noqa: BLE001 -- a settings module that cannot load never turns tagging off
        return True
    return bool(getattr(settings, "nsfw_classifier_enabled", True))


def tag_for(text: Any) -> Tag:
    """The rule's tag for one text: (content_nsfw, content_nsfw_score, content_nsfw_model)."""
    verdict = evaluate(text)
    return (1 if verdict.flagged else 0, SCORES[verdict.tier], RULE_ID)


def is_rule_tag(model_id: Any) -> bool:
    return isinstance(model_id, str) and model_id.startswith(RULE_FAMILY)


def decide(stored_flag: Any, stored_score: Any, stored_model: Any, verdict: Verdict) -> Tuple[Optional[Tag], str]:
    """What to write for a row given its stored tag and the rule's verdict, and the outcome's name.

    See the module docstring: a row no tagger decided is tagged; a rule-tagged row follows the rule exactly; a
    row another tagger decided moves only when the decision moves.
    """
    target: Tag = (1 if verdict.flagged else 0, SCORES[verdict.tier], RULE_ID)
    flagged = stored_flag in (1, True, "1")
    if stored_model is None:
        return target, "tagged"
    if is_rule_tag(stored_model):
        if (flagged, stored_score, stored_model) == (verdict.flagged, target[1], RULE_ID):
            return None, "unchanged"
        if flagged == verdict.flagged:
            return target, "relabelled"
        return target, ("flagged" if verdict.flagged else "cleared")
    if flagged == verdict.flagged:
        return None, ("kept_legacy_flag" if flagged else "kept_legacy_clear")
    return target, ("flagged" if verdict.flagged else "cleared")


# --- the columns -------------------------------------------------------------------------------------------------

def columns_present(conn: sqlite3.Connection, table: str, cache: Optional[Dict[str, bool]] = None) -> bool:
    """Whether ``table`` exists with the three tag columns. ``cache`` remembers a yes for one writer."""
    if cache is not None and cache.get(table):
        return True
    found = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if found is None:
        return False
    present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    ok = set(TAG_COLUMNS) <= present
    if ok and cache is not None:
        cache[table] = True
    return ok


def ensure_columns(conn: sqlite3.Connection, table: str, cache: Optional[Dict[str, bool]] = None) -> bool:
    """Add the tag columns to ``table`` when it exists and lacks them. Under the gate; idempotent."""
    if columns_present(conn, table, cache):
        return True
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
        return False
    present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    with with_db_write():
        for column, declaration in (("content_nsfw", "INTEGER DEFAULT 0"), ("content_nsfw_score", "REAL"),
                                    ("content_nsfw_model", "TEXT")):
            if column not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
    if cache is not None:
        cache[table] = True
    return True


# --- tagging at the write --------------------------------------------------------------------------------------

def _write(conn: sqlite3.Connection, table: str, record_id: str, tag: Tag) -> None:
    conn.execute(f"UPDATE {table} SET content_nsfw=?, content_nsfw_score=?, content_nsfw_model=? "
                 f"WHERE {CANONICAL_ID_COLUMN[table]}=?", (*tag, record_id))


def _count_cleared(conn: sqlite3.Connection, cleared: int) -> None:
    """Add ``cleared`` to :data:`CLEARED_KEY` inside the caller's transaction. No commit: the clear and its count
    land together or not at all. A store with no ``engine_config`` table (no node ever) keeps no count."""
    if cleared <= 0 or conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='engine_config'").fetchone() is None:
        return
    conn.execute("INSERT INTO engine_config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                 "value=CAST(CAST(engine_config.value AS INTEGER) + ? AS TEXT)", (CLEARED_KEY, str(cleared), cleared))


def cleared_total(conn: sqlite3.Connection) -> int:
    """:data:`CLEARED_KEY`, 0 when absent or unreadable. Plain reads only."""
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='engine_config'").fetchone() is None:
            return 0
        row = conn.execute("SELECT value FROM engine_config WHERE key=?", (CLEARED_KEY,)).fetchone()
        return max(int(row[0]), 0) if row else 0
    except (sqlite3.Error, TypeError, ValueError):
        return 0


def tag_inserted(conn: sqlite3.Connection, table: str, record_id: str, content: Any, *, present: bool) -> None:
    """Tag a row just inserted by a lane that owns its transaction. ``present``: the lane's own column check."""
    if present and table in TABLES and tagging_enabled():
        _write(conn, table, record_id, tag_for(content))


def tag_stored(conn: sqlite3.Connection, table: str, record_id: str,
               cache: Optional[Dict[str, bool]] = None) -> Optional[str]:
    """Bring one stored row's tag in line with its stored text. Inside the caller's gated transaction.

    Reads the row back, so an upsert, a heal and a conflict update all leave the tag describing the text that is
    actually in the row. Returns the outcome written, or None when nothing had to be written.
    """
    if table not in TABLES or not tagging_enabled() or not ensure_columns(conn, table, cache):
        return None
    row = conn.execute(
        f"SELECT content, content_nsfw, content_nsfw_score, content_nsfw_model FROM {table} "
        f"WHERE {CANONICAL_ID_COLUMN[table]}=?", (record_id,)).fetchone()
    if row is None:
        return None
    tag, outcome = decide(row[1], row[2], row[3], evaluate(row[0]))
    if tag is None:
        return None
    _write(conn, table, record_id, tag)
    if outcome == "cleared":
        _count_cleared(conn, 1)
    return outcome


# --- the sweep ---------------------------------------------------------------------------------------------------

def _state_read(conn: sqlite3.Connection) -> dict:
    """Plain reads only: the refresh loop asks on a read-only connection."""
    loaded = None
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='engine_config'").fetchone():
        row = conn.execute("SELECT value FROM engine_config WHERE key=?", (STATE_KEY,)).fetchone()
        try:
            loaded = json.loads(row[0]) if row else None
        except (TypeError, ValueError):
            loaded = None
    if not isinstance(loaded, dict) or loaded.get("version") != STATE_VERSION:
        loaded = {"version": STATE_VERSION, "rule": None, "tables": {}, "generation": 0, "cleared_seen": 0,
                  "completed_at": None, "last_run": None}
    return loaded


def _state_write(conn: sqlite3.Connection, state: dict) -> None:
    """Inside the caller's batch, so the cursor commits with the rows it covers (the accessor's commit defers)."""
    from ..core.state import set_engine_config_value

    set_engine_config_value(conn, STATE_KEY, json.dumps(state, sort_keys=True, separators=(",", ":")))


def tag_generation(conn: sqlite3.Connection) -> Optional[int]:
    """How many finished sweeps have seen a flag cleared on an existing row here; None when none has.

    Read by the permissions refresh loop as one part of its proof digest: a cleared row is eligible with no
    assessment, and nothing else about it moves, so this is what makes the loop re-assess it. A node where no
    sweep ever cleared a flag contributes nothing, and its digest keeps the bytes it had.
    """
    try:
        generation = _state_read(conn).get("generation")
    except Exception:  # noqa: BLE001 -- an unreadable state is "none", never a reason to stop a digest
        return None
    return int(generation) if isinstance(generation, int) and generation > 0 else None


def _counts() -> Dict[str, int]:
    return dict.fromkeys(COUNTS, 0)


class Retag:
    """The sweep over the three tables. One instance per run; ``run`` returns counts only."""

    def __init__(self, connect: Callable[[], sqlite3.Connection], *, stop: Optional[Callable[[], bool]] = None,
                 batch_rows: int = BATCH_ROWS, pause: float = PAUSE_SECONDS,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time) -> None:
        self._connect = connect
        self._stop = stop or (lambda: False)
        self._batch_rows = max(1, int(batch_rows))
        self._pause = max(0.0, float(pause))
        self._sleep = sleep
        self._clock = clock

    def run(self, *, dry_run: bool = False, tables: Optional[Sequence[str]] = None,
            full: Optional[bool] = None) -> Dict[str, Any]:
        if dry_run:
            return self._run(dry_run=True, tables=tables, full=full)
        with _WRITE_RUN:
            return self._run(dry_run=False, tables=tables, full=full)

    def _run(self, *, dry_run: bool, tables: Optional[Sequence[str]], full: Optional[bool]) -> Dict[str, Any]:
        """Walk the tables. ``dry_run`` evaluates every row and writes nothing (its counts say what a write run
        would do). ``full`` forces a walk over every row from the top. By default a table is walked whole, from
        where an interrupted run stopped, until a run has finished it under the current rule; after that only its
        rows no tagger decided (or an older version of this rule did) are read."""
        if tables is not None and (not tables or not set(tables) <= set(TABLES)):
            raise ValueError("tables must name some of the NSFW-tagged tables")
        conn = self._connect()
        if conn is None:
            raise RuntimeError("database unavailable")
        if not dry_run and not tagging_enabled():
            logger.info("NSFW tag sweep: tagging is switched off (nsfw_classifier_enabled); nothing written")
            return {"version": STATE_VERSION, "rule": RULE_ID, "dry_run": False, "disabled": True, "walked": None,
                    "finished": False, "stopped": False, "generation": int(_state_read(conn).get("generation") or 0),
                    "generation_raised": False, "state_complete": False, "tables": {},
                    "totals": dict.fromkeys(COUNTS, 0)}
        if not dry_run:
            from ..core.state import _ensure_engine_config_table

            _ensure_engine_config_table(conn)
        state = _state_read(conn)
        same_rule = state.get("rule") == RULE_ID
        done = {table for table in TABLES if same_rule and (state["tables"].get(table) or {}).get("complete")}
        complete = done == set(TABLES)
        if not dry_run and not same_rule:
            state = {**state, "rule": RULE_ID, "tables": {}, "completed_at": None}
        per_table: Dict[str, Dict[str, int]] = {}
        modes = set()
        stopped = False
        for table in TABLES:
            if tables is not None and table not in tables:
                continue
            if not columns_present(conn, table) and (dry_run or not self._ensure(conn, table)):
                if not dry_run:
                    # No such table yet: nothing to walk. Its first rows are tagged where they are written, and the
                    # pending check catches any that are not; left incomplete, every check would walk every table.
                    self._commit(conn, table, state, 0, complete=True)
                continue
            counts = _counts()
            per_table[table] = counts
            # A write run over every row resumes where the last one stopped; a dry run, a forced run and a
            # pending-only walk start from the top (a row no tagger decided can sit anywhere).
            walk_all = dry_run or bool(full) or table not in done
            modes.add("all" if walk_all else "pending")
            progress = state["tables"].get(table) or {}
            cursor = int(progress.get("cursor") or 0) if (walk_all and not dry_run and not full and same_rule) else 0
            while True:
                if self._stop():
                    stopped = True
                    break
                rows = self._read(conn, table, cursor, walk_all)
                if rows:
                    cursor = rows[-1][0]
                    self._evaluate(conn, table, rows, counts, state, dry_run=dry_run, cursor=cursor)
                if len(rows) < self._batch_rows:
                    if not dry_run:
                        self._commit(conn, table, state, cursor, complete=True)
                    break
                if self._pause:
                    self._sleep(self._pause)
            if stopped:
                break
        finished = not stopped and not dry_run
        raised = False
        if finished:
            if tables is None:
                state["completed_at"] = int(self._clock())
            with batched_writes(conn):
                # Read under the gate: a write that clears a flag counts in its own transaction.
                seen = cleared_total(conn)
                if seen > int(state.get("cleared_seen") or 0):
                    state["generation"] = int(state.get("generation") or 0) + 1
                    raised = True
                state["cleared_seen"] = seen
                state.pop("pending_cleared", None)
                _state_write(conn, state)
        totals = {key: sum(c[key] for c in per_table.values()) for key in COUNTS}
        logger.info("NSFW tag sweep: dry_run=%s finished=%s stopped=%s evaluated=%d tagged=%d cleared=%d "
                    "flagged=%d relabelled=%d generation=%s", dry_run, finished, stopped, totals["evaluated"],
                    totals["tagged"], totals["cleared"], totals["flagged"], totals["relabelled"],
                    state.get("generation"))
        return {"version": STATE_VERSION, "rule": RULE_ID, "dry_run": dry_run, "disabled": False,
                "walked": modes.pop() if len(modes) == 1 else ("mixed" if modes else None),
                "finished": finished, "stopped": stopped,
                "generation": int(state.get("generation") or 0), "generation_raised": raised,
                "state_complete": bool(complete or (finished and tables is None)),
                "tables": per_table, "totals": totals}

    def _ensure(self, conn: sqlite3.Connection, table: str) -> bool:
        with batched_writes(conn):
            return ensure_columns(conn, table)

    def _read(self, conn: sqlite3.Connection, table: str, after: int, walk_all: bool) -> List[tuple]:
        pending = "" if walk_all else (" AND (content_nsfw_model IS NULL OR (content_nsfw_model LIKE ? "
                                      "AND content_nsfw_model != ?))")
        args: list = [after]
        if not walk_all:
            args += [RULE_FAMILY + "%", RULE_ID]
        args.append(self._batch_rows)
        return conn.execute(
            f"SELECT rowid, {CANONICAL_ID_COLUMN[table]}, content, content_nsfw, content_nsfw_score, "
            f"content_nsfw_model FROM {table} WHERE rowid > ?{pending} ORDER BY rowid LIMIT ?", args).fetchall()

    def _evaluate(self, conn: sqlite3.Connection, table: str, rows: List[tuple], counts: Dict[str, int],
                  state: dict, *, dry_run: bool, cursor: int) -> None:
        writes: List[Tuple[str, Any, Tag, str]] = []
        for _rowid, record_id, content, flag, score, model in rows:
            counts["rows"] += 1
            if model is None:
                counts["never_tagged"] += 1
            elif is_rule_tag(model):
                counts["rule_tagged"] += 1
            else:
                counts["legacy_tagged"] += 1
            verdict = evaluate(content)
            if isinstance(content, str) and content.strip():
                counts["with_text"] += 1
            counts["evaluated"] += 1
            if verdict.flagged:
                counts["flagged_unambiguous" if verdict.tier == TIER_UNAMBIGUOUS else "flagged_phrase"] += 1
            tag, outcome = decide(flag, score, model, verdict)
            if tag is None or dry_run:
                counts[outcome] += 1
                continue
            writes.append((record_id, content, tag, outcome))
        if dry_run:
            return
        id_col = CANONICAL_ID_COLUMN[table]
        with batched_writes(conn):
            cleared = 0
            for record_id, content, tag, outcome in writes:
                # Written only over the text that was read: a row rewritten meanwhile was tagged by its writer.
                changed = conn.execute(
                    f"UPDATE {table} SET content_nsfw=?, content_nsfw_score=?, content_nsfw_model=? "
                    f"WHERE {id_col}=? AND content IS ?", (*tag, record_id, content)).rowcount
                if changed == 1:
                    counts[outcome] += 1
                    cleared += outcome == "cleared"
                else:
                    counts["not_written"] += 1
            _count_cleared(conn, cleared)
            state["tables"][table] = {"cursor": cursor, "complete": False}
            state["last_run"] = int(self._clock())
            _state_write(conn, state)

    def _commit(self, conn: sqlite3.Connection, table: str, state: dict, cursor: int, *, complete: bool) -> None:
        with batched_writes(conn):
            state["tables"][table] = {"cursor": cursor, "complete": complete}
            state["last_run"] = int(self._clock())
            _state_write(conn, state)


def run_sweep(connect: Callable[[], sqlite3.Connection], *, dry_run: bool = False,
              tables: Optional[Sequence[str]] = None, stop: Optional[Callable[[], bool]] = None,
              full: Optional[bool] = None) -> Dict[str, Any]:
    return Retag(connect, stop=stop).run(dry_run=dry_run, tables=tables, full=full)


async def run_at_startup(connect: Callable[[], Optional[sqlite3.Connection]], *, delay: float = 15.0,
                         interval: float = RECHECK_SECONDS) -> None:
    """The node's own repair: one sweep after startup, then a re-check every ``interval`` seconds.

    Runs on a worker thread off the event loop (the gate is a blocking lock). Stops between batches when the
    runtime is shutting down, and the task's cancellation ends the wait between runs. Never raises.
    """
    import asyncio

    from ..runtime_shutdown import is_shutdown_requested

    if delay:
        await asyncio.sleep(delay)
    while True:
        try:
            await asyncio.to_thread(run_sweep, connect, stop=is_shutdown_requested)
        except Exception as exc:  # noqa: BLE001 -- the class name only, never a row
            logger.warning("NSFW tag sweep failed (%s)", type(exc).__name__)
        if is_shutdown_requested():
            return
        await asyncio.sleep(interval)
