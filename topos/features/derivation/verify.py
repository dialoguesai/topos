"""A4 — assertion verifier (attribution ladder, PLAN_DERIVATION_WAVE2 §WA).

Second pass over each EXTRACTED assertion: a selection-shaped judgment, not a
search. The extractor optimizes recall (measured: qwen3.5-9b keeps 8/9 gold);
the verifier optimizes precision (measured: Qwen3.8-27B-IQ2 kills 21/23 junk
classes but loses half the gold when asked to FIND facts — as a judge it never
has to find anything). Model floors are per-role, config-overridable.

Fail-open BY DESIGN: a verifier error (timeout, parse failure) passes the
assertion through flagged `verifier_status: error` rather than silently dropping
recall on infra failures. The junk gate is measured on verified output, so
fail-open never inflates a gate — it only shows up as coverage loss in telemetry.

Lifecycle note (WA.E): this module holds the line until A5 (entity-first
extraction) proves; then it demotes to an eval-time instrument.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, Optional

VERIFIER_VERSION = "a4-2"
DEFAULT_VERIFIER_MODEL = "smtek/Qwen3.8-27B:IQ2_M"

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)

_PROMPT = """You are a strict fact-checker for a personal knowledge store.
The OWNER wrote or received the record below. An extraction system claims the record
states a fact. Judge the CLAIM against the RECORD only — no outside knowledge.

Record (role={role}, date={date}):
---
{text}
---

Claimed fact: {predicate} = {value}
Claimed about: {about}

Answer three questions:
1. supported: does the record actually STATE this (not hint, plan, hope, or describe
   someone else's situation)? A declined offer, an application, a fundraise, or advice
   received is NOT a completed fact about the owner.
2. about: whose fact is this? "owner" only if the record states it about the author-owner.
   "other:<name>" if it is someone else's (their partner, their appointment, their loss).
   "unclear" if the person cannot be determined from the record.
3. fields_ok: is every NON-EMPTY field value present in or directly stated by the record
   (no invented dates, orgs, titles, or levels)? A field that is null, empty, or omitted
   is honest abstention, NEVER a fabrication — judge only fields that carry a value.

Grounding rules:
- The record's own date IS a stated date for anything the record narrates as happening
  at writing time ("I got cleaned today" on a dated record = dated fact).
- Text the owner merely Likes, quotes, or reacts to is the OTHER person's speech:
  first-person statements inside a quoted/Liked message are about THAT speaker,
  not the owner (their appointment, their firing, their plans).

Respond ONLY with JSON:
{{"supported": true/false, "about": "owner" | "other:<name>" | "unclear", "fields_ok": true/false, "reason": "<=15 words"}}"""


def verifier_model() -> str:
    return os.environ.get("TOPOS_DERIVATION_VERIFIER_MODEL") or DEFAULT_VERIFIER_MODEL


def build_verify_prompt(record_text: str, role: str, date: str,
                        predicate: str, value: Any, about: str) -> str:
    from .template import clean_record_text
    return _PROMPT.format(
        role=role, date=date, text=clean_record_text(record_text)[:2400],
        predicate=predicate,
        value=json.dumps(value, ensure_ascii=False, default=str)[:400],
        about=about or "owner",
    )


def parse_verdict(raw: str) -> Optional[Dict[str, Any]]:
    m = _JSON_RE.search(raw or "")
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict) or not isinstance(d.get("supported"), bool):
        return None
    about = str(d.get("about") or "unclear").strip()
    if not re.match(r"^(owner|unclear|other:.{1,60})$", about):
        about = "unclear"
    return {"supported": d["supported"], "about": about,
            "fields_ok": bool(d.get("fields_ok", True)),
            "reason": str(d.get("reason") or "")[:120]}


def apply_verdict(assertion: Dict[str, Any], verdict: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge a verdict into an assertion. Returns the assertion annotated with
    verifier_status: accepted | rejected | rerouted | error.
    - not supported OR fields not ok  -> rejected (never stored)
    - about resolves to other:<name>  -> rerouted (A3 dossier lane decides storage)
    - about unclear on person-facts   -> rerouted (quarantine, fact_conflicts)
    - verdict None (infra error)      -> error (fail-open, flagged)
    """
    a = dict(assertion)
    if verdict is None:
        a["verifier_status"] = "error"
        return a
    a["verifier_reason"] = verdict["reason"]
    if not verdict["supported"] or not verdict["fields_ok"]:
        a["verifier_status"] = "rejected"
        return a
    extractor_about = str(a.get("about") or "owner")
    final_about = verdict["about"]
    # The verifier can DEMOTE owner->other/unclear (attribution catch) but an
    # extractor's other/unclear is never promoted back to owner by the verifier:
    # two independent passes must agree before a fact lands on the owner.
    if extractor_about != "owner" and final_about == "owner":
        final_about = extractor_about
    a["about"] = final_about
    a["verifier_status"] = "accepted" if final_about == "owner" else "rerouted"
    return a
