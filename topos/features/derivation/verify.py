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

{context}Claimed fact: {predicate} = {value}
Claimed about: {about}

Answer three questions:
1. supported: does the record actually STATE this (not hint, plan, hope, or describe
   someone else's situation)? A declined offer, an application, a fundraise, or advice
   received is NOT a completed fact about the owner. When the Lens above defines its
   facts as stated promises, plans or intentions, a stated one IS supported — its
   status field says whether it is done, and "not yet done" is not a reason to reject.
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
                        predicate: str, value: Any, about: str,
                        lens_note: str = "", label_note: str = "") -> str:
    """`lens_note` is the pack's own definition of what its facts ARE; `label_note` is the
    runner's addressing for the record (who sent it, who it was sent to, as labels).

    Measured 2026-09-06 on the commitment ledger: with neither, the verifier rejected
    every parsed commitment — "a plan, not a completed commit" (it read `commit.made` as
    a finished act, where the pack defines it as a stated promise whose status is open)
    and "counterparty phone number is not stated in the record" (the counterparty was the
    record's own recipient label, which is never in the text by construction). The judge
    has to know the lens's semantics and the record's addressing to judge it at all.
    """
    from .template import clean_record_text
    context = ""
    if lens_note:
        context += f"Lens: {lens_note.strip()}\n"
    if label_note:
        context += (f"Addressing (from the record itself, not from its text): {label_note.strip()} "
                    f"A person value equal to one of these labels is the record's own addressing — "
                    f"judge it as stated, never as invented.\n")
    if context:
        context += "\n"
    return _PROMPT.format(
        role=role, date=date, text=clean_record_text(record_text)[:2400],
        predicate=predicate,
        value=json.dumps(value, ensure_ascii=False, default=str)[:400],
        about=about or "owner", context=context,
    )


def lens_note_for(pack: Any) -> str:
    """The pack's title and definition, one line — what its predicates mean."""
    g = getattr(pack, "guidance", None) or {}
    definitions = " ".join(str(g.get("definitions") or "").split())
    title = str(getattr(pack, "title", "") or getattr(pack, "pack", "") or "")
    return f"{title}. {definitions}".strip(". ") if (title or definitions) else ""


def label_note_for(rec: Dict[str, Any]) -> str:
    """The record's addressing as the runner labelled it: speaker for an observed record,
    recipient for an authored direct message. Empty when the runner knew neither."""
    parts = []
    if rec.get("speaker") and rec.get("speaker_entity_id"):
        parts.append(f"written by {rec['speaker']} = id:{rec['speaker_entity_id']}")
    if rec.get("recipient") and (rec.get("recipient_entity_id") or rec.get("recipient_key")):
        label = (f"id:{rec['recipient_entity_id']}" if rec.get("recipient_entity_id")
                 else f"key:{rec['recipient_key']}")
        parts.append(f"sent to {rec['recipient']} = {label}")
    return "; ".join(parts) + ("." if parts else "")


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
