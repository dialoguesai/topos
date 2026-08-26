"""A5-M1 — entity-first candidate retrieval (the Zep move, prototype).

For a record, surface the people the spine already knows who plausibly appear
in it. The extraction prompt then offers them as a CLOSED CHOICE with two
escape hatches (NEW:<name> for genuine discovery, omission for abstention) —
turning attribution from open generation, where small models drop rules, into
selection, where they are measurably strong.

Lexical M1 by design: name-token match against normalized names + aliases +
kin terms. The R0-era version upgrades retrieval; the prompt contract and the
escape hatches are the load-bearing parts and they start here.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import List

_KIN_TERMS = ("mom", "mother", "dad", "father", "grandma", "grandpa", "brother",
              "sister", "aunt", "uncle", "cousin", "wife", "husband")


def _name_tokens(text: str) -> set:
    return {t for t in re.findall(r"[A-Za-z][a-z']+", text or "") if len(t) > 2}


def person_candidates(conn: sqlite3.Connection, record_text: str, limit: int = 8) -> List[str]:
    toks = _name_tokens(record_text)
    low = {t.lower() for t in toks}
    if not low:
        return []
    out: List[str] = []
    seen = set()
    # kin terms are candidates by their own word — the owner's name for them IS the identity
    for kin in _KIN_TERMS:
        if kin in low:
            out.append(kin)
            seen.add(kin)
    rows = conn.execute(
        "SELECT canonical_name, normalized_name, aliases_json FROM entities"
        " WHERE entity_type='person' AND is_self=0").fetchall()
    for canonical, normalized, aliases_json in rows:
        if len(out) >= limit:
            break
        names = {str(normalized or "")}
        try:
            names |= {str(a).lower() for a in json.loads(aliases_json or "[]")}
        except (json.JSONDecodeError, TypeError):
            pass
        for n in names:
            first = n.split(" ")[0] if n else ""
            if first and first in low and n not in seen:
                out.append(str(canonical or n))
                seen.add(n)
                break
    return out[:limit]
