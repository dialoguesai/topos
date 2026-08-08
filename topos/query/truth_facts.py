"""truth_seed_fact — owner-stated fun facts, written through the aperture.

The one WRITE in the truth feature: the owner types facts about themselves
(FunCamera's "My fun facts" interview) and they land in the FactStore via
normal belief revision. The aperture still governs: a fact that doesn't
categorize fun-safe is REFUSED — this path can fill the fun aperture, never
widen it. Refusal reasons ARE returned here (unlike verify_claim's
refusal==ignorance rule) because the caller is the owner editing their own
sheet, not a claim prober.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict

from .verify_modes import categorize, get_mode
from ..storage.db.write_gate import commit_connection

MAX_VALUE_CHARS = 60
MAX_PREDICATE_CHARS = 40

OWNER_SUBJECT = "self"


def seed_fun_fact(
    conn: sqlite3.Connection,
    *,
    predicate: str,
    value: str,
    mode: str = "fun",
    caller_app_id: str = "",
) -> Dict[str, Any]:
    resolved = get_mode(mode)
    if resolved is None:
        return {"accepted": False, "reason": "unknown_mode"}
    if not str(caller_app_id or "").strip():
        return {"accepted": False, "reason": "caller_app_id_required"}

    predicate = str(predicate or "").strip().lower().replace(" ", "_")[:MAX_PREDICATE_CHARS]
    value = " ".join(str(value or "").split())[:MAX_VALUE_CHARS]
    if not predicate or not value:
        return {"accepted": False, "reason": "predicate_and_value_required"}

    category, safe = categorize(f"{predicate.replace('_', ' ')} {value}")
    if not safe or category not in resolved.allowed_categories:
        return {"accepted": False, "reason": "out_of_aperture", "category": category}
    # The value alone must also be safe: a sensitive value is refused outright;
    # an uncategorized value is allowed only for basics (names/ages/places are
    # not lexicon-categorizable and are disclosed by design).
    value_category, value_safe = categorize(value)
    if value_category is not None and not value_safe:
        return {"accepted": False, "reason": "out_of_aperture", "category": value_category}
    if not (value_safe or category == "basics"):
        return {"accepted": False, "reason": "out_of_aperture", "category": category}

    from ..features.facts.store import FactStore

    fact = FactStore(conn).assert_fact(
        subject_entity_id=OWNER_SUBJECT,
        predicate=predicate,
        object_value=value,
        dimension="interests",
        confidence=0.9,  # user-stated beats extracted priors
        source_refs=[{"table": "user_seed", "record_id": f"fun-facts:{predicate}"}],
        disclosure="scoped",
        asserted_by="owner",
    )
    # assert_fact gates + commits its own writes; this commit only catches any
    # residue on the shared conn — queue it through the gate, never raw.
    commit_connection(conn)
    return {
        "accepted": fact is not None,
        "category": category,
        "_audit": {"caller_app_id": caller_app_id, "predicate": predicate,
                   "category": category, "accepted": fact is not None},
    }
