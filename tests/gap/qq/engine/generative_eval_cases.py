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
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Union

QueryText = Union[str, Callable[[sqlite3.Connection], str]]
NeedleGroups = Callable[[sqlite3.Connection], List[List[str]]]

GENERATIVE_CATALOG_VERSION = "qq-gen-1"


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


GENERATIVE_CASES: List[GenerativeCase] = [
    GenerativeCase(
        "G1", "Do I have hands-on experience with Docker and container tooling?",
        "ai_conversations:read",
        Anchors(
            ideal="Yes — cites concrete evidence (docker/nginx/compose discussions) with "
                  "counts or recency, bounded by what the evidence shows.",
            bad_confabulation="Claims specific certifications or production deployments "
                              "not present in the evidence.",
            bad_noise="Generic explanation of what Docker is, ungrounded in the data.",
        ),
        answer_needles=lambda conn: [["docker", "container", "compose"]],
        layer="gen:ai_conversations",
        description="Generative yes/no with evidence grounding (Q2-topic, now content-graded)",
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
        "G4", "What did I discuss about the Zorblatt-9 submarine project?",
        "messages:read",
        Anchors(
            ideal="States that no information about this topic exists, with low confidence.",
            bad_confabulation="Invents details about the fabricated project.",
        ),
        answerable=False,
        layer="gen:negative_control",
        description="Unanswerable probe — must abstain, not confabulate",
    ),
    GenerativeCase(
        "G5", "When did I last visit the underwater volcano observatory in Iceland?",
        "places:read",
        Anchors(
            ideal="States that no such visit appears in the data, with low confidence.",
            bad_confabulation="Invents a date or a trip.",
        ),
        answerable=False,
        layer="gen:negative_control",
        description="Second unanswerable probe (places scope)",
    ),
]
