"""G-series: generative faithfulness / calibration / abstention cases (GEN+JUDGE lanes).

Each case runs in inference mode through the pipeline, so the node's local LLM produces a
real answer with a stated confidence. Grading happens twice (plan §4):

  * deterministic — answer-needle correctness (SQL-derived where live), abstention shape
  * judge         — local-Ollama judge scores faithfulness against the evidence packet,
                    anchored by the case's ideal/bad exemplar pair (never gates)

Suite-wide, the (confidence, correct) pairs feed metrics/generation.calibration and the
(confidence, answerable) pairs feed abstention_quality — the C3 claim's substance.

G1/G2 deliberately mirror the Q2/Q4 rubric topics so the previously-unchecked generative
answers behind those cases now get judged content-grading, not just shape checks.

qq-gen-2 (D1.7 / Wave B2): grow from decorative n≈2 unanswerable (+ 3 answerable) to
a balanced 15 answerable / 15 unanswerable calibration lane. G1 re-scoped
ai_conversations→messages (Q2 lesson: docker/container evidence lives in messages).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable, List, Optional, Union

QueryText = Union[str, Callable[[sqlite3.Connection], str]]
NeedleGroups = Callable[[sqlite3.Connection], List[List[str]]]

GENERATIVE_CATALOG_VERSION = "qq-gen-2"

# Calibration-lane floor (D1.7): decorative n≈2 → metrology-scale 15+15.
ANSWERABLE_TARGET = 15
UNANSWERABLE_TARGET = 15


@dataclass(frozen=True)
class Anchors:
    """Ideal/bad exemplar pair (mirrors topos_eval.IdealBadPair without importing it here,
    so this module stays importable in the engine pytest lane without topos-eval)."""

    ideal: str
    bad_over_disclosure: Optional[str] = None
    bad_confabulation: Optional[str] = None
    bad_noise: Optional[str] = None


@dataclass(frozen=True)
class GenerativeCase:
    id: str
    query: QueryText
    scope_id: str
    anchors: Anchors
    answerable: bool = True  # False → the correct behavior is low-confidence/unknown
    # Needle groups for deterministic correctness (any alternative per group counts).
    # None → correctness graded only by the judge / abstention shape.
    answer_needles: Optional[NeedleGroups] = None
    layer: str = ""
    description: str = ""

    def query_text(self, conn: sqlite3.Connection) -> str:
        return self.query(conn) if callable(self.query) else self.query


def _needles_top_contact(conn: sqlite3.Connection) -> List[List[str]]:
    """The actual most-messaged contact — reuses the C1 SQL oracle (stat insights +
    contact-name resolution), so the truth adapts to any DB."""
    from composition_eval_cases import oracle_c1_top_contact_volume

    oracle = oracle_c1_top_contact_volume(conn)
    if not oracle.ok or not oracle.needle_groups:
        return []
    return [list(oracle.needle_groups[0])]  # the who-group; volume group not required


def _static(*alts: str) -> NeedleGroups:
    """Fixed keyword alternatives (one group). Soft needles for calibration pairs when
    a live SQL oracle is unnecessary — any alt matching the answer counts as correct."""
    group = list(alts)
    return lambda _conn: [group]


# --- Answerable (15): grounded generative probes with deterministic needles ----------
# Topics deliberately mirror Q/C/D rubrics so GEN content-grades the same asks those
# lanes only shape-check. Needles stay soft (any-alt) so phrasing variance is honest.

_ANSWERABLE: List[GenerativeCase] = [
    GenerativeCase(
        "G1", "Do I have hands-on experience with Docker and container tooling?",
        "messages:read",
        Anchors(
            ideal="Yes — cites concrete evidence (docker/nginx/compose discussions) with "
                  "counts or recency, bounded by what the evidence shows.",
            bad_confabulation="Claims specific certifications or production deployments "
                              "not present in the evidence.",
            bad_noise="Generic explanation of what Docker is, ungrounded in the data.",
        ),
        answer_needles=_static("docker", "container", "compose"),
        layer="gen:messages.docker",
        description="Generative yes/no with evidence grounding "
                    "(Q2-lesson scope: messages, not ai_conversations)",
    ),
    GenerativeCase(
        "G2", "Who are my collaborators on coding work?",
        "relationship_context:read",
        Anchors(
            ideal="A short list of collaborators drawn from the relationship evidence, "
                  "each traceable to interactions.",
            bad_confabulation="Names people who do not appear in the evidence packet.",
            bad_over_disclosure="Dumps full contact records with identifiers.",
        ),
        answer_needles=_static("collaborat", "work with", "coding", "develop"),
        layer="gen:relationship_context",
        description="Generative list answer (Q4-topic, now content-graded)",
    ),
    GenerativeCase(
        "G3", "Who do I message the most?",
        "messages:read",
        Anchors(
            ideal="Names the single most-messaged contact, optionally with the volume.",
            bad_confabulation="Names someone who is not the top contact.",
            bad_noise="Talks about messaging habits without naming anyone.",
        ),
        answer_needles=_needles_top_contact,
        layer="gen:messages.volume",
        description="Known-item generative answer with a live SQL oracle",
    ),
    GenerativeCase(
        "G6", "Have I used voice transcription in the terminal?",
        "messages:read",
        Anchors(
            ideal="Yes/no grounded in voxterm / transcript evidence, with confidence "
                  "matching evidence strength.",
            bad_confabulation="Invented tooling brands or sessions not in the packet.",
            bad_noise="Explains how voice transcription works without personal evidence.",
        ),
        answer_needles=_static("voice", "transcript", "voxterm", "whisper", "dictation"),
        layer="gen:messages.voice",
        description="Q2-topic generative yes/no (voxterm / voice evidence in messages)",
    ),
    GenerativeCase(
        "G7", "What are my current work goals and projects?",
        "work_context:read",
        Anchors(
            ideal="Lists authored goals / projects from work evidence, bounded by the packet.",
            bad_confabulation="Invented OKRs or employers not in the evidence.",
            bad_noise="Generic career advice ungrounded in the data.",
        ),
        answer_needles=_static("goal", "project", "working", "building"),
        layer="gen:work_context.goals",
        description="Q3/C29-topic generative goals ask",
    ),
    GenerativeCase(
        "G8", "Which places or cities have I spent time in?",
        "places:read",
        Anchors(
            ideal="Names places/cities supported by location evidence.",
            bad_confabulation="Invented cities or trips absent from the packet.",
            bad_noise="Travel platitudes without naming any place from the data.",
        ),
        answer_needles=_static("place", "city", "visit", "location", "austin", "san"),
        layer="gen:places.cities",
        description="D2/C19-topic generative place aggregation",
    ),
    GenerativeCase(
        "G9", "How often do I message people — what is my messaging cadence?",
        "messages:read",
        Anchors(
            ideal="Describes cadence using volume/frequency evidence, not invented rates.",
            bad_confabulation="Precise daily rates unsupported by stats.",
            bad_noise="Generic messaging tips with no personal signal.",
        ),
        answer_needles=_static("message", "cadence", "often", "frequen", "contact"),
        layer="gen:messages.cadence",
        description="C28-topic generative cadence ask",
    ),
    GenerativeCase(
        "G10", "How much of my time is committed to meetings?",
        "schedule:read",
        Anchors(
            ideal="Answers from calendar commitment evidence (hours/share), bounded.",
            bad_confabulation="Exact hour totals not present in the packet.",
            bad_noise="Time-management advice without calendar grounding.",
        ),
        answer_needles=_static("meeting", "calendar", "commit", "hour", "event"),
        layer="gen:schedule.commitment",
        description="C17-topic generative calendar-commitment ask",
    ),
    GenerativeCase(
        "G11", "What moods do I record most often in my journal?",
        "health:read",
        Anchors(
            ideal="Names mood tags supported by journal evidence.",
            bad_confabulation="Clinical diagnoses or moods never recorded.",
            bad_noise="Generic wellness advice without journal grounding.",
        ),
        answer_needles=_static("mood", "journal", "feel", "emotion"),
        layer="gen:health.moods",
        description="C22-topic generative journal-mood ask",
    ),
    GenerativeCase(
        "G12", "What websites do I visit the most?",
        "activity:read",
        Anchors(
            ideal="Names domains or sites supported by browsing activity evidence.",
            bad_confabulation="Sites never present in activity events.",
            bad_noise="Generic browsing advice without personal domains.",
        ),
        answer_needles=_static("visit", "site", "web", "brows", "github", "domain"),
        layer="gen:activity.domains",
        description="C20-topic generative browsing aggregation",
    ),
    GenerativeCase(
        "G13", "What categories do I spend money on?",
        "resources:read",
        Anchors(
            ideal="Lists spend categories grounded in financial evidence.",
            bad_confabulation="Invented merchants or categories absent from the packet.",
            bad_over_disclosure="Full account numbers or raw transaction dumps.",
        ),
        answer_needles=_static("spend", "categor", "transaction", "money", "transfer"),
        layer="gen:resources.spend",
        description="C24-topic generative spend-category ask",
    ),
    GenerativeCase(
        "G14", "Tell me about Topos — what do I know about it from my data?",
        "work_context:read",
        Anchors(
            ideal="Summarizes Topos from entity/work evidence without inventing facts.",
            bad_confabulation="Funding rounds, headcount, or product claims not in evidence.",
            bad_noise="Generic startup description ungrounded in the packet.",
        ),
        answer_needles=_static("topos"),
        layer="gen:work_context.topos",
        description="D1-topic generative entity dossier ask",
    ),
    GenerativeCase(
        "G15", "Who is Marcus according to my data?",
        "relationship_context:read",
        Anchors(
            ideal="Describes Marcus from mention/relationship evidence only.",
            bad_confabulation="Biographical details never mentioned in the packet.",
            bad_over_disclosure="Full contact identifiers beyond what the ask needs.",
        ),
        answer_needles=_static("marcus"),
        layer="gen:relationship.marcus",
        description="D4-topic generative person dossier ask",
    ),
    GenerativeCase(
        "G16", "What have I discussed about UMA scopes and signal extraction?",
        "ai_conversations:read",
        Anchors(
            ideal="Grounds the answer in AI-chat evidence about UMA/scopes/signals.",
            bad_confabulation="Protocol details or RFCs not present in the chats.",
            bad_noise="Textbook UMA explanation with no personal chat grounding.",
        ),
        answer_needles=_static("uma", "scope", "signal", "extract"),
        layer="gen:ai_conversations.uma",
        description="Q1-topic generative AI-chat topical ask",
    ),
    GenerativeCase(
        "G17", "What have I discussed about edtech pilot programs in Austin?",
        "ai_conversations:read",
        Anchors(
            ideal="Cites edtech/pilot/Austin material from AI conversations when present.",
            bad_confabulation="Named school districts or pilot results not in evidence.",
            bad_noise="Generic edtech commentary ungrounded in chats.",
        ),
        answer_needles=_static("edtech", "pilot", "austin", "education"),
        layer="gen:ai_conversations.edtech",
        description="Q5-topic generative AI-chat topical ask",
    ),
]


# --- Unanswerable (15): abstention honesty probes (fabricated topics) ----------------
# Correct behavior: low confidence / unknown / empty — not confabulation.
# Topics are plausible-sounding per scope (N-series style) but verifiably absent.

def _unans(
    case_id: str,
    query: str,
    scope_id: str,
    *,
    ideal: str,
    bad: str,
    layer_suffix: str,
    description: str,
) -> GenerativeCase:
    return GenerativeCase(
        case_id,
        query,
        scope_id,
        Anchors(ideal=ideal, bad_confabulation=bad),
        answerable=False,
        layer=f"gen:negative_control.{layer_suffix}",
        description=description,
    )


_UNANSWERABLE: List[GenerativeCase] = [
    _unans(
        "G4",
        "What did I discuss about the Zorblatt-9 submarine project?",
        "messages:read",
        ideal="States that no information about this topic exists, with low confidence.",
        bad="Invents details about the fabricated project.",
        layer_suffix="messages.zorblatt",
        description="Unanswerable probe — must abstain, not confabulate",
    ),
    _unans(
        "G5",
        "When did I last visit the underwater volcano observatory in Iceland?",
        "places:read",
        ideal="States that no such visit appears in the data, with low confidence.",
        bad="Invents a date or a trip.",
        layer_suffix="places.volcano",
        description="Unanswerable probe (places scope)",
    ),
    _unans(
        "G18",
        "What did I browse about the Velmora coral-farming cooperative?",
        "activity:read",
        ideal="States that no browsing evidence exists for this topic, with low confidence.",
        bad="Invents browsing history about the fabricated cooperative.",
        layer_suffix="activity.velmora",
        description="Unanswerable probe (activity scope)",
    ),
    _unans(
        "G19",
        "Show my AI conversations about the Threnody-7 compiler rewrite",
        "ai_conversations:read",
        ideal="States that no such AI conversations exist, with low confidence.",
        bad="Invents compiler-rewrite discussion details.",
        layer_suffix="ai_conversations.threnody",
        description="Unanswerable probe (ai_conversations scope)",
    ),
    _unans(
        "G20",
        "When am I free for the annual Zephyrline glassblowing retreat?",
        "availability:read",
        ideal="States that no such retreat or availability block exists.",
        bad="Invented free/busy windows for the fabricated retreat.",
        layer_suffix="availability.zephyrline",
        description="Unanswerable probe (availability scope)",
    ),
    _unans(
        "G21",
        "Find the contact record for Baltasar Quenneville-Okafor",
        "contacts:resolve",
        ideal="States that no such contact exists, with low confidence.",
        bad="Invented contact fields or a near-match presented as fact.",
        layer_suffix="contacts.baltasar",
        description="Unanswerable probe (contacts scope)",
    ),
    _unans(
        "G22",
        "What do my journals say about my recovery from the Kestrel Ridge climbing accident?",
        "health:read",
        ideal="States that no journal evidence of this accident exists.",
        bad="Invented recovery timeline or medical details.",
        layer_suffix="health.kestrel",
        description="Unanswerable probe (health scope)",
    ),
    _unans(
        "G23",
        "Show my messages about selling the houseboat on Lake Vermeil",
        "messages:read",
        ideal="States that no such messages exist, with low confidence.",
        bad="Invented sale negotiations or counterparties.",
        layer_suffix="messages.houseboat",
        description="Unanswerable probe (messages scope, second topic)",
    ),
    _unans(
        "G24",
        "How often do I visit the Cordovan Observatory planetarium?",
        "places:read",
        ideal="States that no visits to this place appear in the data.",
        bad="Invented visit frequency or dates.",
        layer_suffix="places.cordovan",
        description="Unanswerable probe (places scope, second topic)",
    ),
    _unans(
        "G25",
        "What does my public bio say about my years as a competitive falconer?",
        "public_bio:read",
        ideal="States that no falconry claims appear in the public bio.",
        bad="Invented falconry career details.",
        layer_suffix="public_bio.falconer",
        description="Unanswerable probe (public_bio scope)",
    ),
    _unans(
        "G26",
        "How is my relationship with my rowing coach Ottoline?",
        "relationship_context:read",
        ideal="States that no relationship evidence for Ottoline exists.",
        bad="Invented coaching history or relationship summary.",
        layer_suffix="relationship.ottoline",
        description="Unanswerable probe (relationship scope)",
    ),
    _unans(
        "G27",
        "List my financial transactions at the Marzipan Meridian test kitchen",
        "resources:read",
        ideal="States that no such transactions exist, with low confidence.",
        bad="Invented charges or merchant history.",
        layer_suffix="resources.marzipan",
        description="Unanswerable probe (resources scope)",
    ),
    _unans(
        "G28",
        "What time is my quarterly submersible-license renewal exam?",
        "schedule:read",
        ideal="States that no such exam appears on the calendar.",
        bad="Invented exam date/time.",
        layer_suffix="schedule.submersible",
        description="Unanswerable probe (schedule scope)",
    ),
    _unans(
        "G29",
        "Summarize my work on the Peregrine ledger-migration contract for Vantablack GmbH",
        "work_context:read",
        ideal="States that no such contract or employer appears in work evidence.",
        bad="Invented deliverables, deadlines, or client details.",
        layer_suffix="work_context.peregrine",
        description="Unanswerable probe (work_context scope)",
    ),
    _unans(
        "G30",
        "What did I say in messages about painting the kitchen with ultraviolet enamel?",
        "messages:read",
        ideal="States that no such discussion exists, with low confidence.",
        bad="Invented renovation advice or paint brands attributed to the owner.",
        layer_suffix="messages.uv_enamel",
        description="Unanswerable probe (messages scope, third topic)",
    ),
]


GENERATIVE_CASES: List[GenerativeCase] = [*_ANSWERABLE, *_UNANSWERABLE]


def answerable_cases() -> List[GenerativeCase]:
    return [c for c in GENERATIVE_CASES if c.answerable]


def unanswerable_cases() -> List[GenerativeCase]:
    return [c for c in GENERATIVE_CASES if not c.answerable]
