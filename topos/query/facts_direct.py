"""C7 — deterministic known-item answers straight from the fact store (W3.1).

"What medications am I taking?" has an exact answer sitting in signal_objects
with validity dates and source refs. Routing that through an LLM adds latency,
paraphrase risk, and a hallucination surface to a lookup. This lane answers it
directly: matched predicates -> live facts -> composed answer, zero LLM,
walkable citations. It follows the `band` precedent: a deterministic
answer_type the inference pass must not overwrite.

Gating (all three, in order):
  1. packet_resolution allows content (facts / facts_all) — non-owner requesters
     floor to scores_only upstream, so this lane can never fire for them.
  2. special-class predicates (health, beliefs) require facts_all — the same
     rule the retrieval facts lane enforces.
  3. the query matches a curated alias CONSERVATIVELY — a false fire replaces a
     good LLM answer with a wrong deterministic one, so precision beats recall
     here (the opposite trade from the extraction prefilter, deliberately).
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Dict, List, Optional

#: query-phrase -> (predicate list, special_class). Curated, not generated:
#: every row is a known-item ask class from the frozen query set (queries-1).
_ALIASES: List[tuple] = [
    (r"\b(medication|medicine|meds|prescription)s?\b", ["health.medication"], True),
    (r"\ballerg(y|ies|ic)\b", ["health.allergy"], True),
    (r"\b(condition|diagnos|chronic)\w*\b", ["health.condition"], True),
    (r"\bsymptom\w*\b", ["health.symptom"], True),
    (r"\b(appointment|doctor|dentist|checkup)s?\b", ["health.encounter", "health.provider_relationship"], True),
    (r"\b(employer|work for|company i work)\b", ["work.employer", "work.role", "works_at"], False),
    (r"\b(job title|my role|my job|what do i do for work)\b", ["work.role", "work.employment_shape"], False),
    (r"\b(project)s?\b.*\b(work|current|active|my)\b|\bmy projects\b", ["work.project", "works_on"], False),
    (r"\b(career|fired|hired|promoted|laid off)\b", ["work.career_event"], False),
    (r"\bfamily member|my (family|relatives|parents|siblings|kids|children)\b",
     ["rel.relationship"], False),
    (r"\b(closest|inner circle|best friend)s?\b", ["rel.closeness_tier", "rel.relationship"], False),
    (r"\bchronotype|night owl|early bird\b", ["behavior.chronotype"], False),
]


def match_known_item(query_text: str) -> Optional[Dict[str, Any]]:
    q = (query_text or "").lower()
    # known-item asks are about the OWNER; a question about someone else must
    # never be answered from the owner's fact sheet
    if not re.search(r"\b(i|my|me|am i|do i|i'm)\b", q):
        return None
    for pattern, preds, special in _ALIASES:
        if re.search(pattern, q):
            return {"predicates": preds, "special": special}
    return None


def fetch_direct_facts(
    conn: sqlite3.Connection,
    predicates: List[str],
    *,
    special: bool,
    packet_resolution: str,
) -> Optional[List[Dict[str, Any]]]:
    if packet_resolution not in ("facts", "facts_all"):
        return None
    if special and packet_resolution != "facts_all":
        return None
    owner = conn.execute("SELECT entity_id FROM entities WHERE is_self=1").fetchone()
    if not owner:
        return None
    out: List[Dict[str, Any]] = []
    for pred in predicates:
        rows = conn.execute(
            """SELECT object_key, payload_json, confidence, valid_from, valid_to,
                      ontology_id, altitude
               FROM signal_objects
               WHERE object_type='fact' AND valid_to IS NULL
                 AND object_key LIKE ?
               ORDER BY valid_from DESC LIMIT 40""",
            (f"fact:{owner[0]}:{pred}%",),
        ).fetchall()
        for key, payload_json, conf, vf, vt, pack, altitude in rows:
            try:
                p = json.loads(payload_json or "{}")
            except json.JSONDecodeError:
                continue
            val = p.get("value_struct") or p.get("object_value")
            refs = p.get("source_refs") or []
            out.append({
                "predicate": pred, "value": val,
                "confidence": conf, "valid_from": (vf or "")[:10],
                "altitude": altitude or "stated", "pack": pack,
                "evidence_count": len(refs) if isinstance(refs, list) else 0,
            })
    # Durable facts (a standing role/status: "mom, parent, active") outrank event
    # facts ("met X at the saloon") in a known-item answer. Recency-only ordering
    # let a month of introductions push the owner's parents past the compose cap:
    # live 2026-08-26, "Who's in my family?" listed 16 met-events and cut
    # mom/brother while keeping them in store. Stable sort keeps recency DESC
    # within each band, so event feeds ("what happened with X?") are unaffected
    # below the durable block.
    out.sort(key=lambda f: 0 if _is_durable(f.get("value")) else 1)
    return out or None


def _is_durable(value: Any) -> bool:
    """A fact whose value carries a standing role/status rather than an event."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return False
    return isinstance(value, dict) and bool(value.get("role") or value.get("status"))


def _fmt_value(v: Any) -> str:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return str(v)
    if isinstance(v, dict):
        return ", ".join(f"{k}: {vv}" for k, vv in v.items() if vv not in (None, "", []))
    return str(v)


def compose_facts_answer(facts: List[Dict[str, Any]]) -> str:
    lines = []
    for f in facts[:20]:
        bits = [_fmt_value(f["value"])]
        if f.get("valid_from"):
            bits.append(f"since {f['valid_from']}")
        if f.get("altitude") and f["altitude"] != "stated":
            bits.append(f["altitude"])
        n = f.get("evidence_count") or 0
        if n > 1:
            bits.append(f"{n} sources")
        lines.append(f"- {f['predicate']}: " + " · ".join(bits))
    return "\n".join(lines)


def try_facts_direct(
    conn: sqlite3.Connection,
    query_text: str,
    *,
    packet_resolution: str,
) -> Optional[Dict[str, Any]]:
    """The whole lane. Returns an answer payload or None (fall through to LLM)."""
    m = match_known_item(query_text)
    if not m:
        return None
    facts = fetch_direct_facts(conn, m["predicates"], special=m["special"],
                               packet_resolution=packet_resolution)
    if not facts:
        return None   # empty store falls through to the LLM path (it can say so)
    return {
        "answer_type": "facts",
        "answer": compose_facts_answer(facts),
        "facts": facts[:20],
        "facts_direct": True,
    }
