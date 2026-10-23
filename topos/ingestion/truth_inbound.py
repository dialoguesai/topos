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
history does not queue years of checks. A row is checked once.

Each check. ``statement`` = the row's text (first 1000 characters); ``context`` = the six rows before it in the same
conversation; ``said_at`` = the row's time with the node's offset; ``speaker`` = ``"owner"`` for the owner's own row,
else the contact's display name (never a raw identifier: "a contact" when the contacts do not name them);
``about_others`` = the speaker is not the owner; ``effort`` = ``low``; ``deadline_s`` = 30. A row the engine's gate
refuses (a fragment, a question, empty) costs no model call and is stored as ``not_checkable``. Model checks start at
most ``rate_per_minute`` times a minute (:data:`PACER`), and a job stops after :data:`JOB_BUDGET_S` seconds so the
embeddings catch-up never waits long behind it; what it did not reach is still owed and goes in the next batch.

The model. ``local`` is ``LocalChat`` (the node's pinned local model). ``hosted`` is ``HostedChat`` (the Red Pill key,
behind the hosted wallet check). A hosted 402, an exhausted wallet or a missing key pauses the lane for ten minutes
(``truth_inbound_paused_until``) without storing the row; after the pause a probe (key present, wallet allows) must
pass before the next check, else the pause is renewed. A local model that cannot be reached pauses it the same way, so
a stopped Ollama never marks a week of rows ``unable``.
"""
from __future__ import annotations

import asyncio
import logging
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
CONTEXT_TURNS = 6
PAUSE_S = 600
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


def owed(conn: Any, sources: List[str], limit: int, *, now: Optional[datetime] = None) -> List[List[str]]:
    """``[table, record_id]`` pairs still to check for the chosen sources, newest first."""
    from ..query.truth_checks import CHECKS_TABLE, CONVERSATION_SOURCE_IDS, _present

    cutoff = _cutoff(now or datetime.now(timezone.utc))
    checked = _present(conn, CHECKS_TABLE)
    found: List[Tuple[str, str, str]] = []

    def collect(table: str, id_column: str, time_column: str, extra: str, params: tuple) -> None:
        if not _present(conn, table):
            return
        unchecked = (f" AND NOT EXISTS (SELECT 1 FROM {CHECKS_TABLE} t WHERE t.record_table='{table}' "
                     f"AND t.record_id=m.{id_column})") if checked else ""
        rows = conn.execute(
            f"SELECT m.{id_column}, m.{time_column} FROM {table} m WHERE m.{time_column} >= ? "
            f"AND m.content IS NOT NULL AND TRIM(m.content) != ''{extra}{unchecked} "
            f"ORDER BY m.{time_column} DESC LIMIT ?", (cutoff, *params, int(limit))).fetchall()
        found.extend((str(r[1] or ""), table, str(r[0])) for r in rows if r[0] is not None)

    source_ids = [sid for name in sources for sid in CONVERSATION_SOURCE_IDS.get(name, ())]
    if source_ids:
        collect("conversation_messages", "message_id", "event_at",
                f" AND m.source_id IN ({','.join('?' for _ in source_ids)})", tuple(source_ids))
    if "journal" in sources:
        collect("journal_entries", "entry_id", "entry_at", "", ())
    if "transcripts" in sources:
        collect("transcript_segments", "segment_id", "event_at", " AND COALESCE(m.actor_role, 'ambient') != 'ambient'", ())
    found.sort(key=lambda item: item[0], reverse=True)
    return [[table, record_id] for _, table, record_id in found[:limit]]


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
    # The in-flight check and the enqueue under one hold of the (reentrant) gate: two triggers at once queue one job.
    with with_db_write():
        if conn.execute("SELECT 1 FROM pipeline_jobs WHERE kind=? AND status IN ('queued','running') LIMIT 1",
                        (KIND,)).fetchone():
            return None
        ensure_tables(conn)
        records = owed(conn, settings["sources"], BATCH_SIZE, now=moment)
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


def _prepare(conn: Any, table: str, record_id: str, sources: List[str]):
    """The record and its ClaimRequest, or None when it is gone, already checked, or no longer a chosen source."""
    from ..query.truth_checks import (STATEMENT_CHARS, already_checked, load_record, previous_turns,
                                      speaker_for_reader, with_offset)
    from ..query.verify_v2 import ClaimRequest

    if already_checked(conn, table, record_id):
        return None
    names: Dict[str, Optional[str]] = {}
    record = load_record(conn, table, record_id, names)
    if record is None or record["source"] not in sources or not record["text"].strip():
        return None
    request = ClaimRequest(
        statement=record["text"][:STATEMENT_CHARS],
        context=previous_turns(conn, record, CONTEXT_TURNS, names),
        said_at=with_offset(record["at"]),
        speaker=speaker_for_reader(record),
        about_others=not record["owner"],
        effort=EFFORT,
        deadline_s=DEADLINE_S,
    )
    record.pop("row", None)
    return record, request


def _chat_for(model: str):
    from ..query.verify_v2_models import HostedChat, LocalChat

    return HostedChat() if model == "hosted" else LocalChat()


def _model_label(chat: Any) -> str:
    return f"{getattr(chat, 'name', 'custom')}:{getattr(chat, 'model', '')}"[:120]


def pause_cause(result: Dict[str, Any], hosted: bool) -> Optional[str]:
    reason = str(result.get("reason") or "")
    if not reason.startswith("model_unavailable:"):
        return None
    cause = reason.split(":", 1)[1]
    return cause if cause in (HOSTED_PAUSE_CAUSES if hosted else LOCAL_PAUSE_CAUSES) else None


def check_row(record: Dict[str, Any], result: Dict[str, Any], *, model: Optional[str]) -> Dict[str, Any]:
    """The ``truth_checks`` row for one verify_claim v2 answer: verdict, the reader's main claim, cited refs only."""
    from ..query.truth_checks import VERDICTS

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
        "model": model, "latency_ms": latency,
    }


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
    from ..query.truth_checks import record_check
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
    chat = _chat_for(settings["model"])
    pace = pacer or PACER
    started = time.monotonic()
    counts = {"checked": 0, "not_checkable_by_gate": 0, "skipped": 0, "deferred": 0, "paused": False}
    for index, (table, record_id) in enumerate(records):
        if time.monotonic() - started >= JOB_BUDGET_S:
            counts["deferred"] = len(records) - index
            break
        prepared = await asyncio.to_thread(_db, _prepare, table, record_id, settings["sources"])
        if prepared is None:
            counts["skipped"] += 1
            continue
        record, request = prepared
        refused = gate(request.statement)
        if refused:
            # The engine's own gate, asked first: a refused row is stored without a model call or a pacing slot.
            row = check_row(record, {"verdict": "unable", "reason": f"not_checkable:{refused}",
                                     "timings_ms": {"total": 0.0}}, model=None)
            await asyncio.to_thread(_db, record_check, row)
            counts["not_checkable_by_gate"] += 1
            continue
        await pace.wait(settings["rate_per_minute"])
        result = await _verify(request, chat)
        cause = pause_cause(result, hosted)
        if cause:
            await asyncio.to_thread(_db, _pause, cause)
            counts["paused"] = True
            counts["deferred"] = len(records) - index
            break
        await asyncio.to_thread(_db, record_check, check_row(record, result, model=_model_label(chat)))
        counts["checked"] += 1
    return {"status": "ok", **counts}
