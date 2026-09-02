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
    # Bare role words, not just "my <role>": "Do I have any siblings?" carries the
    # owner frame in "I" and was matching nothing, so it fell past the fact lane and
    # the model answered "Yes." without naming the brother it had in hand.
    (r"\bfamily member|(?:my |any |have any )?\b(family|relatives|parents?|siblings?|kids|children|"
     r"brothers?|sisters?|mom|dad|mother|father|grandma|grandpa|grandparents?)\b",
     ["rel.relationship"], False),
    # `friends?` alone matters: it is the most natural phrasing of the question and
    # it matched nothing here, so "Who are my friends?" fell past the fact view to
    # the interim query-time lane while 23 closeness_tier facts sat in the store.
    # Person-scoped and relational forms, not only owner-scoped LIST questions.
    # "How close am I to X", "am I closer to X or Y", "who should I reconnect with"
    # all matched nothing, so the deterministic lane never fired and the model
    # reasoned from generic retrieval — it once claimed no determination about how
    # often the owner talks to someone who is inner_circle at 550 messages.
    (r"\b(closest|inner circle|close circle|close friend|best friend|friend)s?\b"
     r"|\bhow close (?:am i|are we)\b|\bclos(?:er|est) to\b"
     r"|\breconnect\b|\bdrift(?:ed|ing)?\b|\bout of touch\b"
     r"|\bhaven'?t (?:i )?(?:talked|spoken|messaged|texted|heard)\b",
     ["rel.closeness_tier", "rel.relationship"], False),
    (r"\bchronotype|night owl|early bird\b", ["behavior.chronotype"], False),
]


#: (pattern, predicate, special) triples derived from the LOADED pack registry —
#: every declared predicate gets a deterministic question shape from its own
#: name, so a pack derivation is reachable the day it ships instead of waiting
#: for a hand-written alias. Built once per process; failure to load packs
#: leaves the curated aliases as the whole surface (never raises).
_generic_index_cache: Optional[List[tuple]] = None


def _leaf_pattern(leaf: str) -> Optional[str]:
    """`provider_relationship` -> a word-boundary phrase regex, plural-tolerant.

    Conservative by construction: ALL leaf tokens must appear as an ordered
    phrase. Precision beats recall here for the same reason as the curated
    aliases — a false fire replaces a good LLM answer with a wrong
    deterministic one. Richer shapes come from curation, not generation.
    """
    tokens = [t for t in re.split(r"[._]+", leaf.strip().lower()) if t]
    if not tokens:
        return None
    return r"\b" + r"[\s_-]+".join(re.escape(t) + r"s?" for t in tokens) + r"\b"


def _generic_predicate_index() -> List[tuple]:
    global _generic_index_cache
    if _generic_index_cache is not None:
        return _generic_index_cache
    index: List[tuple] = []
    try:
        from topos.features.derivation.packs import load_packs
        from topos.features.derivation.registry import bundled_pack_dir

        packs = load_packs(bundled_pack_dir())
        pack_iter = packs.values() if isinstance(packs, dict) else packs
        for pack in pack_iter:
            for name in pack.predicates:
                leaf = name.split(".", 1)[1] if "." in name else name
                pattern = _leaf_pattern(leaf)
                if not pattern:
                    continue
                special = pack.effective_sensitivity(name) == "special"
                index.append((re.compile(pattern), name, special))
    except Exception:  # noqa: BLE001 — packs unloadable => curated aliases only
        index = []
    # Legacy FactStore vocabulary (pre-pack, no dot, e.g. lives_in / grew_up_in):
    # real stored facts on live nodes that no pack declares. Measured live
    # 2026-09-02: 5 legacy predicates holding facts had no deterministic path.
    # Never special — the legacy writers predate the sensitivity key entirely.
    try:
        from topos.features.facts.store import KNOWN_PREDICATES

        seen = {name for _p, name, _s in index}
        for name in KNOWN_PREDICATES:
            if name in seen:
                continue
            pattern = _leaf_pattern(name)
            if pattern:
                index.append((re.compile(pattern), name, False))
    except Exception:  # noqa: BLE001
        pass
    _generic_index_cache = index
    return index


def match_known_item(query_text: str) -> Optional[Dict[str, Any]]:
    q = (query_text or "").lower()
    # known-item asks are about the OWNER; a question about someone else must
    # never be answered from the owner's fact sheet
    if not re.search(r"\b(i|my|me|am i|do i|i'm)\b", q):
        return None
    # UNION, not first-match. A question can span two predicate families —
    # "Is my mom in my inner circle?" matches the role alias AND the closeness
    # alias — and returning only the first meant the tiers never reached the
    # answer. Live 2026-08-26 that question came back "there's no explicit
    # 'inner circle' label in your relationship context" while Mike November
    # and Quebec Lima held exactly that label.
    #
    # `special` is OR-ed: if any matching class is special it takes the stricter
    # gate, so widening the match can never widen disclosure.
    predicates: List[str] = []
    special = False
    for pattern, preds, is_special in _ALIASES:
        if not re.search(pattern, q):
            continue
        special = special or is_special
        for pred in preds:
            if pred not in predicates:
                predicates.append(pred)
    # The generic layer: every pack-declared predicate is addressable by its
    # own leaf phrase. Special-class here comes from the pack's declared
    # sensitivity (max of pack and predicate), not a hand-kept boolean — which
    # also closes the old asymmetry where beliefs.* and admin.legal special
    # packs had no alias and hence no special gate on the direct lane.
    for pattern, pred, is_special in _generic_predicate_index():
        if pred in predicates or not pattern.search(q):
            continue
        special = special or is_special
        predicates.append(pred)
    return {"predicates": predicates, "special": special} if predicates else None


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
    # `is_self` is not unique: live 2026-08-26 three entities carried it —
    # ent_e73ff33ae330422b ("Owner", 178 facts) plus two "self" rows created that
    # morning holding none. An unordered fetchone() picked the fact-bearing row by
    # rowid luck; a vacuum or a rewrite of that row would have silently blanked
    # every known-item answer. Prefer the self-entity that actually owns facts,
    # entity_id as the tiebreak, so the choice is stable across rewrites. (No
    # created_at in the ORDER BY: the query fixtures build a minimal `entities`
    # table without it.)
    owner = conn.execute(
        """SELECT e.entity_id
             FROM entities e
            WHERE e.is_self=1
         ORDER BY (SELECT COUNT(*) FROM signal_objects o
                    WHERE o.object_type='fact'
                      AND o.object_key LIKE 'fact:' || e.entity_id || ':%') DESC,
                  e.entity_id ASC
            LIMIT 1"""
    ).fetchone()
    if not owner:
        return None
    out: List[Dict[str, Any]] = []
    for pred in predicates:
        # Delimiter-aware: a bare `{pred}%` prefix also swept sibling predicates.
        # "Who's in my family?" asks for rel.relationship and was handed the 23
        # rel.relationship_event rows too, so the answer listed people the owner
        # merely met (live 2026-08-26: Echo Victor returned as family). Match
        # the exact key or the key plus its ':value' segment, nothing else.
        # `_` is a LIKE wildcard and predicates contain it, so escape it.
        like_pred = pred.replace("\\", "\\\\").replace("_", "\\_").replace("%", "\\%")
        rows = conn.execute(
            """SELECT object_key, payload_json, confidence, valid_from, valid_to,
                      ontology_id, altitude
               FROM signal_objects
               WHERE object_type='fact' AND valid_to IS NULL
                 AND (object_key = ? OR object_key LIKE ? ESCAPE '\\')
               ORDER BY valid_from DESC LIMIT 40""",
            (f"fact:{owner[0]}:{pred}", f"fact:{owner[0]}:{like_pred}:%"),
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
    out.sort(key=lambda f: (0 if _is_durable(f.get("value")) else 1, _tier_rank(f.get("value"))))
    return out or None


#: A closeness tier is ORDERED, and the store has no idea in what order. All 23 rows
#: are written in one pass so `valid_from DESC` is arbitrary among them, and the answer
#: to "who's in my close circle?" led with four `peripheral` people.
_TIER_ORDER = {"inner_circle": 0, "close": 1, "regular": 2, "peripheral": 3}


def _tier_rank(value: Any) -> int:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return 0
    if isinstance(value, dict) and value.get("tier"):
        return _TIER_ORDER.get(str(value["tier"]), 9)
    return 0            # a fact with no tier keeps its place


def _is_durable(value: Any) -> bool:
    """A fact whose value carries a standing role/status rather than an event."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return False
    # A closeness tier is a standing state too — without this it sorted as an event
    # and every tier fact fell below every role fact regardless of how close.
    return isinstance(value, dict) and bool(
        value.get("role") or value.get("status") or value.get("tier"))


def _fmt_value(v: Any) -> str:
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except (json.JSONDecodeError, TypeError):
            return str(v)
    if isinstance(v, dict):
        return ", ".join(f"{k}: {vv}" for k, vv in v.items() if vv not in (None, "", []))
    return str(v)


def _fact_labels(facts: List[Dict[str, Any]]) -> List[str]:
    """Subject labels for `items`.

    The game layer fills `items` speculatively from graph/score text before this
    lane runs, and pipeline's `payload.update(direct)` only overwrites the keys
    this lane returns. Leaving `items` behind shipped a correct `answer` next to a
    contradicting list: live 2026-08-26 "Who's in my family?" carried Mom, Dad,
    brother and grandma in `answer` while `items` held NER debris ("U", "AI",
    "##os", "NAME"), and the local synthesis model answered from the debris.
    """
    out: List[str] = []
    seen: set[str] = set()
    for f in facts:
        value = f.get("value")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                value = None
        label = None
        if isinstance(value, dict):
            label = value.get("person") or value.get("name") or value.get("value")
        if not label:
            label = _fmt_value(f.get("value"))
        label = str(label).strip()
        if label and label not in seen:
            seen.add(label)
            out.append(label)
    return out[:20]


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
        "items": _fact_labels(facts),
        "facts_direct": True,
    }
