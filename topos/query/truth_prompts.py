"""truth_prompts — "Things you can ask me" seeds from the fun aperture
(PLAN_TRUTHFULNESS_PLUGIN.md follow-on, owner request 2026-07-27).

Prompts are rendered INTO the caller's video frame by Fun Camera, i.e. they
are deliberately published to the other party on the call. Containment:

  * Only fun-mode-eligible facts may seed a prompt (same aperture as
    verify_claim: safe category, disclosure below the ceiling). owner_only
    and sensitive-category facts can never surface a topic.
  * A prompt reveals the TOPIC, never the stance: templates carry the
    object value only — the predicate (enjoys/dislikes/…) is dropped, so
    the viewer learns "there's something about cilantro," and the fun is
    discovering which way the nose goes.
  * Same door policy as verify_claim: caller_app_id mandatory, mode must be
    registered (fun-only in v1), never exposed on /mcp.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List

from .verify_modes import categorize, get_mode, registered_mode_names

# Stance-hiding templates, rotated deterministically per topic.
_TEMPLATES = (
    "Ask me how I really feel about {topic}",
    "Ask me about my history with {topic}",
    "Ask me where I stand on {topic}",
)

# Basics facts flip the hiding rule: the QUESTION comes from the predicate and
# the VALUE stays hidden ("Ask me my name" — never the name itself). The
# viewer asks; the nose adjudicates the answer.
_PREDICATE_QUESTIONS = {
    "goes_by": "Ask me my name",
    "years_old": "Ask me how old I am",
    "hails_from": "Ask me where I'm from",
    "hometown": "Ask me about my hometown",
    "grew_up_in": "Ask me where I grew up",
    "favorite_food": "Ask me about my favorite food",
    "favourite_food": "Ask me about my favorite food",
    "favorite_color": "Ask me my favorite color",
    "favourite_colour": "Ask me my favorite color",
    "favorite_colour": "Ask me my favorite color",
}

MAX_TOPIC_CHARS = 40
DEFAULT_LIMIT = 5
MAX_LIMIT = 8


def suggest_prompts(
    conn: sqlite3.Connection,
    *,
    mode: str = "fun",
    caller_app_id: str = "",
    limit: int = DEFAULT_LIMIT,
) -> Dict[str, Any]:
    resolved = get_mode(mode)
    if resolved is None:
        return {"error": "unknown_mode", "registered_modes": list(registered_mode_names())}
    if not str(caller_app_id or "").strip():
        return {"error": "caller_app_id_required"}
    limit = max(1, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))

    rows = conn.execute(
        "SELECT payload_json FROM signal_objects "
        "WHERE object_type='fact' AND valid_to IS NULL "
        "ORDER BY updated_at DESC LIMIT 500"
    ).fetchall()

    prompts: List[Dict[str, str]] = []
    seen_topics: set[str] = set()
    eligible = 0
    for (payload_json,) in rows:
        if len(prompts) >= limit:
            break
        try:
            payload = json.loads(payload_json or "{}")
        except json.JSONDecodeError:
            continue
        topic = str(payload.get("object_value") or "").strip()
        predicate = str(payload.get("predicate") or "").strip()
        if not topic or not predicate:
            continue
        if str(payload.get("disclosure") or "scoped") in resolved.excluded_disclosures:
            continue
        category, safe = categorize(f"{predicate.replace('_', ' ')} {topic}")
        if not safe or category not in resolved.allowed_categories:
            continue
        eligible += 1
        predicate_key = predicate.lower()
        if predicate_key in _PREDICATE_QUESTIONS:
            # Basics: question from the predicate, value hidden entirely —
            # no topic-safety gate needed because the value never renders.
            if predicate_key in seen_topics:
                continue
            seen_topics.add(predicate_key)
            prompts.append({
                "question": _PREDICATE_QUESTIONS[predicate_key],
                "category": category,
            })
            continue
        # Non-basics prompts DO render the topic — it must independently be
        # safe to speak aloud (a safe predicate must not smuggle a sensitive
        # object into the frame).
        _, topic_alone_safe = categorize(topic)
        if not topic_alone_safe:
            continue
        key = topic.lower()
        if key in seen_topics:
            continue
        seen_topics.add(key)
        template = _TEMPLATES[(len(prompts)) % len(_TEMPLATES)]
        prompts.append({
            "question": template.format(topic=topic[:MAX_TOPIC_CHARS]),
            "category": category,
        })

    return {
        "mode": resolved.name,
        "prompts": prompts,
        "_audit": {
            "mode": resolved.name,
            "caller_app_id": caller_app_id,
            "eligible_facts": eligible,
            "prompt_count": len(prompts),
        },
    }
