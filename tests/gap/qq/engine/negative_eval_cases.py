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
