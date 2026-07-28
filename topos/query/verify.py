"""verify_claim — mode-gated truthfulness check of a statement against the
owner's fact store (PLAN_TRUTHFULNESS_PLUGIN.md §3.1).

Containment contract (owner directive 2026-07-27):
  * The mode's aperture is applied BEFORE retrieval and before any model call.
  * An out-of-aperture claim short-circuits to the same neutral response as a
    claim with no evidence — refusal is indistinguishable from ignorance.
  * The response NEVER carries evidence text or record ids — lane stances,
    confidences and a coarse category tag only.
  * ``caller_app_id`` is mandatory (stamped by the control plane); generic
    forwarding paths cannot invoke this casually.

The internal ``_audit`` block (popped by the handler, logged node-side, never
sent to callers) records mode, category, per-lane evidence counts and whether
retrieval was touched — the leak-gate evals assert on it.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

from ..features.facts.store import FactStore
from .verify_modes import VerifyMode, categorize, get_mode, registered_mode_names

logger = logging.getLogger(__name__)

_STOP_TOKENS = frozenset(
    "the a an i my me our we you your of to in on at for and or but is are was "
    "were be been am it its this that these those have has had do does did with "
    "about really actually never not no dont don't ever always".split()
)

_NEGATION_CUES = frozenset(
    ("never", "not", "don't", "dont", "no", "hate", "hates", "dislike",
     "dislikes", "stopped", "quit", "can't", "cant", "won't", "wont",
     "refuse", "refuses")
)

_MAX_FACT_EVIDENCE = 12

# Spoken-claim phrasings → fact predicates. Token search finds SUPPORTING
# facts (shared value words) but can never find the CONTRADICTING fact for a
# lie ("my name is Timothy" shares no tokens with "goes_by Jonny"), so
# predicate-hinted facts are pulled as evidence regardless of value overlap
# and the judge compares values.
_CLAIM_PREDICATE_HINTS = (
    (("my name", "name is", "call me", "i'm called", "i am called"),
     ("goes_by",)),
    (("i'm from", "i am from", "come from", "grew up", "hometown", "born in",
      "raised in", "hails from"),
     ("hails_from", "hometown", "grew_up_in")),
    (("years old", "my age"),
     ("years_old",)),
    (("favorite food", "favourite food"),
     ("favorite_food", "favourite_food")),
    (("favorite color", "favourite colour", "favorite colour"),
     ("favorite_color", "favourite_color", "favorite_colour")),
)


def _hinted_predicates(statement: str) -> frozenset:
    lowered = " ".join(str(statement or "").lower().split())
    hits = set()
    for phrases, predicates in _CLAIM_PREDICATE_HINTS:
        if any(phrase in lowered for phrase in phrases):
            hits.update(predicates)
    return frozenset(hits)


def _facts_by_predicates(store: "FactStore", predicates: frozenset) -> List[Dict[str, Any]]:
    if not predicates:
        return []
    rows = store._conn.execute(  # noqa: SLF001 — engine-internal module
        "SELECT object_id, signal_dimension, payload_json, confidence, "
        "source_refs_json, valid_from, valid_to FROM signal_objects "
        "WHERE object_type='fact' AND valid_to IS NULL "
        "ORDER BY updated_at DESC LIMIT 200"
    ).fetchall()
    matched = []
    for row in rows:
        fact = store._row_to_fact(row)  # noqa: SLF001
        predicate = str((fact.get("payload") or {}).get("predicate") or "").lower()
        if predicate in predicates:
            matched.append(fact)
    return matched


def _content_tokens(text: str) -> List[str]:
    return [
        t for t in re.findall(r"[a-z0-9']+", str(text or "").lower())
        if len(t) > 2 and t not in _STOP_TOKENS
    ]


def _soundex(token: str) -> str:
    """Tiny Soundex — spoken-name spelling variants (jonny/johnny) collapse to
    the same code, so ASR spelling never turns a truth into a lie."""
    token = "".join(ch for ch in str(token or "").lower() if ch.isalpha())
    if not token:
        return ""
    codes = {"b": "1", "f": "1", "p": "1", "v": "1",
             "c": "2", "g": "2", "j": "2", "k": "2", "q": "2", "s": "2",
             "x": "2", "z": "2", "d": "3", "t": "3", "l": "4",
             "m": "5", "n": "5", "r": "6"}
    head = token[0]
    encoded = [codes.get(head, "")]
    for ch in token[1:]:
        code = codes.get(ch, "")
        if code and code != encoded[-1]:
            encoded.append(code)
        elif not code:
            encoded.append("")
    digits = "".join(c for c in encoded[1:] if c)
    return (head + digits + "000")[:4]


def _value_token_match(value_token: str, claim_token: str) -> bool:
    return _prefix_match(value_token, claim_token) or (
        _soundex(value_token) == _soundex(claim_token) != ""
    )


def _prefix_match(token: str, other: str) -> bool:
    # Prefix matching bridges morphology ("play"→"playing", "played"→
    # "playing" via the shared 4-char stem) — terse fact renders miss
    # exact-token overlap otherwise (stat-tag precedent).
    if token == other:
        return True
    if min(len(token), len(other)) >= 4 and (
        token.startswith(other) or other.startswith(token)
    ):
        return True
    return len(token) >= 5 and len(other) >= 5 and token[:4] == other[:4]


def _neutral_lanes(mode: VerifyMode) -> Dict[str, Dict[str, Any]]:
    return {lane: {"stance": "no_evidence", "confidence": 0.0} for lane in mode.lanes}


def _lane_for_fact(fact: Dict[str, Any]) -> str:
    asserted_by = str((fact.get("payload") or {}).get("asserted_by") or "owner")
    if asserted_by == "owner":
        return "self"
    if asserted_by == "page-author":
        return "ambient"
    return "attributed"  # assistant / contact:<id>


def _llm_enabled() -> bool:
    """Tri-state TOPOS_TRUTH_LLM: off / on / auto (auto = inert under pytest,
    the TOPOS_FACTS_LLM convention — evals stay deterministic and offline)."""
    value = os.environ.get("TOPOS_TRUTH_LLM", "auto").strip().lower()
    if value in ("0", "false", "off"):
        return False
    if value in ("1", "true", "on"):
        return True
    return "PYTEST_CURRENT_TEST" not in os.environ


def _heuristic_lane_stance(claim: str, entries: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Deterministic no-model fallback. Confidence is capped at 0.55 — below
    every mode's contradict floor — so the heuristic can corroborate but can
    never push the nose on its own; only a real judge accuses."""
    fact_texts = [entry["text"] for entry in entries]
    claim_tokens = set(_content_tokens(claim))

    # Predicate-hinted facts (the claim names the same attribute): compare
    # VALUES directly — "my name is Timothy" vs "goes_by Jonny" shares no
    # tokens, but the value mismatch IS the contradiction.
    hinted = [entry for entry in entries if entry.get("hinted")]
    if hinted:
        for entry in hinted:
            value_tokens = _content_tokens(entry.get("value") or "")
            if value_tokens and all(
                any(_value_token_match(value_token, claim_token) for claim_token in claim_tokens)
                for value_token in value_tokens
            ):
                return {"stance": "supports", "confidence": 0.55, "via": "hinted_value"}
        return {"stance": "contradicts", "confidence": 0.55, "via": "hinted_value"}
    claim_words = set(re.findall(r"[a-z']+", str(claim or "").lower()))
    claim_negated = bool(claim_words & _NEGATION_CUES)

    def _overlap(a: set, b: set) -> tuple:
        count, longest = 0, 0
        for token in a:
            if any(_prefix_match(token, other) for other in b):
                count += 1
                longest = max(longest, len(token))
        return count, longest

    best_overlap = 0
    best_longest = 0
    best_conflict = False
    for text in fact_texts:
        fact_tokens = set(_content_tokens(text))
        overlap, longest = _overlap(claim_tokens, fact_tokens)
        if overlap > best_overlap:
            best_overlap = overlap
            best_longest = longest
            fact_words = set(re.findall(r"[a-z']+", text.lower()))
            best_conflict = claim_negated != bool(fact_words & _NEGATION_CUES)
    # Related when two tokens overlap, or one distinctive (long) token does —
    # single-word preference claims ("cilantro") are the common fun case.
    if best_overlap < 2 and not (best_overlap == 1 and best_longest >= 6):
        return {"stance": "no_evidence", "confidence": 0.0}
    confidence = min(0.55, 0.25 + 0.1 * best_overlap)
    return {
        "stance": "contradicts" if best_conflict else "supports",
        "confidence": round(confidence, 2),
    }


def _llm_lane_stances(claim: str, lane_texts: Dict[str, List[str]]) -> Optional[Dict[str, Dict[str, Any]]]:
    """One truth_verdict model call over all lanes; None on any failure so the
    caller falls back to the heuristic."""
    try:
        from ..engine.client import get_engine_client_or_local
        from ..engine.tasks import ModelRequest, ProcessingTask
        from ..config.settings import settings

        task = ProcessingTask(
            id="truth_verdict",
            type="truth_verdict",
            subtype="truth_verdict",
            source_id="truth",
            record_ids=[],
            input={"claim": claim, "lanes": lane_texts},
            model_request=ModelRequest(provider="ollama", model=settings.ollama_query_model),
        )
        result = get_engine_client_or_local(None).run(task)
        if result.status != "completed":
            return None
        lanes = (result.output or {}).get("lanes")
        if not isinstance(lanes, dict) or not lanes:
            return None
        cleaned: Dict[str, Dict[str, Any]] = {}
        for lane, verdict in lanes.items():
            if not isinstance(verdict, dict):
                return None
            stance = str(verdict.get("stance") or "no_evidence")
            if stance not in ("supports", "contradicts", "no_evidence"):
                stance = "no_evidence"
            try:
                confidence = max(0.0, min(1.0, float(verdict.get("confidence") or 0.0)))
            except (TypeError, ValueError):
                confidence = 0.0
            cleaned[str(lane)] = {"stance": stance, "confidence": round(confidence, 2)}
        return cleaned
    except Exception as exc:  # noqa: BLE001 — judge failure must degrade, not crash
        logger.warning("truth_verdict LLM failed, falling back to heuristic: %s", exc)
        return None


def verify_claim(
    conn: sqlite3.Connection,
    *,
    statement: str,
    mode: str = "fun",
    caller_app_id: str = "",
) -> Dict[str, Any]:
    """Check ``statement`` against the owner's facts under ``mode``'s aperture.

    Returns a caller-safe response plus an ``_audit`` key the handler pops.
    """
    resolved = get_mode(mode)
    if resolved is None:
        return {
            "error": "unknown_mode",
            "registered_modes": list(registered_mode_names()),
        }
    if not str(caller_app_id or "").strip():
        return {"error": "caller_app_id_required"}

    statement = str(statement or "").strip()[: resolved.max_statement_chars]
    audit: Dict[str, Any] = {
        "mode": resolved.name,
        "caller_app_id": caller_app_id,
        "retrieval_touched": False,
        "judge": None,
        "evidence_counts": {},
    }

    def _neutral(reason: str, category: Optional[str] = None) -> Dict[str, Any]:
        # Out-of-aperture and no-evidence produce byte-identical caller-visible
        # shapes (category is included only for in-aperture claims).
        audit["reason"] = reason
        return {
            "mode": resolved.name,
            "category": category,
            "lanes": _neutral_lanes(resolved),
            "abstained": True,
            "_audit": audit,
        }

    if not statement:
        return _neutral("empty_statement")

    category, fun_safe = categorize(statement)
    audit["category"] = category
    if not fun_safe or category not in resolved.allowed_categories:
        return _neutral("out_of_aperture")

    # --- Evidence (facts only in v1; aperture applied per fact) -----------------------
    audit["retrieval_touched"] = True
    store = FactStore(conn)
    tokens = _content_tokens(statement)
    hinted_predicates = _hinted_predicates(statement)

    lane_entries: Dict[str, List[Dict[str, Any]]] = {lane: [] for lane in resolved.lanes}
    seen_object_ids: set = set()
    candidates = list(store.search(tokens, limit=50))
    candidates += _facts_by_predicates(store, hinted_predicates)
    for fact in candidates:
        if sum(len(entries) for entries in lane_entries.values()) >= _MAX_FACT_EVIDENCE:
            break
        object_id = fact.get("object_id")
        if object_id in seen_object_ids:
            continue
        seen_object_ids.add(object_id)
        payload = fact.get("payload") or {}
        disclosure = str(payload.get("disclosure") or fact.get("disclosure") or "scoped")
        if disclosure in resolved.excluded_disclosures:
            continue
        predicate = str(payload.get("predicate") or "")
        fact_blob = f"{predicate.replace('_', ' ')} {payload.get('object_value') or ''}"
        fact_category, fact_safe = categorize(fact_blob)
        if not fact_safe or fact_category not in resolved.allowed_categories:
            continue
        lane = _lane_for_fact(fact)
        if lane in lane_entries:
            lane_entries[lane].append({
                "text": FactStore.render(fact),
                "value": str(payload.get("object_value") or ""),
                "hinted": predicate.lower() in hinted_predicates,
            })

    audit["evidence_counts"] = {lane: len(entries) for lane, entries in lane_entries.items()}
    audit["hinted_predicates"] = sorted(hinted_predicates)
    lane_texts = {lane: [entry["text"] for entry in entries]
                  for lane, entries in lane_entries.items()}
    if not any(lane_texts.values()):
        return _neutral("no_evidence", category=category)

    # --- Stance ----------------------------------------------------------------------
    stances: Optional[Dict[str, Dict[str, Any]]] = None
    if _llm_enabled():
        populated = {lane: texts for lane, texts in lane_texts.items() if texts}
        stances = _llm_lane_stances(statement, populated)
        audit["judge"] = "llm" if stances is not None else "heuristic_fallback"
    if stances is None:
        stances = {}
        if audit["judge"] is None:
            audit["judge"] = "heuristic"

    lanes: Dict[str, Dict[str, Any]] = {}
    for lane in resolved.lanes:
        entries = lane_entries.get(lane) or []
        if not entries:
            lanes[lane] = {"stance": "no_evidence", "confidence": 0.0}
            continue
        deterministic = _heuristic_lane_stance(statement, entries)
        via = deterministic.pop("via", None)
        if lane in stances:
            verdict = stances[lane]
            # Phonetic guard: when the deterministic hinted-value comparison
            # says the claimed value IS the stored value (ASR spelling
            # variants — Johnny/Jonny), a literal-minded model must not turn
            # the truth into a lie.
            if (via == "hinted_value" and deterministic["stance"] == "supports"
                    and verdict["stance"] == "contradicts"):
                verdict = {"stance": "supports",
                           "confidence": max(0.8, verdict.get("confidence", 0.0))}
                audit["judge"] = "llm+phonetic_guard"
            lanes[lane] = verdict
        else:
            lanes[lane] = deterministic

    return {
        "mode": resolved.name,
        "category": category,
        "lanes": lanes,
        "abstained": all(v["stance"] == "no_evidence" for v in lanes.values()),
        "_audit": audit,
    }
