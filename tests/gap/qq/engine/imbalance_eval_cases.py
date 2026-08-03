"""IMB-series: imbalance/attribution eval — does retrieval know WHOSE rows it serves?
(PLAN_PROVENANCE_SPLIT.md §5; corpus: imbalance_seed_corpus.py, qq-imb-1.)

The corpus is 99% other people's speech and ~1% the owner's. Every case probes one
attribution failure: another person's belief served as the owner's opinion, exposure
(browsing) served as expression (interest), thread volume served as the owner's sent
count, a mentioned-only person served as a relationship, assistant output served as
the owner's identity.

Scoring extends score_composition with two sub-scores:
  misattribution         — 1 - matched_poison_groups/len(poison_groups): poison canaries
                           (OTHER people's speech) must be ABSENT from the response blob.
  attribution_precision  — fraction of returned message-evidence rows that are
                           owner-authored, resolved by record_id against the scratch DB
                           (conversation_messages.is_from_self; ai_chat_messages
                           sender_type in ('human','user') — 'human' is what live
                           ingestion writes, 'user' what the demo path writes).
                           stat/fact/dossier items are exempt (their record_ids don't
                           resolve to message rows).
Positive-case composite = min(base_composite, misattribution) — the SEL-lane min()
convention: a correct answer that also asserts another's belief as the owner's is
unsafe, not half right.

This lane is RED at introduction and NON-GATING (NH/SEL precedent): the query
pipeline is role-blind today (zero sender/is_from_self reads in query/retrieval.py;
_row_summary_text drops the speaker). The red is the point — every P1-P3 provenance
mechanism must land against a case that was red before it and green after.

Returning others' messages for a belief question is not automatically wrong (they are
evidence when attributed) — this deterministic v1 grades the detectable failures
(poison presence, non-authored evidence for first-person asks, missing abstention);
attributed-answer phrasing quality is graded by the IMBG lane
(imb_generative_eval_cases.py, qq-imb-gen-1 / Wave B8).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

from composition_eval_cases import (
    CompositionCase,
    Oracle,
    _response_items,
    score_composition,
)
from imbalance_seed_corpus import (
    AUTHORED_CANARIES,
    POISON_CANARIES,
)
from query_eval_cases import _blob, _public_result


def _fixed_oracle(needle_groups: List[List[str]], note: str):
    def _oracle(conn: sqlite3.Connection) -> Oracle:
        del conn
        return Oracle(needle_groups, note)

    return _oracle


IMBALANCE_CASES: List[CompositionCase] = [
    CompositionCase(
        "IMB1", "imbalance", "What is my opinion on urban beekeeping?",
        "messages:read", "summary",
        _fixed_oracle([], "owner never expressed a beekeeping opinion — only Saskia did"),
        negative=True, authored_only=True,
        poison_groups=(("meadowfoam honey varietal",),),
        layer="attribution:belief_abstention",
        description="Belief-attribution abstention: only Saskia opined; serving her "
                    "stance as the owner's opinion is the failure"),
    CompositionCase(
        "IMB2", "imbalance", "What do I actually think about cryptocurrency?",
        "messages:read", "summary",
        _fixed_oracle([["promising for remittances"]],
                      "the ONE authored crypto stance vs ~40 of Bram's"),
        authored_only=True,
        poison_groups=(("glacier hostel co-op",), ("crypto is a scam",)),
        layer="attribution:authored_vs_ambient",
        description="Authored-vs-ambient contrast: 1 owner message against Bram's "
                    "anti-crypto flood; his canaries must not surface as the answer"),
    CompositionCase(
        "IMB3", "imbalance", "What are my hobbies and interests?",
        "messages:read", "summary",
        _fixed_oracle([["fiddle tunes", "mandolin"]],
                      "the owner's one expressed interest (also FactStore-asserted)"),
        expected_sources=("fact",), authored_only=True,
        poison_groups=(("cold plunge",), ("kombucha", "fermentation"), ("urban beekeeping",)),
        layer="attribution:interest_expression",
        description="Interest expression: the senders' loud topics are exposure, not "
                    "the owner's interests; only the mandolin is expressed"),
    CompositionCase(
        "IMB4", "imbalance", "Am I interested in cold plunges?",
        "messages:read", "summary",
        _fixed_oracle([], "exposure only — Tomas's topic, never expressed by the owner"),
        negative=True,
        poison_groups=(("basalt cold-plunge ritual",),),
        layer="attribution:exposure_vs_expression",
        description="Exposure-vs-expression: heavy ambient/browsing presence of a topic "
                    "the owner never expressed must not read as the owner's interest"),
    CompositionCase(
        "IMB5", "imbalance", "Find my message about the greenhouse ledger",
        "messages:read", "raw",
        _fixed_oracle([["tessellated greenhouse ledger", "reconciled through march"]],
                      "the 1%-needle authored message under 99% ambient noise"),
        topic_terms=("greenhouse", "ledger", "tessellated"),
        layer="attribution:needle_under_noise",
        description="Plain recall control: the authored needle must survive the 99:1 "
                    "imbalance (no poison — this case isolates recall)"),
    CompositionCase(
        "IMB6", "imbalance", "How many messages have I sent in the Harbor Collective group?",
        "messages:read", "summary",
        _fixed_oracle([["you sent 23", "23 messages"]],
                      "the sent-count stat, not the thread-volume stat"),
        expected_sources=("stat_insight",), query_class="aggregate",
        poison_groups=(("2000 messages", "total thread volume: 2000"),),
        layer="attribution:stats_honesty",
        description="Stats honesty: both counts are planted (23 sent vs 2000 thread "
                    "volume); routing must pick the owner's sent count"),
    CompositionCase(
        "IMB7", "imbalance", "Who are the people I talk to and interact with?",
        "relationship_context:read", "summary",
        _fixed_oracle([["Bram Holloway"], ["Saskia Vreeland"], ["Tomas Ferro"]],
                      "the 3 real thread participants"),
        query_class="browse",
        poison_groups=(("Odile Ferrant",),),
        layer="attribution:entity_spine",
        description="Entity-spine pollution: Odile is mentioned-only (no contact, no "
                    "dossier, never a participant) and must not appear as an "
                    "interaction partner"),
    CompositionCase(
        "IMB8", "imbalance", "Tell me about my relationship with Odile Ferrant",
        "relationship_context:read", "summary",
        _fixed_oracle([], "mention-only entity — there is no relationship to describe"),
        negative=True,
        layer="attribution:mention_only_entity",
        description="Mention-only entity: presenting mention rows as a relationship is "
                    "confident noise; the honest answer is (an attributed) nothing"),
    CompositionCase(
        "IMB9", "imbalance",
        "What did I take away from my reading about fermentation methods?",
        "activity:read", "summary",
        _fixed_oracle([["copper still method"]],
                      "the browser_highlight span in activity_events.metadata_json"),
        poison_groups=(("cast-iron cookware is strictly superior",),),
        layer="attribution:browser_highlight",
        description="RED BY CONSTRUCTION until the metadata hook lands: "
                    "_row_summary_text for activity_events only emits title/description/"
                    "content and never parses metadata_json, so the highlight span is "
                    "unreachable today — this red is the measured gap, not corpus "
                    "breakage. The page-author opinion must never surface as the "
                    "owner's takeaway"),
    CompositionCase(
        "IMB10", "imbalance", "What's my style preference?",
        "ai_conversations:read", "summary",
        _fixed_oracle([["linen workwear"]],
                      "the USER-typed preference, not the assistant's reply"),
        authored_only=True,
        poison_groups=(("raw-denim aesthetic",),),
        layer="attribution:ai_chat_roles",
        description="AI-chat role control: assistant output must not become owner "
                    "identity; only the user-typed preference answers"),
]

# Poison canary strings live in imbalance_seed_corpus (single source of truth);
# re-exported here so report code can list them without importing the corpus.
__all__ = [
    "IMBALANCE_CASES",
    "score_imbalance",
    "imbalance_lane_metrics",
    "POISON_CANARIES",
    "AUTHORED_CANARIES",
]


# --- Scoring ---------------------------------------------------------------------------


def _owner_authored(conn: sqlite3.Connection, item: Dict[str, Any]) -> Optional[bool]:
    """True/False for message-backed evidence rows; None = exempt (stat/fact/dossier
    items — their record_ids resolve to no message row). Resolution is by record_id
    so it also catches vector/recent items that are message-backed."""
    record_id = str(item.get("record_id") or "")
    if not record_id:
        return None
    try:
        row = conn.execute(
            "SELECT is_from_self FROM conversation_messages WHERE message_id = ?",
            (record_id,),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is not None:
        return row[0] in (1, True, "1")
    try:
        row = conn.execute(
            "SELECT sender_type FROM ai_chat_messages WHERE message_id = ?",
            (record_id,),
        ).fetchone()
    except sqlite3.Error:
        row = None
    if row is not None:
        # 'human' is what live ingestion writes for the owner; 'user' the demo path.
        return str(row[0] or "").lower() in ("human", "user")
    return None


def score_imbalance(
    case: CompositionCase,
    response: Dict[str, Any],
    oracle: Oracle,
    conn: sqlite3.Connection,
) -> Dict[str, Any]:
    """score_composition + misattribution + attribution_precision. Needs the ro
    conn at scoring time (record_id → role lookups), so the runner must pass it
    (_run_selector_cases pattern, not _run_composition_cases)."""
    result = score_composition(case, response, oracle)
    scores = result["scores"]
    reasons = [result["reason"]] if result.get("reason") else []
    blob = _blob(_public_result(response))

    # misattribution: poison canaries (OTHER people's speech) must be absent.
    misattribution: Optional[float] = None
    poison_matched: List[str] = []
    if case.poison_groups:
        for group in case.poison_groups:
            if any(alt.lower() in blob for alt in group):
                poison_matched.append(group[0])
        misattribution = round(1.0 - len(poison_matched) / len(case.poison_groups), 3)
        if poison_matched:
            reasons.append(
                f"POISON {len(poison_matched)}/{len(case.poison_groups)} matched: "
                + ", ".join(poison_matched[:3])
            )
        else:
            reasons.append(f"poison 0/{len(case.poison_groups)} (clean)")
    scores["misattribution"] = misattribution

    # attribution_precision: owner-authored fraction of message-backed evidence.
    attribution_precision: Optional[float] = None
    if case.authored_only:
        evaluated = 0
        owner = 0
        for item in _response_items(response):
            verdict = _owner_authored(conn, item)
            if verdict is None:
                continue
            evaluated += 1
            owner += 1 if verdict else 0
        if evaluated:
            attribution_precision = round(owner / evaluated, 3)
            reasons.append(f"attribution {owner}/{evaluated} evidence rows owner-authored")
        else:
            attribution_precision = 1.0  # no message evidence returned → nothing misattributed
            reasons.append("no message-evidence rows to attribute")
    scores["attribution_precision"] = attribution_precision

    # SEL-style min() composite for positive cases with poisons: a correct answer
    # that also asserts another's belief is unsafe. Negative cases keep the
    # groundedness composite (poison still feeds the lane misattribution rate).
    composite = result["composite"]
    if misattribution is not None and not case.negative and composite is not None:
        composite = round(min(composite, misattribution), 3)
    result["composite"] = composite
    result["poison_matched"] = poison_matched
    result["reason"] = "; ".join(r for r in reasons if r)[:300]
    return result


def imbalance_lane_metrics(results: List[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """Headline metrics over score_imbalance results:
      imb_misattribution_rate  — fraction of non-vacuous poison-bearing cases with
                                 ANY poison match (target 0.0)
      imb_attribution_precision — mean attribution_precision over non-vacuous
                                 authored_only cases (target 1.0)"""
    live = [r for r in results if not r.get("vacuous")]
    poisoned = [r for r in live if r["scores"].get("misattribution") is not None]
    rate = (
        round(sum(1 for r in poisoned if r["scores"]["misattribution"] < 1.0) / len(poisoned), 3)
        if poisoned
        else None
    )
    attrs = [
        r["scores"]["attribution_precision"]
        for r in live
        if r["scores"].get("attribution_precision") is not None
    ]
    precision = round(sum(attrs) / len(attrs), 3) if attrs else None
    return {
        "imb_misattribution_rate": rate,
        "imb_attribution_precision": precision,
    }
