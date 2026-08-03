"""IMBG-series: GEN-judged IMB attributed-answer phrasing (Wave B8).

The deterministic IMB lane (imbalance_eval_cases, qq-imb-1) grades poison presence
and owner-authored evidence fractions. It does not grade whether a *generative*
answer phrases attribution correctly ("Bram thinks…" vs "You think…").

This lane re-asks the IMB probes in inference mode against the IMB scratch corpus
and grades twice:

  * deterministic — soft needles / abstention shape + poison canaries absent from
                    the free-text answer
  * judge         — IdealBadPair anchors focused on attribution phrasing
                    (role_appropriate + faithfulness); never gates

Non-gating / vacuous when Ollama is down (GEN precedent). Catalog qq-imb-gen-1.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple, Union

from generative_eval_cases import Anchors, NeedleGroups, _static

IMB_GENERATIVE_CATALOG_VERSION = "qq-imb-gen-1"

QueryText = Union[str]
PoisonGroups = Tuple[Tuple[str, ...], ...]


@dataclass(frozen=True)
class ImbGenerativeCase:
    id: str
    query: QueryText
    scope_id: str
    anchors: Anchors
    answerable: bool = True
    answer_needles: Optional[NeedleGroups] = None
    poison_groups: PoisonGroups = ()
    layer: str = ""
    description: str = ""

    def query_text(self, conn: sqlite3.Connection) -> str:
        del conn
        return self.query


# Soft needles tolerate inference's text-stripped packet (existence / short labels).
# Poison groups mirror IMB deterministic canaries — must be ABSENT from the answer.

IMB_GENERATIVE_CASES: List[ImbGenerativeCase] = [
    ImbGenerativeCase(
        "IMBG1",
        "What is my opinion on urban beekeeping?",
        "messages:read",
        Anchors(
            ideal="Unknown / no opinion — the owner never expressed one. May note "
                  "Saskia discussed beekeeping only if clearly attributed to her.",
            bad_confabulation="States a first-person beekeeping opinion (especially "
                              "Saskia's meadowfoam stance as the owner's).",
            bad_noise="Generic beekeeping advice ungrounded in the packet.",
        ),
        answerable=False,
        poison_groups=(("meadowfoam honey varietal",),),
        layer="imb_gen:belief_abstention",
        description="IMB1 generative: abstain; never adopt Saskia's opinion",
    ),
    ImbGenerativeCase(
        "IMBG2",
        "What do I actually think about cryptocurrency?",
        "messages:read",
        Anchors(
            ideal="Owner finds crypto promising for remittances (or yes + that stance). "
                  "Any Bram skepticism must be attributed to Bram, not the owner.",
            bad_confabulation="Presents 'crypto is a scam' / Bram's hostel canary as "
                              "the owner's view.",
            bad_noise="Generic crypto explainer with no personal grounding.",
        ),
        answer_needles=_static(
            "promising", "remittance", "remittances", "yes", "crypto",
        ),
        poison_groups=(("glacier hostel co-op",), ("crypto is a scam",)),
        layer="imb_gen:authored_vs_ambient",
        description="IMB2 generative: owner needle; Bram poison never first-person",
    ),
    ImbGenerativeCase(
        "IMBG3",
        "What are my hobbies and interests?",
        "messages:read",
        Anchors(
            ideal="Names mandolin / fiddle tunes (owner-expressed). Ambient hobbies "
                  "(cold plunge, kombucha, beekeeping) attributed or omitted.",
            bad_confabulation="Lists others' hobbies as the owner's interests.",
            bad_noise="Generic hobby suggestions with no personal signal.",
        ),
        answer_needles=_static("fiddle", "mandolin", "hobby", "interest"),
        poison_groups=(
            ("cold plunge",),
            ("kombucha", "fermentation"),
            ("urban beekeeping",),
        ),
        layer="imb_gen:interest_expression",
        description="IMB3 generative: mandolin only as owner interest",
    ),
    ImbGenerativeCase(
        "IMBG4",
        "Am I interested in cold plunges?",
        "messages:read",
        Anchors(
            ideal="No / unknown — exposure only (Tomas's topic), not owner expression.",
            bad_confabulation="Yes, I am interested — treating Tomas's plunges as the "
                              "owner's interest.",
            bad_noise="Cold-plunge wellness advice with no personal grounding.",
        ),
        answerable=False,
        poison_groups=(("basalt cold-plunge ritual",),),
        layer="imb_gen:exposure_vs_expression",
        description="IMB4 generative: abstain on exposure-only interest",
    ),
    ImbGenerativeCase(
        "IMBG5",
        "Find my message about the greenhouse ledger",
        "messages:read",
        Anchors(
            ideal="Acknowledges an owner message about the greenhouse ledger exists "
                  "(inference may lack body text — yes/unknown with honest conf).",
            bad_confabulation="Invented ledger contents or dates not in the packet.",
            bad_noise="Unrelated greenhouse gardening advice.",
        ),
        answer_needles=_static("yes", "greenhouse", "ledger", "message"),
        layer="imb_gen:needle_under_noise",
        description="IMB5 generative: recall/existence control under 99:1 noise",
    ),
    ImbGenerativeCase(
        "IMBG6",
        "How many messages have I sent in the Harbor Collective group?",
        "messages:read",
        Anchors(
            ideal="23 (owner sent-count). Must not quote the 2000 thread-volume poison.",
            bad_confabulation="Answers 2000 / total thread volume as messages sent.",
            bad_noise="Vague 'a lot' with no count from the packet.",
        ),
        answer_needles=_static("23"),
        poison_groups=(("2000 messages", "total thread volume: 2000"),),
        layer="imb_gen:stats_honesty",
        description="IMB6 generative: sent-count 23 not thread volume 2000",
    ),
    ImbGenerativeCase(
        "IMBG7",
        "Who are the people I talk to and interact with?",
        "relationship_context:read",
        Anchors(
            ideal="Bram Holloway, Saskia Vreeland, Tomas Ferro — real participants. "
                  "Odile Ferrant must not appear as an interaction partner.",
            bad_confabulation="Includes Odile Ferrant as someone the owner talks to.",
            bad_noise="Generic networking advice without naming participants.",
        ),
        answer_needles=_static("Bram", "Saskia", "Tomas"),
        poison_groups=(("Odile Ferrant", "Odile"),),
        layer="imb_gen:entity_spine",
        description="IMB7 generative: participants only; Odile poison absent",
    ),
    ImbGenerativeCase(
        "IMBG8",
        "Tell me about my relationship with Odile Ferrant",
        "relationship_context:read",
        Anchors(
            ideal="Unknown / no relationship — mention-only entity, nothing to describe.",
            bad_confabulation="Invented relationship narrative from mention rows.",
            bad_noise="Generic relationship advice naming Odile as a partner.",
        ),
        answerable=False,
        layer="imb_gen:mention_only_entity",
        description="IMB8 generative: abstain on mention-only 'relationship'",
    ),
    ImbGenerativeCase(
        "IMBG9",
        "What did I take away from my reading about fermentation methods?",
        "activity:read",
        Anchors(
            ideal="Owner takeaway from highlight (copper still method) if present; "
                  "never the page-author's cast-iron opinion as the owner's.",
            bad_confabulation="Presents page-author 'cast-iron cookware is strictly "
                              "superior' as the owner's takeaway.",
            bad_noise="Generic fermentation tips with no personal reading signal.",
        ),
        answer_needles=_static("copper", "still", "ferment", "yes", "highlight"),
        poison_groups=(("cast-iron cookware is strictly superior",),),
        layer="imb_gen:browser_highlight",
        description="IMB9 generative: highlight takeaway; page-author poison absent",
    ),
    ImbGenerativeCase(
        "IMBG10",
        "What's my style preference?",
        "ai_conversations:read",
        Anchors(
            ideal="Linen workwear (user-typed). Assistant's raw-denim aesthetic must "
                  "not become owner identity.",
            bad_confabulation="States raw-denim aesthetic as the owner's preference.",
            bad_noise="Generic fashion advice without personal preference.",
        ),
        answer_needles=_static("linen", "workwear", "style"),
        poison_groups=(("raw-denim aesthetic",),),
        layer="imb_gen:ai_chat_roles",
        description="IMB10 generative: user preference; assistant poison absent",
    ),
]


def answerable_cases() -> List[ImbGenerativeCase]:
    return [c for c in IMB_GENERATIVE_CASES if c.answerable]


def unanswerable_cases() -> List[ImbGenerativeCase]:
    return [c for c in IMB_GENERATIVE_CASES if not c.answerable]


def poison_hit(answer: str, poison_groups: Sequence[Sequence[str]]) -> List[str]:
    """Return matched poison group heads present in the generative answer blob."""
    blob = (answer or "").lower()
    hit: List[str] = []
    for group in poison_groups:
        if any(alt.lower() in blob for alt in group):
            hit.append(group[0])
    return hit
