"""The inbound truth lane's owner-only tables, and the owner's three reads and one write over them.

``truth_checks`` holds one row per checked record: what verify_claim v2 made of it (the reader's main claim, the
verdict, the cited records by table, id, role and date, never their text). An ``unable`` row may be checked again by
the lane (:func:`recheck_reason`: a changed model, effort or reader timeout, or a cooldown; at most
:data:`MAX_ATTEMPTS` checks in all, never once the owner labelled it), in place: the row keeps its id, its week and
its ``created_at``, and ``attempts`` / ``read_timeout_s`` / ``checked_at`` say how many checks it had and how the last
one ran. ``engine_version`` / ``engine_sha16`` stamp every row with the engine build of its last check. ``truth_labels`` holds the owner's
judgement of a claim (true / partly / false and an optional note), one per check; a new submit replaces it. Both
are created lazily on first write, as ``ai_chat_message_facets`` is, so a node that never turns the lane on keeps
neither (no migration). Both are in ``data_explorer_tables.OWNER_ONLY_TABLES``: the legacy table tools serve them to
the owner's own app only.

A label also writes one ``verified_claim`` signal object (dimension ``memory``) with the label as its confidence
(1.0 true, 0.5 partly, 0.0 false), the checked record as its source ref and that record's time as ``valid_from``.
It is owner-only with no switch (DISCLOSURE_RULES DR-49): ``disclosure: owner_only`` in its payload, refused below
the owner tier whatever a scope grants (``retrieval._NEVER_BELOW_OWNER``), not indexed for search
(``derived_index.DERIVED_RECORD_TYPES`` does not name it), never read by a share path (they read ``fact`` objects
only), and withheld from the legacy table tools for every caller but the owner's own app. A claim about a contact
is the owner's judgement and never becomes a shareable fact.

The owner's reads (VERIFY_CLAIM_WEEKLY_PROGRAM.md §2a): ``weekly_stats`` answers counts only, never claim text;
``claim_cards`` answers the claims with snippets cut from the cited records' own text at read time, so a record the
owner deleted since stops appearing.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..storage.db.write_gate import commit_connection, with_db_write

CHECKS_TABLE = "truth_checks"
LABELS_TABLE = "truth_labels"
TABLES = (CHECKS_TABLE, LABELS_TABLE)

VERDICTS = ("supported", "no_evidence", "unable", "not_checkable")
LABELS = ("true", "partly", "false")
#: Calibrate step 1 (owner, 27 Oct 2026): a card can be dismissed as not worth checking, with one reason from this
#: closed list, instead of being labelled. Kept apart from the labels (a dismissed claim is not true, partly or false:
#: it should not have been a claim), never a ``verified_claim`` object, and counted by reason in the weekly readout so
#: the reason a kind of claim keeps surfacing can be found without reading notes.
DISMISSALS_TABLE = "truth_dismissals"
DISMISS_REASONS = ("someone_else", "not_a_claim", "trivial", "misread", "private", "other")
#: A skip is recorded too (owner, 27 Oct 2026): the card stays on the list, and the weekly readout counts skips by reason
#: to find where a card or a claim fails the owner. One row per skip (a card skipped three times is its own signal);
#: ``reason`` is optional (NULL: skipped without saying why); ``evidence_opened``: whether "What it leaned on" was open.
SKIPS_TABLE = "truth_skips"
SKIP_REASONS = ("dont_know", "unclear_claim", "need_context", "not_now")
#: The lane's denominator (owner, 27 Oct 2026: a starved lane read like a quiet one): per local day and truth source,
#: what the lane's batches did with the rows they were given. ``unresolved`` holds rows a batch deferred or skipped
#: before their source was known.
LANE_DAILY_TABLE = "truth_lane_daily"
LANE_COUNTS = ("queued", "gated", "checked", "deferred", "skipped", "paused")
#: Which cards the owner sees by default: their own claims (the lane checks only those from step 1 on; contacts'
#: claims checked by earlier builds stay stored and appear with ``speakers: "all"``).
CARD_SPEAKERS = ("owner", "all")
LABEL_CONFIDENCE = {"true": 1.0, "partly": 0.5, "false": 0.0}
OBJECT_TYPE = "verified_claim"
OBJECT_DIMENSION = "memory"
EXTRACTOR_VERSION = "truth_inbound_v1"
SNIPPET_CHARS = 400
STATEMENT_CHARS = 1000
#: The most cards one read returns. 500 (was 50): the node returns every open claim and the app chooses how many to
#: show; on 27 Oct 2026 105 cards waited behind the 50 cap, and the owner never saw the other 55.
MAX_CARDS = 500
MAX_WEEKS = 8
MAX_NOTE_CHARS = 1000

#: The truth sources and where their rows live. ``transcripts`` is the owner's voice transcripts (VoxTerm, stored as
#: conversation rows) and transcript segments that are someone's speech; an ``ambient`` segment (a YouTube caption,
#: a room the owner only overheard) is nobody's statement to the owner and is not checked.
CONVERSATION_SOURCE_IDS = {"imessage": ("imessage",), "signal": ("signal",), "transcripts": ("voxterm_transcripts",)}
#: Checked table -> (id column, time column, conversation column or None).
CHECKED_TABLES = {
    "conversation_messages": ("message_id", "event_at", "conversation_id"),
    "journal_entries": ("entry_id", "entry_at", None),
    "transcript_segments": ("segment_id", "event_at", "transcript_id"),
}
_TEXT_COLUMNS = ("content", "summary_text", "title", "description", "text")

_DDL = (
    f"""CREATE TABLE IF NOT EXISTS {CHECKS_TABLE} (
        check_id TEXT PRIMARY KEY,
        record_table TEXT NOT NULL,
        record_id TEXT NOT NULL,
        conversation_id TEXT,
        source TEXT NOT NULL,
        speaker_kind TEXT NOT NULL CHECK (speaker_kind IN ('owner', 'contact')),
        week TEXT NOT NULL,
        category TEXT,
        claim_text TEXT,
        verdict TEXT NOT NULL CHECK (verdict IN ('supported', 'no_evidence', 'unable', 'not_checkable')),
        reason TEXT,
        cited_refs_json TEXT NOT NULL DEFAULT '[]',
        effort TEXT,
        model TEXT,
        latency_ms REAL,
        created_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 1,
        read_timeout_s REAL,
        checked_at TEXT,
        engine_version TEXT,
        engine_sha16 TEXT,
        contradicted INTEGER
    )""",
    f"CREATE UNIQUE INDEX IF NOT EXISTS idx_{CHECKS_TABLE}_record ON {CHECKS_TABLE}(record_table, record_id)",
    f"CREATE INDEX IF NOT EXISTS idx_{CHECKS_TABLE}_week ON {CHECKS_TABLE}(week, created_at)",
    f"""CREATE TABLE IF NOT EXISTS {LABELS_TABLE} (
        label_id TEXT PRIMARY KEY,
        check_id TEXT NOT NULL UNIQUE,
        label TEXT NOT NULL CHECK (label IN ('true', 'partly', 'false')),
        note TEXT,
        labelled_at TEXT NOT NULL
    )""",
    f"""CREATE TABLE IF NOT EXISTS {DISMISSALS_TABLE} (
        dismissal_id TEXT PRIMARY KEY,
        check_id TEXT NOT NULL UNIQUE,
        reason TEXT NOT NULL CHECK (reason IN ('someone_else', 'not_a_claim', 'trivial', 'misread', 'private', 'other')),
        note TEXT,
        dismissed_at TEXT NOT NULL
    )""",
    f"""CREATE TABLE IF NOT EXISTS {SKIPS_TABLE} (
        skip_id TEXT PRIMARY KEY,
        check_id TEXT NOT NULL,
        reason TEXT CHECK (reason IS NULL OR reason IN ('dont_know', 'unclear_claim', 'need_context', 'not_now')),
        evidence_opened INTEGER,
        skipped_at TEXT NOT NULL
    )""",
    f"CREATE INDEX IF NOT EXISTS idx_{SKIPS_TABLE}_check ON {SKIPS_TABLE}(check_id)",
    f"""CREATE TABLE IF NOT EXISTS {LANE_DAILY_TABLE} (
        day TEXT NOT NULL,
        source TEXT NOT NULL,
        queued INTEGER NOT NULL DEFAULT 0,
        gated INTEGER NOT NULL DEFAULT 0,
        checked INTEGER NOT NULL DEFAULT 0,
        deferred INTEGER NOT NULL DEFAULT 0,
        skipped INTEGER NOT NULL DEFAULT 0,
        paused INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (day, source)
    )""",
)
#: Columns added after 1.5.19 shipped the table: added in place, on first use, to a table an earlier build created
#: (the table itself was created lazily, with no migration, so its columns are too). ``attempts`` counts the checks
#: of the record (1 for every row stored before retries existed); ``read_timeout_s`` is the reader cap the last check
#: ran with (NULL for a row stored before it was recorded, or for a row the gate refused without a model call);
#: ``checked_at`` is when the last check ran (NULL: ``created_at``). ``engine_version`` / ``engine_sha16`` name the
#: engine build of the last check: ``verify_v2.VERSION`` and ``verify_v2.engine_sha16()`` (one digest over the four
#: engine files of ``verify_v2.ENGINE_MANIFEST``; before 27 Oct 2026 the digest of ``verify_v2.py`` alone; NULL on rows
#: stored before the stamp), so the weekly eval tells builds apart by stamp rather than by time.
#: ``contradicted`` (27 Oct 2026): 1 when the engine answered "contradicted" (only for a category the owner opted in,
#: ``verify_v2.CONTRADICTION_CATEGORIES``). The verdict column keeps its four values (its CHECK cannot change in place):
#: such a row is ``no_evidence`` with reason ``contradicted`` and this flag set.
_ADDED_COLUMNS = (("attempts", "INTEGER NOT NULL DEFAULT 1"), ("read_timeout_s", "REAL"), ("checked_at", "TEXT"),
                  ("engine_version", "TEXT"), ("engine_sha16", "TEXT"), ("contradicted", "INTEGER"))
#: Added to ``truth_labels`` by step 1: whether the owner says the cited evidence shows the claim (1 / 0; NULL when not
#: asked: the card was not "backed", or the owner did not answer).
_ADDED_LABEL_COLUMNS = (("evidence_ok", "INTEGER"),)
#: An ``unable`` row is checked again at most until it has had this many checks in all.
MAX_ATTEMPTS = 3


class TruthRequestError(ValueError):
    """A request the store refuses; ``str(exc)`` is a fixed code, never the input."""


# --- tables ---------------------------------------------------------------------------------------------------------
def _present(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def tables_present(conn: sqlite3.Connection) -> bool:
    return all(_present(conn, t) for t in TABLES)


def _missing_columns(conn: sqlite3.Connection) -> List[Tuple[str, str, str]]:
    out = []
    for table, added in ((CHECKS_TABLE, _ADDED_COLUMNS), (LABELS_TABLE, _ADDED_LABEL_COLUMNS)):
        have = set(_columns(conn, table))
        out += [(table, name, decl) for name, decl in added if name not in have]
    return out


def ensure_tables(conn: sqlite3.Connection) -> None:
    """Create the tables on first use, and add the later tables and columns to what an earlier build created. The
    probes are reads, so a node that has everything takes no write gate."""
    if (tables_present(conn) and all(_present(conn, t) for t in (DISMISSALS_TABLE, SKIPS_TABLE, LANE_DAILY_TABLE))
            and not _missing_columns(conn)):
        return
    with with_db_write():
        for statement in _DDL:
            conn.execute(statement)
        for table, name, decl in _missing_columns(conn):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        commit_connection(conn)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_week(when: Optional[datetime] = None) -> str:
    """The ISO week (Monday start) in the node's local time, e.g. ``2026-W43``."""
    local = (when or now_utc()).astimezone()
    year, week, _ = local.isocalendar()
    return f"{year}-W{week:02d}"


def instant(value: Any) -> Optional[datetime]:
    """A stored time as an aware UTC instant: the one reading of the checked tables' times, shared with the engine
    (``verify_v2`` imports it). None for an empty, date-only or unreadable value: a bare date is not an instant.

    A naive time (no offset, no ``Z``) is the node's LOCAL time. Until 27 Oct 2026 this module read naive as UTC
    while ``verify_v2._instant`` read it as local, so the reader's date and the as-of cutoff could disagree about the
    same row. A read-only census of the owner's node that day decided the rule: ``conversation_messages.event_at``
    is written with an offset (98,502 of 98,568 rows; 59 ``Z``; 7 naive, all voice transcripts), while every
    ``journal_entries.entry_at`` (616 of 616) is naive, because the journal normaliser strips an entry's offset without
    converting it, so the stored value is the wall clock where it was written, i.e. local. Reading it as UTC had put
    every journal entry five hours early."""
    text = str(value or "").strip()
    if len(text) <= 10:
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when.astimezone(timezone.utc)  # a naive time is local: astimezone() applies the node's own zone


_aware = instant  # the old name, read by reflection.py and reflection_facts.py


def with_offset(value: Any) -> Optional[str]:
    """A stored time as ISO 8601 with the node's local offset (``said_at``)."""
    when = instant(value)
    return when.astimezone().isoformat() if when else None


# --- records --------------------------------------------------------------------------------------------------------
def _columns(conn: sqlite3.Connection, table: str) -> List[str]:
    return [str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


def _row(conn: sqlite3.Connection, table: str, id_column: str, record_id: str) -> Optional[Dict[str, Any]]:
    """One row by id. ``table`` and ``id_column`` come only from closed registries, never from a caller."""
    if not _present(conn, table):
        return None
    cursor = conn.execute(f'SELECT * FROM "{table}" WHERE "{id_column}"=? LIMIT 1', (record_id,))
    found = cursor.fetchone()
    if found is None:
        return None
    return dict(zip([d[0] for d in cursor.description], found))


def _written_by_owner_door(row: Dict[str, Any]) -> bool:
    from ..features.provenance.writer_class import is_owner_writer

    return is_owner_writer(row.get("writer_class")) if "writer_class" in row else True


def _owner_row(table: str, row: Dict[str, Any]) -> bool:
    """Whether the owner said this row, by the rule retrieval applies to a whole row: a journal entry is the owner's
    by construction; a message or segment is the owner's only when typed so (``is_from_self``) and written by an
    owner door. Anything else is not the owner's: never guess authored."""
    if table == "journal_entries":
        return True
    if row.get("is_from_self") in (1, True, "1"):
        return _written_by_owner_door(row)
    if table == "conversation_messages" and str(row.get("sender_id") or "").strip().lower() == "self":
        return _written_by_owner_door(row)
    return False


def _contact_name(conn: sqlite3.Connection, sender_id: str, cache: Dict[str, Optional[str]]) -> Optional[str]:
    """A sender's display name through the owner's contacts, or None. A raw identifier is never a name here: it
    would reach the reader (and, on the hosted model, a provider) and the owner's screen as a phone number."""
    if not sender_id:
        return None
    if sender_id not in cache:
        name = None
        try:
            found = conn.execute(
                "SELECT c.display_name FROM contact_identifiers ci JOIN contacts c ON c.contact_id = ci.contact_id "
                "WHERE ci.identifier = ? AND c.display_name IS NOT NULL AND TRIM(c.display_name) != '' LIMIT 1",
                (sender_id,),
            ).fetchone()
            name = str(found[0]).strip() if found else None
        except sqlite3.Error:
            name = None
        cache[sender_id] = name
    return cache[sender_id]


def truth_source(table: str, row: Dict[str, Any]) -> Optional[str]:
    """Which truth source a checked row belongs to, or None when the lane does not check it."""
    if table == "journal_entries":
        return "journal"
    if table == "transcript_segments":
        return "transcripts" if str(row.get("actor_role") or "ambient") != "ambient" else None
    if table == "conversation_messages":
        source_id = str(row.get("source_id") or "")
        return next((name for name, ids in CONVERSATION_SOURCE_IDS.items() if source_id in ids), None)
    return None


def load_record(conn: sqlite3.Connection, table: str, record_id: str,
                names: Optional[Dict[str, Optional[str]]] = None) -> Optional[Dict[str, Any]]:
    """A checked row as the lane and the cards need it, or None when it is gone or not a row the lane checks."""
    spec = CHECKED_TABLES.get(table)
    if spec is None:
        return None
    id_column, time_column, conversation_column = spec
    row = _row(conn, table, id_column, record_id)
    if row is None:
        return None
    source = truth_source(table, row)
    if source is None:
        return None
    owner = _owner_row(table, row)
    name = None
    if not owner:
        cache = names if names is not None else {}
        name = (_contact_name(conn, str(row.get("sender_id") or ""), cache) if table == "conversation_messages"
                else (str(row.get("speaker_label") or "").strip()[:60] or None))
    return {
        "table": table, "record_id": str(record_id), "source": source, "owner": owner, "name": name,
        "text": str(row.get("content") or ""), "at": row.get(time_column),
        "conversation_id": str(row.get(conversation_column) or "") or None if conversation_column else None,
        "row": row,
    }


def speaker_for_reader(record: Dict[str, Any]) -> str:
    return "owner" if record["owner"] else (record.get("name") or "a contact")


def speaker_for_card(record: Dict[str, Any]) -> str:
    return "you" if record["owner"] else (record.get("name") or "a contact")


def previous_turns(conn: sqlite3.Connection, record: Dict[str, Any], limit: int = 6,
                   names: Optional[Dict[str, Optional[str]]] = None) -> List[Dict[str, str]]:
    """The ``limit`` rows of the same conversation before this one, oldest first, as ``{speaker, text}``. A journal
    entry has no conversation, so it gets none."""
    table = record["table"]
    id_column, time_column, conversation_column = CHECKED_TABLES[table]
    if not conversation_column or not record.get("conversation_id"):
        return []
    at = str(record.get("at") or "")
    cursor = conn.execute(
        f'SELECT * FROM "{table}" WHERE "{conversation_column}"=? AND "{id_column}"!=? '
        f'AND ("{time_column}" < ? OR ("{time_column}" = ? AND "{id_column}" < ?)) '
        f'ORDER BY "{time_column}" DESC, "{id_column}" DESC LIMIT ?',
        (record["conversation_id"], record["record_id"], at, at, record["record_id"], int(limit)),
    )
    keys = [d[0] for d in cursor.description]
    cache = names if names is not None else {}
    turns = []
    for found in reversed(cursor.fetchall()):
        row = dict(zip(keys, found))
        text = str(row.get("content") or "").strip()
        if not text:
            continue
        if _owner_row(table, row):
            speaker = "owner"
        elif table == "conversation_messages":
            speaker = _contact_name(conn, str(row.get("sender_id") or ""), cache) or "a contact"
        else:
            speaker = str(row.get("speaker_label") or "").strip()[:60] or "a contact"
        turns.append({"speaker": speaker, "text": text[:STATEMENT_CHARS]})
    return turns


def _cited_text(conn: sqlite3.Connection, table: str, record_id: str) -> Optional[Dict[str, Any]]:
    """A cited record's own text and source, read now; None when it no longer exists."""
    if table in ("signal_objects", "verified_claim"):  # an owner label cited as evidence (verify_v2.LABELS_AS_EVIDENCE)
        found = _row(conn, "signal_objects", "object_id", record_id)
        if found is None:
            return None
        try:
            payload = json.loads(found.get("payload_json") or "{}")
        except ValueError:
            payload = {}
        text = next((str(payload.get(k)) for k in ("object_value", "claim", "summary_text", "text")
                     if isinstance(payload, dict) and payload.get(k)), "")
        return {"text": text, "source": str(found.get("object_type") or "derived")}
    from ..storage.adapters.sqlite.stores import _NATIVE_ID_COL

    id_column = _NATIVE_ID_COL.get(table)
    if id_column is None:
        return {"text": "", "source": table}
    found = _row(conn, table, id_column, record_id)
    if found is None:
        return None
    column = next((c for c in _TEXT_COLUMNS if found.get(c)), None)
    source = truth_source(table, found) or str(found.get("source_id") or "") or table
    return {"text": str(found.get(column) or "") if column else "", "source": source}


# --- writes ---------------------------------------------------------------------------------------------------------
def already_checked(conn: sqlite3.Connection, table: str, record_id: str) -> bool:
    if not _present(conn, CHECKS_TABLE):
        return False
    return conn.execute(f"SELECT 1 FROM {CHECKS_TABLE} WHERE record_table=? AND record_id=?",
                        (table, record_id)).fetchone() is not None


def record_check(conn: sqlite3.Connection, check: Dict[str, Any]) -> Optional[str]:
    """Store one check; returns its id, or None when the record was already checked (one row per record: a later
    check of an ``unable`` row goes through :func:`recheck`)."""
    ensure_tables(conn)
    if check.get("verdict") not in VERDICTS or check.get("speaker_kind") not in ("owner", "contact"):
        raise TruthRequestError("invalid_check")
    check_id = uuid.uuid4().hex
    created = now_utc()
    with with_db_write():
        cursor = conn.execute(
            f"INSERT OR IGNORE INTO {CHECKS_TABLE} (check_id, record_table, record_id, conversation_id, source, "
            "speaker_kind, week, category, claim_text, verdict, reason, cited_refs_json, effort, model, latency_ms, "
            "created_at, attempts, read_timeout_s, checked_at, engine_version, engine_sha16, contradicted) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
            (check_id, check["record_table"], check["record_id"], check.get("conversation_id"), check["source"],
             check["speaker_kind"], iso_week(created), check.get("category"), check.get("claim_text"),
             check["verdict"], check.get("reason"), json.dumps(check.get("cited_refs") or []), check.get("effort"),
             check.get("model"), check.get("latency_ms"), created.isoformat(), check.get("read_timeout_s"),
             created.isoformat(), check.get("engine_version"), check.get("engine_sha16"),
             1 if check.get("contradicted") else None),
        )
        commit_connection(conn)
    return check_id if cursor.rowcount else None


# --- re-checks of unable rows -----------------------------------------------------------------------------------------
#: What a check ran with, as the lane compares it: (model label, effort, reader timeout in seconds).
Signature = Tuple[Optional[str], Optional[str], Optional[float]]


def state_columns(conn: sqlite3.Connection) -> str:
    """The re-check columns of ``truth_checks c`` as a select list, on a table an earlier build created too (a column
    it lacks reads as its default), and with or without the labels table: check id, verdict, model, effort, reader
    timeout, attempts, last check time, labelled."""
    have = set(_columns(conn, CHECKS_TABLE))
    col = lambda name, fallback: f"c.{name}" if name in have else fallback  # noqa: E731
    labelled = (f"EXISTS (SELECT 1 FROM {LABELS_TABLE} l WHERE l.check_id = c.check_id)"
                if _present(conn, LABELS_TABLE) else "0")
    return (f"c.check_id, c.verdict, c.model, c.effort, {col('read_timeout_s', 'NULL')}, "
            f"COALESCE({col('attempts', 'NULL')}, 1), COALESCE({col('checked_at', 'NULL')}, c.created_at), {labelled}")


_STATE_KEYS = ("check_id", "verdict", "model", "effort", "read_timeout_s", "attempts", "checked_at", "labelled")


def state_of(found: Iterable[Any]) -> Dict[str, Any]:
    """A :func:`state_columns` row as a dict."""
    state = dict(zip(_STATE_KEYS, found))
    state["attempts"] = int(state["attempts"] or 1)
    state["labelled"] = bool(state["labelled"])
    return state


def check_state(conn: sqlite3.Connection, table: str, record_id: str) -> Optional[Dict[str, Any]]:
    """What the store holds for one record, for the lane's re-check decision; None when it has no row."""
    if not _present(conn, CHECKS_TABLE):
        return None
    found = conn.execute(f"SELECT {state_columns(conn)} FROM {CHECKS_TABLE} c WHERE c.record_table=? AND c.record_id=?",
                         (table, record_id)).fetchone()
    return state_of(found) if found is not None else None


def recheck_reason(state: Optional[Dict[str, Any]], signature: Signature, *, now: Optional[datetime] = None,
                   cooldown_s: float) -> Optional[str]:
    """Why the lane should check a stored row again, or None. Only an ``unable`` row the owner has not labelled, with
    fewer than :data:`MAX_ATTEMPTS` checks, is checked again: at once when the lane's model, effort or reader timeout
    is not the one its last check ran with (a timeout the row does not record differs from every timeout), else once
    ``cooldown_s`` has passed since that check."""
    if not state or state.get("verdict") != "unable" or state.get("labelled"):
        return None
    if int(state.get("attempts") or 1) >= MAX_ATTEMPTS:
        return None
    model, effort, read_timeout = signature
    if (state.get("model") or None) != (model or None):
        return "model"
    if (state.get("effort") or None) != (effort or None):
        return "effort"
    recorded = state.get("read_timeout_s")
    if recorded is None or read_timeout is None or abs(float(recorded) - float(read_timeout)) > 1e-6:
        return "read_timeout"
    last = _aware(state.get("checked_at"))
    if last is None or (now or now_utc()) - last >= timedelta(seconds=cooldown_s):
        return "cooldown"
    return None


def recheck(conn: sqlite3.Connection, check: Dict[str, Any], attempt: int) -> Optional[str]:
    """Replace an ``unable`` row's result with a later check of the same record, in place: the row keeps its id, its
    week and its ``created_at`` (the week the record was first checked), and ``attempts`` becomes ``attempt``.
    Returns the check id, or None when the row is no longer the ``unable`` row with ``attempt - 1`` checks (another
    check got there first, or the owner labelled it)."""
    ensure_tables(conn)
    if check.get("verdict") not in VERDICTS or check.get("speaker_kind") not in ("owner", "contact"):
        raise TruthRequestError("invalid_check")
    if type(attempt) is not int or not 2 <= attempt <= MAX_ATTEMPTS:
        raise TruthRequestError("invalid_check")
    with with_db_write():
        found = conn.execute(
            f"SELECT c.check_id FROM {CHECKS_TABLE} c WHERE c.record_table=? AND c.record_id=? AND c.verdict='unable' "
            f"AND c.attempts=? AND NOT EXISTS (SELECT 1 FROM {LABELS_TABLE} l WHERE l.check_id = c.check_id)",
            (check["record_table"], check["record_id"], attempt - 1)).fetchone()
        if found is None:
            return None
        conn.execute(
            f"UPDATE {CHECKS_TABLE} SET conversation_id=?, source=?, speaker_kind=?, category=?, claim_text=?, "
            "verdict=?, reason=?, cited_refs_json=?, effort=?, model=?, latency_ms=?, attempts=?, read_timeout_s=?, "
            "checked_at=?, engine_version=?, engine_sha16=?, contradicted=? WHERE check_id=?",
            (check.get("conversation_id"), check["source"], check["speaker_kind"], check.get("category"),
             check.get("claim_text"), check["verdict"], check.get("reason"), json.dumps(check.get("cited_refs") or []),
             check.get("effort"), check.get("model"), check.get("latency_ms"), attempt, check.get("read_timeout_s"),
             now_utc().isoformat(), check.get("engine_version"), check.get("engine_sha16"),
             1 if check.get("contradicted") else None, found[0]),
        )
        commit_connection(conn)
    return str(found[0])


def _claim_check(conn: sqlite3.Connection, check_id: Any, note: Any) -> Tuple[Dict[str, Any], Optional[str]]:
    """The labelable check a label or a dismissal names, and its cleaned note; refuses with a fixed code."""
    if not isinstance(check_id, str) or not check_id.strip() or len(check_id) > 64:
        raise TruthRequestError("invalid_truth_label")
    if note is not None and not isinstance(note, str):
        raise TruthRequestError("invalid_truth_label")
    note = " ".join((note or "").split())[:MAX_NOTE_CHARS] or None
    if not tables_present(conn):
        raise TruthRequestError("unknown_check")
    found = conn.execute(
        f"SELECT check_id, record_table, record_id, source, speaker_kind, claim_text, verdict, created_at "
        f"FROM {CHECKS_TABLE} WHERE check_id=?", (check_id.strip(),)).fetchone()
    if found is None:
        raise TruthRequestError("unknown_check")
    check = dict(zip(("check_id", "record_table", "record_id", "source", "speaker_kind", "claim_text", "verdict",
                      "created_at"), found))
    if not check["claim_text"] or check["verdict"] == "not_checkable":
        raise TruthRequestError("not_a_claim")
    return check, note


def label_claim(conn: sqlite3.Connection, check_id: Any, label: Any, note: Any = None,
                evidence_ok: Any = None) -> Dict[str, Any]:
    """The owner's label of one claim, and its ``verified_claim`` object, in one gated commit. A second submit
    replaces both (the label keeps its id); a label replaces a dismissal of the same claim. ``evidence_ok``: whether
    the cited evidence shows the claim (a backed card's second question), or None."""
    if label not in LABELS or evidence_ok not in (None, True, False):
        raise TruthRequestError("invalid_truth_label")
    check, note = _claim_check(conn, check_id, note)
    ensure_tables(conn)
    record = load_record(conn, check["record_table"], check["record_id"])
    valid_from = (with_offset(record["at"]) if record else None) or check["created_at"]
    now = now_utc().isoformat()
    payload = {"claim": check["claim_text"], "label": label, "note": note, "speaker_kind": check["speaker_kind"],
               "source": check["source"], "check_id": check["check_id"], "disclosure": "owner_only"}
    refs = [{"table": check["record_table"], "record_id": check["record_id"]}]
    key = f"truth_check:{check['check_id']}"
    with with_db_write():
        existing = conn.execute(f"SELECT label_id FROM {LABELS_TABLE} WHERE check_id=?", (check["check_id"],)).fetchone()
        label_id = str(existing[0]) if existing else uuid.uuid4().hex
        conn.execute(
            f"INSERT INTO {LABELS_TABLE} (label_id, check_id, label, note, labelled_at, evidence_ok) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(check_id) DO UPDATE SET label=excluded.label, note=excluded.note, labelled_at=excluded.labelled_at, "
            "evidence_ok=excluded.evidence_ok",
            (label_id, check["check_id"], label, note, now, None if evidence_ok is None else int(evidence_ok)),
        )
        conn.execute(f"DELETE FROM {DISMISSALS_TABLE} WHERE check_id=?", (check["check_id"],))
        _write_verified_claim(conn, key, payload, LABEL_CONFIDENCE[label], refs, valid_from, now)
        commit_connection(conn)
    return {"ok": True, "label_id": label_id}


def dismiss_claim(conn: sqlite3.Connection, check_id: Any, reason: Any, note: Any = None) -> Dict[str, Any]:
    """The owner says a card should not have been a claim at all, with one reason from :data:`DISMISS_REASONS`. Replaces
    a label of the same claim and removes its ``verified_claim`` object (a dismissed claim is no judgement of the
    owner's); a second dismissal replaces the first (it keeps its id)."""
    if reason not in DISMISS_REASONS:
        raise TruthRequestError("invalid_truth_label")
    check, note = _claim_check(conn, check_id, note)
    ensure_tables(conn)
    now = now_utc().isoformat()
    with with_db_write():
        existing = conn.execute(f"SELECT dismissal_id FROM {DISMISSALS_TABLE} WHERE check_id=?", (check["check_id"],)).fetchone()
        dismissal_id = str(existing[0]) if existing else uuid.uuid4().hex
        conn.execute(
            f"INSERT INTO {DISMISSALS_TABLE} (dismissal_id, check_id, reason, note, dismissed_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(check_id) DO UPDATE SET reason=excluded.reason, note=excluded.note, dismissed_at=excluded.dismissed_at",
            (dismissal_id, check["check_id"], reason, note, now),
        )
        conn.execute(f"DELETE FROM {LABELS_TABLE} WHERE check_id=?", (check["check_id"],))
        conn.execute("DELETE FROM signal_objects WHERE object_type=? AND object_key=?",
                     (OBJECT_TYPE, f"truth_check:{check['check_id']}"))
        commit_connection(conn)
    return {"ok": True, "dismissal_id": dismissal_id}


def skip_claim(conn: sqlite3.Connection, check_id: Any, reason: Any = None, evidence_opened: Any = None) -> Dict[str, Any]:
    """The owner skipped a card, optionally saying why (:data:`SKIP_REASONS`). Appends one row; the card stays on the
    list and nothing else changes."""
    if (reason is not None and reason not in SKIP_REASONS) or evidence_opened not in (None, True, False):
        raise TruthRequestError("invalid_truth_label")
    check, _ = _claim_check(conn, check_id, None)
    ensure_tables(conn)
    skip_id = uuid.uuid4().hex
    with with_db_write():
        conn.execute(f"INSERT INTO {SKIPS_TABLE} (skip_id, check_id, reason, evidence_opened, skipped_at) VALUES (?, ?, ?, ?, ?)",
                     (skip_id, check["check_id"], reason, None if evidence_opened is None else int(evidence_opened),
                      now_utc().isoformat()))
        commit_connection(conn)
    return {"ok": True, "skip_id": skip_id}


def count_lane(conn: sqlite3.Connection, counts: Dict[str, Dict[str, int]], when: Optional[datetime] = None) -> None:
    """Add one batch's counts (``{source: {count: n}}``, names from :data:`LANE_COUNTS`) to today's rows."""
    rows = [(source, {k: int(v) for k, v in c.items() if k in LANE_COUNTS and v}) for source, c in counts.items()]
    rows = [(source, c) for source, c in rows if c]
    if not rows:
        return
    day = (when or now_utc()).astimezone().date().isoformat()
    ensure_tables(conn)
    with with_db_write():
        for source, c in rows:
            cols = sorted(c)
            conn.execute(
                f"INSERT INTO {LANE_DAILY_TABLE} (day, source, {', '.join(cols)}) VALUES (?, ?, {', '.join('?' for _ in cols)}) "
                f"ON CONFLICT(day, source) DO UPDATE SET {', '.join(f'{k} = {k} + excluded.{k}' for k in cols)}",
                (day, str(source)[:40], *[c[k] for k in cols]))
        commit_connection(conn)


#: What a failed local sync's stored error says, as a closed category (the error text itself never leaves the node's
#: job table): the first matching phrase wins.
_SYNC_FAILURES = (("full_disk_access", ("operation not permitted", "full disk access")), ("disk_floor", ("disk space",)),
                  ("source_missing", ("no such file", "not found")))


def sources_health(conn: sqlite3.Connection) -> Dict[str, Any]:
    """The synced truth sources' last sync, read from the job table: when it last ran, whether it succeeded, when it
    last succeeded, and a failure category. Journal and transcripts arrive through other doors and are not listed."""
    out: Dict[str, Any] = {}
    if not _present(conn, "pipeline_jobs"):
        return out
    for source in ("imessage", "signal"):
        rows = conn.execute(
            "SELECT status, updated_at, detail_json FROM pipeline_jobs WHERE kind = 'local_sync' "
            "AND json_extract(payload_json, '$.source_id') = ? AND status IN ('done', 'failed') "
            "ORDER BY updated_at DESC LIMIT 50", (source,)).fetchall()
        if not rows:
            continue
        status, at, detail = rows[0]
        ok_at = next((r[1] for r in rows if r[0] == "done"), None)
        failure = None
        if status == "failed":
            text = str(detail or "").lower()
            failure = next((name for name, phrases in _SYNC_FAILURES if any(p in text for p in phrases)), "other")
        out[source] = {"last_sync_at": at, "last_sync_ok": status == "done", "last_ok_at": ok_at, "failure": failure}
    return out


def _write_verified_claim(conn: sqlite3.Connection, key: str, payload: Dict[str, Any], confidence: float,
                          refs: List[Dict[str, Any]], valid_from: str, now: str) -> None:
    """Inside the caller's gate. Direct SQL, as FactStore writes facts: the memory dimension's definition lists the
    types its derivation produces, and this one is the owner's own judgement, not a derived type."""
    found = conn.execute(
        "SELECT object_id FROM signal_objects WHERE signal_dimension=? AND object_type=? AND object_key=? "
        "AND valid_to IS NULL", (OBJECT_DIMENSION, OBJECT_TYPE, key)).fetchone()
    if found:
        conn.execute("UPDATE signal_objects SET payload_json=?, confidence=?, source_refs_json=?, valid_from=?, "
                     "updated_at=? WHERE object_id=?",
                     (json.dumps(payload), confidence, json.dumps(refs), valid_from, now, found[0]))
        return
    conn.execute(
        "INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json, confidence, "
        "source_refs_json, valid_from, valid_to, extractor_version, created_at, updated_at, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, 'owner')",
        (str(uuid.uuid4()), OBJECT_DIMENSION, OBJECT_TYPE, key, json.dumps(payload), confidence, json.dumps(refs),
         valid_from, EXTRACTOR_VERSION, now, now),
    )


def _gone_from(conn: sqlite3.Connection, table: str) -> str:
    """The SQL clause that keeps only checks whose record is no longer in ``table`` (every check, when the table itself
    is absent)."""
    id_column = CHECKED_TABLES[table][0]
    return "" if not _present(conn, table) else (
        f" AND NOT EXISTS (SELECT 1 FROM {table} m WHERE m.{id_column} = c.record_id)")


def forget_records(conn: sqlite3.Connection, records: Optional[Iterable[Any]] = None) -> int:
    """Delete the checks of removed records, with their labels, dismissals, skips and ``verified_claim`` objects: a
    claim read out of a record does not outlive it. Returns the number of checks removed.

    ``records`` (the owner's "remove this" flow) names records as ``(record_table, record_id)`` pairs, or as bare ids
    when the caller knows only those. A check is keyed on both: record ids are unique within a table, not across the
    checked tables. A bare id that one checked table's rows carry names that check; a bare id two tables' rows carry
    (a collision) names only the checks whose record is actually gone from its own table (until 27 Oct 2026 a bare id
    removed every check carrying it, whichever table the record lived in). Without ``records`` (a source scrub), every
    check whose record is no longer in its table."""
    if not tables_present(conn):
        return 0
    gone: List[str] = []
    if records is not None:
        pairs: set = set()
        bare: set = set()
        for item in records:
            if isinstance(item, (tuple, list)) and len(item) == 2:
                if str(item[0]) in CHECKED_TABLES and str(item[1]).strip():
                    pairs.add((str(item[0]), str(item[1])))
            elif str(item).strip():
                bare.add(str(item))
        for table in CHECKED_TABLES:
            ids = sorted({record_id for t, record_id in pairs if t == table})
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                gone += [str(r[0]) for r in conn.execute(
                    f"SELECT c.check_id FROM {CHECKS_TABLE} c WHERE c.record_table = ? "
                    f"AND c.record_id IN ({','.join('?' for _ in chunk)})", (table, *chunk))]
        ids = sorted(bare)
        carrying: Dict[str, List[Tuple[str, str, bool]]] = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            for table in CHECKED_TABLES:
                present = _present(conn, table)
                id_column = CHECKED_TABLES[table][0]
                exists = f"EXISTS (SELECT 1 FROM {table} m WHERE m.{id_column} = c.record_id)" if present else "0"
                for check_id, record_id, still_there in conn.execute(
                        f"SELECT c.check_id, c.record_id, {exists} FROM {CHECKS_TABLE} c WHERE c.record_table = ? "
                        f"AND c.record_id IN ({','.join('?' for _ in chunk)})", (table, *chunk)):
                    carrying.setdefault(str(record_id), []).append((str(check_id), table, bool(still_there)))
        for found in carrying.values():
            if len(found) == 1:
                gone.append(found[0][0])
            else:
                gone += [check_id for check_id, _, still_there in found if not still_there]
        gone = sorted(set(gone))
    else:
        for table in CHECKED_TABLES:
            gone += [str(r[0]) for r in conn.execute(
                f"SELECT c.check_id FROM {CHECKS_TABLE} c WHERE c.record_table = ?{_gone_from(conn, table)}", (table,))]
    if not gone:
        return 0
    with with_db_write():
        for start in range(0, len(gone), 500):
            chunk = gone[start:start + 500]
            marks = ",".join("?" for _ in chunk)
            conn.execute(f"DELETE FROM {LABELS_TABLE} WHERE check_id IN ({marks})", chunk)
            for side_table in (DISMISSALS_TABLE, SKIPS_TABLE):
                if _present(conn, side_table):
                    conn.execute(f"DELETE FROM {side_table} WHERE check_id IN ({marks})", chunk)
            conn.execute(f"DELETE FROM {CHECKS_TABLE} WHERE check_id IN ({marks})", chunk)
            conn.execute(f"DELETE FROM signal_objects WHERE object_type = ? AND object_key IN ({marks})",
                         [OBJECT_TYPE, *(f"truth_check:{c}" for c in chunk)])
        commit_connection(conn)
    return len(gone)


# --- the owner's reads ----------------------------------------------------------------------------------------------
def recent_weeks(weeks: int, when: Optional[datetime] = None) -> List[str]:
    """This ISO week and the ``weeks - 1`` before it, newest first."""
    start = when or now_utc()
    out: List[str] = []
    for i in range(weeks):
        week = iso_week(start - timedelta(days=7 * i))
        if week not in out:
            out.append(week)
    return out


def _empty_week(week: str) -> Dict[str, Any]:
    from ..config.truth_inbound import SOURCES

    return {"week": week, "checked": 0, "backed": 0, "nothing_found": 0, "unable": 0, "not_a_claim": 0,
            "rechecked": 0, "attempts_exhausted": 0,
            "by_source": {s: 0 for s in SOURCES}, "by_category": {}, "labelled": 0, "dismissed": 0, "skipped": 0, "contradicted": 0, "to_confirm": 0,
            "lane": {k: 0 for k in LANE_COUNTS},
            "agreement": {"backed_true": 0, "backed_false": 0, "nothing_true": 0}, "latency_ms_p50": None}


def weekly_stats(conn: sqlite3.Connection, weeks: int = 1, *, when: Optional[datetime] = None) -> Dict[str, Any]:
    """Counts per ISO week, newest first. Aggregates only: no claim text, no record id, no name. A row counts in the
    week its record was first checked; ``rechecked`` is how many of that week's rows were checked more than once, and
    ``attempts_exhausted`` how many are still ``unable`` after the last check allowed (:data:`MAX_ATTEMPTS`). Reads a
    table an earlier build created as it is (the CLI's connection is read-only)."""
    if type(weeks) is not int or not 1 <= weeks <= MAX_WEEKS:
        raise TruthRequestError("invalid_truth_request")
    names = recent_weeks(weeks, when)
    out = {week: _empty_week(week) for week in names}
    if tables_present(conn):
        attempts = "COALESCE(c.attempts, 1)" if "attempts" in _columns(conn, CHECKS_TABLE) else "1"
        dismissed, join = _dismissal_join(conn)
        skipped = (f"EXISTS (SELECT 1 FROM {SKIPS_TABLE} s WHERE s.check_id = c.check_id)"
                   if _present(conn, SKIPS_TABLE) else "0")
        against = "COALESCE(c.contradicted, 0)" if "contradicted" in _columns(conn, CHECKS_TABLE) else "0"
        rows = conn.execute(
            f"SELECT c.week, c.source, c.category, c.verdict, c.latency_ms, c.model IS NOT NULL, "
            f"c.claim_text IS NOT NULL, l.label, {attempts}, {dismissed}, c.speaker_kind, {skipped}, {against} FROM {CHECKS_TABLE} c "
            f"LEFT JOIN {LABELS_TABLE} l ON l.check_id = c.check_id{join} "
            f"WHERE c.week IN ({','.join('?' for _ in names)})", names).fetchall()
        latencies: Dict[str, List[float]] = {week: [] for week in names}
        for week, source, category, verdict, latency, modelled, has_claim, label, tries, reason, speaker, was_skipped, was_against in rows:
            entry = out[week]
            entry["checked"] += 1
            if tries > 1:
                entry["rechecked"] += 1
            if verdict == "unable" and tries >= MAX_ATTEMPTS:
                entry["attempts_exhausted"] += 1
            key = {"supported": "backed", "no_evidence": "nothing_found", "unable": "unable",
                   "not_checkable": "not_a_claim"}.get(verdict)
            if key:
                entry[key] += 1
            entry["skipped"] += int(bool(was_skipped))  # cards skipped at least once, whatever happened after
            entry["contradicted"] += int(bool(was_against))
            if source in entry["by_source"]:
                entry["by_source"][source] += 1
            if category:
                entry["by_category"][category] = entry["by_category"].get(category, 0) + 1
            if label:
                entry["labelled"] += 1
                if verdict == "supported" and label in ("true", "false"):
                    entry["agreement"]["backed_" + label] += 1
                elif verdict == "no_evidence" and label == "true":
                    entry["agreement"]["nothing_true"] += 1
            elif reason:
                entry["dismissed"] += 1
            elif has_claim and verdict != "not_checkable" and speaker == "owner":  # what the cards show by default
                entry["to_confirm"] += 1
            if modelled and latency is not None:
                latencies[week].append(float(latency))
        for week, values in latencies.items():
            if values:
                out[week]["latency_ms_p50"] = round(statistics.median(values), 1)
    if _present(conn, LANE_DAILY_TABLE):
        for day, *values in conn.execute(f"SELECT day, {', '.join(LANE_COUNTS)} FROM {LANE_DAILY_TABLE}").fetchall():
            try:
                week = iso_week(datetime.fromisoformat(str(day)).replace(hour=12).astimezone())
            except ValueError:
                continue
            if week in out:
                for key, value in zip(LANE_COUNTS, values):
                    out[week]["lane"][key] += int(value or 0)
    return {"weeks": [out[week] for week in names], "sources": sources_health(conn)}


def _dismissal_join(conn: sqlite3.Connection) -> Tuple[str, str]:
    """The dismissal reason column and its join, or NULL on a node whose tables predate dismissals (a read never
    creates a table)."""
    if _present(conn, DISMISSALS_TABLE):
        return "d.reason", f" LEFT JOIN {DISMISSALS_TABLE} d ON d.check_id = c.check_id"
    return "NULL", ""


def claim_cards(conn: sqlite3.Connection, limit: int = 20, unlabelled_only: bool = True,
                speakers: str = "owner") -> Dict[str, Any]:
    """The claims the lane found, newest check first, for the owner to label in the app. A card whose record is gone
    is skipped; a cited record that is gone is left out of its card. ``unlabelled_only``: neither labelled nor
    dismissed. ``speakers``: ``owner`` (the default: the owner's own claims) or ``all``. Each card carries what was
    said (``said``: the record's own text) beside the claim the reader took from it (``claim_text``)."""
    if (type(limit) is not int or not 1 <= limit <= MAX_CARDS or type(unlabelled_only) is not bool
            or speakers not in CARD_SPEAKERS):
        raise TruthRequestError("invalid_truth_request")
    if not tables_present(conn):
        return {"cards": []}
    dismissed, join = _dismissal_join(conn)
    evidence_ok = "l.evidence_ok" if "evidence_ok" in _columns(conn, LABELS_TABLE) else "NULL"
    contradicted = "c.contradicted" if "contradicted" in _columns(conn, CHECKS_TABLE) else "NULL"
    where = f" AND l.check_id IS NULL AND {dismissed} IS NULL" if unlabelled_only else ""
    if speakers == "owner":
        where += " AND c.speaker_kind = 'owner'"
    rows = conn.execute(
        f"SELECT c.check_id, c.record_table, c.record_id, c.source, c.category, c.claim_text, c.verdict, "
        f"c.cited_refs_json, c.latency_ms, l.label, l.note, {evidence_ok}, {dismissed}, {contradicted} FROM {CHECKS_TABLE} c "
        f"LEFT JOIN {LABELS_TABLE} l ON l.check_id = c.check_id{join} "
        f"WHERE c.claim_text IS NOT NULL AND c.verdict != 'not_checkable'{where} "
        f"ORDER BY c.created_at DESC, c.check_id DESC LIMIT ?", (limit * 3,)).fetchall()
    names: Dict[str, Optional[str]] = {}
    cards = []
    for (check_id, table, record_id, source, category, claim_text, verdict, refs_json, latency, label, note, ok,
         reason, against) in rows:
        record = load_record(conn, table, record_id, names)
        if record is None:
            continue
        cards.append({
            "check_id": check_id, "speaker": speaker_for_card(record), "speaker_is_owner": bool(record["owner"]),
            "source": source, "date": with_offset(record["at"]),
            "said": " ".join(record["text"].split())[:STATEMENT_CHARS], "claim_text": claim_text, "category": category,
            "verdict": verdict, "cited": list(_cited(conn, refs_json)), "latency_ms": latency, "label": label,
            "note": note, "evidence_ok": None if ok is None else bool(ok),
            "relevance": "dismissed" if reason else ("relevant" if label else None), "dismiss_reason": reason,
            "contradicted": bool(against),
        })
        if len(cards) >= limit:
            break
    return {"cards": cards}


def _cited(conn: sqlite3.Connection, refs_json: Any) -> Iterable[Dict[str, Any]]:
    try:
        refs = json.loads(refs_json or "[]")
    except ValueError:
        return
    for ref in refs if isinstance(refs, list) else []:
        if not isinstance(ref, dict) or not ref.get("table") or not ref.get("record_id"):
            continue
        found = _cited_text(conn, str(ref["table"]), str(ref["record_id"]))
        if found is None:
            continue
        yield {"source": found["source"], "date": ref.get("date"), "role": ref.get("role"),
               "snippet": " ".join(found["text"].split())[:SNIPPET_CHARS]}
