"""verify_claim v2: the owner's own check of a spoken claim against the data on their node.

Design: product-iterations/fun-camera/VERIFY_CLAIM_V2_DESIGN.md. Owner-only (the truth door admits an unlisted truth
type for the owner class only). It does not replace the fun-mode ``verify_claim``, which stays as it is.

Stages, each timed and each with its own outcome:

0. gate      no model: empty, too long, a question, a fragment.
1. read      one model call: the sentence + ClaimContext (prior turns, conversation time, speaker) -> one core
             claim: kind (did_or_made | setup | opinion), subject (owner | someone_else | both), reported speech,
             names, period. ClaimContext interprets; it is never evidence.
2. retrieve  the node's own hybrid retrieval (``DefaultSignalRetrievalAdapter``, summary mode: RRF over vectors, FTS,
             graph, entity threads, facts, raw records) with ``now`` = the conversation time, on the worker's OWN
             connection. Derived facts are followed through their source refs to the records they came from.
             Records dated more than a day after the conversation, aggregate items and records quoting the sentence
             are dropped.
3. qualify   each record's actor role from the node's resolver (``features.provenance.roles.record_role``):
             authored, addressed, participated, observed, ambient; ``derived`` when a fact's source cannot be found.
4. judge     two calls name the shown records that show the claim, in opposite order, run in parallel (the reply
             is the numbers alone: output tokens are the judge's latency). A record counts only if both name it and
             the role and period rules accept it; a record the two split on gets one call on its own.
5. release   owner: verdict, the cited records with role, date and snippet.

Contradictions are not produced (no labelled contradictions to measure against yet). Any model, retrieval or
deadline failure yields ``unable``, never a guess. Nothing on this path is written.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .verify_v2_models import ModelUnavailable, OutputSchema, Usage

VERSION = "verify-claim/v2.0-dev"
MAX_STATEMENT_CHARS = 1000  # the truth-mirror's own statement cap
DEFAULT_DEADLINE_S = 10.0
SHOWN = 10
SNIPPET = 400
KINDS = ("did_or_made", "setup", "opinion", "not_checkable")
WHY_NOT = ("question", "not_about_the_owner", "plan_or_intention", "about_the_call_itself", "fragment", "too_vague")
SUBJECTS = ("owner", "someone_else", "both")
ROLES = ("authored", "addressed", "participated", "observed", "ambient", "derived")
AGGREGATE_LANES = frozenset({"stat_insights", "goals", "topic_clusters"})
#: Search with the claim worded as the owner's own records would word it (first person), not "The owner ...".
QUERY_AS_WRITTEN = True
#: The reader's own cap inside the request deadline.
READ_TIMEOUT_S = 8.0
#: When the two list orders disagree on a record, one more call on that record alone decides it (2 of 3).
TIEBREAK = True
PREFIXED_SOURCES = ("canonical", "entity_thread", "commitment_evidence", "overheard")
_QUESTION = re.compile(r"\?\s*$")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=\S)")
_STOP = frozenset("the a an and or of to in on at for with it this that thing stuff project app code work today yesterday "
                  "tomorrow week month year morning evening night".split())
_GENERIC = frozenset("""project projects app apps application repo repository code codebase interface ui extension feature
    features thing things tool tools website site page chatbot bot data database source sources transcript system setup
    machine model models file files demo version stuff work job open new old local personal initial little big small
    simple cool fun first last other same""".split())
_ENGLISH = frozenset("""a about after all also am an and any are around as at be because been before being both but by can
    could did do does done down during each few from get got had has have he her here him his how i if in into is it its
    just like made make many me more most much my no not now of off on once only or other our out over own really same she
    should so some still such than that the their them then there these they this those through to too under up uses use
    used very was we were what when where which while who why will with would you your wants want something owner""".split())

ROLE_TEXT = {"authored": "written by the owner", "addressed": "an assistant's reply to the owner",
             "participated": "someone else's message in the owner's thread", "observed": "someone else's message",
             "ambient": "observed activity, not anyone's statement", "derived": "a fact derived from the owner's records (source not found)"}

_READ_TEMPLATE = """You prepare what someone just said in a conversation (a sentence or a short passage) for a fact check
against the records of the person who owns them (their coding sessions, GitHub pushes, browser history, journal and
messages). You get the conversation's date, who said it, the turns just before it, and what was said. Call the records'
owner "the owner".

Speech is loose: one turn can hold fillers, asides, a question and several assertions. Take its ONE most specific
checkable assertion (a named thing, a change made, how something is set up, an opinion about a specific thing) and
prepare that. {scope}

"type" - what that assertion says:
- "did_or_made": something someone did, made, changed, adopted, built or worked on (past or ongoing).
- "setup": how the owner's tools, data or system are set up or work now.
- "opinion": an opinion about a specific thing ("that would be cool", "its UI was terrible").
- "not_checkable": nothing said asserts anything checkable: it is only a question, only a future plan or intention,
  only a remark about the call itself happening right now (audio, loading, screen sharing), or a fragment that asserts
  nothing even with context.
"not_checkable_because": for not_checkable, one of the listed reasons; otherwise "".
"subject": who the claim is about: "owner" (the speaker, or their own projects, tools and things), "someone_else"
(another person or group, or their things), or "both".
"reported_speech": true if the sentence reports what someone said rather than stating it directly.
"claim": that one assertion, at most 20 words. Use the earlier turns ONLY to replace "it", "this", "that
project" and similar with the thing meant, and to turn relative dates into absolute ones. Add no other details.
"names": 1 to 4 distinctive names or words from what was said or the earlier turns that identify what the claim is about
(a project, repo, character, tool, product, company, site or person's name). Not generic words like project, app or repo.
"date_from"/"date_to": the dates the claim is about (YYYY-MM-DD), from the conversation's date; "" if none.
"as_written": the claim worded the way the owner's own notes, messages or coding sessions would put it: first person for
the owner's own actions ("I ..."), the same facts and names, plain words, at most 25 words; "" for not_checkable."""

#: The request's ``about_others`` option picks the reader's scope. Off (the default): only what the speaker says about
#: themself (alone or with others) is checked. On: a claim about someone else may be checked against the owner's
#: messages, under the same actor-role rules.
READ_SYSTEM = _READ_TEMPLATE.format(scope=(
    "Take an assertion about the speaker or the speaker's own things: what they did, made, set up, use or think, alone\n"
    "or with others, including their projects, tools and data. Only when everything said is about another person or\n"
    'group is the type "not_checkable" because "not_about_the_owner".'))
READ_SYSTEM_WITH_OTHERS = _READ_TEMPLATE.format(scope=(
    "Prefer an assertion about the speaker themself; take one about someone else only when nothing said is about the\n"
    "speaker. A claim about someone else is still checkable: the owner's messages may show it."))

def _read_schema(why_not) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": list(KINDS)},
            "not_checkable_because": {"type": "string", "enum": [""] + list(why_not)},
            "subject": {"type": "string", "enum": list(SUBJECTS)},
            "reported_speech": {"type": "boolean"},
            "claim": {"type": "string"},
            "names": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
            "date_from": {"type": "string"},
            "date_to": {"type": "string"},
            "as_written": {"type": "string"},
        },
        "required": ["type", "not_checkable_because", "subject", "reported_speech", "claim", "names", "date_from", "date_to",
                     "as_written"],
    }


READ_SCHEMA = _read_schema(WHY_NOT)
READ_SCHEMA_WITH_OTHERS = _read_schema(w for w in WHY_NOT if w != "not_about_the_owner")

LABEL_SYSTEM = """You check excerpts from a person's own records against one claim. Each excerpt is numbered and shows its
source, its date (UTC) and who stands behind it. Label EVERY excerpt:
- "shows": the excerpt itself shows the claim's core is true: the same specific thing, consistent with the claim's time.
  For an opinion, the person's own words express the same opinion about the same thing.
- "related": about the same topic or thing, but it does not show the claim.
- "unrelated": not about the claim.
Being about the same project or tool is "related", not "shows". Return one label per excerpt number."""

LABEL_SCHEMA = {
    "type": "object",
    "properties": {"labels": {"type": "array", "items": {
        "type": "object", "properties": {"n": {"type": "integer"}, "label": {"type": "string", "enum": ["shows", "related", "unrelated"]}},
        "required": ["n", "label"]}}},
    "required": ["labels"],
}

LIST_SYSTEM = """You check excerpts from a person's own records against one claim. Each excerpt is numbered and shows its
source, its date (UTC) and who stands behind it. An excerpt shows the claim when the excerpt itself shows the claim's
core is true: the same specific thing, consistent with the claim's time. For an opinion, the person's own words express
the same opinion about the same thing. Being about the same topic, project or tool is not enough.
Return the numbers of the excerpts that show the claim; an empty list when none does."""

LIST_SCHEMA = OutputSchema({"type": "object", "properties": {"shows": {"type": "array", "items": {"type": "integer"}}}, "required": ["shows"]},
                           plain_json=True, max_tokens=64)

#: How the judge answers. "list": only the numbers of the showing excerpts (~7 output tokens, ~0.6 s a call on Red Pill);
#: "labels": a label for every excerpt (~160 tokens for ten, ~4 s). The model writes about 40 tokens a second, so the
#: reply length is the judge stage's latency; the two shapes agreed on the owner's development rows.
JUDGE_OUTPUT = "list"

#: Legacy table names in fact refs ("iMessage" -> conversation_messages, "Grow Journal" -> journal_entries).
REF_TABLE_ALIASES = {"iMessage": "conversation_messages", "Grow Journal": "journal_entries"}
#: Source tables a fact's refs can be followed into: (primary key, event-time column).
SOURCE_ROWS = {"ai_chat_messages": ("message_id", "event_at"), "journal_entries": ("entry_id", "entry_at"),
               "conversation_messages": ("message_id", "event_at"), "activity_events": ("event_id", "occurred_at")}


@dataclass
class ClaimRequest:
    statement: str
    context: List[Dict[str, str]] = field(default_factory=list)  # [{"speaker", "text"}], oldest first
    said_at: Optional[str] = None                                  # ISO 8601 with offset; None -> now
    speaker: str = "owner"
    about_others: bool = False  # also check claims about someone else (see READ_SYSTEM_WITH_OTHERS)
    effort: str = "high"
    deadline_s: float = DEFAULT_DEADLINE_S


@dataclass
class Claim:
    kind: str
    subject: str
    reported_speech: bool
    text: str
    names: List[str]
    date_from: str
    date_to: str
    as_written: str = ""


# --- small pure helpers --------------------------------------------------------------------------------------------
def _day(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def conversation_time(said_at: Optional[str]) -> datetime:
    if said_at:
        try:
            t = datetime.fromisoformat(str(said_at).replace("Z", "+00:00"))
            return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def gate(statement: str) -> Optional[str]:
    s = (statement or "").strip()
    if not s:
        return "empty"
    if len(s) > MAX_STATEMENT_CHARS:
        return "too_long"
    if _QUESTION.search(s) and not _SENTENCE_BREAK.search(s):  # a passage that ends on a question may still assert
        return "question"
    if len(s.split()) < 4:
        return "fragment"
    return None


def clean_names(names: Any) -> List[str]:
    out: List[str] = []
    for n in names if isinstance(names, list) else []:
        n = re.sub(r"^(the|this|that|these|those|my|our|his|her|their|a|an)\s+", "", " ".join(str(n).split()), flags=re.I)
        if not re.fullmatch(r"[\w .:/@#+-]{2,40}", n):
            continue
        if all(w.lower() in _GENERIC or w.lower() in _STOP for w in n.split()):
            continue
        if n.lower() not in (x.lower() for x in out):
            out.append(n)
    return out[:4]


def claim_words(text: str) -> List[str]:
    out: List[str] = []
    for w in re.findall(r"[A-Za-z][\w'-]{2,}", text or ""):
        if w.lower() not in _STOP and w.lower() not in _ENGLISH and w.lower() not in (x.lower() for x in out):
            out.append(w)
    return out[:8]


def ngrams(text: str, n: int = 6) -> set:
    words = re.findall(r"[a-z0-9']+", (text or "").lower())
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def window_of(claim: Claim) -> Optional[Tuple[date, date]]:
    lo, hi = _day(claim.date_from), _day(claim.date_to) or _day(claim.date_from)
    if lo and hi and hi < lo:
        lo, hi = hi, lo
    return (lo - timedelta(days=1), hi + timedelta(days=1)) if lo and hi else None


def accept(role: str, record_day: Optional[date], claim: Claim, window, authored_agreed: bool) -> Tuple[bool, str]:
    """Actor-role and period rules for a record both judge calls labelled "shows"."""
    if role == "derived" and claim.kind == "opinion":
        return False, "opinion_not_authored"
    if claim.subject == "someone_else":
        if role not in ("authored", "participated", "observed"):
            return False, "role_not_allowed"
    elif claim.kind == "opinion":
        if role != "authored":
            return False, "opinion_not_authored"
    else:
        if role in ("participated", "observed"):
            return False, "role_not_allowed"
        if role in ("addressed", "derived") and not authored_agreed:
            return False, f"{role}_without_authored"
    if window and not (record_day and window[0] <= record_day <= window[1]):
        return False, "outside_period"
    return True, "accepted"


def retrieval_query(claim: "Claim") -> str:
    """The claim with its period spelled out, so the node's planner applies its own time window instead of the
    period being filtered afterwards (measured 22 Oct: 25/29 items in the period with the date, 0/39 without)."""
    q = (claim.as_written.strip() if QUERY_AS_WRITTEN else "") or claim.text.strip()
    lo, hi = claim.date_from.strip(), claim.date_to.strip()
    if lo and lo not in q:
        q = q.rstrip(".") + (f" between {lo} and {hi}." if hi and hi != lo else f" on {lo}.")
    return q


# --- stage 1: read --------------------------------------------------------------------------------------------------
def read_claim(req: ClaimRequest, chat: Callable, usage: Usage, timeout: float) -> Tuple[Optional[Claim], Optional[str]]:
    turns = "\n".join((f"{t['speaker']}: " if t.get("speaker") else "- ") + str(t.get("text") or "")
                      for t in req.context[-6:]) or "(none)"
    user = (f"Conversation date: {conversation_time(req.said_at).date().isoformat()}\nSaid by: {req.speaker}\n\n"
            f"Earlier turns:\n{turns}\n\nWhat was said: {req.statement.strip()}")
    system, schema = (READ_SYSTEM_WITH_OTHERS, READ_SCHEMA_WITH_OTHERS) if req.about_others else (READ_SYSTEM, READ_SCHEMA)
    out = chat(system, user, schema, timeout, usage)
    kind = out.get("type") if out.get("type") in KINDS else "not_checkable"
    if kind == "not_checkable":
        why = out.get("not_checkable_because")
        return None, why if why in WHY_NOT else "too_vague"
    subject = out.get("subject") if out.get("subject") in SUBJECTS else "owner"
    if subject == "someone_else" and not req.about_others:  # the option holds even when the model strays
        return None, "not_about_the_owner"
    return Claim(kind=kind, subject=subject, reported_speech=out.get("reported_speech") is True,
                 text=str(out.get("claim") or req.statement)[:300], names=clean_names(out.get("names")),
                 date_from=str(out.get("date_from") or ""), date_to=str(out.get("date_to") or ""),
                 as_written=str(out.get("as_written") or "")[:300]), None


# --- stages 2-3: retrieve and qualify (inside a DB worker, on its own connection) -----------------------------------
class NodeEvidence:
    """The node's own hybrid retrieval plus fact-to-source expansion and actor roles. Build it INSIDE the worker."""

    def __init__(self, conn):
        from ..features.provenance import roles
        from . import retrieval as R
        from .manifest import ScopeResolutionManifest
        from .narrowing import NarrowingLedger
        from .runtime import adapter_bundle_for_conn
        from .types import RetrievalRequest

        self.conn, self.roles = conn, roles
        self._Request, self._Ledger = RetrievalRequest, NarrowingLedger
        self._table_of = getattr(R, "_RECORD_TYPE_TABLE", {}) or {}
        self.adapter = R.DefaultSignalRetrievalAdapter(adapter_bundle_for_conn(conn))
        self.manifest = ScopeResolutionManifest(
            scope_id="owner_all:verify", primary_dimensions=["Interests", "Memory", "Places", "Profile", "Relationships",
                                                             "Resources", "Time", "Wellbeing", "Work"],
            signal_objects=["user_goals", "message_emotions", "fact"],
            canonical_tables=["activity_events", "ai_chat_messages", "calendar_events", "contact_identifiers", "contacts",
                              "conversation_messages", "entities", "entity_edges", "financial_transactions", "journal_entries",
                              "location_events", "transcript_segments"], access_mode_ceiling="raw")

    def _table(self, item):
        pre, _, tbl = str(item.get("retrieval_source") or "").partition(":")
        if pre in PREFIXED_SOURCES and tbl:
            return tbl
        return self._table_of.get(item.get("record_type")) or item.get("canonical_table") or ""

    def _row(self, table, rid):
        if table not in SOURCE_ROWS or not rid:
            return None
        key, _ = SOURCE_ROWS[table]
        try:
            cur = self.conn.execute(f"SELECT * FROM {table} WHERE {key} = ?", (rid,))
        except sqlite3.OperationalError:  # a source table this database does not have: skip the ref
            return None
        row = cur.fetchone()
        return dict(zip([d[0] for d in cur.description], row)) if row else None

    def _record(self, table, rid, lanes, lane):
        row = self._row(table, rid)
        if row is None:
            return None
        text = row.get("content") or " ".join(str(row.get(k) or "") for k in ("title", "url"))
        context = ""
        if table == "ai_chat_messages":  # the session's title often names the project the speech only alludes to
            try:
                context = str((json.loads(row.get("metadata_json") or "{}").get("_metadata") or {}).get("conversation_title") or "")[:120]
            except (ValueError, AttributeError):
                context = ""
        return {"table": table, "record_id": str(rid), "event_at": str(row.get(SOURCE_ROWS[table][1]) or ""),
                "role": self.roles.record_role(row, table=table), "lanes": list(lanes), "lane": lane, "text": str(text or ""),
                "context": context}

    def _expand_fact(self, item):
        row = self.conn.execute("SELECT source_refs_json FROM signal_objects WHERE object_id = ?", (item.get("record_id"),)).fetchone()
        try:
            refs = json.loads(row[0]) if row and row[0] else []
        except ValueError:
            refs = []
        out = []
        for ref in refs if isinstance(refs, list) else []:
            ref = ref if isinstance(ref, dict) else {}
            # Extractors write the source key as "id" or as "record_id"; two legacy table names are aliases.
            table = REF_TABLE_ALIASES.get(ref.get("table"), ref.get("table"))
            rid = ref.get("id") if ref.get("id") is not None else ref.get("record_id")
            if table == "entity_mentions" and rid:
                m = self.conn.execute("SELECT canonical_table, record_id FROM entity_mentions WHERE mention_id = ?", (rid,)).fetchone()
                table, rid = (m[0], m[1]) if m else (None, None)
            got = self._record(table, rid, item.get("fusion_sources") or [], "via_fact") if table in SOURCE_ROWS else None
            if got:
                out.append(got)
        return out

    def gather(self, query: str, needles: str, now: datetime) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        ledger = self._Ledger()
        req = self._Request(manifest=self.manifest, access_mode="summary", query_text=query, needle_text=needles or None,
                            disclosure_tier="owner_raw", owner_mode=True, packet_resolution="facts_all", now=now, ledger=ledger)
        from ..features.entities.linking import MATERIALIZE_MISSING_DOSSIERS

        token = MATERIALIZE_MISSING_DOSSIERS.set(False)  # a read: never materialise a dossier on this path
        try:
            packet = self.adapter.retrieve(req).context_packet
        finally:
            MATERIALIZE_MISSING_DOSSIERS.reset(token)
        packet = packet if isinstance(packet, dict) else {}
        stats = {"candidates": 0, "aggregates_skipped": 0, "facts_expanded": 0, "derived": 0}
        records: List[Dict[str, Any]] = []
        for lane in ("summaries", "semantic_hits"):
            for it in packet.get(lane) or []:
                if not isinstance(it, dict):
                    continue
                stats["candidates"] += 1
                lanes = list(it.get("fusion_sources") or [])
                table = self._table(it)
                if not table and set(lanes) <= AGGREGATE_LANES:
                    stats["aggregates_skipped"] += 1
                    continue
                if not table and "facts_store" in lanes:
                    srcs = self._expand_fact(it)
                    if srcs:
                        stats["facts_expanded"] += 1
                        records.extend(srcs)
                        continue
                if table in SOURCE_ROWS:
                    rec = self._record(table, it.get("record_id"), lanes, lane)
                    if rec:
                        if it.get("speaker_role") in self.roles.ROLES:
                            rec["role"] = it["speaker_role"]
                        records.append(rec)
                        continue
                text = str(it.get("summary_text") or it.get("topic") or "")
                if text:
                    stats["derived"] += 1
                    records.append({"table": table or "derived", "record_id": str(it.get("record_id") or it.get("entity_id") or ""),
                                    "event_at": str(it.get("event_at") or ""), "role": "derived", "lanes": lanes, "lane": lane, "text": text,
                                    "origin": str(it.get("object_type") or it.get("retrieval_source") or "")[:40]})
        return records, stats


def select_evidence(records, sentence: str, claim: Claim, now: datetime):
    """As of the conversation; no quoting records; one per source record; first SHOWN in retrieval order."""
    cutoff = (now + timedelta(days=1)).date()
    grams = ngrams(sentence)
    seen, kept, dropped = set(), [], {"as_of": 0, "quoting": 0}
    for r in records:
        key = (r["table"], r["record_id"])
        if key in seen:
            continue
        seen.add(key)
        d = _day(r["event_at"])
        if d and d > cutoff:
            dropped["as_of"] += 1
            continue
        if grams and ngrams(r["text"]) & grams:
            dropped["quoting"] += 1
            continue
        kept.append(r)
    words = claim.names + claim_words(claim.text)
    shown = []
    for i, r in enumerate(kept[:SHOWN]):
        text = " ".join(r["text"].split())
        low = text.lower()
        hits = [low.find(w.lower()) for w in words if w and low.find(w.lower()) >= 0]
        start = max(0, min(hits) - SNIPPET // 4) if hits else 0
        shown.append({"n": i + 1, "ref": f"{r['table']}:{r['record_id']}", "source": r["table"], "date": r["event_at"][:10],
                      "role": r["role"] if r["role"] in ROLES else "derived", "lane": r["lane"], "snippet": text[start:start + SNIPPET],
                      "context": r.get("context") or "", "origin": r.get("origin") or r["table"]})
    return shown, dropped


# --- stage 4: judge -------------------------------------------------------------------------------------------------
def judge(claim: Claim, evidence, chat, usage: Usage, timeout: float) -> Dict[str, Any]:
    """One judge (a chat callable) or several (a sequence of them). With several, each runs its own opposite-order pair
    in parallel and a record counts when any judge's own pair agrees and the rules accept it: different models miss
    different records, so the union finds more without loosening any one judge's agreement rule."""
    judges = list(chat) if isinstance(chat, (list, tuple)) else [chat]
    if len(judges) == 1:
        return _judge_one(claim, evidence, judges[0], usage, timeout)
    results, failures = [], []
    with ThreadPoolExecutor(max_workers=len(judges)) as pool:
        futures = [pool.submit(_judge_one, claim, evidence, j, usage, timeout) for j in judges]
        for f in futures:
            try:
                results.append(f.result())
            except ModelUnavailable as exc:
                failures.append(exc)
    if not results:  # every judge failed: refuse, never guess
        raise failures[0]
    cites = sorted(set().union(*(r["cites"] for r in results)))
    agreed = sorted(set().union(*(r["agreed_ns"] for r in results)))
    reasons = {r["reason"] for r in results}
    reason = None if cites else next(x for x in ("shows_failed_rules", "judges_disagree", "no_record_shows_it") if x in reasons)
    return {"cites": cites, "agreed_ns": agreed, "agreed": len(agreed), "tiebroken": sum(r["tiebroken"] for r in results),
            "rejections": sorted(x for r in results for x in r["rejections"]), "reason": reason,
            "judges": len(judges), "judges_failed": len(failures), "judge_cites": [len(r["cites"]) for r in results]}


def judge_prompt() -> Tuple[str, Dict[str, Any]]:
    return (LIST_SYSTEM, LIST_SCHEMA) if JUDGE_OUTPUT == "list" else (LABEL_SYSTEM, LABEL_SCHEMA)


def shows_in(reply: Dict[str, Any]) -> set:
    """The excerpt numbers a judge reply marks as showing the claim, in either answer shape."""
    out = {x.get("n") for x in (reply.get("labels") or []) if isinstance(x, dict) and x.get("label") == "shows"}
    for x in reply.get("shows") or []:
        if isinstance(x, bool):
            continue
        if isinstance(x, int) or (isinstance(x, str) and x.strip().isdigit()):
            out.add(int(x))
    return out


def _judge_one(claim: Claim, evidence, chat: Callable, usage: Usage, timeout: float) -> Dict[str, Any]:
    system, schema = judge_prompt()
    when = f"{claim.date_from} to {claim.date_to}" if claim.date_from else "not stated"
    about = "someone other than the owner" if claim.subject == "someone_else" else "the owner"
    head = f"Claim ({claim.kind}, about {about}): {claim.text}\nTime: {when}\n\nExcerpts:\n"
    line = lambda e: (f"[{e['n']}] {e['source']}" + (f" (session: {e['context']})" if e.get("context") else "")
                      + f", {e['date'] or 'undated'}, {ROLE_TEXT[e['role']]}: {e['snippet']}")
    forward = head + "\n".join(line(e) for e in evidence)
    backward = head + "\n".join(line(e) for e in reversed(evidence))
    ends = time.perf_counter() + timeout
    with ThreadPoolExecutor(max_workers=2) as pool:  # the two orders in parallel: half the judge latency
        f1 = pool.submit(chat, system, forward, schema, timeout, usage)
        f2 = pool.submit(chat, system, backward, schema, timeout, usage)
        first, second = f1.result(), f2.result()
    shows = shows_in
    valid = lambda ns: {n for n in ns if isinstance(n, int) and 1 <= n <= len(evidence)}
    by_n = {e["n"]: e for e in evidence}
    agreed_set, split = valid(shows(first) & shows(second)), sorted(valid(shows(first) ^ shows(second)))
    tiebroken = 0
    if TIEBREAK and split and ends - time.perf_counter() > 1.0:
        def alone(n):  # the record on its own, so its place in a list cannot decide it
            try:
                return 1 in shows(chat(system, head + line({**by_n[n], "n": 1}), schema, ends - time.perf_counter(), usage))
            except ModelUnavailable:  # no third opinion: the record stays split, which never counts
                return False
        with ThreadPoolExecutor(max_workers=min(4, len(split))) as pool:
            for n, ok in zip(split, pool.map(alone, split)):
                if ok:
                    agreed_set.add(n)
                    tiebroken += 1
    agreed = sorted(agreed_set)
    window = window_of(claim)
    authored_ok = any(accept("authored", _day(by_n[n]["date"]), claim, window, False)[0] for n in agreed if by_n[n]["role"] == "authored")
    verdicts = {n: accept(by_n[n]["role"], _day(by_n[n]["date"]), claim, window, authored_ok) for n in agreed}
    cites = sorted(n for n, (ok, _) in verdicts.items() if ok)
    if cites:
        reason = None
    elif agreed:
        reason = "shows_failed_rules"
    elif split:
        reason = "judges_disagree"
    else:
        reason = "no_record_shows_it"
    return {"cites": cites, "agreed_ns": agreed, "agreed": len(agreed), "tiebroken": tiebroken, "rejections": sorted(r for ok, r in verdicts.values() if not ok), "reason": reason}


# --- orchestration --------------------------------------------------------------------------------------------------
def unable(reason: str, out: Dict[str, Any]) -> Dict[str, Any]:
    return {**out, "verdict": "unable", "reason": reason}


def respond(out: Dict[str, Any], started: float, usage: Usage) -> Dict[str, Any]:
    out["timings_ms"]["total"] = round((time.perf_counter() - started) * 1000, 1)
    out["usage"] = {"model_calls": usage.calls, "prompt_tokens": usage.prompt_tokens, "output_tokens": usage.output_tokens}
    return out


async def verify_owner_claim(req: ClaimRequest, *, chat, gather_in_worker, to_thread, judges=None) -> Dict[str, Any]:
    """Run the stages under one deadline. ``gather_in_worker(fn)`` runs ``fn(conn)`` on a DB worker's own connection
    (the node passes ``run_db_read``); ``to_thread(fn, *a)`` runs a blocking model call off the loop. ``chat`` reads the
    claim; ``judges`` (default: ``chat`` alone) labels the evidence."""
    started = time.perf_counter()
    deadline = started + max(0.5, float(req.deadline_s or DEFAULT_DEADLINE_S))
    left = lambda: deadline - time.perf_counter()
    usage = Usage()
    out: Dict[str, Any] = {"version": VERSION, "model": getattr(chat, "name", "custom"), "verdict": None, "reason": None,
                           "claim": None, "evidence": [], "cited_roles": [], "stages": {}, "timings_ms": {}}
    t = time.perf_counter()
    refused = gate(req.statement)
    out["timings_ms"]["gate"] = round((time.perf_counter() - t) * 1000, 2)
    if refused:
        out["stages"]["gate"] = refused
        return respond(unable(f"not_checkable:{refused}", out), started, usage)
    out["stages"]["gate"] = "passed"
    if req.effort == "low":
        return respond(unable("effort_low", out), started, usage)
    try:
        t = time.perf_counter()
        claim, why = await to_thread(read_claim, req, chat, usage, min(READ_TIMEOUT_S, left()))
        out["timings_ms"]["read"] = round((time.perf_counter() - t) * 1000, 1)
        if claim is None:
            out["stages"]["read"] = f"not_checkable:{why}"
            return respond(unable(f"not_checkable:{why}", out), started, usage)
        out["stages"]["read"] = "ok"
        out["claim"] = {"kind": claim.kind, "subject": claim.subject, "reported_speech": claim.reported_speech,
                        "text": claim.text, "period": {"from": claim.date_from, "to": claim.date_to}}
        if left() <= 1.0:
            return respond(unable("deadline", out), started, usage)
        t = time.perf_counter()
        now = conversation_time(req.said_at)
        records, stats = await gather_in_worker(lambda conn: NodeEvidence(conn).gather(retrieval_query(claim), " ".join(claim.names), now))
        evidence, dropped = select_evidence(records, req.statement, claim, now)
        out["timings_ms"]["retrieve"] = round((time.perf_counter() - t) * 1000, 1)
        out["stages"]["retrieve"] = {**stats, **{f"dropped_{k}": v for k, v in dropped.items()}, "shown": len(evidence)}
        if not evidence:
            return respond({**out, "verdict": "no_evidence", "reason": "no_records"}, started, usage)
        if left() <= 1.0:
            return respond(unable("deadline", out), started, usage)
        t = time.perf_counter()
        verdict = await to_thread(judge, claim, evidence, judges or chat, usage, left())
        out["timings_ms"]["judge"] = round((time.perf_counter() - t) * 1000, 1)
        out["stages"]["judge"] = {"agreed": verdict["agreed"], "tiebroken": verdict["tiebroken"], "rejections": verdict["rejections"],
                                  "judges": verdict.get("judges", 1), "judges_failed": verdict.get("judges_failed", 0)}
        cited = set(verdict["cites"])
        agreed = set(verdict["agreed_ns"])
        out["evidence"] = [{**e, "cited": e["n"] in cited, "agreed": e["n"] in agreed} for e in evidence]
        out["cited_roles"] = [e["role"] for e in evidence if e["n"] in cited]
        if cited:
            return respond({**out, "verdict": "supported"}, started, usage)
        return respond({**out, "verdict": "no_evidence", "reason": verdict["reason"]}, started, usage)
    except ModelUnavailable as exc:
        return respond(unable(f"model_unavailable:{exc}", out), started, usage)
    except Exception as exc:  # noqa: BLE001 — retrieval or parse trouble refuses, never guesses
        return respond(unable(f"operational:{type(exc).__name__}", out), started, usage)
