"""Engine-owned prompt template + schema-validated output parsing (plan §1.2).

The ENGINE owns the skeleton, output contract, and parser. The pack contributes
vocabulary, definitions, and abstention rules into fixed slots — nothing else.
Non-conforming assertions are dropped and counted, never repaired silently.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .packs import Pack, load_scales

TEMPLATE_VERSION = "shadow-6"
_PACK_DIR = Path(__file__).resolve().parents[4] if False else None  # set by set_pack_dir()


def set_pack_dir(path) -> None:
    """Bind the catalog directory so {scale:} references resolve."""
    global _PACK_DIR
    _PACK_DIR = path


def _enum_of(spec: Any) -> Optional[List[Any]]:
    """Allowed values for a value_schema field, or None if free-form.

    Packs write enums two ways — `{enum: [...]}` and a bare `[...]` list. Treating only
    the first as an enum meant the bare-list fields were neither ADVERTISED in the prompt
    nor ENFORCED in the parser, so `behavior.habit_event.event` (5 legal values) became a
    free-text activity log. Both notations are enums; this is the single place that decides.
    """
    if isinstance(spec, dict) and "enum" in spec:
        return list(spec["enum"])
    if isinstance(spec, dict) and "scale" in spec:
        # THIRD notation: a reference into the shared _scales.yaml vocabulary.
        # Unresolved, cadence went free-text and behavior.habit absorbed a work journal.
        return list(load_scales(_PACK_DIR).get(str(spec["scale"]), []))
    if isinstance(spec, list):
        return list(spec)
    return None


def _predicate_menu(pack: Pack) -> str:
    lines = []
    for p in pack.predicates.values():
        if p.altitude != "stated":
            continue  # extraction emits STATED facts only; inferred/predicted come from synthesis
        if p.values:
            shape = "one of: " + ", ".join(p.values[:14])
        elif p.value_schema:
            keys = []
            for k, v in p.value_schema.items():
                allowed = _enum_of(v)
                req = " [REQUIRED]" if k in (p.required_fields or []) else ""
                if allowed:
                    keys.append(f"{k}{req} MUST BE one of [{', '.join(str(x) for x in allowed[:12])}]")
                else:
                    keys.append(f"{k}{req}(free text)")
            shape = "object with: " + "; ".join(keys)
        else:
            shape = "short string"
        # Notes carry the pack's CONTRACT (what counts as an instance of this predicate),
        # not decoration. Truncating at 100 chars silently cut work.career_event's
        # completed-vs-intended rule off before its examples, so the measured junk for that
        # predicate was scored against a contract the model never saw.
        note = " ".join(str(p.note or "").split())
        lines.append(f"- {p.name}: {shape}" + (f"\n    RULE: {note[:600]}" if note else ""))
    return "\n".join(lines)


def build_prompt(pack: Pack, record_text: str, record_date: str, actor_role: str) -> str:
    g = pack.guidance
    abst = "\n".join(f"- {a}" for a in (g.get("abstention") or []))
    return f"""You extract personal facts about the OWNER of a private journal/message archive.
Lens: {pack.title}. {str(g.get('definitions') or '').strip()}

Record (role={actor_role}, date={record_date}):
---
{record_text[:4000]}
---

Allowed predicates (extract ONLY these; anything else is invalid):
{_predicate_menu(pack)}

Example (a record often holds a fact even when most of it is about something else):
  record: "Pushed the release out. Also — dentist moved my cleaning to Friday. Back to debugging."
  output: {{"assertions": [{{"predicate": "<an encounter/appointment-like predicate from the menu>",
            "value": {{"kind": "appointment"}}, "occurrence_date": null, "confidence": 0.9,
            "quote": "dentist moved my cleaning to Friday"}}]}}

Hard rules:
{abst}
- Extract only what this record actually states about the OWNER. No guesses, no world knowledge.
- Object fields are ALL OPTIONAL: emit only the fields the record states, omit the rest.
  A fact with one known field is still a fact — do NOT skip a fact because other fields are unknown.
- A field marked [REQUIRED] must come FROM THE RECORD. If the record does not state it,
  do NOT guess a plausible value — omit the whole assertion. A required field is a test of
  whether this really is an instance of the predicate, not a box to fill.
- For enum fields pick the CLOSEST allowed value (grandparents/aunts/uncles/cousins -> extended_family).
  NEVER invent an enum value or write a sentence into an enum field — an assertion with an
  out-of-vocabulary enum value is discarded entirely.
- Empty is a good answer for records with no facts for this lens — but when a stated fact IS present, extract it.
- occurrence_date: ISO date the event happened, ONLY for dated events, resolved against the record date.
- quote: the exact phrase (<=15 words) the fact comes from.
- confidence: 0.0-1.0, how clearly the record states it.

Respond with ONLY this JSON, nothing else:
{{"assertions": [{{"predicate": "...", "value": "... or object", "occurrence_date": null, "confidence": 0.0, "quote": "..."}}]}}"""


_JSON_RE = re.compile(r"\{.*\}", re.S)


def parse_output(raw: str, pack: Pack) -> Tuple[List[Dict[str, Any]], int]:
    """Return (valid_assertions, schema_reject_count)."""
    m = _JSON_RE.search(raw or "")
    if not m:
        return [], 1 if (raw or "").strip() else 0
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return [], 1
    valid, rejects = [], 0
    for a in data.get("assertions") or []:
        if not isinstance(a, dict):
            rejects += 1
            continue
        pred = pack.predicates.get(str(a.get("predicate") or ""))
        val = a.get("value")
        if pred is None or pred.altitude != "stated" or val in (None, "", {}):
            rejects += 1
            continue
        if pred.values and isinstance(val, str) and val.strip().lower() not in {v.lower() for v in pred.values}:
            rejects += 1
            continue
        if pred.value_schema:
            if not isinstance(val, dict):
                rejects += 1
                continue
            unknown = set(val) - set(pred.value_schema)
            if unknown:
                val = {k: v for k, v in val.items() if k in pred.value_schema}
                if not val:
                    rejects += 1
                    continue
            missing = [k for k in (pred.required_fields or [])
                       if not str(val.get(k) or "").strip()]
            if missing:
                rejects += 1
                continue
            bad_enum = False
            for k, spec in pred.value_schema.items():
                allowed = _enum_of(spec)
                if allowed and k in val and val[k] is not None:
                    if str(val[k]).strip().lower() not in {str(x).lower() for x in allowed}:
                        bad_enum = True
            if bad_enum:
                rejects += 1
                continue
        try:
            conf = min(1.0, max(0.0, float(a.get("confidence") or 0.5)))
        except (TypeError, ValueError):
            conf = 0.5
        valid.append({"predicate": pred.name, "value": val, "confidence": conf,
                      "occurrence": (str(a.get("occurrence_date"))[:10] if a.get("occurrence_date") else None),
                      "quote": str(a.get("quote") or "")[:200]})
    return valid, rejects
