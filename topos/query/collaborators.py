"""Who is nearest the owner's own work — the collaborator lane.

WHY THIS EXISTS
---------------
"Who works on this with me?" reached nothing. The scope registry has
`relationship_context:read` for people and `work_context:read` for work, and nothing
joins them: roles are derived from COMMITS and land in `topic_clusters(dimension=work)`,
people are known through CONVERSATIONS and land in `dimension=relationships`, and the two
sets share zero labels. Asked live, the question fell through to the LLM with a packet
that contained goals and no people in it.

WHAT THIS ANSWERS, AND WHAT IT REFUSES TO
-----------------------------------------
Whose conversations sit nearest the work the owner's own commits describe. That is
ENGAGEMENT, not capability, and the two are different claims — the bench's candidate half
asks who can DO a role and cannot be answered here at all: there are zero
`net.demonstrated_skill` facts on this corpus and the person-to-work-cluster join returns
zero pairs. Every string this lane emits says the weaker thing it means.

It is one ranking, not one per role, and that is measured rather than convenient: the ten
work clusters sit at 0.730 mean cosine to each OTHER against 0.773 within themselves, a
separation of 0.043. They are one person's commit messages about one codebase. Ranking
against an individual role returned lifts of two hundredths and the same three people at
the top of every role, which is message volume wearing a job title.

Ordered by warmth, then engagement, because a warm second-best is worth more than a cold
ideal and a person you cannot reach is not a collaborator.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, List, Optional

#: Phrasings that ask who the owner works WITH, as opposed to who they are close to.
#: Kept separate from `closeness._CLOSENESS_RE` so "closest" keeps its interaction
#: ranking instead of being answered from the work region.
_COLLAB_RE = re.compile(
    r"\b(work(s|ing)? (on .{0,40})?with me|work with me|working with me|"
    r"collaborat\w*|co-?workers?|colleagues?|teammates?|"
    r"who (else )?(is |are )?(also )?work(s|ing)? on|"
    r"who (should|could|can) i ask about|who knows about|"
    r"who is (closest|nearest) to (my|the) work)\b"
)
#: "who else is working on X" carries no pronoun and still presupposes the owner is one
#: of them — without this the phrasing fell through for want of the word "my".
_OWNER_RE = re.compile(r"\b(i|my|me|am i|do i|i'm|mine|who else)\b")


def matches_collaborators(query_text: str) -> bool:
    q = (query_text or "").lower()
    return bool(_COLLAB_RE.search(q) and _OWNER_RE.search(q))


def compose_collaborators_answer(people: List[Dict[str, Any]]) -> str:
    lines = []
    for person in people:
        bits = []
        closeness = person.get("closeness")
        bits.append(f"closeness {closeness:.2f}" if isinstance(closeness, (int, float))
                    else "closeness unmeasured")
        bits.append(f"{person.get('messages_considered', 0)} messages")
        if person.get("tie_state"):
            bits.append(str(person["tie_state"]))
        name = person.get("name") or person.get("node_id")
        lines.append(f"- {name}: " + " · ".join(bits))
    return "\n".join(lines)


def try_collaborators(
    conn: sqlite3.Connection,
    query_text: str,
    *,
    packet_resolution: str,
    limit: int = 8,
) -> Optional[Dict[str, Any]]:
    """The lane. Returns an answer payload, or None to fall through.

    Gated on packet_resolution exactly as `closeness` and `facts_direct` are. That gate is
    load-bearing here and not ceremony: this answer NAMES PEOPLE, and it is reachable from
    a work-shaped question. A caller who holds work context but not relationship context
    must not learn the owner's contacts through this door, and a non-owner floors to
    scores_only upstream, so names cannot leave by this route.
    """
    if not matches_collaborators(query_text):
        return None
    if packet_resolution not in ("facts", "facts_all"):
        return None
    try:
        from ..analytics.dataset_resolution import resolve_messaging_dataset
        from ..features.derivation.social_bench import work_engagement

        dataset_id, _ = resolve_messaging_dataset(conn, "")
        result = work_engagement(conn, dataset_id, limit=limit)
    except Exception:  # noqa: BLE001 — this lane must never break a turn
        return None

    people = [p for p in (result.get("people") or []) if not p.get("needs_name")]
    if not people:
        # Fall through rather than assert an absence. The reason is carried so a caller
        # that wants to explain the silence can, but "nobody" from a lane that could not
        # run is the row-limit-as-conclusion failure in another costume.
        return None

    coverage = result.get("coverage") or {}
    return {
        "answer_type": "facts",
        "answer": compose_collaborators_answer(people),
        "items": [p.get("name") for p in people if p.get("name")],
        "collaborators_direct": True,
        # Said in the payload, not only in the docstring: a consumer that renders this
        # without the qualifier would be making the stronger claim on the lane's behalf.
        "collaborators_basis": (
            "people whose conversations with you sit nearest the work your own commits "
            "describe — not people evidenced to be able to do it"
        ),
        "collaborators_scored": coverage.get("scored"),
        "collaborators_why_one_list": coverage.get("why_not_per_role"),
    }
