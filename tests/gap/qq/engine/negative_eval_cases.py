"""N-series: one absence-honesty negative control per scope (plan §5 N-series).

Today's suite has exactly two global negative controls (C10/S13) and both score 0.00 —
the node returns confident noise for topics that do not exist. This module makes that
failure visible PER SCOPE: one fabricated topic per registry scope, graded by the
existing composition negative branch (groundedness = absence of confident noise).

These will mostly be red at first. That is the point — they are the accountability
baseline (plan Phase 1: "they'll be red — that's the accountability baseline"), and the
lane feeds claim C3 ("Topos is honest about what it doesn't know").

Fabricated topics are chosen to be plausible-sounding for their scope (so lexical overlap
can't trivially save the node) while being verifiably absent from any real database.
"""

from __future__ import annotations

from typing import List

from composition_eval_cases import CompositionCase, Oracle


def _negative_oracle(conn) -> Oracle:
    return Oracle([], "fabricated topic — nothing in the DB should match")


def _case(case_id: str, query: str, scope_id: str) -> CompositionCase:
    return CompositionCase(
        case_id, "live", query, scope_id, "summary", _negative_oracle,
        negative=True, layer=f"negative:{scope_id}",
        description=f"Per-scope absence honesty ({scope_id})",
    )


def _common_word_negative(pair: tuple, all_words: tuple):
    """Oracle factory for the HARD negative lane: fabricated topics built ONLY from
    words the corpus already contains. The zero-df/rare-token honesty gate is blind
    here BY CONSTRUCTION (plan D1.1 — the known open flank): every content token
    lexically matches real records, but the *combination* (the topic) never
    happened. Runtime guards keep the case honest as live data drifts:
      - every content word must have FTS df >= 3 (else the absent-token gate would
        legitimately catch it, and this case would test nothing) → vacuous;
      - the defining pair must never co-occur in one message or one indexed chunk
        (else the topic may genuinely exist) → vacuous."""

    def _oracle(conn) -> Oracle:
        try:
            for w in all_words:
                df = conn.execute(
                    "SELECT count(*) FROM signal_embeddings_fts WHERE signal_embeddings_fts MATCH ?",
                    (f'"{w}"',),
                ).fetchone()[0]
                if df < 3:
                    return Oracle([], f"'{w}' df={df} — absent-gate would catch this; not a blind-flank case", ok=False)
            a, b = pair
            co_fts = conn.execute(
                "SELECT count(*) FROM signal_embeddings_fts WHERE signal_embeddings_fts MATCH ?",
                (f'"{a}" AND "{b}"',),
            ).fetchone()[0]
            co_msg = conn.execute(
                "SELECT count(*) FROM conversation_messages WHERE lower(content) LIKE ? AND lower(content) LIKE ?",
                (f"%{a}%", f"%{b}%"),
            ).fetchone()[0]
            if co_fts or co_msg:
                return Oracle([], f"'{a}'+'{b}' co-occur ({co_fts} fts / {co_msg} msg) — topic may exist", ok=False)
        except Exception as exc:
            return Oracle([], f"guard query failed: {exc}", ok=False)
        return Oracle([], f"fabricated topic from common corpus words {all_words} — combination never occurred")

    return _oracle


def _hard_case(case_id: str, query: str, scope_id: str, pair: tuple, all_words: tuple) -> CompositionCase:
    return CompositionCase(
        case_id, "live", query, scope_id, "summary",
        _common_word_negative(pair, all_words),
        negative=True, layer=f"negative_hard:{scope_id}", query_class="known_item",
        description="Common-word fabricated topic — lexically plausible, factually absent (D1.1)",
    )


# Deliberately red at introduction (plan D1.1): these bypass the rare-token gate
# because every content token is genuinely in the corpus. Reported in their OWN
# lane (negative_hard) so the gating negative composite keeps its meaning; promote
# to gating once a mechanism (topical-coherence check, co-occurrence evidence
# requirement) turns them green.
NEGATIVE_HARD_CASES: List[CompositionCase] = [
    _hard_case("NH1", "Show my messages about selling my bike", "messages:read",
               ("selling", "bike"), ("selling", "bike")),
    _hard_case("NH2", "Find my messages about booking the hotel for the wedding", "messages:read",
               ("hotel", "wedding"), ("booking", "hotel", "wedding")),
    _hard_case("NH3", "What did I say about painting the kitchen?", "messages:read",
               ("kitchen", "paint"), ("paint", "kitchen")),
]


NEGATIVE_CONTROL_CASES: List[CompositionCase] = [
    _case("N1", "What did I browse about the Velmora coral-farming cooperative?",
          "activity:read"),
    _case("N2", "Show my AI conversations about the Threnody-7 compiler rewrite",
          "ai_conversations:read"),
    _case("N3", "When am I free for the annual Zephyrline glassblowing retreat?",
          "availability:read"),
    _case("N4", "Find the contact record for Baltasar Quenneville-Okafor",
          "contacts:resolve"),
    _case("N5", "What do my journals say about my recovery from the Kestrel Ridge climbing accident?",
          "health:read"),
    _case("N6", "Show my messages about selling the houseboat on Lake Vermeil",
          "messages:read"),
    _case("N7", "How often do I visit the Cordovan Observatory planetarium?",
          "places:read"),
    _case("N8", "What does my public bio say about my years as a competitive falconer?",
          "public_bio:read"),
    _case("N9", "How is my relationship with my rowing coach Ottoline?",
          "relationship_context:read"),
    _case("N10", "List my financial transactions at the Marzipan Meridian test kitchen",
          "resources:read"),
    _case("N11", "What time is my quarterly submersible-license renewal exam?",
          "schedule:read"),
    _case("N12", "Summarize my work on the Peregrine ledger-migration contract for Vantablack GmbH",
          "work_context:read"),
]
