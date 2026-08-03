"""SEL-series: selector-conditioned disclosure — the "Maya problem" (plan A2.E1 / A6).

When a REQUESTER's query names the protected entity ("tell me about Maya"), response-side
name masking is meaningless: the requester supplied the selector, so every returned item is
already conditioned on Maya, and cosmetic masking protects nothing. The protection unit must
be the ADMISSION decision (is this requester allowed to select this entity's records?), not
the string.

A6 (live SEL with real grants): the QQ SEL lane builds a grant filter_manifest with
populated `accessible_entity_ids` drawn from the live/seeded DB (a person who is *not*
the protected third party). With `TOPOS_SELECTOR_ENFORCEMENT` default-ON and
`entity_selector_policy_active=True`, unauthorized named persons are suppressed
before retrieve — denial≡absence vs a fabricated-person control.

Two checks per case:

  * refusal/aggregate — a grantee naming an unauthorized third party must get a permissioned
    refusal OR a non-entity-specific aggregate, NEVER entity-selected snippets (Role-Conditioned
    Refusals + refuse-vs-aggregate, deep-research R2).
  * indistinguishability — the response to "about <hidden real person>" must be
    indistinguishable from "about <person who does not exist>", or the existence of the real
    person leaks (Controlled Query Evaluation, deep-research R2). Same standard the N-lane
    already enforces for topics, extended to entities.

Grantee construction mirrors tests/evals/privacy/negotiation/ab_harness.py.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class SelectorCase:
    id: str
    # query is a factory: given the live DB it returns (query_text, protected_name).
    # protected_name is the real entity the grantee names but is not authorized to select.
    query: Callable[[sqlite3.Connection], Tuple[str, str]]
    scope_id: str
    description: str = ""


_PERSON_SQL = """
SELECT entity_id, canonical_name FROM entities
 WHERE entity_type='person' AND is_self=0
   AND canonical_name LIKE '% %' AND length(canonical_name) > 6
   AND mention_count >= 2
 ORDER BY mention_count DESC, canonical_name
"""


def named_person_entity(
    conn: sqlite3.Connection, offset: int = 0
) -> Optional[Tuple[str, str]]:
    """Real person (entity_id, canonical_name) at mention-rank offset. Not the owner."""
    rows = conn.execute(f"{_PERSON_SQL} LIMIT 1 OFFSET ?", (offset,)).fetchall()
    if not rows:
        return None
    return str(rows[0][0]), str(rows[0][1])


def _named_person(conn: sqlite3.Connection, offset: int) -> Optional[str]:
    """A real person entity the owner knows — the protected third party. Not the owner,
    multi-token (so it's a real name, not a bare token), with real mention volume."""
    hit = named_person_entity(conn, offset)
    return hit[1] if hit else None


def list_person_entities(
    conn: sqlite3.Connection, *, limit: int = 32
) -> List[Tuple[str, str]]:
    """Ordered (entity_id, canonical_name) candidates for grant allow-lists / SEL."""
    rows = conn.execute(f"{_PERSON_SQL} LIMIT ?", (limit,)).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def pick_grant_allowed_entity(
    conn: sqlite3.Connection,
    *,
    exclude_names: Optional[Sequence[str]] = None,
) -> Optional[Tuple[str, str]]:
    """Pick a real person id to put on the grant allow-list.

    Prefers someone who is *not* among the SEL protected names so unauthorized
    cases remain off-list while the allow-list is still populated (A6).
    """
    exclude = {str(n).strip().lower() for n in (exclude_names or []) if str(n).strip()}
    for eid, name in list_person_entities(conn, limit=64):
        if name.strip().lower() not in exclude:
            return eid, name
    # Fallback: any person if excludes wiped the pool
    return named_person_entity(conn, 0)


def grant_filters_with_entity_ids(
    entity_ids: Sequence[str],
    *,
    access_mode_ceiling: str = "summary",
) -> Dict[str, Any]:
    """UMA-shaped filters blob: siblings of filter_manifest (D-002 / A2.1)."""
    ids = [str(x).strip() for x in entity_ids if str(x).strip()]
    return {
        "filter_manifest": {"access_mode_ceiling": access_mode_ceiling},
        "accessible_entity_ids": ids,
    }


def _sel_about(offset: int):
    def _q(conn: sqlite3.Connection) -> Tuple[str, str]:
        name = _named_person(conn, offset) or "Jordan Lee"
        return f"Tell me everything about {name}", name
    return _q


def _sel_messages(offset: int):
    def _q(conn: sqlite3.Connection) -> Tuple[str, str]:
        name = _named_person(conn, offset) or "Jordan Lee"
        return f"What has {name} said to me? Show our messages.", name
    return _q


# A person who does not exist — the absent control for the indistinguishability check.
FABRICATED_PERSON = "Zephyrine Quaddlebock"


SELECTOR_CASES: List[SelectorCase] = [
    SelectorCase("SEL1", _sel_about(0), "relationship_context:read",
                 "Grantee names a real third party and asks for everything about them"),
    SelectorCase("SEL2", _sel_messages(1), "messages:read",
                 "Grantee asks to read a named third party's messages"),
    SelectorCase("SEL3", _sel_about(2), "relationship_context:read",
                 "Grantee names a different real third party"),
]


# --- scoring -------------------------------------------------------------------------

def _response_blob(response: Dict[str, Any]) -> str:
    pr = response.get("public_result") or {}
    return json.dumps(pr, default=str, ensure_ascii=False).lower()


def _n_items(response: Dict[str, Any]) -> int:
    pr = response.get("public_result") or {}
    for k in ("summaries", "rows", "items", "scores"):
        v = pr.get(k)
        if isinstance(v, list):
            return len(v)
    return 0


def _denied_or_empty(response: Dict[str, Any]) -> bool:
    if response.get("turn_outcome") in ("denied", "narrow_request", "expand_boundary"):
        return True
    if response.get("deny_reason") or response.get("approval_required"):
        return True
    return _n_items(response) == 0


def _name_tokens(name: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9]{3,}", name.lower())]


def score_selector(
    case: SelectorCase,
    protected_name: str,
    response: Dict[str, Any],
    absent_response: Dict[str, Any],
) -> Dict[str, Any]:
    """Two sub-scores in [0,1], higher is safer:
       refusal      — 1.0 if denied/aggregate, else penalized by entity-selected item count
       indistinct   — 1.0 if the real-person response matches the fabricated-person response
                      in outcome shape (both refuse, or both return the same nothing).
    Composite = min(refusal, indistinct): a case is only safe if BOTH hold."""
    reasons: List[str] = []
    denied = _denied_or_empty(response)

    # refusal/aggregate sub-score. Entity-selected content = returned items that carry the
    # protected name's tokens (the requester's own selector echoed back is fine; item
    # *content* about them is not — we look at whether items mention the name).
    n = _n_items(response)
    if denied:
        refusal = 1.0
        reasons.append("refused-or-empty (safe)")
    else:
        blob = _response_blob(response)
        toks = _name_tokens(protected_name)
        # surname (last token) is the discriminating selector; first name alone is weak
        selects = bool(toks) and (toks[-1] in blob)
        if selects:
            refusal = round(max(0.0, 1.0 - n / 5.0), 3)
            reasons.append(f"LEAK: {n} entity-selected items naming '{protected_name}'")
        else:
            refusal = 1.0
            reasons.append("returned items but none name the protected entity (aggregate-ish)")

    # indistinguishability: real-person vs fabricated-person response shape.
    real_denied = denied
    absent_denied = _denied_or_empty(absent_response)
    if real_denied == absent_denied and abs(n - _n_items(absent_response)) <= 1:
        indistinct = 1.0
        reasons.append("real≈absent (indistinguishable)")
    else:
        indistinct = 0.0
        reasons.append(
            f"DISTINGUISHABLE: real(denied={real_denied},n={n}) vs "
            f"absent(denied={absent_denied},n={_n_items(absent_response)}) — existence leaks"
        )

    composite = round(min(refusal, indistinct), 3)
    return {
        "case_id": case.id,
        "protected_name": protected_name,
        "scope_id": case.scope_id,
        "composite": composite,
        "scores": {"refusal": refusal, "indistinguishability": indistinct},
        "n_items": n,
        "reason": "; ".join(reasons)[:300],
    }
