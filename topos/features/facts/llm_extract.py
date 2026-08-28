"""Role-gated LLM fact extraction (B4 / PLAN_PROVENANCE_SPLIT P4.3).

The rules-only path (``extract.py``) is the FLOOR: it fires on a handful of
strict first-person patterns, so it misses paraphrased or multi-hop owner
facts ("been vegetarian since college", "switched from tea to cold brew this
year"). This module layers an LLM extraction pass ON TOP of the rules floor —
purely additive, because ``FactStore.assert_fact`` de-dupes on
``object_key`` (same predicate+object refreshes rather than duplicating).

Three things make this safe to run at ingest:

  1. ROLE GATE (the load-bearing invariant). We only ask the LLM about rows the
     owner is entitled to speak *for*:
       - ``authored``  → owner belief. asserted_by='owner'. subject = owner.
       - ``addressed`` → an ATTRIBUTED claim (an assistant reply in the owner's
                         AI chat, or a contact's DM to the owner). asserted_by
                         is the actual speaker ('assistant' or 'contact:<id>'),
                         NEVER 'owner'. It is retrievable memory, never an owner
                         belief.
     Everything else — ``participated`` / ``observed`` / ``ambient`` — is
     SKIPPED entirely. A witnessed "I'm deathly allergic to shellfish" from
     another sender therefore yields ZERO owner facts even with the LLM on
     (safety-critical A1 class). The gate reuses ``provenance.roles`` +
     ``provenance.posture`` so the per-connector ambient-posture cap applies:
     an ambient-flagged source can never mint a belief here.

  2. GRACEFUL DEGRADATION. The real Ollama call is wrapped; if the server is
     unreachable (or any single row errors) we log and continue. The pass
     returns the count it managed to write and NEVER raises into ingestion —
     the rules floor already ran.

  3. INCREMENTAL PERSISTENCE. Each completed row is written (and progress-marked)
     immediately — not held until the whole batch finishes — so a multi-hour
     pass survives Ctrl+C and resumes without re-paying Ollama for finished rows.

Injecting ``extractor`` bypasses Ollama entirely (unit tests, offline CI):
it is a callable ``(prompt: str, row: dict) -> list[dict]`` returning parsed
SPO-triple dicts (see ``TRIPLE`` shape below). When ``extractor`` is None and
Ollama is configured/reachable, the real model is used.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..provenance.posture import make_posture_resolver
from ..provenance.roles import (
    ROLE_ADDRESSED,
    ROLE_AUTHORED,
    record_role,
)
from .store import KNOWN_PREDICATES, FactStore, normalize_predicate

logger = logging.getLogger("topos.features.facts.llm_extract")

# ---------------------------------------------------------------------------
# Public contract for the density-lane sibling / tests
# ---------------------------------------------------------------------------
#
# EXTRACTOR interface (the injectable ``extractor`` param):
#
#     def extractor(prompt: str, row: dict) -> list[dict]:
#         '''Return a list of triple dicts about the owner. NO network in tests.'''
#
#   - ``prompt`` is the fully-rendered owner-only extraction prompt (build it
#     with ``build_extraction_prompt(content)`` if you need the real text; test
#     stubs may ignore it and key off ``row['content']``).
#   - ``row`` is the canonical row being processed (so a stub can branch on
#     content). Return [] to extract nothing.
#
# TRIPLE dict shape (one per extracted fact; ``parse_triples`` also emits this
# shape from raw model text):
#
#     {
#         "predicate": str,              # REQUIRED. normalized to KNOWN_PREDICATES
#                                        #   vocab where possible; unknown preds
#                                        #   are normalized (snake_case) & kept.
#         "object": str,                 # REQUIRED. the object value.
#         "period_start": str | None,    # OPTIONAL. e.g. "2019" or ISO date.
#         "period_end": str | None,      # OPTIONAL.
#         "confidence": float | None,    # OPTIONAL. defaults to DEFAULT_CONFIDENCE.
#         "subject": str | None,         # OPTIONAL. "owner"/"me"/"i" (or absent)
#                                        #   => owner-subject, kept. Any other
#                                        #   subject => DROPPED (not an owner fact).
#     }
#
# The subject is ALWAYS coerced to the owner entity before writing; a triple
# whose subject names someone other than the owner is dropped (never
# re-attributed). period_start/end flow into FactStore.assert_fact untouched.
# ---------------------------------------------------------------------------

DEFAULT_CONFIDENCE = 0.55  # below resume/profile facts so a chat remark never
# silently supersedes a strong incumbent (CONFLICT_CONFIDENCE_MARGIN=0.10).
_MAX_CONTENT_CHARS = 4000  # keep the prompt bounded; ingest is slow-tolerant.
_NUM_PREDICT = 512  # bounded output tokens for the JSON array.

# Bounded concurrency for the (slow, ~25s each) ollama calls. Mirrors
# GoalExtractionJob.GOAL_BATCH_CONCURRENCY's semaphore approach — the LLM calls
# fan out under a semaphore, and each completed row is written to sqlite
# immediately on the event-loop thread (one connection is not threadsafe).
# Env-overridable so a big re-enrichment can be tuned to the box; kept modest
# so we never starve concurrent LongMemEval / query traffic on the same ollama.
FACTS_LLM_CONCURRENCY = max(1, int(os.environ.get("TOPOS_FACTS_LLM_CONCURRENCY", "4")))
# Bound in-flight Ollama waits so Ctrl+C / shutdown cannot stall for the adapter
# default (300s) once cooperative cancel has stopped scheduling new rows.
FACTS_LLM_HTTP_TIMEOUT = max(
    5.0, float(os.environ.get("TOPOS_FACTS_LLM_HTTP_TIMEOUT", "60"))
)

# Per-record progress markers in extraction_artifacts so a multi-hour fact_llm
# pass can resume after Ctrl+C without re-paying Ollama for finished rows
# (including successful empty extractions).
_FACT_LLM_PROGRESS_TYPE = "fact_llm_pass"
_FACT_LLM_PROGRESS_VERSION = "fact_llm_v1"

# Subject tokens that mean "the owner" (first person). Anything else is a
# third party and its triple is dropped — we never mint an owner fact about
# someone else, nor re-attribute someone else's fact to the owner.
_OWNER_SUBJECT_TOKENS = frozenset({"", "owner", "me", "i", "myself", "self", "the owner"})


# ---------------------------------------------------------------------------
# Flag resolution
# ---------------------------------------------------------------------------

_DISABLED_TOKENS = frozenset({"0", "false", "off", "no"})


def _resolved_extraction_model(settings: Any, conn: Any = None) -> str:
    """The model B4 would use. Owner-selectable, first non-empty wins:
    engine_config device override (when ``conn`` is given) → env
    TOPOS_FACTS_LLM_MODEL → ollama_extraction_model → ollama_query_model."""
    from ...config.facts_llm import resolve_facts_llm_model

    return resolve_facts_llm_model(settings, conn)


def _resolved_extraction_request(settings: Any, conn: Any = None) -> tuple[str, str]:
    """(provider, model) B4 would use — provider from the device override,
    ollama when unset (the only pre-provider behavior)."""
    from ...config.facts_llm import resolve_facts_llm_request

    return resolve_facts_llm_request(settings, conn)


def facts_llm_enabled(settings: Any = None, conn: Any = None) -> bool:
    """Is the additive LLM fact pass ON?

    Tri-state ``TOPOS_FACTS_LLM`` (Settings.topos_facts_llm):
      - explicit disable ("0"/"off"/"false"/"no") ⇒ OFF regardless of model.
      - explicit enable (anything else truthy)     ⇒ ON.
      - unset ⇒ AUTO: ON when a non-empty extraction model resolves.

    NB: "enabled" ≠ "will produce facts". In offline CI the flag can be ON but
    the pass is INERT unless an ``extractor`` is injected — the real Ollama
    call degrades to a no-op when the server is unreachable.
    """
    if settings is None:
        from ...config.settings import settings as _settings

        settings = _settings
    flag = getattr(settings, "topos_facts_llm", None)
    if flag is not None:
        token = str(flag).strip().lower()
        if token in _DISABLED_TOKENS:
            return False
        if token:
            return True
        # empty string ⇒ fall through to auto
    # AUTO: on when a model resolves — EXCEPT under pytest, where hitting the
    # real Ollama from extract_facts_from_batch would be non-deterministic and
    # forbidden (contract). Tests must opt in explicitly (TOPOS_FACTS_LLM=1) or
    # call extract_owner_facts_llm(..., extractor=...) directly with a stub.
    import os

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    return bool(_resolved_extraction_request(settings, conn)[1])


# ---------------------------------------------------------------------------
# Prompt + parser (real Ollama path). Deterministic, temp 0.
# ---------------------------------------------------------------------------

_PREDICATE_HINT = ", ".join(sorted(KNOWN_PREDICATES))

_PROMPT_TEMPLATE = (
    "You extract atomic facts about the SPEAKER (the first-person \"I\") from a "
    "single message. The subject of every fact is the speaker.\n"
    "Return ONLY a JSON array. Each element: "
    '{{"predicate": <string>, "object": <string>, "period_start": <string|null>, '
    '"period_end": <string|null>}}.\n'
    "Rules:\n"
    "- Prefer these predicates when they fit: {predicates}. Otherwise use a short "
    "snake_case predicate.\n"
    "- object is the concrete value (a place, org, activity, preference, etc.).\n"
    "- Only include facts the speaker states about THEMSELVES. Skip facts about "
    "other people, questions, opinions about the world, and small talk.\n"
    "- period_start/period_end: fill only when the message dates the fact "
    '(e.g. "since 2019" -> period_start "2019"). Otherwise null.\n'
    "- If there are no first-person facts, return [].\n"
    "Message:\n"
    "{content}\n"
    "JSON array:"
)


def build_extraction_prompt(content: str) -> str:
    text = str(content or "")
    if len(text) > _MAX_CONTENT_CHARS:
        text = text[:_MAX_CONTENT_CHARS]
    return _PROMPT_TEMPLATE.format(predicates=_PREDICATE_HINT, content=text)


# A rigid fallback line format tolerated by the parser when the model does not
# emit clean JSON: ``predicate | object | period_start | period_end`` (last two
# optional). Robustness over strictness: junk lines are dropped, not fatal.
_LINE_SPLIT = re.compile(r"\s*\|\s*")
_JSON_ARRAY = re.compile(r"\[.*\]", re.S)


def _coerce_triple(obj: Any) -> Optional[Dict[str, Any]]:
    """One raw dict -> a normalized TRIPLE dict, or None to drop it."""
    if not isinstance(obj, dict):
        return None
    predicate = obj.get("predicate") or obj.get("pred") or obj.get("relation")
    # object is a Python builtin name in the payload; accept common aliases.
    value = obj.get("object")
    if value is None:
        value = obj.get("object_value") or obj.get("value") or obj.get("obj")
    predicate = str(predicate or "").strip()
    value = str(value or "").strip()
    if not predicate or not value:
        return None
    triple: Dict[str, Any] = {
        "predicate": predicate,
        "object": value,
    }
    subject = obj.get("subject")
    if subject is not None:
        triple["subject"] = str(subject).strip()
    for key in ("period_start", "period_end"):
        raw = obj.get(key)
        if raw not in (None, "", "null"):
            triple[key] = str(raw).strip()
    conf = obj.get("confidence")
    if conf is not None:
        try:
            triple["confidence"] = float(conf)
        except (TypeError, ValueError):
            pass
    return triple


def parse_triples(raw: str) -> List[Dict[str, Any]]:
    """Best-effort parse of model output into TRIPLE dicts.

    Tolerates: a clean JSON array, a JSON array embedded in prose / fenced code,
    a single JSON object, and a rigid ``pred | object | start | end`` line
    format. Anything unparseable is dropped (never raises).
    """
    text = str(raw or "").strip()
    if not text:
        return []
    out: List[Dict[str, Any]] = []

    # 1) Try to find and parse a JSON array anywhere in the text.
    candidates: List[Any] = []
    match = _JSON_ARRAY.search(text)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, list):
                candidates = parsed
        except (json.JSONDecodeError, ValueError):
            candidates = []
    # 2) Fall back to a single top-level JSON object.
    if not candidates:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                candidates = [parsed]
            elif isinstance(parsed, list):
                candidates = parsed
        except (json.JSONDecodeError, ValueError):
            candidates = []

    if candidates:
        for item in candidates:
            triple = _coerce_triple(item)
            if triple is not None:
                out.append(triple)
        return out

    # 3) Rigid pipe-delimited line format.
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "//")):
            continue
        parts = _LINE_SPLIT.split(line)
        if len(parts) < 2:
            continue
        obj: Dict[str, Any] = {"predicate": parts[0], "object": parts[1]}
        if len(parts) >= 3 and parts[2]:
            obj["period_start"] = parts[2]
        if len(parts) >= 4 and parts[3]:
            obj["period_end"] = parts[3]
        triple = _coerce_triple(obj)
        if triple is not None:
            out.append(triple)
    return out


def _is_owner_subject(triple: Dict[str, Any]) -> bool:
    subject = str(triple.get("subject") or "").strip().lower()
    return subject in _OWNER_SUBJECT_TOKENS


# ---------------------------------------------------------------------------
# Durable-fact pre-filter (skips rows that cannot carry an owner self-fact
# BEFORE spending a ~25s LLM call). CONSERVATIVE by construction: a false
# negative here only costs one wasted LLM call (the row is processed anyway),
# but a false positive would drop a real fact — so every rule below fires only
# on content that is *unambiguously* devoid of a durable self-fact (pure
# greeting/ack, bare logistics, or link-only). Role gate / attribution are
# untouched: this runs AFTER eligibility is decided, purely to save an LLM call.
# ---------------------------------------------------------------------------

_MIN_FACT_CONTENT_CHARS = 15  # below this, no room for a durable SPO self-fact.

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)

# Exact-match throwaways: greetings, acknowledgements, and one-liners that never
# state a durable fact. Matched against the fully-normalized content (lowercased,
# stripped of trailing punctuation/emoji whitespace). Kept SHORT and literal so a
# fact-bearing sentence that merely *starts* with "ok" ("ok so I moved to Berlin")
# is never skipped — only content that IS one of these, whole, is dropped.
_GREETING_ACK_PHRASES = frozenset(
    {
        "hi", "hii", "hey", "heyy", "hello", "helloo", "yo", "sup", "hiya",
        "gm", "good morning", "good afternoon", "good evening", "good night",
        "gn", "night", "morning", "afternoon", "evening",
        "ok", "okay", "k", "kk", "okey", "okey dokey", "cool", "nice", "great",
        "sure", "yep", "yup", "yeah", "yes", "no", "nope", "nah", "yea",
        "thanks", "thank you", "thanks!", "thx", "ty", "tysm", "np", "no problem",
        "you're welcome", "yw", "welcome", "cheers", "ok cool", "sounds good",
        "sg", "got it", "gotcha", "roger", "understood", "makes sense",
        "lol", "lmao", "haha", "hahaha", "hah", "hehe", "nice one", "amazing",
        "bye", "goodbye", "cya", "see you", "see ya", "later", "ttyl", "take care",
        "on my way", "omw", "be right there", "brb", "one sec", "one moment",
        "hold on", "coming", "here", "im here", "i'm here", "almost there",
        "are you around", "you around", "you there", "u there", "you free",
        "u free", "you up", "u up", "wyd", "what's up", "whats up", "how are you",
        "how's it going", "hows it going", "how are things", "wassup",
        "sorry", "my bad", "oops", "np np", "all good", "no worries", "np!",
        "please", "pls", "plz", "asap", "done", "finished", "ready", "wait",
        "hmm", "hm", "huh", "what", "why", "when", "where", "who", "how",
        "really", "wow", "omg", "oh", "ah", "ahh", "oh ok", "oh okay", "i see",
        "will do", "sounds great", "perfect", "awesome", "congrats",
        "congratulations", "happy birthday", "hbd", "welcome back",
    }
)

# Bare logistics-question shells that carry no durable self-fact ("what time?",
# "where at?"). Anchored so a fact-bearing clause after the shell is never lost.
_LOGISTICS_SHELL_RE = re.compile(
    r"^(?:what time|where|when|what about|how about|can you|could you|will you|"
    r"are you|is it|do you|did you|have you|should we|shall we|want to|wanna)\b"
    r"[^.?!]*\?+\s*$",
    re.I,
)


def _strip_for_prefilter(content: str) -> str:
    """Normalized comparison form: lowercased, collapsed whitespace, trailing
    punctuation/emoji-ish trailer removed. Only used by the pre-filter — the
    prompt/extractor still see the raw content."""
    text = str(content or "").strip().lower()
    # collapse internal whitespace so "ok   cool" == "ok cool"
    text = re.sub(r"\s+", " ", text)
    # drop a trailing run of punctuation / emoji-ish non-word chars
    text = re.sub(r"[\s\W]+$", "", text)
    return text.strip()


def _likely_has_owner_fact(content: str) -> bool:
    """Conservative gate: return False ONLY when the row is unambiguously a
    greeting/ack, a bare logistics question, or link-only — i.e. cannot carry a
    durable owner self-fact. Everything else returns True (spend the LLM call).

    NB: false negatives (returning False on a fact-bearing row) would DROP a
    real fact, so every branch here is deliberately narrow. False positives
    (returning True on junk) only cost one LLM call — acceptable.
    """
    raw = str(content or "")
    stripped = raw.strip()

    # 1) Too short to hold a subject-predicate-object self-fact.
    if len(stripped) < _MIN_FACT_CONTENT_CHARS:
        return False

    # 2) URL-only / link-dominated: strip every URL; if almost nothing durable
    #    remains (< 15 chars of non-URL text), there is no self-fact to extract.
    without_urls = _URL_RE.sub(" ", raw)
    residue = re.sub(r"\s+", " ", without_urls).strip(" \t\n\r-—–|:>·•")
    if _URL_RE.search(raw) and len(residue) < _MIN_FACT_CONTENT_CHARS:
        return False

    normalized = _strip_for_prefilter(raw)
    if not normalized:
        return False

    # 3) Whole-message greeting / acknowledgement / one-liner.
    if normalized in _GREETING_ACK_PHRASES:
        return False

    # 4) Bare logistics-question shell ("what time?", "where at?") — no fact.
    if _LOGISTICS_SHELL_RE.match(stripped):
        return False

    return True


# ---------------------------------------------------------------------------
# Real Ollama extractor (used when no extractor is injected).
# ---------------------------------------------------------------------------


def _make_ollama_extractor(
    model: str,
    conn: Optional[sqlite3.Connection] = None,
    *,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Callable[[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """Build a synchronous extractor bound to the OllamaAdapter transport.

    The returned callable raises on transport failure so the batch loop's
    try/except can degrade gracefully (and stop hammering a dead server).

    `conn` used to be read here for the active pack's `classify` binding, so the
    extractor inherited that role's knobs. That rung retired with the role on
    2026-08-15 (see `config/facts_llm.resolve_facts_llm_model`): fact extraction
    is an INGEST function, and a query-time pack must not steer it. The control
    plane now strips `classify` from every pack write, so the lookup could only
    ever have returned None in production — it survived that removal as dead
    code, and dead code that reads a retired role reads like the role is alive.
    `conn` stays in the signature: callers hold one and the seam is worth
    keeping for parameters that grow their own home under Node functions.
    """
    from ...engine.backends.ollama import OllamaAdapter

    adapter = OllamaAdapter()

    # think=False is the measured default, not an absence of opinion: reasoning
    # models (qwen3.5 &c.) otherwise spend the whole num_predict budget on
    # chain-of-thought and return an empty response.
    think = False
    num_ctx = None
    num_predict = _NUM_PREDICT

    def _extract(prompt: str, row: Dict[str, Any]) -> List[Dict[str, Any]]:
        # Last gate before an expensive Ollama call. `should_stop` carries this
        # batch's cancel as well as the run's shutdown; without one, fall back
        # to the current run so a directly-built extractor still honours Ctrl+C.
        from ...runtime_shutdown import is_shutdown_requested

        gate = should_stop if should_stop is not None else is_shutdown_requested
        if gate():
            raise InterruptedError("fact_llm extraction stopped")
        # temp 0 => deterministic; bounded output; keep_alive default so the
        # model stays warm across the batch. The adapter adapts `think` per
        # model capability, so non-thinking models are unaffected either way.
        generated = adapter._generate(  # noqa: SLF001 (intentional low-level reuse)
            model,
            prompt,
            num_predict=num_predict,
            think=think,
            temperature=0.0,
            num_ctx=num_ctx,
            timeout=FACTS_LLM_HTTP_TIMEOUT,
        )
        response = str(generated.get("text") or "") if isinstance(generated, dict) else str(generated or "")
        usage = generated.get("usage") if isinstance(generated, dict) else None
        if isinstance(usage, dict) and int(usage.get("total_tokens") or 0) > 0:
            try:
                from ...engine.usage_observation import emit_engine_llm_usage_observation

                record_id = str(row.get("id") or row.get("record_id") or "")
                task_id = (
                    f"fact_llm:{record_id}"
                    if record_id
                    else f"fact_llm:{hash(prompt) & 0xFFFFFFFF:x}"
                )
                emit_engine_llm_usage_observation(
                    task_id=task_id,
                    task_type="enrichment",
                    subtype="fact_llm_extract",
                    provider="ollama",
                    model=model,
                    usage={
                        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                        "completion_tokens": int(usage.get("completion_tokens") or 0),
                        "total_tokens": int(usage.get("total_tokens") or 0),
                    },
                    origin="ingestion_pipeline",
                    source_id=str(row.get("source_id") or ""),
                )
            except Exception:
                pass
        return parse_triples(response)

    return _extract


def _make_hosted_extractor(
    provider: str,
    model: str,
    *,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Callable[[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """Build a synchronous extractor bound to a hosted chat-completions backend.

    Same contract as `_make_ollama_extractor`: raises on transport failure so
    the batch loop can degrade gracefully. Hosted backends run at their own
    default sampling — the temp-0 determinism knob is Ollama-specific — which
    is acceptable for an additive fact pass whose parser drops malformed rows.
    """
    from ...config.node_function_providers import engine_provider_for

    if provider == "redpill":
        from ...engine.backends.redpill import RedpillAdapter, resolve_redpill_model

        adapter = RedpillAdapter()
        model = resolve_redpill_model(model)
    else:  # platform / openai — both run on the OpenAI adapter
        from ...engine.backends.openai import OpenAIAdapter

        adapter = OpenAIAdapter(wallet_gated=provider == "platform")

    usage_provider = engine_provider_for(provider)

    def _extract(prompt: str, row: Dict[str, Any]) -> List[Dict[str, Any]]:
        from ...runtime_shutdown import is_shutdown_requested

        gate = should_stop if should_stop is not None else is_shutdown_requested
        if gate():
            raise InterruptedError("fact_llm extraction stopped")
        completed = adapter._chat_completion(model=model, prompt=prompt)  # noqa: SLF001
        response = str(completed.get("text") or "") if isinstance(completed, dict) else ""
        usage = completed.get("usage") if isinstance(completed, dict) else None
        if isinstance(usage, dict) and int(usage.get("total_tokens") or 0) > 0:
            try:
                from ...engine.usage_observation import emit_engine_llm_usage_observation

                record_id = str(row.get("id") or row.get("record_id") or "")
                task_id = (
                    f"fact_llm:{record_id}"
                    if record_id
                    else f"fact_llm:{hash(prompt) & 0xFFFFFFFF:x}"
                )
                emit_engine_llm_usage_observation(
                    task_id=task_id,
                    task_type="enrichment",
                    subtype="fact_llm_extract",
                    provider=usage_provider,
                    model=model,
                    usage={
                        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
                        "completion_tokens": int(usage.get("completion_tokens") or 0),
                        "total_tokens": int(usage.get("total_tokens") or 0),
                    },
                    origin="ingestion_pipeline",
                    source_id=str(row.get("source_id") or ""),
                )
            except Exception:
                pass
        return parse_triples(response)

    return _extract


# ---------------------------------------------------------------------------
# Table inference (mirror extract_facts_from_batch so roles resolve identically)
# ---------------------------------------------------------------------------


def _infer_table(row: Dict[str, Any]) -> str:
    table = str(row.get("_table") or row.get("canonical_table") or "")
    if table:
        return table
    if row.get("record_type") is not None and row.get("organization") is not None:
        return "profile_records"
    if row.get("entry_at") is not None or row.get("mood_tag") is not None:
        return "journal_entries"
    if str(row.get("sender_type") or "") in ("user", "assistant", "human", "system"):
        return "ai_chat_messages"
    if row.get("is_from_self") is not None or row.get("sender_id") is not None:
        return "conversation_messages"
    return ""


def _asserted_by_for_addressed(row: Dict[str, Any], table: str) -> str:
    """The speaker of an ADDRESSED row → an attribution string (never 'owner').

    - ai_chat assistant reply  → 'assistant'.
    - a contact's message      → 'contact:<sender_id>' when we know who; else
                                  'contact:unknown' (mirrors graph_sanitize).
    """
    sender_type = str(row.get("sender_type") or "").strip().lower()
    if sender_type == "assistant":
        return "assistant"
    sender_id = str(row.get("sender_id") or "").strip()
    if sender_id and sender_id.lower() != "self":
        return f"contact:{sender_id}"
    return "contact:unknown"


def _source_ref(table: str, record_id: Any, source_id: Any = None) -> Dict[str, str]:
    ref = {"table": table, "record_id": str(record_id or "")}
    if source_id:
        ref["source_id"] = str(source_id)
    return ref


def _owner_entity_id(conn: sqlite3.Connection) -> str:
    # Reuse extract.py's owner resolution so both paths agree on the entity.
    from .extract import _owner_entity_id as _resolve_owner

    return _resolve_owner(conn)


# ---------------------------------------------------------------------------
# Progress markers (resume after interrupt) + bounded-concurrent fan-out
# ---------------------------------------------------------------------------


def _progress_source_ref(table: str, record_id: Any, source_id: Any = None) -> Dict[str, str]:
    return _source_ref(table, record_id, source_id)


def _already_processed_record_ids(
    conn: sqlite3.Connection, eligible: List[Dict[str, Any]]
) -> set:
    """Record ids that already finished a fact_llm pass (including empty)."""
    if not eligible:
        return set()
    try:
        from ..signal.extraction.artifact_store import source_ref_hash
    except Exception:  # noqa: BLE001
        return set()

    id_by_hash: Dict[str, str] = {}
    for item in eligible:
        ref = _progress_source_ref(
            item["table"], item["record_id"], item["row"].get("source_id")
        )
        id_by_hash[source_ref_hash(_FACT_LLM_PROGRESS_TYPE, [ref])] = str(item["record_id"])

    processed: set = set()
    hashes = list(id_by_hash.keys())
    # Stay under typical SQLite bind-variable limits.
    chunk_size = 400
    for offset in range(0, len(hashes), chunk_size):
        chunk = hashes[offset : offset + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        try:
            rows = conn.execute(
                f"""
                SELECT source_ref_hash FROM extraction_artifacts
                WHERE artifact_type=? AND source_ref_hash IN ({placeholders})
                """,
                (_FACT_LLM_PROGRESS_TYPE, *chunk),
            ).fetchall()
        except sqlite3.OperationalError:
            return set()
        for row in rows:
            rid = id_by_hash.get(str(row[0]))
            if rid is not None:
                processed.add(rid)
    return processed


def _mark_record_processed(
    conn: sqlite3.Connection,
    *,
    table: str,
    record_id: Any,
    source_id: Any,
    facts_written: int,
) -> None:
    """Persist that this record's LLM pass finished so a restart can skip it."""
    try:
        from ..signal.extraction.artifact_store import ExtractionArtifactStore

        ExtractionArtifactStore(conn).upsert_artifact(
            _FACT_LLM_PROGRESS_TYPE,
            {"facts_written": int(facts_written), "status": "ok"},
            source_refs=[_progress_source_ref(table, record_id, source_id)],
            confidence=1.0,
            extractor_version=_FACT_LLM_PROGRESS_VERSION,
        )
    except Exception as exc:  # noqa: BLE001 — progress is best-effort; facts already committed
        logger.debug("fact_llm progress mark failed for %s (%s)", record_id, exc)


def _run_coro_blocking(
    coro: "asyncio.Future",
    should_stop: Optional[Callable[[], bool]] = None,
) -> Any:
    """Drive an async coroutine to completion from SYNCHRONOUS code.

    extract_owner_facts_llm is sync (called inside extract_facts_from_batch).
    Live ingest offloads that sync path via asyncio.to_thread from
    FactExtractionJob.enrich, so this usually runs on a worker thread with no
    event loop. We still handle both cases:
      - no running loop (to_thread worker, unit tests, CLI) -> asyncio.run().
      - a running loop (mis-call from async context)        -> run the coroutine
        on a dedicated worker thread with its own loop, so we never re-enter
        the outer loop. The ollama calls inside still fan out concurrently there.
    Writes happen inside the coroutine on the event-loop thread as each row
    finishes (sqlite write-safety: one thread owns the connection).
    """
    from concurrent.futures import TimeoutError as FuturesTimeoutError

    from ...runtime_shutdown import is_shutdown_requested

    if should_stop is None:
        should_stop = is_shutdown_requested

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # A loop is already running on this thread: offload to a fresh thread+loop.
    # Poll with a short timeout so Ctrl+C / shutdown / cancel can interrupt the
    # wait instead of parking forever on Future.result().
    with ThreadPoolExecutor(max_workers=1) as pool:
        fut = pool.submit(lambda: asyncio.run(coro))
        while True:
            try:
                return fut.result(timeout=0.25)
            except FuturesTimeoutError:
                if should_stop():
                    # Cooperative fan-out should finish shortly; don't wait forever.
                    try:
                        return fut.result(timeout=min(5.0, FACTS_LLM_HTTP_TIMEOUT))
                    except FuturesTimeoutError as exc:
                        raise InterruptedError(
                            "fact_llm fan-out stopped before it drained"
                        ) from exc


async def _extract_and_apply(
    extractor: Callable[[str, Dict[str, Any]], List[Dict[str, Any]]],
    tasks: List[Tuple[int, str, Dict[str, Any]]],
    concurrency: int,
    apply_row: Callable[
        [int, Optional[List[Dict[str, Any]]], Optional[Exception], bool],
        Tuple[int, bool],
    ],
    should_stop: Optional[Callable[[], bool]] = None,
) -> int:
    """Fan out LLM calls; apply each completed row immediately.

    ``apply_row(index, triples, error, ran) -> (facts_written, halt)`` runs on
    the event-loop thread (sqlite-safe). ``ran=False`` means the extractor was
    never invoked (shutdown / cancel) — those rows are left unmarked so a
    restart retries them. Completed LLM work is always applied even after a stop
    is requested (cost already paid).
    """
    from ...runtime_shutdown import is_shutdown_requested

    # Assigned back onto the parameter deliberately: the loop below binds a
    # local `stop` from apply_row's return, so a second short name here would
    # shadow the predicate with a bool.
    if should_stop is None:
        should_stop = is_shutdown_requested

    sem = asyncio.Semaphore(concurrency)
    written = 0
    stop_scheduling = False

    async def _one(
        index: int, prompt: str, row: Dict[str, Any]
    ) -> Tuple[int, Optional[List[Dict[str, Any]]], Optional[Exception], bool]:
        nonlocal stop_scheduling
        if stop_scheduling or should_stop():
            return (index, None, None, False)
        async with sem:
            if stop_scheduling or should_stop():
                return (index, None, None, False)
            try:
                triples = await asyncio.to_thread(extractor, prompt, row)
                return (index, triples or [], None, True)
            except InterruptedError as exc:
                stop_scheduling = True
                return (index, None, exc, False)
            except Exception as exc:  # noqa: BLE001 — captured, handled by apply_row
                return (index, None, exc, True)

    pending = [asyncio.create_task(_one(i, p, r)) for (i, p, r) in tasks]
    try:
        for fut in asyncio.as_completed(pending):
            try:
                index, triples, error, ran = await fut
            except asyncio.CancelledError:
                continue
            row_written, stop = apply_row(index, triples, error, ran)
            written += int(row_written or 0)
            if stop:
                # Stop scheduling further extractor calls, but do NOT cancel
                # in-flight to_thread work — those Ollama costs are already paid
                # and must still be applied (and progress-marked).
                stop_scheduling = True
    finally:
        # Ensure tasks are awaited so they don't leak warnings.
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
    return written


def _write_triples_for_row(
    store: FactStore,
    *,
    owner: str,
    item: Dict[str, Any],
    triples: List[Dict[str, Any]],
) -> int:
    """Assert owner-subject triples for one row. Returns facts written."""
    record_id = item["record_id"]
    table = item["table"]
    asserted_by = item["asserted_by"]
    row = item["row"]
    written = 0
    for triple in triples or []:
        if not _is_owner_subject(triple):
            continue  # a fact about someone else — never an owner fact
        predicate = normalize_predicate(triple.get("predicate") or "")
        value = str(triple.get("object") or "").strip()
        if not predicate or not value:
            continue
        confidence = triple.get("confidence")
        try:
            confidence = float(confidence) if confidence is not None else DEFAULT_CONFIDENCE
        except (TypeError, ValueError):
            confidence = DEFAULT_CONFIDENCE
        try:
            asserted = store.assert_fact(
                subject_entity_id=owner,
                predicate=predicate,
                object_value=value,
                dimension=_dimension_for(predicate),
                confidence=confidence,
                source_refs=[_source_ref(table, record_id, row.get("source_id"))],
                period_start=triple.get("period_start"),
                period_end=triple.get("period_end"),
                disclosure="owner_only" if asserted_by == "owner" else "scoped",
                asserted_by=asserted_by,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "assert_fact failed (%s %s=%s): %s", asserted_by, predicate, value, exc
            )
            continue
        if asserted is not None:  # None => owner-excluded, never re-asserted
            written += 1
    return written


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def extract_owner_facts_llm(
    conn: sqlite3.Connection,
    rows: List[Dict[str, Any]],
    *,
    extractor: Optional[Callable[[str, Dict[str, Any]], List[Dict[str, Any]]]] = None,
    model: Optional[str] = None,
    settings: Any = None,
    concurrency: Optional[int] = None,
    resume: bool = True,
    cancel: Optional[threading.Event] = None,
    stats: Optional[Dict[str, Any]] = None,
) -> int:
    """Additive LLM fact pass over a canonical batch. Returns facts written.

    Only ``authored`` (owner belief) and ``addressed`` (attributed claim) rows
    are processed; observed/participated/ambient are skipped. Never raises into
    ingestion — transport/parse failures are logged and skipped.

    Progress is saved per completed row (facts + a resume marker), so Ctrl+C
    mid-batch keeps finished work and a restart skips those records.

    ``extractor``: injectable ``(prompt, row) -> list[triple]`` for tests /
    offline (NO network). When None, the real Ollama model is used (built
    lazily so importing this module never touches the network).

    ``concurrency``: optional fan-out bound (B10 / P4.3). Defaults to
    ``FACTS_LLM_CONCURRENCY`` / ``TOPOS_FACTS_LLM_CONCURRENCY`` (default 4).
    Bulk re-enrichment CLIs pass an explicit bound so a single process can
    A/B serial vs concurrent without reloading the module.

    ``resume``: when True (default), skip records already marked
    ``fact_llm_pass``. Ops timing / re-extract passes set False to re-pay
    Ollama even for previously marked rows.

    ``cancel``: a ``threading.Event`` scoped to THIS call. The caller sets it
    when its own task is cancelled — cancelling one batch must not touch any
    other batch, which is exactly what routing a cancel through the process-wide
    shutdown flag used to do (one cancelled batch, LLM facts dark until the node
    restarted, job still reporting success).

    ``stats``: optional out-dict, filled with ``written`` / ``eligible`` /
    ``stopped`` / ``stop_reason`` / ``unprocessed`` (rows the extractor was
    never invoked for) so the caller can tell "no facts in this batch" from
    "stopped before finishing it". The return value stays a plain int, so the
    existing call sites are untouched.
    """
    from ...runtime_shutdown import current_generation, stop_checker, stop_reason

    # Capture the run this batch belongs to. Polling the *current* run instead
    # would let a run that ends mid-batch hand the batch a fresh generation and
    # carry on working through a shutdown.
    generation = current_generation()
    should_stop = stop_checker(generation, cancel)

    def _record(written: int, eligible: int, unprocessed: int) -> int:
        if stats is not None:
            stopped = should_stop()
            stats.update(
                {
                    "written": int(written),
                    "eligible": int(eligible),
                    "stopped": bool(stopped),
                    "stop_reason": stop_reason(generation, cancel) if stopped else "",
                    "unprocessed": int(unprocessed),
                }
            )
        return int(written)

    if not rows:
        return _record(0, 0, 0)
    if should_stop():
        # Not silent: a lane that declines to run says so with its reason, so a
        # stop that is NOT an ordinary shutdown is visible in the log.
        logger.info(
            "LLM fact pass skipped for %d row(s); reason=%s",
            len(rows),
            stop_reason(generation, cancel) or "stopped",
        )
        return _record(0, 0, len(rows))
    if settings is None:
        from ...config.settings import settings as _settings

        settings = _settings

    real_extractor_factory: Optional[Callable[[], Callable]] = None
    if extractor is None:
        # An explicit `model` param is a caller-pinned local model; only the
        # device config can steer the pass onto a hosted provider.
        if model:
            resolved_provider, resolved_model = "ollama", str(model).strip()
        else:
            resolved_provider, resolved_model = _resolved_extraction_request(settings, conn)
            resolved_model = str(resolved_model or "").strip()
        if not resolved_model:
            logger.debug("LLM fact pass: no extraction model configured; inert")
            return _record(0, 0, 0)

        def real_extractor_factory() -> Callable[[str, Dict[str, Any]], List[Dict[str, Any]]]:
            if resolved_provider == "ollama":
                return _make_ollama_extractor(resolved_model, conn, should_stop=should_stop)
            return _make_hosted_extractor(resolved_provider, resolved_model, should_stop=should_stop)

    from ..lifecycle.exclusions import excluded_record_ids

    store = FactStore(conn)
    owner = _owner_entity_id(conn)
    excluded_records = excluded_record_ids(conn)
    posture_for = make_posture_resolver(conn)

    # Build the real extractor once (keeps the model warm across the batch). A
    # failure here (e.g. adapter import) leaves ``active_extractor`` None and we
    # fall back to inert — the rules floor already ran, so we never crash.
    active_extractor = extractor
    if active_extractor is None and real_extractor_factory is not None:
        try:
            active_extractor = real_extractor_factory()
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM fact pass: extractor init failed (%s); skipping", exc)
            return _record(0, 0, 0)

    from ..signal.embed_context import is_derivable_content

    # ----- Phase 1: eligibility (SYNC, no LLM). Compute role/attribution and the
    # pre-filter for every row FIRST, so we only fan out LLM calls for rows that
    # (a) pass the role gate and (b) plausibly carry a durable owner self-fact.
    # The role gate stays strictly upstream of every extractor call. --------------
    eligible: List[Dict[str, Any]] = []  # one dict per row we will LLM-extract
    for row in rows:
        record_id = row.get("record_id") or row.get("message_id") or row.get("id")
        if str(record_id or "") in excluded_records:
            continue
        content = str(row.get("content") or "")
        if not content.strip() or not is_derivable_content(content):
            continue

        table = _infer_table(row)
        role = record_role(row, table=table, posture=posture_for(row))
        if role == ROLE_AUTHORED:
            asserted_by = "owner"
        elif role == ROLE_ADDRESSED:
            asserted_by = _asserted_by_for_addressed(row, table)
        else:
            continue  # observed / participated / ambient => not eligible

        # Pre-filter: skip rows that cannot carry a durable owner self-fact
        # (greeting/ack, bare logistics question, link-only, too-short) BEFORE
        # spending an LLM call. Conservative — see _likely_has_owner_fact. Does
        # NOT touch role/attribution: it only decides whether to call the LLM.
        if not _likely_has_owner_fact(content):
            continue

        eligible.append(
            {
                "record_id": record_id,
                "row": row,
                "table": table,
                "asserted_by": asserted_by,
                "prompt": build_extraction_prompt(content),
            }
        )

    if not eligible:
        return _record(0, 0, 0)

    # Resume: skip records whose LLM pass already finished (incl. empty).
    if resume:
        already = _already_processed_record_ids(conn, eligible)
        if already:
            before = len(eligible)
            eligible = [item for item in eligible if str(item["record_id"]) not in already]
            skipped = before - len(eligible)
            if skipped:
                logger.info(
                    "LLM fact pass resuming: skipped %d already-processed record(s), %d remaining",
                    skipped,
                    len(eligible),
                )
    if not eligible:
        return _record(0, 0, 0)

    # ----- Phase 2: LLM fan-out + incremental writes. Each completed row is
    # asserted and progress-marked immediately so Ctrl+C keeps finished work.
    # Stop scheduling on shutdown / cancel / unreachable; still apply in-flight
    # results that already returned. ----------------------------------------------
    eligible_count = len(eligible)
    processed = 0
    tasks = [(i, item["prompt"], item["row"]) for i, item in enumerate(eligible)]
    bound = FACTS_LLM_CONCURRENCY if concurrency is None else int(concurrency)
    fanout = max(1, min(bound, len(tasks)))

    def _apply_row(
        index: int,
        triples: Optional[List[Dict[str, Any]]],
        error: Optional[Exception],
        ran: bool,
    ) -> Tuple[int, bool]:
        nonlocal processed
        item = eligible[index]
        record_id = item["record_id"]

        if not ran:
            # Extractor never ran (shutdown / cancel) — leave unmarked for retry.
            # Signal stop-scheduling only; in-flight paid work must still apply.
            return 0, True
        processed += 1

        if error is not None:
            if isinstance(error, InterruptedError):
                return 0, True
            logger.warning(
                "LLM fact extraction failed for row %s (%s)", record_id, error
            )
            if _looks_unreachable(error):
                # Server is down: don't burn the rest of the batch on timeouts.
                return 0, True
            # Transient/parse-ish failure: leave unmarked so a later pass retries.
            return 0, False

        row_written = _write_triples_for_row(
            store, owner=owner, item=item, triples=triples or []
        )
        _mark_record_processed(
            conn,
            table=item["table"],
            record_id=record_id,
            source_id=item["row"].get("source_id"),
            facts_written=row_written,
        )
        # Keep draining in-flight results after a stop; just stop new starts.
        return row_written, should_stop()

    try:
        written = _run_coro_blocking(
            _extract_and_apply(
                active_extractor, tasks, fanout, _apply_row, should_stop=should_stop
            ),
            should_stop=should_stop,
        )
    except InterruptedError:
        logger.info(
            "LLM fact pass interrupted after %d/%d row(s); reason=%s",
            processed,
            eligible_count,
            stop_reason(generation, cancel) or "interrupted",
        )
        return _record(0, eligible_count, eligible_count - processed)
    except Exception as exc:  # noqa: BLE001 — the fan-out harness must never crash ingest
        logger.warning("LLM fact pass: concurrent extraction failed (%s); skipping", exc)
        return _record(0, eligible_count, eligible_count - processed)

    unprocessed = max(0, eligible_count - processed)
    if should_stop() and unprocessed:
        # WARNING, not INFO: this batch left rows on the floor, so the count
        # above is NOT "there was nothing to extract". They stay unmarked, so
        # the next pass retries them — but a lane that keeps landing here is
        # quietly under-extracting and must be visible while that is true.
        logger.warning(
            "LLM fact pass stopped early; wrote %d fact(s) from %d/%d row(s), "
            "%d never attempted; reason=%s",
            written,
            processed,
            eligible_count,
            unprocessed,
            stop_reason(generation, cancel) or "stopped",
        )
    return _record(int(written or 0), eligible_count, unprocessed)


# Predicates that describe intimate wellbeing/place facts default to a tighter
# dimension; everything else is profile-grade. Mirrors the rules extractors.
_DIMENSION_BY_PREDICATE = {
    "lives_in": "places",
    "works_at": "work",
    "worked_at": "work",
    "works_on": "work",
    "practices": "wellbeing",
    "training_for": "wellbeing",
    "prefers": "preferences",
}


def _dimension_for(predicate: str) -> str:
    return _DIMENSION_BY_PREDICATE.get(predicate, "profile")


_UNREACHABLE_MARKERS = (
    "ollama request failed",
    "ollama_unreachable",
    # Hosted extractors (_make_hosted_extractor): transport AND auth failures
    # both degrade the batch — retrying per row would fail identically.
    "openai-compatible request failed",
    "insufficient_credits",
    "connection refused",
    "urlopen error",
    "max retries",
    "timed out",
    "timeout",
    "connection reset",
    "failed to establish",
)


def _looks_unreachable(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _UNREACHABLE_MARKERS)
