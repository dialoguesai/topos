"""LongMemEval → Topos node benchmark adapter (plan D3.1). ADAPTER_VERSION = "lme-adapter-1".

Builds one throwaway bench DB per LongMemEval question and ingests its haystack
sessions ONE SESSION PER CALL, sequentially, in chronological order, through the
REAL ingest path (``ingest_file_payload`` → parser → raw retention → canonicalize →
``run_post_canonical_pipeline`` with the source's default enrichment jobs). Batch
seeding / direct INSERTs are deliberately not used here — this adapter is a
measurement instrument and the ingest path is part of what it measures.

Key invariants (enforced by the F1–F5 fidelity assertions):
- Event time comes from the DATASET (session date + turn-index seconds), emitted as
  uniform ISO-8601 UTC strings; wall clock appears only in provenance/meta fields.
- Roles map user→human / assistant→assistant with per-session counts preserved.
- Within-conversation chronology (ORDER BY event_at, sequence) equals turn order.
- Row counts and first/last turn contents are byte-identical to the dataset.
- Gold (answer_session_ids / has_answer turns) maps onto ingested rows.

Global-connection coupling: enrichment writes, vector search, topic clusters and the
cross-db vector guard (`_bundle_is_global_db`) all flow through
``topos.core.state.get_db_connection()``. The sanctioned injection (verified in
core/state.py) is: set ``settings.topos_database_path`` to the bench DB and reset the
module-level ``state.db_conn`` / ``state._db_conn_path`` cache so ``get_db_connection``
re-opens against the bench file; restore both afterwards. ``use_bench_db`` wraps this.

Engine imports are deliberately lazy (inside functions) so the pure helpers
(date parsing, JSONL construction, gold mapping) are unit-testable without models,
a DB, or the engine settings environment.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

ADAPTER_VERSION = "lme-adapter-1"

# Frozen instrument choices (see D31_CONTRACT.md).
SOURCE_ID = "chatgpt_file_ingestion"  # already in scope manifest default_source_ids
SCHEMA_ID = "chatgpt.conversation.v1"  # JSONL flat records {id, thread_id, role, content, created_at}
SCOPE_ID = "ai_conversations:read"
FILE_FORMAT = "jsonl"

# Dataset date format: "2023/05/20 (Sat) 02:21", treated as UTC.
LME_DATE_FORMAT = "%Y/%m/%d (%a) %H:%M"
_DOW_TOKEN = re.compile(r"\s*\([A-Za-z]{3}\)\s*")

# F1 guard: the dataset is 2023-era; any event_at this close to wall-clock now means
# a timestamp fallback injected ingestion time (the Mem0 reproducibility failure).
WALL_CLOCK_GUARD_DAYS = 30

_VALID_ROLES = ("user", "assistant")


# ---------------------------------------------------------------------------
# Pure helpers — no engine imports; unit-tested directly.
# ---------------------------------------------------------------------------


def parse_lme_date(raw: str) -> datetime:
    """Parse a LongMemEval date ("2023/05/20 (Sat) 02:21") as UTC.

    Tries the exact contract format first; falls back to stripping the
    parenthesised day-of-week token so parsing is locale-independent (%a matching
    depends on LC_TIME). Raises ValueError on anything else — loud failures.
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError("empty LongMemEval date")
    try:
        naive = datetime.strptime(text, LME_DATE_FORMAT)
    except ValueError:
        stripped = _DOW_TOKEN.sub(" ", text).strip()
        naive = datetime.strptime(stripped, "%Y/%m/%d %H:%M")
    return naive.replace(tzinfo=timezone.utc)


def iso_utc(dt: datetime) -> str:
    """Uniform ISO-8601 UTC string ("2023-05-20T02:21:00+00:00").

    event_at is TEXT in SQLite; one canonical shape keeps lexicographic order equal
    to chronological order.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


@dataclass(frozen=True)
class LMEQuestion:
    """One LongMemEval instance (fields mirror the dataset)."""

    question_id: str
    question_type: str
    question: str
    question_date: str
    answer: str
    answer_session_ids: Tuple[str, ...]
    haystack_dates: Tuple[str, ...]
    haystack_session_ids: Tuple[str, ...]
    haystack_sessions: Tuple[Tuple[Dict[str, Any], ...], ...]

    @property
    def is_abstention(self) -> bool:
        return self.question_id.endswith("_abs")

    @property
    def gold(self) -> Tuple[Tuple[str, int], ...]:
        """(session_id, turn_index) pairs for every has_answer turn (dataset space)."""
        pairs: List[Tuple[str, int]] = []
        for sid, session in zip(self.haystack_session_ids, self.haystack_sessions):
            for turn_idx, turn in enumerate(session):
                if turn.get("has_answer"):
                    pairs.append((sid, turn_idx))
        return tuple(pairs)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "LMEQuestion":
        required = (
            "question_id",
            "question_type",
            "question",
            "question_date",
            "answer",
            "answer_session_ids",
            "haystack_dates",
            "haystack_session_ids",
            "haystack_sessions",
        )
        missing = [key for key in required if key not in data]
        if missing:
            raise ValueError(f"LongMemEval instance missing fields: {missing}")

        sessions = tuple(
            tuple(dict(turn) for turn in session) for session in data["haystack_sessions"]
        )
        question = cls(
            question_id=str(data["question_id"]),
            question_type=str(data["question_type"]),
            question=str(data["question"]),
            question_date=str(data["question_date"]),
            # A handful of answers are ints in the source JSON; the judge prompt
            # takes a string either way.
            answer=str(data["answer"]),
            answer_session_ids=tuple(str(s) for s in data["answer_session_ids"]),
            haystack_dates=tuple(str(d) for d in data["haystack_dates"]),
            haystack_session_ids=tuple(str(s) for s in data["haystack_session_ids"]),
            haystack_sessions=sessions,
        )
        n = len(question.haystack_sessions)
        if len(question.haystack_dates) != n or len(question.haystack_session_ids) != n:
            raise ValueError(
                f"{question.question_id}: misaligned haystack lists "
                f"(sessions={n}, dates={len(question.haystack_dates)}, "
                f"ids={len(question.haystack_session_ids)})"
            )
        stray = set(question.answer_session_ids) - set(question.haystack_session_ids)
        if stray:
            raise ValueError(
                f"{question.question_id}: answer_session_ids not in haystack: {sorted(stray)}"
            )
        return question


def load_questions(path: "str | Path") -> List[LMEQuestion]:
    """Load a LongMemEval split (JSON list of instances) into LMEQuestion objects."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path}: expected a JSON list of question instances")
    return [LMEQuestion.from_mapping(item) for item in data]


@dataclass(frozen=True)
class SessionPlan:
    """One haystack session, keyed and ordered for ingest.

    ``session_key`` is the ingest thread_id: equal to the dataset session_id except
    for duplicate occurrences within one haystack (13 questions in the S split repeat
    a session id verbatim at a second date), which get an ``__dupN`` suffix so they
    ingest as distinct conversations with distinct message ids. Duplicates are never
    gold, so gold mapping always resolves to the base (first-occurrence) key.
    """

    haystack_index: int
    session_id: str
    session_key: str
    date_raw: str
    date: datetime
    turns: Tuple[Dict[str, Any], ...]


def plan_sessions(question: LMEQuestion) -> Tuple[List[SessionPlan], bool]:
    """Chronologically ordered ingest plan; returns (plans, sort_was_needed).

    Ordering is by parsed haystack date with the original haystack index as a
    deterministic tiebreak (stable). ``__dupN`` keys are assigned in haystack
    occurrence order BEFORE sorting, so keys are stable regardless of dates.
    """
    seen: Dict[str, int] = {}
    plans: List[SessionPlan] = []
    for idx, (sid, date_raw, turns) in enumerate(
        zip(question.haystack_session_ids, question.haystack_dates, question.haystack_sessions)
    ):
        occurrence = seen.get(sid, 0)
        seen[sid] = occurrence + 1
        session_key = sid if occurrence == 0 else f"{sid}__dup{occurrence}"
        plans.append(
            SessionPlan(
                haystack_index=idx,
                session_id=sid,
                session_key=session_key,
                date_raw=date_raw,
                date=parse_lme_date(date_raw),
                turns=tuple(turns),
            )
        )
    ordered = sorted(plans, key=lambda p: (p.date, p.haystack_index))
    sort_needed = any(o.haystack_index != i for i, o in enumerate(ordered))
    return ordered, sort_needed


def dataset_id_for(question_id: str) -> str:
    """One RawFileStore dataset per question (frozen shape: "lme:{question_id}")."""
    return f"lme:{question_id}"


def build_message_id(question_id: str, session_key: str, turn_idx: int) -> str:
    return f"lme:{question_id}:{session_key}:{turn_idx:03d}"


def build_session_records(question_id: str, plan: SessionPlan) -> List[Dict[str, Any]]:
    """chatgpt.conversation.v1 records for one session.

    created_at = session date + turn-index SECONDS (uniform ISO-8601 UTC). Never
    omitted: build_staging_record falls back to wall-clock now() on a missing ts,
    which is exactly the corruption F1 exists to catch.
    """
    records: List[Dict[str, Any]] = []
    for turn_idx, turn in enumerate(plan.turns):
        role = str(turn.get("role") or "")
        if role not in _VALID_ROLES:
            raise ValueError(
                f"{question_id}/{plan.session_key} turn {turn_idx}: "
                f"unexpected role {role!r} (want one of {_VALID_ROLES})"
            )
        records.append(
            {
                "id": build_message_id(question_id, plan.session_key, turn_idx),
                "thread_id": plan.session_key,
                "role": role,
                "content": str(turn.get("content") or ""),
                "created_at": iso_utc(plan.date + timedelta(seconds=turn_idx)),
            }
        )
    return records


def build_session_jsonl(question_id: str, plan: SessionPlan) -> bytes:
    lines = [
        json.dumps(record, ensure_ascii=False)
        for record in build_session_records(question_id, plan)
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def gold_plan_pairs(question: LMEQuestion, plans: Sequence[SessionPlan]) -> List[Tuple[str, int]]:
    """(session_key, turn_index) for every has_answer turn, in ingest order."""
    pairs: List[Tuple[str, int]] = []
    for plan in plans:
        for turn_idx, turn in enumerate(plan.turns):
            if turn.get("has_answer"):
                pairs.append((plan.session_key, turn_idx))
    return pairs


def expected_sender_type(role: str) -> str:
    """Mirror of ChatGPTParser role mapping (user→human, else assistant)."""
    return "human" if role == "user" else "assistant"


# ---------------------------------------------------------------------------
# Bench DB build + fidelity (engine imports lazy from here on).
# ---------------------------------------------------------------------------


@dataclass
class FidelityReport:
    """Outcome of the F1–F5 ingestion-fidelity assertions."""

    passed: bool
    checks: Dict[str, bool]
    violations: List[str]
    gold_message_rows: int
    total_messages: int
    total_conversations: int
    wall_clock_guard_days: int = WALL_CLOCK_GUARD_DAYS


@dataclass
class BenchDB:
    """Handle to one built bench database (one LongMemEval question)."""

    db_path: Path
    question: LMEQuestion
    conversation_id_by_session: Dict[str, str]  # session_key -> conversation_id (read back)
    gold_conversation_ids: List[str]
    gold_message_ids: List[str]
    counts: Dict[str, int]
    effective_jobs: Dict[str, Any]
    fidelity: FidelityReport
    meta: Dict[str, Any] = field(default_factory=dict)


@contextlib.contextmanager
def use_bench_db(db_path: "str | Path") -> Iterator[sqlite3.Connection]:
    """Point the GLOBAL engine connection at ``db_path``; restore on exit.

    Verified against topos/core/state.py: ``get_db_connection()`` re-resolves from
    ``settings.topos_database_path`` and caches (``db_conn``/``_db_conn_path``), so the
    sanctioned injection is settings override + cache reset (the same seam tests and
    TOPOS_DATABASE_PATH use). Yields the global connection (check_same_thread=False,
    tuned, migrated) for read-back and fidelity queries.
    """
    from topos.config.settings import settings
    from topos.core import state

    resolved = Path(db_path).resolve()
    prev_setting = settings.topos_database_path
    prev_conn = state.db_conn
    prev_conn_path = state._db_conn_path

    settings.topos_database_path = str(resolved)
    state.db_conn = None
    state._db_conn_path = None
    try:
        conn = state.get_db_connection()
        if conn is None:
            raise RuntimeError(f"could not open bench DB at {resolved}")
        row = conn.execute("PRAGMA database_list").fetchone()
        main_path = str(row[2] or "") if row is not None else ""
        if Path(main_path).resolve() != resolved:
            raise RuntimeError(
                f"global connection points at {main_path!r}, expected bench DB {resolved}"
            )
        yield conn
    finally:
        bench_conn = state.db_conn
        settings.topos_database_path = prev_setting
        state.db_conn = prev_conn
        state._db_conn_path = prev_conn_path
        if bench_conn is not None and bench_conn is not prev_conn:
            with contextlib.suppress(Exception):
                bench_conn.close()


def _apply_job_overrides(
    conn: sqlite3.Connection,
    source_def: Any,
    job_names: Optional[List[str]],
    disable_jobs: Optional[List[str]] = None,
) -> Dict[str, List[str]]:
    """Translate a desired canonical-lane job list into per-source overrides.

    ``ingest_file_payload`` has no job_names parameter: the manager resolves the
    canonical lane via ``effective_canonical_enrichment_jobs`` (definition defaults +
    engine_config ``source_enrichment_overrides`` read from the GLOBAL connection —
    the bench DB here). So overrides written to the bench DB are the production-parity
    way to alter the job list per experiment. Unknown/untogglable jobs raise.

    ``disable_jobs`` detaches a job in ALL its togglable lanes (canonical AND signal)
    via the same user attach/detach mechanism — needed because ``job_names`` only
    reshapes the canonical lane, while signal-lane jobs (e.g. the Ollama-backed
    dimension_summary / goal_extraction) resolve from CANONICAL_BASELINE_SIGNAL_JOBS
    + registry extras. Effective lists are read back after overrides, so anything
    disabled here is visible in BenchDB.effective_jobs.
    """
    result: Dict[str, List[str]] = {"enabled": [], "disabled": []}
    if job_names is None and not disable_jobs:
        return result
    from topos.enrichment.source_overrides import set_source_enrichment_override

    if job_names is not None:
        desired = list(dict.fromkeys(job_names))
        static = list(getattr(source_def, "canonical_enrichment_jobs", []) or [])
        result["enabled"] = [job for job in desired if job not in static]
        result["disabled"] = [job for job in static if job not in desired]
        for job in result["enabled"]:
            set_source_enrichment_override(SOURCE_ID, job, True, conn=conn)
        for job in result["disabled"]:
            set_source_enrichment_override(SOURCE_ID, job, False, conn=conn)
    for job in dict.fromkeys(disable_jobs or []):
        if job in result["disabled"]:
            continue
        set_source_enrichment_override(SOURCE_ID, job, False, conn=conn)
        result["disabled"].append(job)
    return result


def _read_back_conversations(
    conn: sqlite3.Connection, question: LMEQuestion, plans: Sequence[SessionPlan]
) -> Dict[str, str]:
    """session_key -> conversation_id as actually minted by the canonicalizer.

    (The chatgpt mapper mints f"chatgpt:{thread_id}"; we read it back from
    ai_chat_messages rather than assume.)
    """
    mapping: Dict[str, str] = {}
    for plan in plans:
        first_id = build_message_id(question.question_id, plan.session_key, 0)
        row = conn.execute(
            "SELECT conversation_id FROM ai_chat_messages WHERE message_id = ?",
            (first_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"{question.question_id}: session {plan.session_key} has no ingested "
                f"first message ({first_id}) — ingest silently dropped a session"
            )
        mapping[plan.session_key] = str(row[0])
    return mapping


def run_fidelity_checks(
    conn: sqlite3.Connection,
    question: LMEQuestion,
    plans: Sequence[SessionPlan],
    conversation_id_by_session: Dict[str, str],
    *,
    soft: bool = False,
) -> FidelityReport:
    """F1–F5 ingestion-fidelity assertions (plan D3.1).

    Raises AssertionError with specifics unless ``soft=True``, in which case all
    violations are collected into the returned report.
    """
    violations: List[str] = []
    qid = question.question_id

    expected: Dict[str, Dict[str, Any]] = {}
    turn_ids_by_conv: Dict[str, List[str]] = {}
    role_counts_by_conv: Dict[str, Dict[str, int]] = {}
    for plan in plans:
        conv = conversation_id_by_session[plan.session_key]
        ids: List[str] = []
        counts = {"human": 0, "assistant": 0}
        for turn_idx, turn in enumerate(plan.turns):
            mid = build_message_id(qid, plan.session_key, turn_idx)
            sender = expected_sender_type(str(turn.get("role") or ""))
            counts[sender] += 1
            expected[mid] = {
                "conversation_id": conv,
                "sender_type": sender,
                "event_at": iso_utc(plan.date + timedelta(seconds=turn_idx)),
                "content": str(turn.get("content") or ""),
                "turn_idx": turn_idx,
            }
            ids.append(mid)
        turn_ids_by_conv[conv] = ids
        role_counts_by_conv[conv] = counts

    rows = conn.execute(
        "SELECT message_id, conversation_id, sender_type, event_at, content, sequence "
        "FROM ai_chat_messages"
    ).fetchall()
    by_id: Dict[str, sqlite3.Row] = {str(r["message_id"]): r for r in rows}

    # F1 — timestamps un-normalized: exact round-trip + wall-clock guard.
    f1_ok = True
    now = datetime.now(timezone.utc)
    for mid, exp in expected.items():
        row = by_id.get(mid)
        if row is None:
            continue  # counted under F4
        if str(row["event_at"]) != exp["event_at"]:
            f1_ok = False
            violations.append(
                f"F1 {mid}: event_at {row['event_at']!r} != dataset {exp['event_at']!r}"
            )
            continue
        event_dt = datetime.fromisoformat(str(row["event_at"]))
        if event_dt.tzinfo is None:
            event_dt = event_dt.replace(tzinfo=timezone.utc)
        if abs(now - event_dt) <= timedelta(days=WALL_CLOCK_GUARD_DAYS):
            f1_ok = False
            violations.append(
                f"F1 {mid}: event_at {row['event_at']} within {WALL_CLOCK_GUARD_DAYS}d "
                "of wall-clock now — wall-clock injection"
            )

    # F2 — speaker/role preserved: per-session human/assistant counts.
    f2_ok = True
    for conv, exp_counts in role_counts_by_conv.items():
        got = {"human": 0, "assistant": 0}
        for r in conn.execute(
            "SELECT sender_type, COUNT(*) FROM ai_chat_messages "
            "WHERE conversation_id = ? GROUP BY sender_type",
            (conv,),
        ).fetchall():
            got[str(r[0])] = int(r[1])
        if got != exp_counts:
            f2_ok = False
            violations.append(f"F2 {conv}: sender counts {got} != dataset {exp_counts}")

    # F3 — chronology monotonic: ORDER BY event_at, sequence == turn order,
    # and sequence values are 0..n-1 in that order.
    f3_ok = True
    for conv, ids in turn_ids_by_conv.items():
        ordered = conn.execute(
            "SELECT message_id, sequence FROM ai_chat_messages "
            "WHERE conversation_id = ? ORDER BY event_at ASC, sequence ASC",
            (conv,),
        ).fetchall()
        got_ids = [str(r[0]) for r in ordered]
        got_seq = [int(r[1]) for r in ordered]
        if got_ids != ids:
            f3_ok = False
            violations.append(
                f"F3 {conv}: event_at ordering != turn order "
                f"(first divergence at {_first_divergence(ids, got_ids)})"
            )
        if got_seq != list(range(len(ordered))):
            f3_ok = False
            violations.append(f"F3 {conv}: sequence values not 0..n-1: {got_seq[:10]}...")

    # F4 — completeness: rowcount, conversation count, first+last content bytes.
    f4_ok = True
    total_turns = sum(len(plan.turns) for plan in plans)
    if len(rows) != total_turns:
        f4_ok = False
        violations.append(f"F4: {len(rows)} message rows != {total_turns} dataset turns")
    missing = [mid for mid in expected if mid not in by_id]
    if missing:
        f4_ok = False
        violations.append(f"F4: {len(missing)} expected messages missing (first: {missing[:3]})")
    conv_count = int(
        conn.execute("SELECT COUNT(*) FROM ai_chat_conversations").fetchone()[0]
    )
    if conv_count != len(plans):
        f4_ok = False
        violations.append(f"F4: {conv_count} conversations != {len(plans)} sessions")
    for plan in plans:
        for turn_idx in {0, len(plan.turns) - 1}:
            mid = build_message_id(qid, plan.session_key, turn_idx)
            row = by_id.get(mid)
            if row is None:
                continue  # already reported
            if str(row["content"]) != expected[mid]["content"]:
                f4_ok = False
                violations.append(f"F4 {mid}: content differs from dataset (byte compare)")

    # F5 — gold integrity.
    f5_ok = True
    stray = set(question.answer_session_ids) - set(question.haystack_session_ids)
    if stray:
        f5_ok = False
        violations.append(f"F5: answer_session_ids not in haystack: {sorted(stray)}")
    gold_ids = [
        build_message_id(qid, session_key, turn_idx)
        for session_key, turn_idx in gold_plan_pairs(question, plans)
    ]
    gold_rows = sum(1 for gid in gold_ids if gid in by_id)
    if gold_rows != len(gold_ids):
        f5_ok = False
        violations.append(
            f"F5: only {gold_rows}/{len(gold_ids)} has_answer turns present as message rows"
        )

    checks = {"F1": f1_ok, "F2": f2_ok, "F3": f3_ok, "F4": f4_ok, "F5": f5_ok}
    report = FidelityReport(
        passed=not violations,
        checks=checks,
        violations=violations,
        gold_message_rows=gold_rows,
        total_messages=len(rows),
        total_conversations=conv_count,
    )
    if violations and not soft:
        raise AssertionError(
            f"{qid}: {len(violations)} fidelity violation(s):\n" + "\n".join(violations)
        )
    return report


def _first_divergence(expected: List[str], got: List[str]) -> str:
    for i, (a, b) in enumerate(zip(expected, got)):
        if a != b:
            return f"index {i}: expected {a!r}, got {b!r}"
    return f"length {len(expected)} vs {len(got)}"


async def build_bench_db(
    question: LMEQuestion,
    db_path: "str | Path",
    *,
    job_names: Optional[List[str]] = None,
    disable_jobs: Optional[List[str]] = None,
    soft: bool = False,
) -> BenchDB:
    """Build a fresh bench DB for one question via the real ingest path.

    - Fresh file (existing DB + wal/shm deleted), created via
      AdapterFactory.create("local_database", ...) — tuning + migrations + ai_chat DDL.
    - Global connection pointed at the bench DB for the whole build (use_bench_db).
    - One ``ingest_file_payload`` call per session, sequential (RawFileStore keys the
      raw file by (dataset_id, schema_id) — parallel sessions would clobber it),
      chronological order.
    - ``job_names=None`` → the source's default canonical_enrichment_jobs; overrides
      are written as per-source engine-config overrides (see _apply_job_overrides).
      The EFFECTIVE lists are read back and recorded in ``effective_jobs``.
    - F1–F5 fidelity assertions run before returning (AssertionError unless soft).
    """
    from topos.enrichment.source_overrides import effective_canonical_enrichment_jobs
    from topos.ingestion.ingest_helpers import ingest_file_payload
    from topos.sources.canonical_signal_defaults import resolved_signal_derivation_jobs
    from topos.sources.registry import REGISTRY
    from topos.storage.adapters.factory import AdapterFactory

    resolved_db = Path(db_path).resolve()
    for suffix in ("", "-wal", "-shm"):
        stale = Path(str(resolved_db) + suffix)
        if stale.exists():
            stale.unlink()
    resolved_db.parent.mkdir(parents=True, exist_ok=True)

    source_def = REGISTRY[SOURCE_ID]
    plans, sort_needed = plan_sessions(question)
    duplicate_keys = {
        plan.session_key: plan.session_id
        for plan in plans
        if plan.session_key != plan.session_id
    }

    # Keep raw ingestion files next to the bench DB (not ~/.topos/ingestion),
    # so per-question temp dirs stay self-contained and cleanable.
    ingest_dir = resolved_db.parent / "ingestion"
    prev_ingest_base = os.environ.get("TOPOS_INGESTION_BASE_PATH")
    os.environ["TOPOS_INGESTION_BASE_PATH"] = str(ingest_dir)
    build_started_wall_clock = iso_utc(datetime.now(timezone.utc))  # provenance only
    try:
        setup_conn = sqlite3.connect(str(resolved_db))
        setup_conn.row_factory = sqlite3.Row
        try:
            AdapterFactory.create("local_database", conn=setup_conn)
        finally:
            setup_conn.close()

        with use_bench_db(resolved_db) as conn:
            overrides = _apply_job_overrides(conn, source_def, job_names, disable_jobs)

            session_timings: List[Dict[str, Any]] = []
            t_build0 = time.perf_counter()
            for plan in plans:
                payload = build_session_jsonl(question.question_id, plan)
                t0 = time.perf_counter()
                result = await ingest_file_payload(
                    dataset_id=dataset_id_for(question.question_id),
                    schema_id=SCHEMA_ID,
                    file_bytes=payload,
                    file_format=FILE_FORMAT,
                    source_id=SOURCE_ID,
                    job_id=f"lme:{question.question_id}:{plan.session_key}",
                )
                elapsed = round(time.perf_counter() - t0, 3)
                if result.get("status") != "ok" or result.get("errors_count"):
                    raise RuntimeError(
                        f"{question.question_id}: ingest failed for session "
                        f"{plan.session_key}: status={result.get('status')!r} "
                        f"errors={result.get('errors') or result.get('error')!r}"
                    )
                processed = int(result.get("records_processed") or 0)
                if processed != len(plan.turns):
                    raise RuntimeError(
                        f"{question.question_id}: session {plan.session_key} processed "
                        f"{processed} records, expected {len(plan.turns)}"
                    )
                session_timings.append(
                    {"session_key": plan.session_key, "n_turns": len(plan.turns), "seconds": elapsed}
                )
            ingest_seconds = round(time.perf_counter() - t_build0, 3)

            effective_jobs: Dict[str, Any] = {
                "canonical": effective_canonical_enrichment_jobs(source_def, conn),
                "signal": resolved_signal_derivation_jobs(source_def),
                "enrichment_trigger": str(
                    getattr(source_def, "enrichment_trigger", "automatic")
                ),
            }

            conversation_id_by_session = _read_back_conversations(conn, question, plans)
            gold_conversation_ids = [
                conversation_id_by_session[sid] for sid in question.answer_session_ids
            ]
            gold_message_ids = [
                build_message_id(question.question_id, session_key, turn_idx)
                for session_key, turn_idx in gold_plan_pairs(question, plans)
            ]
            fidelity = run_fidelity_checks(
                conn, question, plans, conversation_id_by_session, soft=soft
            )
    finally:
        if prev_ingest_base is None:
            os.environ.pop("TOPOS_INGESTION_BASE_PATH", None)
        else:
            os.environ["TOPOS_INGESTION_BASE_PATH"] = prev_ingest_base

    counts = {
        "sessions": len(plans),
        "turns": sum(len(plan.turns) for plan in plans),
        "conversations": fidelity.total_conversations,
        "messages": fidelity.total_messages,
        "gold_sessions": len(question.answer_session_ids),
        "gold_messages": len(gold_message_ids),
    }
    meta = {
        "adapter_version": ADAPTER_VERSION,
        "dataset_id": dataset_id_for(question.question_id),
        "schema_id": SCHEMA_ID,
        "source_id": SOURCE_ID,
        "scope_id": SCOPE_ID,
        "sessions_sorted": sort_needed,
        "duplicate_session_keys": duplicate_keys,
        "job_overrides": overrides,
        "ingest_seconds": ingest_seconds,
        "session_timings": session_timings,
        # Wall clock is provenance here, never event time.
        "build_started_wall_clock": build_started_wall_clock,
    }
    return BenchDB(
        db_path=resolved_db,
        question=question,
        conversation_id_by_session=conversation_id_by_session,
        gold_conversation_ids=gold_conversation_ids,
        gold_message_ids=gold_message_ids,
        counts=counts,
        effective_jobs=effective_jobs,
        fidelity=fidelity,
        meta=meta,
    )


# ---------------------------------------------------------------------------
# Query lane.
# ---------------------------------------------------------------------------


def _items_from_public_result(pr: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Ranked items, same extraction order as adapter/target_engine.normalize_result
    (summary items first, raw rows as the fallback). Re-implemented here because
    target_engine imports topos_eval at module scope, which the engine venv lacks."""
    items = pr.get("summaries") or pr.get("summary_items") or pr.get("scores") or []
    items = [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
    if items:
        return items
    rows = pr.get("rows") or pr.get("raw_rows") or pr.get("messages") or []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _normalize_items(
    raw_items: List[Dict[str, Any]], conn: sqlite3.Connection
) -> List[Dict[str, Any]]:
    """Attach conversation_id/event_at to each ranked item.

    Summary-mode items carry only record_id (message_id for the vector/canonical
    lanes); session-level qrels need conversation ids, so hydrate them from
    ai_chat_messages. Non-message record_ids (facts, clusters, briefs) hydrate to
    None — kept, honestly, rather than dropped.
    """
    normalized: List[Dict[str, Any]] = []
    for rank, item in enumerate(raw_items):
        record_id = item.get("record_id") or item.get("message_id")
        record_id = str(record_id) if record_id is not None else None
        conversation_id = item.get("conversation_id")
        event_at = item.get("event_at")
        sender_type = item.get("sender_type")
        content = item.get("content") or item.get("summary_text") or item.get("topic")
        if record_id and (conversation_id is None or event_at is None):
            row = conn.execute(
                "SELECT conversation_id, event_at, sender_type, content "
                "FROM ai_chat_messages WHERE message_id = ?",
                (record_id,),
            ).fetchone()
            if row is not None:
                conversation_id = conversation_id or str(row["conversation_id"])
                event_at = event_at or str(row["event_at"])
                sender_type = sender_type or str(row["sender_type"])
                content = content or str(row["content"])
        normalized.append(
            {
                "rank": rank,
                "record_id": record_id,
                "conversation_id": conversation_id,
                "event_at": event_at,
                "sender_type": sender_type,
                "content": content,
                "retrieval_source": item.get("retrieval_source"),
                "relevance_score": item.get("relevance_score"),
                "raw_item": item,
            }
        )
    return normalized


async def query_bench(
    bench: BenchDB,
    query_text: str,
    *,
    access_mode: str = "summary",
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Run one query through the real pipeline against the bench DB.

    The global connection is pointed at the bench DB for the call so the vector and
    topic-cluster lanes apply (cross-db guard `_bundle_is_global_db`); check
    ``stores_touched`` in the result to confirm.

    ``now`` is forwarded into the pipeline (execute(now=...) → RetrievalRequest.now →
    build_query_plan), so month/as-of arithmetic runs against the bench's reference
    time. Runs without a ``now`` fall back to wall clock — still recorded in
    ``threats`` so temporal grading can discount them.
    """
    from topos.query.manifest_validation import resolve_scope_manifest
    from topos.query.pipeline import QueryPipelineOrchestrator
    from topos.storage.adapters.factory import AdapterFactory

    os.environ.setdefault("TOPOS_QUERY_DDR", "1")  # surface the DDR on results
    with use_bench_db(bench.db_path) as conn:
        adapters = AdapterFactory.create("local_database", db_path=bench.db_path)
        orchestrator = QueryPipelineOrchestrator(adapters=adapters)
        manifest = resolve_scope_manifest(SCOPE_ID)
        t0 = time.perf_counter()
        raw = await orchestrator.execute(
            query_text=query_text,
            scope_id=SCOPE_ID,
            access_mode=access_mode,
            manifest=manifest,
            query_session_id=f"lme-{bench.question.question_id}-{uuid.uuid4().hex[:8]}",
            now=now,
        )
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)

        public_result = raw.get("public_result")
        pr: Dict[str, Any] = public_result if isinstance(public_result, dict) else {}
        items = _normalize_items(_items_from_public_result(pr), conn)

    audit = raw.get("audit") if isinstance(raw.get("audit"), dict) else {}
    answer = pr.get("answer")
    return {
        "items": items,
        # inference mode: run_query_inference merges {"answer": str, "confidence":
        # float} (answer == "unknown" on timeout/failure, optional "deferred"/"error")
        # into public_result; summary/raw modes have no "answer" key.
        "answer": answer if isinstance(answer, str) else None,
        "confidence": pr.get("confidence"),
        "turn_outcome": raw.get("turn_outcome"),
        "stores_touched": list(audit.get("stores_touched") or []),
        "latency_ms": latency_ms,
        "access_mode": access_mode,
        "now_requested": iso_utc(now) if now is not None else None,
        "threats": [] if now is not None else ["planner_now=wall-clock"],
        "raw": raw,
    }
