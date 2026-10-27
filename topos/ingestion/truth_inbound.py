"""The inbound truth lane: verify_claim v2 on new messages, transcripts and journal entries, for the owner alone.

When the owner turns it on (``topos/config/truth_inbound.py``, default off), the node checks rows of the chosen
sources as they land and keeps what it found in ``truth_checks`` for the owner to label in the app
(VERIFY_CLAIM_WEEKLY_PROGRAM.md §2a). Everything runs on the node; the claim text, the cards and the labels are
owner-only (``topos/query/truth_checks.py``).

Where it runs. Never on the request path. A finished ingest only queues work: ``run_post_canonical_pipeline`` (file
imports, app ingest, voice transcripts, journal readers) calls :func:`after_rows_stored`; a finished local sync
(iMessage, Signal: that path does not go through the post-canonical pipeline) and each finished batch call
:func:`enqueue_truth_checks` from ``job_runner.process_job``; the general worker's five-minute sweep calls it too, so
rows from any other door, and a lane coming out of a pause, are picked up. A job is kind ``truth_inbound_check`` on
the catch-up worker's lane (``job_runner._CATCHUP_KINDS``), beside the embeddings catch-up, and its payload holds
``[table, record_id]`` pairs only, never text. At most one is queued or running at a time; each finished batch queues
the next.

What is owed. A row of a chosen source from the last :data:`WINDOW_DAYS` days (by its own time), with text, and no
``truth_checks`` row yet: newest first, so turning the lane on checks the past week, and a first sync of years of
history does not queue years of checks. A row is checked once, unless its check came back ``unable``: then the lane
checks it again (:func:`recheck_due`, ``truth_checks.recheck_reason``) at once when the lane's model, effort or reader
timeout is not the one that check ran with, else after :data:`RECHECK_COOLDOWN_S`; at most
``truth_checks.MAX_ATTEMPTS`` checks in all, never once the owner labelled it, and only while the row is still a
chosen source's row with text (the filters new rows get, with no time window). New rows go first; re-checks fill the
rest of a batch. A re-check replaces the row's result in place (``truth_checks.recheck``): one row per record, its
week unchanged, ``attempts`` counting the checks.

Each check. ``statement`` = the row's text (first 1000 characters); ``context`` = the six rows before it in the same
conversation; ``said_at`` = the row's time with the node's offset; ``speaker`` = ``"owner"`` for the owner's own row,
else the contact's display name (never a raw identifier: "a contact" when the contacts do not name them);
``about_others`` = the speaker is not the owner; ``effort`` = ``low``; ``deadline_s`` = 30; ``read_timeout_s`` =
:func:`read_timeout_for` the lane's model (20 s on the local model, whose reader cannot finish in the engine's 8 s on
a busy machine; the engine's 8 s on the hosted one); ``source_kind`` = :data:`SOURCE_KINDS` of the row's source
(``message``, ``transcript`` or ``journal``), so the reader refuses a turn as talk about the conversation itself only
by what that means for a chat line, a recording or a journal entry. A row the engine's gate
refuses (a fragment, a question, empty) costs no model call and is stored as ``not_checkable``. Model checks start at
most ``rate_per_minute`` times a minute (:data:`PACER`), and a job stops after :data:`JOB_BUDGET_S` seconds so the
embeddings catch-up never waits long behind it; what it did not reach is still owed and goes in the next batch.

The model. ``local`` and ``hosted`` are sides of the owner's verify_claim model pick (``config/verify_claim_model.py``,
``verify_v2_models.chat_for_lane``): ``local`` is ``LocalChat`` on the picked local model (else the node's default
local model), ``hosted`` is ``HostedChat`` on the picked hosted model (else the default; the Red Pill key, behind the
hosted wallet check). A hosted 402, an exhausted wallet or a missing key pauses the lane for ten minutes
(``truth_inbound_paused_until``) without storing the row; after the pause a probe (key present, wallet allows) must
pass before the next check, else the pause is renewed. A local model that cannot be reached pauses it the same way, so
a stopped Ollama never marks a week of rows ``unable``.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("topos.ingestion.truth_inbound")

#: pipeline_jobs kind. A queued row outlives the build that wrote it, so the kind is never renamed.
KIND = "truth_inbound_check"
WINDOW_DAYS = 7
BATCH_SIZE = 50
MAX_BATCH_SIZE = 200
#: One job's longest run before it hands the catch-up lane back (about one minute of checks at the default rate).
JOB_BUDGET_S = 60.0
DEADLINE_S = 30.0
EFFORT = "low"
#: The reader's cap on the local model, inside DEADLINE_S. The first evening on the owner's node (23 Oct 2026) timed
#: out 116 of 119 gate-passed rows at the engine's 8 s, which was sized for the hosted 70B; a deadline of 30 s leaves
#: 10 s for retrieval and the one judge call at effort low.
LOCAL_READ_TIMEOUT_S = 20.0
#: The reader's source kind for each truth source (``verify_v2.SOURCE_KINDS``).
SOURCE_KINDS = {"imessage": "message", "signal": "message", "transcripts": "transcript", "journal": "journal"}
#: How long an ``unable`` row waits before the same model, effort and reader timeout check it again.
RECHECK_COOLDOWN_S = 6 * 3600
CONTEXT_TURNS = 6
PAUSE_S = 600
#: Calibrate step 1 (owner, 27 Oct 2026): "claims are mine; other people's messages are evidence, not claims". On, the
#: lane checks only the owner's own rows (their messages, their transcript turns, their journal); a contact's message is
#: never queued or re-checked, and stays readable as evidence. Off: every row of the chosen sources, as in 1.5.28 (in
#: the lane's first week 685 of its 804 iMessage checks were contacts' messages, which the owner could not label).
OWNER_CLAIMS_ONLY = True
_OWNER_ROWS = {"conversation_messages": " AND (m.is_from_self = 1 OR LOWER(COALESCE(m.sender_id, '')) = 'self')",
               "transcript_segments": " AND m.is_from_self = 1"}

#: VERIFY_V2_WARM_LOCAL (verify_claim tracker round r2; off unless ``TOPOS_VERIFY_V2_WARM_LOCAL=1``). The node's Ollama
#: holds one model at a time and the derivation lane swaps its 9B extractor and 27B verifier all day (the owner's
#: node, 27 Oct 2026: the 9B loaded 37 times and the 27B 12 times in one day; a cold 9B load took 76 s, a warm reader
#: call 9-13 s). A check that lands on a swap spends its 20 s reader cap waiting for the load and is stored ``unable``;
#: after three such checks the row is never checked again (91 of 91 ``unable`` rows in the lane's first week). With
#: the switch on, a local check first makes sure the lane's model is loaded, outside the row's deadline and reader cap;
#: a load that does not finish defers the rest of the batch and stores nothing, so no attempt is spent on a swap.
WARM_LOCAL_ENV = "TOPOS_VERIFY_V2_WARM_LOCAL"
#: The longest a load may take before the batch is deferred (a cold 9B behind a 27B unload took 76 s).
WARM_TIMEOUT_S = 120.0


def warm_local_on() -> bool:
    return os.environ.get(WARM_LOCAL_ENV, "").strip() == "1"


def ensure_warm(chat: Any) -> Tuple[bool, float]:
    """(ready, seconds spent loading). A chat with no ``resident``/``warm`` (the hosted one) is always ready. When the
    server cannot say whether the model is loaded, it is loaded anyway: the load call is cheap on a loaded model."""
    resident, warm = getattr(chat, "resident", None), getattr(chat, "warm", None)
    if resident is None or warm is None:
        return True, 0.0
    try:
        if resident():
            return True, 0.0
    except Exception:  # noqa: BLE001 — an unanswered /api/ps is not a reason to skip the load
        pass
    started = time.monotonic()
    try:
        warm(WARM_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — ModelUnavailable or anything else: the row waits for the next batch
        return False, time.monotonic() - started
    return True, time.monotonic() - started


#: ``model_unavailable:<cause>`` causes that pause the lane instead of storing an ``unable`` row.
HOSTED_PAUSE_CAUSES = frozenset({"http_402", "insufficient_credits", "no_hosted_key"})
LOCAL_PAUSE_CAUSES = frozenset({"URLError"})


def truth_source_for_definition(source_def: Any) -> Optional[str]:
    """The truth source a stored batch belongs to, from its definition (installed journal readers included)."""
    source_id = str(getattr(source_def, "source_id", "") or "")
    if source_id in ("imessage", "signal"):
        return source_id
    if source_id == "voxterm_transcripts":
        return "transcripts"
    group = str(getattr(source_def, "canonical_group_id", "") or "")
    if group == "journal":
        return "journal"
    if group == "transcripts":
        return "transcripts"
    return None


# --- discovery ------------------------------------------------------------------------------------------------------
def _cutoff(now: datetime) -> str:
    # A date prefix compares correctly against every stored time format (Z, offset, naive).
    return (now - timedelta(days=WINDOW_DAYS)).date().isoformat()


def _chosen(sources: List[str]) -> List[Tuple[str, str, str, str, tuple]]:
    """For each checked table the chosen sources reach: (table, id column, time column, extra filter on ``m``, its
    parameters). The one place that says which rows of a table belong to the chosen sources."""
    from ..query.truth_checks import CONVERSATION_SOURCE_IDS

    out: List[Tuple[str, str, str, str, tuple]] = []
    source_ids = [sid for name in sources for sid in CONVERSATION_SOURCE_IDS.get(name, ())]
    if source_ids:
        out.append(("conversation_messages", "message_id", "event_at",
                    f" AND m.source_id IN ({','.join('?' for _ in source_ids)})", tuple(source_ids)))
    if "journal" in sources:
        out.append(("journal_entries", "entry_id", "entry_at", "", ()))
    if "transcripts" in sources:
        out.append(("transcript_segments", "segment_id", "event_at", " AND COALESCE(m.actor_role, 'ambient') != 'ambient'", ()))
    if OWNER_CLAIMS_ONLY:  # a cheap pre-filter; _prepare still applies the full owner rule (truth_checks._owner_row)
        out = [(table, id_column, time_column, extra + _OWNER_ROWS.get(table, ""), params)
               for table, id_column, time_column, extra, params in out]
    return out


def owed(conn: Any, sources: List[str], limit: int, *, now: Optional[datetime] = None) -> List[List[str]]:
    """``[table, record_id]`` pairs still to check for the chosen sources, newest first."""
    from ..query.truth_checks import CHECKS_TABLE, _present

    cutoff = _cutoff(now or datetime.now(timezone.utc))
    checked = _present(conn, CHECKS_TABLE)
    found: List[Tuple[str, str, str]] = []
    for table, id_column, time_column, extra, params in _chosen(sources):
        if not _present(conn, table):
            continue
        unchecked = (f" AND NOT EXISTS (SELECT 1 FROM {CHECKS_TABLE} t WHERE t.record_table='{table}' "
                     f"AND t.record_id=m.{id_column})") if checked else ""
        rows = conn.execute(
            f"SELECT m.{id_column}, m.{time_column} FROM {table} m WHERE m.{time_column} >= ? "
            f"AND m.content IS NOT NULL AND TRIM(m.content) != ''{extra}{unchecked} "
            f"ORDER BY m.{time_column} DESC LIMIT ?", (cutoff, *params, int(limit))).fetchall()
        found.extend((str(r[1] or ""), table, str(r[0])) for r in rows if r[0] is not None)
    found.sort(key=lambda item: item[0], reverse=True)
    return [[table, record_id] for _, table, record_id in found[:limit]]


def recheck_due(conn: Any, sources: List[str], signature: Any, limit: int, *,
                now: Optional[datetime] = None) -> List[List[str]]:
    """``[table, record_id]`` pairs of stored ``unable`` checks to run again (``truth_checks.recheck_reason`` against
    the lane's ``signature``), newest check first. Only rows the job will not skip: still a chosen source's row, with
    text, in its table (else a batch of skipped rows would queue itself again and again)."""
    from ..query.truth_checks import CHECKS_TABLE, _present, recheck_reason, state_columns, state_of

    if limit <= 0 or not _present(conn, CHECKS_TABLE):
        return []
    moment = now or datetime.now(timezone.utc)
    columns = state_columns(conn)
    found: List[Tuple[str, str, str]] = []
    for table, id_column, _, extra, params in _chosen(sources):
        if not _present(conn, table):
            continue
        rows = conn.execute(
            f"SELECT c.created_at, c.record_id, {columns} FROM {CHECKS_TABLE} c JOIN {table} m "
            f"ON m.{id_column} = c.record_id WHERE c.record_table = ? AND c.verdict = 'unable' "
            f"AND m.content IS NOT NULL AND TRIM(m.content) != ''{extra}", (table, *params)).fetchall()
        for created, record_id, *state in rows:
            if recheck_reason(state_of(state), signature, now=moment, cooldown_s=RECHECK_COOLDOWN_S):
                found.append((str(created or ""), table, str(record_id)))
    found.sort(key=lambda item: item[0], reverse=True)
    return [[table, record_id] for _, table, record_id in found[:limit]]


def read_timeout_for(model: str) -> float:
    """The reader's cap for the lane's model setting: the engine's default on the hosted model, longer on the local one."""
    from ..query.verify_v2 import READ_TIMEOUT_S

    return READ_TIMEOUT_S if model == "hosted" else LOCAL_READ_TIMEOUT_S


def signature_for(chat: Any, model: str) -> Tuple[str, str, float]:
    """What a check by this lane runs with, as ``truth_checks`` records it: (model label, effort, reader timeout)."""
    return _model_label(chat), EFFORT, read_timeout_for(model)


def enqueue_truth_checks(conn: Any, *, now: Optional[datetime] = None) -> Optional[str]:
    """Queue the next owed batch when the lane is on, not paused and has nothing in flight. Returns the job id."""
    from ..config.truth_inbound import load, paused_until
    from ..pipeline.job_store import enqueue_job, ensure_pipeline_jobs_schema
    from ..query.truth_checks import ensure_tables
    from ..storage.db.write_gate import with_db_write

    if conn is None:
        return None
    settings = load(conn)
    if not settings["enabled"] or not settings["sources"]:
        return None
    moment = now or datetime.now(timezone.utc)
    until = paused_until(conn)
    if settings["model"] == "hosted" and until is not None and until > moment:
        return None
    ensure_pipeline_jobs_schema(conn)
    signature = signature_for(_chat_for(settings["model"]), settings["model"])
    # The in-flight check and the enqueue under one hold of the (reentrant) gate: two triggers at once queue one job.
    with with_db_write():
        if conn.execute("SELECT 1 FROM pipeline_jobs WHERE kind=? AND status IN ('queued','running') LIMIT 1",
                        (KIND,)).fetchone():
            return None
        ensure_tables(conn)
        records = owed(conn, settings["sources"], BATCH_SIZE, now=moment)
        records += recheck_due(conn, settings["sources"], signature, BATCH_SIZE - len(records), now=moment)
        if not records:
            return None
        return enqueue_job(conn, kind=KIND, payload={"records": records})


def after_rows_stored(source_def: Any) -> Optional[str]:
    """The post-canonical hook (runs in a worker thread, on that thread's own connection). Cheap when the lane is off:
    one ``engine_config`` read."""
    truth = truth_source_for_definition(source_def)
    if truth is None:
        return None
    from ..config.truth_inbound import load
    from ..core.state import get_db_connection

    conn = get_db_connection()
    if conn is None:
        return None
    settings = load(conn)
    if not settings["enabled"] or truth not in settings["sources"]:
        return None
    return enqueue_truth_checks(conn)


# --- pacing ---------------------------------------------------------------------------------------------------------
class Pacer:
    """Spaces model checks at least ``60 / rate_per_minute`` seconds apart, across jobs, in this process."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], Any] = asyncio.sleep):
        self._clock, self._sleep = clock, sleep
        self._last: Optional[float] = None

    async def wait(self, rate_per_minute: int) -> None:
        interval = 60.0 / max(1, int(rate_per_minute))
        if self._last is not None:
            delay = self._last + interval - self._clock()
            if delay > 0:
                await self._sleep(delay)
        self._last = self._clock()


PACER = Pacer()


# --- one check ------------------------------------------------------------------------------------------------------
def _valid_records(value: Any) -> Optional[List[Tuple[str, str]]]:
    from ..query.truth_checks import CHECKED_TABLES

    if not isinstance(value, list) or not 0 < len(value) <= MAX_BATCH_SIZE:
        return None
    out = []
    for item in value:
        if (not isinstance(item, (list, tuple)) or len(item) != 2 or item[0] not in CHECKED_TABLES
                or not isinstance(item[1], str) or not item[1]):
            return None
        out.append((str(item[0]), item[1]))
    return out


def _prepare(conn: Any, table: str, record_id: str, sources: List[str], signature: Any):
    """The record, its ClaimRequest and which check of the record this is (1 for a new row), or None when it is
    gone, no longer a chosen source, or already checked and not due a re-check under ``signature``."""
    from ..query.truth_checks import (STATEMENT_CHARS, check_state, load_record, previous_turns, recheck_reason,
                                      speaker_for_reader, with_offset)
    from ..query.verify_v2 import ClaimRequest

    state = check_state(conn, table, record_id)
    if state is not None and not recheck_reason(state, signature, cooldown_s=RECHECK_COOLDOWN_S):
        return None
    names: Dict[str, Optional[str]] = {}
    record = load_record(conn, table, record_id, names)
    if record is None or record["source"] not in sources or not record["text"].strip():
        return None
    if OWNER_CLAIMS_ONLY and not record["owner"]:
        return None
    request = ClaimRequest(
        statement=record["text"][:STATEMENT_CHARS],
        context=previous_turns(conn, record, CONTEXT_TURNS, names),
        said_at=with_offset(record["at"]),
        speaker=speaker_for_reader(record),
        about_others=not record["owner"],
        effort=EFFORT,
        deadline_s=DEADLINE_S,
        read_timeout_s=signature[2],
        source_kind=SOURCE_KINDS.get(record["source"], "message"),
        speaker_is_owner=bool(record["owner"]),
        source_ref=f"{table}:{record_id}",
        conversation_id=record.get("conversation_id"),
    )
    record.pop("row", None)
    return record, request, (state["attempts"] + 1 if state else 1)


def _chat_for(model: str):
    """The chat for the lane's side, on the model the owner picked for verify_claim (read from this node: call it off
    the event loop)."""
    from ..config.verify_claim_model import device_pick
    from ..query.verify_v2_models import chat_for_lane

    return chat_for_lane(model, _db(device_pick))


def _model_label(chat: Any) -> str:
    return f"{getattr(chat, 'name', 'custom')}:{getattr(chat, 'model', '')}"[:120]


def pause_cause(result: Dict[str, Any], hosted: bool) -> Optional[str]:
    reason = str(result.get("reason") or "")
    if not reason.startswith("model_unavailable:"):
        return None
    cause = reason.split(":", 1)[1]
    return cause if cause in (HOSTED_PAUSE_CAUSES if hosted else LOCAL_PAUSE_CAUSES) else None


def check_row(record: Dict[str, Any], result: Dict[str, Any], *, model: Optional[str],
              read_timeout_s: Optional[float] = None) -> Dict[str, Any]:
    """The ``truth_checks`` row for one verify_claim v2 answer: verdict, the reader's main claim, cited refs only,
    stamped with the engine build (the answer's ``version``, else ``VERSION``: a row the engine's gate refused got no
    answer, but the gate is the engine's own; and the sha16 of ``verify_v2.py``)."""
    from ..query.truth_checks import VERDICTS
    from ..query.verify_v2 import VERSION, engine_sha16

    verdict, reason = result.get("verdict"), result.get("reason")
    if verdict == "unable" and str(reason or "").startswith("not_checkable:"):
        verdict = "not_checkable"
    if verdict not in VERDICTS:
        verdict = "unable"
    claim = result.get("claim") if isinstance(result.get("claim"), dict) else {}
    cited = []
    for item in result.get("evidence") or []:
        if not isinstance(item, dict) or not item.get("cited"):
            continue
        table, _, record_id = str(item.get("ref") or "").partition(":")
        if table and record_id:
            cited.append({"table": table, "record_id": record_id, "role": item.get("role"), "date": item.get("date")})
    latency = (result.get("timings_ms") or {}).get("total")
    return {
        "record_table": record["table"], "record_id": record["record_id"], "conversation_id": record.get("conversation_id"),
        "source": record["source"], "speaker_kind": "owner" if record["owner"] else "contact",
        "category": result.get("category"), "claim_text": (str(claim.get("text") or "") or None) if claim else None,
        "verdict": verdict, "reason": str(reason)[:120] if reason else None, "cited_refs": cited, "effort": EFFORT,
        "model": model, "latency_ms": latency, "read_timeout_s": read_timeout_s,
        "engine_version": str(result.get("version") or VERSION)[:80], "engine_sha16": engine_sha16() or None,
    }


def _store(conn: Any, row: Dict[str, Any], attempt: int) -> Optional[str]:
    """A first check is a new row; a later one replaces the ``unable`` row's result in place."""
    from ..query.truth_checks import record_check, recheck

    return record_check(conn, row) if attempt <= 1 else recheck(conn, row, attempt)


async def _verify(request: Any, chat: Any) -> Dict[str, Any]:
    from ..core.handlers.common import run_db_read
    from ..query.verify_v2 import VERSION, verify_owner_claim

    started = time.perf_counter()
    try:
        return await asyncio.wait_for(
            verify_owner_claim(request, chat=chat, gather_in_worker=run_db_read, to_thread=asyncio.to_thread),
            timeout=request.deadline_s + 2.0,
        )
    except asyncio.TimeoutError:
        return {"version": VERSION, "verdict": "unable", "reason": "deadline",
                "timings_ms": {"total": round((time.perf_counter() - started) * 1000, 1)}}


def _db(fn: Callable[..., Any], *args: Any) -> Any:
    from ..core.state import get_db_connection

    return fn(get_db_connection(), *args)


def _pause(conn: Any, cause: str) -> None:
    from ..config.truth_inbound import set_paused_until

    set_paused_until(conn, datetime.now(timezone.utc) + timedelta(seconds=PAUSE_S))
    logger.info("truth lane paused for %ss: %s", PAUSE_S, cause)


def _probe_hosted() -> bool:
    """Before a paused hosted lane resumes: a key is configured and the wallet allows a call (a fresh probe)."""
    from ..engine.hosted_llm_wallet import hosted_llm_wallet_allows
    from ..query.verify_v2_models import HostedChat

    try:
        return HostedChat().available and bool(hosted_llm_wallet_allows(force=True))
    except Exception:  # noqa: BLE001 — a probe that cannot run does not resume the lane
        return False


async def execute_truth_check(payload: Dict[str, Any], *, pacer: Optional[Pacer] = None) -> Dict[str, Any]:
    """One batch. The result is counts only (it is stored as the job's detail)."""
    from ..config.truth_inbound import load, paused_until, set_paused_until
    from ..query.verify_v2 import gate

    records = _valid_records(payload.get("records"))
    if records is None:
        return {"status": "error", "error": "Invalid truth check batch"}
    settings = await asyncio.to_thread(_db, load)
    if not settings["enabled"]:
        return {"status": "ok", "skipped": "truth lane is off"}
    hosted = settings["model"] == "hosted"
    if hosted:
        until = await asyncio.to_thread(_db, paused_until)
        if until is not None:
            if until > datetime.now(timezone.utc):
                return {"status": "ok", "skipped": "paused"}
            if not await asyncio.to_thread(_probe_hosted):
                await asyncio.to_thread(_db, _pause, "probe_failed")
                return {"status": "ok", "skipped": "paused"}
            await asyncio.to_thread(_db, set_paused_until, None)
    chat = await asyncio.to_thread(_chat_for, settings["model"])
    pace = pacer or PACER
    signature = signature_for(chat, settings["model"])
    started = time.monotonic()
    counts = {"checked": 0, "not_checkable_by_gate": 0, "skipped": 0, "deferred": 0, "paused": False, "rechecked": 0}
    warming = not hosted and warm_local_on()
    if warming:
        counts.update({"warmed": 0, "warm_failed": 0, "warm_s": 0.0})
    for index, (table, record_id) in enumerate(records):
        if time.monotonic() - started >= JOB_BUDGET_S:
            counts["deferred"] = len(records) - index
            break
        prepared = await asyncio.to_thread(_db, _prepare, table, record_id, settings["sources"], signature)
        if prepared is None:
            counts["skipped"] += 1
            continue
        record, request, attempt = prepared
        refused = gate(request.statement)
        if refused:
            # The engine's own gate, asked first: a refused row is stored without a model call or a pacing slot.
            row = check_row(record, {"verdict": "unable", "reason": f"not_checkable:{refused}",
                                     "timings_ms": {"total": 0.0}}, model=None)
            await asyncio.to_thread(_db, _store, row, attempt)
            counts["not_checkable_by_gate"] += 1
            counts["rechecked"] += int(attempt > 1)
            continue
        await pace.wait(settings["rate_per_minute"])
        if warming:
            ready, spent = await asyncio.to_thread(ensure_warm, chat)
            if spent:
                counts["warmed"] += int(ready)
                counts["warm_s"] = round(counts["warm_s"] + spent, 1)
            if not ready:
                counts["warm_failed"] += 1
                counts["deferred"] = len(records) - index
                break
        result = await _verify(request, chat)
        cause = pause_cause(result, hosted)
        if cause:
            await asyncio.to_thread(_db, _pause, cause)
            counts["paused"] = True
            counts["deferred"] = len(records) - index
            break
        row = check_row(record, result, model=_model_label(chat), read_timeout_s=request.read_timeout_s)
        await asyncio.to_thread(_db, _store, row, attempt)
        counts["checked"] += 1
        counts["rechecked"] += int(attempt > 1)
    return {"status": "ok", **counts}
