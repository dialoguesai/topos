"""Role-gated LLM fact extraction (B4 / PLAN_PROVENANCE_SPLIT P4.3).

The rules-only path (``extract.py``) is the FLOOR: it fires on a handful of
strict first-person patterns, so it misses paraphrased or multi-hop owner
facts ("been vegetarian since college", "switched from tea to cold brew this
year"). This module layers an LLM extraction pass ON TOP of the rules floor —
purely additive, because ``FactStore.assert_fact`` de-dupes on
``object_key`` (same predicate+object refreshes rather than duplicating).

Two things make this safe to run at ingest:

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

Injecting ``extractor`` bypasses Ollama entirely (unit tests, offline CI):
it is a callable ``(prompt: str, row: dict) -> list[dict]`` returning parsed
SPO-triple dicts (see ``TRIPLE`` shape below). When ``extractor`` is None and
Ollama is configured/reachable, the real model is used.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any, Callable, Dict, List, Optional

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

# Subject tokens that mean "the owner" (first person). Anything else is a
# third party and its triple is dropped — we never mint an owner fact about
# someone else, nor re-attribute someone else's fact to the owner.
_OWNER_SUBJECT_TOKENS = frozenset({"", "owner", "me", "i", "myself", "self", "the owner"})


# ---------------------------------------------------------------------------
# Flag resolution
# ---------------------------------------------------------------------------

_DISABLED_TOKENS = frozenset({"0", "false", "off", "no"})


def _resolved_extraction_model(settings: Any) -> str:
    """The model B4 would use: ollama_extraction_model, else the query model."""
    model = str(getattr(settings, "ollama_extraction_model", "") or "").strip()
    if model:
        return model
    return str(getattr(settings, "ollama_query_model", "") or "").strip()


def facts_llm_enabled(settings: Any = None) -> bool:
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
    return bool(_resolved_extraction_model(settings))


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
# Real Ollama extractor (used when no extractor is injected).
# ---------------------------------------------------------------------------


def _make_ollama_extractor(model: str) -> Callable[[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """Build a synchronous extractor bound to the OllamaAdapter transport.

    The returned callable raises on transport failure so the batch loop's
    try/except can degrade gracefully (and stop hammering a dead server).
    """
    from ...engine.backends.ollama import OllamaAdapter

    adapter = OllamaAdapter()

    def _extract(prompt: str, row: Dict[str, Any]) -> List[Dict[str, Any]]:
        # temp 0 => deterministic; bounded output; keep_alive default so the
        # model stays warm across the batch.
        response = adapter._generate(  # noqa: SLF001 (intentional low-level reuse)
            model,
            prompt,
            num_predict=_NUM_PREDICT,
            temperature=0.0,
        )
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
# Main entry
# ---------------------------------------------------------------------------


def extract_owner_facts_llm(
    conn: sqlite3.Connection,
    rows: List[Dict[str, Any]],
    *,
    extractor: Optional[Callable[[str, Dict[str, Any]], List[Dict[str, Any]]]] = None,
    model: Optional[str] = None,
    settings: Any = None,
) -> int:
    """Additive LLM fact pass over a canonical batch. Returns facts written.

    Only ``authored`` (owner belief) and ``addressed`` (attributed claim) rows
    are processed; observed/participated/ambient are skipped. Never raises into
    ingestion — transport/parse failures are logged and skipped.

    ``extractor``: injectable ``(prompt, row) -> list[triple]`` for tests /
    offline (NO network). When None, the real Ollama model is used (built
    lazily so importing this module never touches the network).
    """
    if not rows:
        return 0
    if settings is None:
        from ...config.settings import settings as _settings

        settings = _settings

    real_extractor_factory: Optional[Callable[[], Callable]] = None
    if extractor is None:
        resolved_model = str(model or _resolved_extraction_model(settings) or "").strip()
        if not resolved_model:
            logger.debug("LLM fact pass: no extraction model configured; inert")
            return 0

        def real_extractor_factory() -> Callable[[str, Dict[str, Any]], List[Dict[str, Any]]]:
            return _make_ollama_extractor(resolved_model)

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
            return 0

    from ..signal.embed_context import is_derivable_content

    written = 0
    transport_dead = False
    for row in rows:
        if transport_dead:
            break  # server is down; stop hammering it (graceful degradation)
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

        prompt = build_extraction_prompt(content)
        try:
            triples = active_extractor(prompt, row) or []
        except Exception as exc:  # noqa: BLE001
            # Transport failure (Ollama down) or a bad row: log, degrade, keep
            # the rules floor. If it looks like the server is unreachable, stop
            # the whole pass to avoid a timeout per row.
            logger.warning("LLM fact extraction failed for row %s (%s)", record_id, exc)
            if _looks_unreachable(exc):
                transport_dead = True
            continue

        for triple in triples:
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
