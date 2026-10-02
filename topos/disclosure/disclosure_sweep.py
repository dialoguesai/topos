"""PII disclosure for every canonical row with text, whatever wrote it: the privacy layer's sweep.

**The gap.** The privacy layer (:mod:`.privacy_layer`) writes each row's PII-redacted copy (``content_disclosure``,
``*_disclosure_hash``, ``*_disclosure_model``), and only the ingest pipeline ran it. Rows written any other way never
got one: the node's own messenger sync (every iMessage and Signal row ``local_sync`` writes through
``ConversationsTablesManager``), the owner-attested iMessage and ChatGPT snapshot lanes, a message whose body a re-sync
healed (the heal clears the old disclosure and nothing refilled it), an AI-chat or journal row whose text a later write
replaced (those upserts keep the old disclosure beside the new text), and any import whose privacy stage failed or was
interrupted. On one owner's node 90,936 of 96,700 messages and 2,621 of 14,737 AI-chat rows had none.

**What a missing disclosure does.** The legacy grantee reads (UMA scope reads, the query pipeline's disclosure SQL)
fail closed: they show ``[disclosure pending]`` where the text would be. Permissions v2 releases read ``content`` and
never this column, and the column is outside every reviewed surface, so writing it stales no review and no index.

**Which rows.** A field needs the layer when it has text and its stored hash is not
:func:`.privacy_layer.disclosure_hash` of that text. That one predicate is both the resume point and the version key:

* no hash: never disclosed (every gap above), or a record the filter failed on (the layer writes nothing for it);
* a hash of other text: the text changed under a kept disclosure;
* a hash under another layer version: raising ``PRIVACY_LAYER_VERSION`` makes every stored disclosure read as out of
  date, so the next full walk re-runs the layer over every row. Version 1 hashes the text alone, as every disclosure
  written before this module did, so nothing already current is redone.

A row the sweep has redacted matches again, so an interrupted walk resumes by itself with no cursor to keep; there is
no state in the database at all.

**How.** Journal entries, then messages, then AI-chat rows (:data:`ORDER`), each newest first (descending rowid), read
in batches with no gate held. The model runs outside the write gate, one call at a time, at most :data:`CHUNK_ROWS`
rows and about :data:`CHUNK_CHARS` characters per call; each call's results are written in one short gated
transaction, and only over the text that was read (``WHERE <field> IS ?``), so a row rewritten meanwhile keeps whatever
its writer left and is caught by the next walk. A record the filter failed on is left empty and retried by the next
walk. While the pipeline's own privacy stage is redacting in this process the sweep waits between calls, rather than
run the same model beside it. Walks run one after another: rows written during a long first walk over a backlog are
filled by the pending walk that follows it.

**When** (:func:`run_at_startup`, no owner action): a full walk (every row's hash checked; about 5 s of reads on
111,000 rows) a minute after startup and then every :data:`VERIFY_SECONDS`; in between, a pending walk (rows with no
hash, found in SQL) every :data:`RECHECK_SECONDS`, or within :data:`POLL_SECONDS` when a writer that bypasses the
pipeline calls :func:`request_run` after it commits. A walk that finds the filter unavailable stops and is tried again
at the next interval.

**Cost** (measured on invented text, CPU, the node's two torch threads): 40-120 ms for a text message, 0.4 s at
1,000 characters, 1.1 s at 2,100, 3 s at 4,000 and 17 s at the filter's 8,000-character cap. End to end through this
sweep and the in-process engine, on invented rows with the owner's length mix and the machine at load 10-12: 127 ms a
message, 3.7 s an AI-chat row. The owner's backlog above is therefore about 2-3 h for the messages and 2.5-3 h for the
AI-chat rows, once; after that a day's new rows take seconds.

**Off.** ``platform_privacy_via_engine`` off switches the layer off everywhere: the sweep calls no model and writes
nothing (a dry run still counts). Changing ``privacy_filter_model`` does not re-run stored rows; raise ``PRIVACY_LAYER_VERSION`` for that.

Counts only, in results and in the log: no row id or text leaves this module.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..storage.db.write_gate import batched_writes
from .canonical_writer import DISCLOSURE_MODEL_SETTING
from .field_registry import CANONICAL_ID_COLUMN, PII_DISCLOSURE_FIELDS, disclosure_column, disclosure_hash_column

logger = logging.getLogger("topos.disclosure.disclosure_sweep")

#: Rows read per query (no gate held).
READ_ROWS = 500
#: Rows per model call and per gated write.
CHUNK_ROWS = 8
#: Characters per model call, about 4 s of filter time on CPU; a longer row is a call of its own.
CHUNK_CHARS = 6000
#: Pause between calls, so every other writer and the model's other users get a turn.
PAUSE_SECONDS = 0.05
#: A pending walk (rows with no hash) at least this often.
RECHECK_SECONDS = 600.0
#: A full walk (every row's hash checked) at least this often.
VERIFY_SECONDS = 6 * 3600.0
#: How soon a writer's request is noticed, and how often a waiting sweep looks at the pipeline's stage.
POLL_SECONDS = 5.0
STARTUP_DELAY_SECONDS = 60.0
MODES = ("verify", "pending")
#: Walk order: the tables a grant is likeliest to read and cheapest to fill first (AI-chat rows run long), then any
#: other table the field registry discloses.
ORDER = ("journal_entries", "conversation_messages", "ai_chat_messages", "location_events")
COUNTS = ("rows", "fields_with_text", "current", "missing", "stale", "redacted", "failed", "not_written", "calls")

Target = Tuple[str, str, Tuple[str, ...], Tuple[str, ...]]
_REQUESTED = threading.Event()
#: One write run at a time in this process.
_RUN_LOCK = threading.Lock()
_warned_unavailable = False


def request_run() -> None:
    """Ask the running sweep for a pending walk now. For writers that bypass the pipeline's privacy stage; call it
    after the rows are committed. Costs nothing when no sweep is running."""
    _REQUESTED.set()


def layer_enabled() -> bool:
    """``platform_privacy_via_engine``: the switch for the whole privacy layer (the pipeline's stage reads it too)."""
    try:
        from ..config.settings import settings
    except Exception:  # noqa: BLE001 -- a settings module that cannot load never switches the layer off
        return True
    return bool(getattr(settings, "platform_privacy_via_engine", True))


def targets(conn: sqlite3.Connection) -> List[Target]:
    """(table, id column, disclosed fields, model columns) for every table that holds disclosure columns.

    A field counts when the field, its disclosure and its hash are all columns; the model columns are every
    ``<field>_disclosure_model`` the table has (an AI-chat row's one model column covers both its fields), which is
    what the layer's own writer sets."""
    found: List[Target] = []
    names = [t for t in ORDER if t in PII_DISCLOSURE_FIELDS] + [t for t in PII_DISCLOSURE_FIELDS if t not in ORDER]
    for table in names:
        fields = PII_DISCLOSURE_FIELDS[table]
        id_col = CANONICAL_ID_COLUMN.get(table)
        if not id_col or conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
            continue
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        disclosed = tuple(f for f in fields if {f, disclosure_column(f), disclosure_hash_column(f)} <= columns)
        if id_col in columns and disclosed:
            models = tuple(f"{f}_disclosure_model" for f in fields if f"{f}_disclosure_model" in columns)
            found.append((table, id_col, disclosed, models))
    return found


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _read(conn: sqlite3.Connection, target: Target, before: Optional[int], mode: str, limit: int) -> List[tuple]:
    table, id_col, fields, _models = target
    selected = ", ".join(f"{f}, {disclosure_hash_column(f)}" for f in fields)
    where, args = [], []
    if before is not None:
        where.append("rowid < ?")
        args.append(before)
    if mode == "pending":
        where.append("(" + " OR ".join(f"({f} IS NOT NULL AND trim({f}) != '' AND {disclosure_hash_column(f)} IS NULL)"
                                      for f in fields) + ")")
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    return conn.execute(f"SELECT rowid, {id_col}, {selected} FROM {table}{clause} ORDER BY rowid DESC LIMIT ?",
                        (*args, limit)).fetchall()


def _write(conn: sqlite3.Connection, target: Target, ops: Sequence[tuple], model_id: str) -> int:
    """One gated transaction: each redacted field, over the text that was read and nothing else."""
    table, id_col, _fields, models = target
    written = 0
    with batched_writes(conn):
        for record_id, field, raw, redacted, digest in ops:
            sets = [f"{disclosure_column(field)}=?", f"{disclosure_hash_column(field)}=?", *(f"{m}=?" for m in models)]
            written += conn.execute(
                f"UPDATE {table} SET {', '.join(sets)} WHERE {id_col}=? AND {field} IS ?",
                (redacted, digest, *([model_id] * len(models)), record_id, raw)).rowcount
    return written


class Sweep:
    """One walk over every disclosed table. ``run`` returns counts only."""

    def __init__(self, connect: Callable[[], Optional[sqlite3.Connection]], *, client: Any = None,
                 stop: Optional[Callable[[], bool]] = None, read_rows: int = READ_ROWS, chunk_rows: int = CHUNK_ROWS,
                 chunk_chars: int = CHUNK_CHARS, pause: float = PAUSE_SECONDS, poll: float = POLL_SECONDS,
                 stage_active: Optional[Callable[[], bool]] = None) -> None:
        self._connect = connect
        self._client = client
        self._stop = stop or (lambda: False)
        self._read_rows = max(1, int(read_rows))
        self._chunk_rows = max(1, int(chunk_rows))
        self._chunk_chars = max(1, int(chunk_chars))
        self._pause = max(0.0, float(pause))
        self._poll = max(0.0, float(poll))
        if stage_active is None:
            from .privacy_layer import privacy_stage_active as stage_active
        self._stage_active = stage_active

    async def _db(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        """Every database section on a worker thread with that thread's own connection: the gate is a blocking lock."""
        def call():
            conn = self._connect()
            if conn is None:
                raise RuntimeError("database unavailable")
            return fn(conn)

        return await asyncio.to_thread(call)

    async def run(self, *, mode: str = "verify", dry_run: bool = False,
                  tables: Optional[Sequence[str]] = None) -> Dict[str, Any]:
        """Walk the tables. ``verify`` reads every row and checks every hash; ``pending`` reads only rows with a
        field that has text and no hash. ``dry_run`` makes no model call and writes nothing: its counts say what a
        write run would redact (``missing`` + ``stale``)."""
        if mode not in MODES:
            raise ValueError("mode must be verify or pending")
        known = tuple(PII_DISCLOSURE_FIELDS)
        if tables is not None and (not tables or not set(tables) <= set(known)):
            raise ValueError("tables must name some of the disclosed tables")
        result: Dict[str, Any] = {"mode": mode, "dry_run": dry_run, "disabled": False, "finished": False,
                                  "stopped": False, "unavailable": False, "tables": {}, "totals": dict.fromkeys(COUNTS, 0)}
        if not dry_run and not layer_enabled():
            result["disabled"] = True
            return result
        if dry_run:
            return await self._walk_all(result, mode, tables, dry_run=True)
        if not _RUN_LOCK.acquire(blocking=False):
            result["busy"] = True
            return result
        try:
            return await self._walk_all(result, mode, tables, dry_run=False)
        finally:
            _RUN_LOCK.release()

    async def _walk_all(self, result: Dict[str, Any], mode: str, tables: Optional[Sequence[str]], *,
                        dry_run: bool) -> Dict[str, Any]:
        from .privacy_layer import disclosure_hash

        found = await self._db(targets)
        client = self._client
        if client is None and not dry_run:
            from .privacy_layer import PrivacyLayerClient

            client = PrivacyLayerClient.from_settings()
        outcome = "finished"
        for target in found:
            table = target[0]
            if tables is not None and table not in tables:
                continue
            counts = dict.fromkeys(COUNTS, 0)
            result["tables"][table] = counts
            before: Optional[int] = None
            while outcome == "finished":
                if self._stop():
                    outcome = "stopped"
                    break
                rows = await self._db(lambda conn, b=before: _read(conn, target, b, mode, self._read_rows))
                if not rows:
                    break
                before = rows[-1][0]
                pending: List[tuple] = []
                for row in rows:
                    counts["rows"] += 1
                    record_id = row[1]
                    for index, field in enumerate(target[2]):
                        text, stored = row[2 + 2 * index], row[3 + 2 * index]
                        if not _has_text(text):
                            continue
                        counts["fields_with_text"] += 1
                        digest = disclosure_hash(text)
                        if stored == digest:
                            counts["current"] += 1
                            continue
                        counts["missing" if stored is None else "stale"] += 1
                        pending.append((row[0], record_id, field, text, digest))
                if not dry_run and pending:
                    outcome = await self._redact(client, target, pending, counts)
                if len(rows) < self._read_rows:
                    break
        result["finished"] = outcome == "finished"
        result["stopped"] = outcome == "stopped"
        result["unavailable"] = outcome == "unavailable"
        result["totals"] = {key: sum(c[key] for c in result["tables"].values()) for key in COUNTS}
        totals = result["totals"]
        level = logging.INFO if (totals["redacted"] or totals["failed"] or not result["finished"]) and not dry_run \
            else logging.DEBUG
        logger.log(level, "PII disclosure sweep: mode=%s dry_run=%s finished=%s stopped=%s unavailable=%s rows=%d "
                   "missing=%d stale=%d redacted=%d failed=%d not_written=%d calls=%d", mode, dry_run,
                   result["finished"], result["stopped"], result["unavailable"], totals["rows"], totals["missing"],
                   totals["stale"], totals["redacted"], totals["failed"], totals["not_written"], totals["calls"])
        return result

    def _chunks(self, pending: List[tuple]) -> List[List[tuple]]:
        """Whole rows per chunk (a row's fields travel together), at most ``chunk_rows`` rows and about
        ``chunk_chars`` characters; a row longer than that is a chunk of its own."""
        chunks: List[List[tuple]] = []
        current: List[tuple] = []
        rows: set = set()
        chars = 0
        for item in pending:
            rowid, text = item[0], item[3]
            new_row = rowid not in rows
            if current and new_row and (len(rows) >= self._chunk_rows or chars + len(text) > self._chunk_chars):
                chunks.append(current)
                current, rows, chars = [], set(), 0
            current.append(item)
            rows.add(rowid)
            chars += len(text)
        if current:
            chunks.append(current)
        return chunks

    async def _redact(self, client: Any, target: Target, pending: List[tuple], counts: Dict[str, int]) -> str:
        global _warned_unavailable
        from ..sanitization.privacy_filter import PRIVACY_DISCLOSE_MAX_BATCH

        table = target[0]
        for chunk in self._chunks(pending):
            while self._stage_active():
                if self._stop():
                    return "stopped"
                await asyncio.sleep(self._poll)
            if self._stop():
                return "stopped"
            keyed = {f"{table}:{record_id}:{field}": (record_id, field, text, digest)
                     for _rowid, record_id, field, text, digest in chunk}
            items = [{"id": key, "text": value[2]} for key, value in keyed.items()][:PRIVACY_DISCLOSE_MAX_BATCH]
            counts["calls"] += 1
            answer = await client.redact_batch(items)
            if not isinstance(answer, dict) or answer.get("status") in ("unavailable", "failed", "too_large", "invalid"):
                status = answer.get("status") if isinstance(answer, dict) else "invalid"
                if not _warned_unavailable:
                    logger.warning("PII disclosure sweep: the privacy filter answered %s; trying again later", status)
                    _warned_unavailable = True
                return "unavailable"
            _warned_unavailable = False
            by_id = {str(item.get("id")): item for item in (answer.get("items") or []) if isinstance(item, dict)}
            model_id = str(answer.get("model") or DISCLOSURE_MODEL_SETTING)
            ops = []
            for key, (record_id, field, text, digest) in keyed.items():
                item = by_id.get(key) or {}
                redacted = item.get("text")
                # A record the filter failed on comes back with its raw text beside the error: never written as its
                # disclosure (see the privacy layer); left empty, it is tried again by the next walk.
                if not isinstance(redacted, str) or item.get("error"):
                    counts["failed"] += 1
                    continue
                ops.append((record_id, field, text, redacted, digest))
            if ops:
                written = await self._db(lambda conn, o=ops: _write(conn, target, o, model_id))
                counts["redacted"] += written
                counts["not_written"] += len(ops) - written
            if self._pause:
                await asyncio.sleep(self._pause)
        return "finished"


async def run_sweep(connect: Callable[[], Optional[sqlite3.Connection]], *, mode: str = "verify",
                    dry_run: bool = False, tables: Optional[Sequence[str]] = None,
                    stop: Optional[Callable[[], bool]] = None, client: Any = None) -> Dict[str, Any]:
    return await Sweep(connect, client=client, stop=stop).run(mode=mode, dry_run=dry_run, tables=tables)


async def run_at_startup(connect: Callable[[], Optional[sqlite3.Connection]], *,
                         delay: float = STARTUP_DELAY_SECONDS, recheck: float = RECHECK_SECONDS,
                         verify_every: float = VERIFY_SECONDS, poll: float = POLL_SECONDS,
                         client: Any = None, clock: Callable[[], float] = time.monotonic) -> None:
    """The node's own catch-up: a full walk after startup, pending walks on request and on an interval, a full walk
    again every ``verify_every`` seconds. Stops between calls when the runtime is shutting down; never raises."""
    from ..runtime_shutdown import is_shutdown_requested

    if delay:
        await asyncio.sleep(delay)
    last_verify: Optional[float] = None
    while not is_shutdown_requested():
        started = clock()
        mode = "verify" if last_verify is None or started - last_verify >= verify_every else "pending"
        _REQUESTED.clear()
        unavailable = False
        try:
            result = await run_sweep(connect, mode=mode, stop=is_shutdown_requested, client=client)
            unavailable = bool(result.get("unavailable"))
            if mode == "verify" and result.get("finished"):
                last_verify = started
        except Exception as exc:  # noqa: BLE001 -- the class name only, never a row
            logger.warning("PII disclosure sweep failed (%s)", type(exc).__name__)
            unavailable = True
        if is_shutdown_requested():
            return
        # Wait for the interval, or for a writer's request; after a failure only the interval, so a writer that asks
        # on every batch cannot turn an unavailable filter into a busy loop.
        waited = 0.0
        while waited < recheck and not is_shutdown_requested() and (unavailable or not _REQUESTED.is_set()):
            await asyncio.sleep(poll)
            waited += poll
