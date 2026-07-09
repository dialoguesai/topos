"""FD-series: fact-density eval — does extraction reach the owner's PARAPHRASED /
MULTI-HOP self-facts, and does it stay role-safe while doing so?
(PLAN_NODE_UPGRADE_AND_EVAL_EXPANSION.md B4; corpus: fact_density_corpus.py, qq-fd-1.)

Three cases, mirroring the IMB lane's structure but with density's own scorer:

  FD-density       — recall of the gold owner facts (extracted ∩ gold / gold) plus
                     precision (no fabricated owner facts). RED with the rules-only
                     extractor (recall ~0 — the patterns miss paraphrase/multi-hop),
                     GREEN with a stub (or real) LLM extractor returning the gold
                     triples. Reported for BOTH extractors so the lift is measured.
  FD-role-safety   — the WITNESSED shellfish row (observed role) yields ZERO owner
                     facts regardless of extractor: the role gate is UPSTREAM of the
                     LLM, so even a hallucinating extractor cannot mint an owner fact
                     from another person's medical claim (safety-critical A1 class).
  FD-attribution   — the ADDRESSED assistant reply that states a fact becomes an
                     ATTRIBUTED claim (asserted_by != 'owner'), never a first-person
                     owner belief.

This lane is RED at introduction and NON-GATING (NH/SEL precedent): rules-only is
the floor and it misses these facts by construction. The red is the point — B4's LLM
extraction path must land against a case that was red before it and green after.

Contract consumed (extractor interface — see module-level CONTRACT note below):
    extract_owner_facts_llm(conn, rows, *, extractor=None) -> List[fact-dict]
The reference implementation here IS that gate (role-gate upstream, then per-row
extractor, then owner-subject filter, then FactStore.assert_fact with the right
asserted_by). features/facts/llm_extract.py (B4) must match this signature so the
job and this lane share one code path; until it exists, this reference stands in and
the stub extractor keeps the lane deterministic and machine-independent (NO network).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from fact_density_corpus import (
    GOLD_FACTS,
    OWNER_ENTITY_ID,
    ai_assistant_row,
    owner_message_rows,
    witness_shellfish_row,
)

# An SPO triple as an extractor emits it: (subject, predicate, object_value).
# subject is a coarse role tag the reference impl trusts only for 'owner' — a
# real LLM cannot name the owner_entity_id, so it emits the sentinel 'owner' and
# the gate resolves the id. Non-'owner' subjects are dropped for authored rows
# (an owner message asserting a fact about someone else is not an owner belief).
Triple = Tuple[str, str, str]
Extractor = Callable[[Dict[str, Any]], List[Triple]]


# --- CONTRACT: the LLM-extraction interface this lane consumes -------------------------
#
# extract_owner_facts_llm(conn, rows, *, extractor=None) -> List[Dict[str, Any]]
#
#   rows      — batch-shaped canonical dicts (each carries _table, sender_type/
#               is_from_self/sender_id, content, record_id, source_id).
#   extractor — injectable callable(row) -> List[(subject, predicate, object)] for
#               tests. When None the real impl calls ollama (ollama_extraction_model,
#               temp 0); THIS module never passes None (no network in the lane).
#   returns   — fact-dicts, one per asserted fact:
#               {"record_id", "predicate", "object_value", "dimension",
#                "confidence", "asserted_by", "subject_entity_id"}
#
# Gate semantics (role-gate is UPSTREAM of the extractor call, so it holds even if
# the extractor hallucinates):
#   role 'authored'  -> run extractor; asserted_by='owner'; keep only owner-subject
#                       triples; assert via FactStore.assert_fact(asserted_by='owner').
#   role 'addressed' -> run extractor; asserted_by = the speaker ('assistant' or
#                       'contact:<id>'); the fact is an ATTRIBUTED claim about the
#                       owner, never an owner belief.
#   any other role   -> skip entirely; extractor is NOT called.
#
# features/facts/llm_extract.py (B4) must match this signature and these semantics.


def _owner_entity_id(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT entity_id FROM entities WHERE is_self=1 LIMIT 1"
    ).fetchone()
    return str(row[0]) if row else OWNER_ENTITY_ID


def _speaker_for_addressed(row: Dict[str, Any]) -> str:
    """asserted_by for an addressed row: the actual speaker, never 'owner'.

    Mirrors features/facts/llm_extract._asserted_by_for_addressed EXACTLY so the
    lane's reference gate and the B4 production gate emit byte-identical
    attribution strings: assistant -> 'assistant'; a known contact ->
    'contact:<sender_id>'; empty / 'self' sender_id -> 'contact:unknown'.
    """
    sender_type = str(row.get("sender_type") or "").strip().lower()
    if sender_type == "assistant":
        return "assistant"
    sender_id = str(row.get("sender_id") or "").strip()
    if sender_id and sender_id.lower() != "self":
        return f"contact:{sender_id}"
    return "contact:unknown"


def extract_owner_facts_llm(
    conn: sqlite3.Connection,
    rows: List[Dict[str, Any]],
    *,
    extractor: Optional[Extractor] = None,
    assert_facts: bool = False,
) -> List[Dict[str, Any]]:
    """Reference role-gated LLM extraction path (the contract above).

    Pure by default (assert_facts=False): returns the fact-dicts it WOULD assert
    without touching the store, so the density scorer can grade recall/precision
    without mutating the corpus. assert_facts=True also writes them via
    FactStore.assert_fact (used by the role-safety / attribution assertions).
    """
    from topos.features.provenance.roles import record_role

    if extractor is None:  # pragma: no cover - the lane never calls ollama
        raise RuntimeError(
            "extract_owner_facts_llm requires an injected extractor in the FD lane; "
            "real ollama is orchestrator-only (no network in unit tests)"
        )

    owner = _owner_entity_id(conn)
    out: List[Dict[str, Any]] = []
    for row in rows:
        table = str(row.get("_table") or row.get("canonical_table") or "")
        role = record_role(row, table=table)
        if role == "authored":
            asserted_by = "owner"
            keep_non_owner_subject = False
        elif role == "addressed":
            asserted_by = _speaker_for_addressed(row)
            keep_non_owner_subject = True  # attributed claim about the owner
        else:
            continue  # observed / participated / ambient: extractor NOT called
        for subject, predicate, object_value in extractor(row):
            subject = str(subject or "").strip().lower()
            if not keep_non_owner_subject and subject != "owner":
                continue  # owner message asserting someone else's fact -> not owner belief
            fact = {
                "record_id": row.get("record_id") or row.get("message_id"),
                "predicate": str(predicate),
                "object_value": str(object_value),
                "dimension": "profile",
                "confidence": 0.6,
                "asserted_by": asserted_by,
                "subject_entity_id": owner,
            }
            out.append(fact)
            if assert_facts:
                from topos.features.facts.store import FactStore

                FactStore(conn).assert_fact(
                    subject_entity_id=owner,
                    predicate=fact["predicate"],
                    object_value=fact["object_value"],
                    dimension=fact["dimension"],
                    confidence=fact["confidence"],
                    asserted_by=asserted_by,
                )
    return out


# --- Rules-only baseline extraction (the RED floor) -------------------------------------


def extract_facts_rules_only(
    conn: sqlite3.Connection, rows: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """The rules-only path, shaped like extract_owner_facts_llm's output so the
    density scorer can grade both with one code path. Uses the REAL extractor
    (features/facts/extract.extract_message_facts) — no stub."""
    from topos.features.facts.extract import extract_message_facts

    out: List[Dict[str, Any]] = []
    owner = _owner_entity_id(conn)
    for row in rows:
        table = str(row.get("_table") or row.get("canonical_table") or "")
        for spec in extract_message_facts(row, conn, table=table):
            out.append(
                {
                    "record_id": row.get("record_id") or row.get("message_id"),
                    "predicate": str(spec["predicate"]),
                    "object_value": str(spec["object_value"]),
                    "dimension": spec.get("dimension", "profile"),
                    "confidence": spec.get("confidence", 0.6),
                    "asserted_by": "owner",
                    "subject_entity_id": owner,
                }
            )
    return out


# --- Cases -----------------------------------------------------------------------------


@dataclass(frozen=True)
class FactDensityCase:
    id: str
    layer: str
    description: str
    negative: bool = False


FACT_DENSITY_CASES: List[FactDensityCase] = [
    FactDensityCase(
        "FD-density", "density:paraphrase_recall",
        "Recall of paraphrased/multi-hop owner facts (diet/sibling/prefers/practices). "
        "RED with rules-only (patterns miss them), GREEN with the LLM(stub) extractor."),
    FactDensityCase(
        "FD-role-safety", "safety:witnessed_no_owner_fact",
        "The witnessed first-person shellfish allergy (observed role) yields ZERO owner "
        "facts even with the LLM on — the role gate is upstream of the extractor "
        "(safety-critical A1 class).", negative=True),
    FactDensityCase(
        "FD-attribution", "attribution:addressed_claim",
        "The addressed assistant reply that states a fact becomes an ATTRIBUTED claim "
        "(asserted_by != 'owner'), never a first-person owner belief."),
]


__all__ = [
    "FACT_DENSITY_CASES",
    "FactDensityCase",
    "Triple",
    "Extractor",
    "extract_owner_facts_llm",
    "extract_facts_rules_only",
    "score_fact_density",
    "fact_density_lane_metrics",
]


# --- Scoring ---------------------------------------------------------------------------


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _matches_gold(fact: Dict[str, Any], predicate: str, object_value: str) -> bool:
    """A gold fact is hit if an extracted OWNER fact carries the same normalized
    predicate and its object contains (or is contained by) the gold object — the
    LLM may phrase 'cold brew' as 'cold brew coffee'; substring both ways is the
    same tolerance score_composition uses for needles."""
    if _norm(fact.get("asserted_by")) != "owner":
        return False  # attributed claims are not owner facts
    if _norm(fact.get("predicate")) != _norm(predicate):
        return False
    got, want = _norm(fact.get("object_value")), _norm(object_value)
    return want in got or got in want


def score_fact_density(
    extractor_fn: Callable[[sqlite3.Connection, List[Dict[str, Any]]], List[Dict[str, Any]]],
    conn: sqlite3.Connection,
    *,
    gold: Tuple[Tuple[str, str, str], ...] = GOLD_FACTS,
) -> Dict[str, Any]:
    """Grade an extraction path over the owner fact-bearing rows.

    ``extractor_fn(conn, rows) -> List[fact-dict]`` is either
    ``extract_facts_rules_only`` (the RED baseline) or a partial of
    ``extract_owner_facts_llm`` with a stub/real extractor bound. Returns:
      recall     — gold facts hit / total gold      (RED low, GREEN high)
      precision  — owner facts that hit a gold / all owner facts (fabrication guard)
      hit / missed / fabricated for the report.
    """
    rows = owner_message_rows(conn)
    facts = extractor_fn(conn, rows)
    owner_facts = [f for f in facts if _norm(f.get("asserted_by")) == "owner"]

    hit: List[str] = []
    missed: List[str] = []
    for _mid, predicate, object_value in gold:
        if any(_matches_gold(f, predicate, object_value) for f in facts):
            hit.append(f"{predicate}={object_value}")
        else:
            missed.append(f"{predicate}={object_value}")

    # An owner fact is "grounded" if it hits ANY gold pair; the rest are fabricated.
    def _grounded(f: Dict[str, Any]) -> bool:
        return any(_matches_gold(f, p, o) for _m, p, o in gold)

    grounded = sum(1 for f in owner_facts if _grounded(f))
    fabricated = [
        f"{f.get('predicate')}={f.get('object_value')}"
        for f in owner_facts
        if not _grounded(f)
    ]

    recall = round(len(hit) / len(gold), 3) if gold else None
    precision = round(grounded / len(owner_facts), 3) if owner_facts else 1.0
    return {
        "recall": recall,
        "precision": precision,
        "hit": hit,
        "missed": missed,
        "fabricated": fabricated,
        "n_owner_facts": len(owner_facts),
    }


def score_role_safety(
    extractor: Extractor, conn: sqlite3.Connection
) -> Dict[str, Any]:
    """FD-role-safety: the witnessed shellfish row must yield ZERO owner facts even
    with an extractor that WOULD emit an owner triple — the role gate is upstream.
    Returns owner_facts (must be 0) and a pass flag."""
    facts = extract_owner_facts_llm(
        conn, [witness_shellfish_row(conn)], extractor=extractor
    )
    owner_facts = [f for f in facts if _norm(f.get("asserted_by")) == "owner"]
    return {"owner_facts": len(owner_facts), "passed": len(owner_facts) == 0}


def score_attribution(
    extractor: Extractor, conn: sqlite3.Connection
) -> Dict[str, Any]:
    """FD-attribution: the addressed assistant reply's fact lands attributed
    (asserted_by != 'owner') and never as an owner belief."""
    facts = extract_owner_facts_llm(
        conn, [ai_assistant_row(conn)], extractor=extractor
    )
    owner_facts = [f for f in facts if _norm(f.get("asserted_by")) == "owner"]
    attributed = [f for f in facts if _norm(f.get("asserted_by")) not in ("", "owner")]
    return {
        "owner_facts": len(owner_facts),
        "attributed": len(attributed),
        "asserted_by": sorted({str(f.get("asserted_by")) for f in facts}),
        "passed": len(owner_facts) == 0 and len(attributed) >= 1,
    }


def fact_density_lane_metrics(
    rules_result: Dict[str, Any], llm_result: Dict[str, Any]
) -> Dict[str, Optional[float]]:
    """Headline metrics: the density LIFT the LLM path buys over rules-only.
      fd_recall_rules / fd_recall_llm — recall of the two paths
      fd_recall_lift                  — llm - rules (the B4 win)
      fd_precision_llm                — LLM owner-fact precision (fabrication guard)"""
    rr = rules_result.get("recall")
    lr = llm_result.get("recall")
    lift = round(lr - rr, 3) if (rr is not None and lr is not None) else None
    return {
        "fd_recall_rules": rr,
        "fd_recall_llm": lr,
        "fd_recall_lift": lift,
        "fd_precision_llm": llm_result.get("precision"),
    }
