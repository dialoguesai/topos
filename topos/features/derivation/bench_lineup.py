"""Small, explainable purpose taxonomy and bounded bench lineup selection.

Categories are lexical hints about the owner's task, never inferred skills or roles
of a person. Unknown tasks stay uncategorized. No model or derived fact is written.
"""
from __future__ import annotations

import re

VERSION = "bench-lineup-1"
CATEGORIES = (
    ("feedback", "Review and feedback", r"\b(review|reviewing|feedback|edit|editing|critique|missing information)\b"),
    ("building", "Building and troubleshooting", r"\b(build|building|implement|implementing|debug|debugging|troubleshoot|fix|fixing)\b"),
    ("introductions", "Introductions and connections", r"\b(introduce|introduction|introductions|connect me|put me in touch)\b"),
    ("practice", "Learning and practice", r"\b(teach|learn|learning|practice|rehearse|rehearsal|mentor|coaching)\b"),
    ("practical", "Practical support", r"\b(childcare|babysit|pickup|pick up|moving|move furniture|errands|ride to)\b"),
)


def purpose_categories(role):
    # Peers' occupations and replies cannot reclassify what the owner asked for.
    texts = [str(role.get("label") or "")]
    texts += [str(e.get("excerpt") or "") for e in role.get("evidence", [])
              if e.get("speaker") == "owner"]
    text = " ".join(texts)
    result = []
    for category_id, label, pattern in CATEGORIES:
        cues = sorted({m.group(0).lower() for m in re.finditer(pattern, text, re.I)})
        if cues:
            result.append({"id": category_id, "label": label,
                           "basis": "owner_task_wording", "matched_cues": cues})
    return result or [{"id": "other", "label": "Other or unspecified purpose",
                      "basis": "no_supported_category_cue", "matched_cues": []}]


def _strength(role):
    candidates = role.get("candidates", [])
    if any(c.get("bench_qualification", {}).get("status") == "ready" for c in candidates):
        return 0
    if any(c.get("basis") == "request_and_reply" for c in candidates):
        return 1  # Includes limitations; they must not disappear in ranking.
    if any(c.get("capability_status") == "demonstrated" for c in candidates):
        return 2
    return 3 if candidates else 4


def select_lineup(roles, limit=8):
    """Evidence tier first, then uncovered people and purposes, then input recency.

    This is display diversity, not a score for a person's helpfulness. Repeated people
    can still appear for distinct tasks. Coverage makes the display cap explicit.
    """
    pool = [{**r, "purpose_categories": purpose_categories(r)} for r in roles]
    remaining = list(enumerate(pool))
    selected, people_seen, categories_seen = [], set(), set()
    def people(role):
        return {str(c["person_id"]) for c in role.get("candidates", [])}
    def categories(role):
        return {c["id"] for c in role["purpose_categories"] if c["id"] != "other"}
    while remaining and len(selected) < limit:
        chosen = min(remaining, key=lambda pair: (
            _strength(pair[1]),
            -bool(people(pair[1]) - people_seen),
            -bool(categories(pair[1]) - categories_seen), pair[0]))
        remaining.remove(chosen)
        role = chosen[1]
        selected.append(role)
        people_seen.update(people(role)); categories_seen.update(categories(role))
    coverage = {"version": VERSION, "purpose_pool_count": len(pool),
                "displayed_purpose_count": len(selected), "omitted_purpose_count": len(remaining),
                "displayed_unique_people": len(people_seen),
                "selection_basis": "evidence tier, new people, new purpose categories, source order",
                "category_basis": "tentative owner-task wording; not a skill or readiness claim",
                "categories": []}
    for cid, label in [(c[0], c[1]) for c in CATEGORIES] + [("other", "Other or unspecified purpose")]:
        matching = [r for r in pool if any(c["id"] == cid for c in r["purpose_categories"])]
        shown = [r for r in selected if any(c["id"] == cid for c in r["purpose_categories"])]
        coverage["categories"].append({"id": cid, "label": label,
            "pool_purposes": len(matching), "displayed_purposes": len(shown),
            "displayed_people": len(set().union(*(people(r) for r in shown)))})
    return selected, coverage
