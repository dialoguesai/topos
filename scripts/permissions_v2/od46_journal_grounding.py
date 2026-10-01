"""Journal-cited facts and goals through the release grounding rules, and which one guard change would free the
most of them. Owner-local, on a census copy, counts only: no content, value, name or identifier is printed.

Plan: WS0's journal lane (30 Sep) on OD-46 / OD-52; design LC/JOURNAL_AND_BROWSER_SOURCES_DESIGN.md §2.3, §4.6.
It builds on `od50_journal_browser_sources.journal_typed` (the journals session's gates) with three grounding
rules and a what-if per guard:

  (a) fullmatch: the node's floor (`explicitly_states_claim`, `_goal_stated`) over the whole entry, and over
      any one sentence of it;
  (b) OD-38/OD-45: `entailment_grounding.guard_failure` as the node runs it with both flags on (reported speech
      scoped to the value's sentence), no waivers; the verdict is assumed (it is what (c) supplies);
  (c) owner-confirm (OD-38 option 1): (b) with the owner-waivable guards waived.

Gates before grounding, per cited entry (the record-citation decision: a journal-cited item shows its entry):
not NSFW-flagged, Off-limits clear (the boundary's own row match and mention link), not owner-only, at most 8,000
characters. Facts also need a releasable class (`predicate_classes`), a scalar value and the attested subject;
goals need an attested self to exist. Windows are 30 / 90 / 365 days on the entry's stated day (naive stamps read
as their day, OD-53).

(d) the structured goal-field rule (IF-5 Lane H1, `journal_goal_field.refusal`, called, not mirrored), as if its
    flag were on: a goal that is its entry's goal field verbatim, every guard of the rule passing. The census reads
    no review here, so the entry's authorship and its sensitivity are assumed (owner-original, personal): an upper
    bound on those two inputs only, like the other columns. `structured_goal_field` counts the entries whose field
    the rule grounds (`releasable:engine_rule`), which is what the lane's derivation would store.

The what-if: journal entries are many sentences, and OD-38 judges negation, hedges, ended states, sarcasm and
questions over the WHOLE message (only reported speech is sentence-scoped, by OD-45). `scope:<guard>` counts
what passes if that one guard were judged on the value's own sentence instead, every other guard unchanged
(Off-limits and special categories stay whole-entry in every variant). `scope:none` must equal (b)/(c)
exactly; the script checks that and reports `detector_parity`.

Run (zsh; every flag its own token):
  export TOPOS_DATABASE_PATH=<scratch>/throwaway.db TOPOS_ENV_FILE=<scratch>/topos.env
  PYTHONPATH=$PWD <engine venv>/bin/python3 scripts/permissions_v2/od46_journal_grounding.py \\
      --copy <candidates>/census-copy/<run-id> --out <LC>/runs/<run-id>/od46-journal-grounding.json
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import census_support as cs  # noqa: E402

WINDOWS = {"30d": 30, "90d": 90, "365d": 365}
SCOPABLE = ("question_or_quote", "negated", "hedged", "sarcasm", "ended", "not_yet")
OD45_ON = {"TOPOS_PERMISSIONS_V2_ENTAILMENT_SENTENCE_REPORTING": "true"}


def stated_day_age(now_s: int, stamp) -> float | None:
    """Age in days of the entry's stated day: a naive stamp is its day, never a guessed instant (OD-53)."""
    from topos.permissions_v2.fact_eligibility import canonical_utc_microseconds
    us = canonical_utc_microseconds(stamp)
    if us is not None:
        return (now_s * 1_000_000 - us) / 86_400_000_000
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        # The whole stated day must be inside the window: measure from the day's START (the conservative edge).
        parsed = datetime(parsed.year, parsed.month, parsed.day, tzinfo=timezone.utc)
    return (now_s - parsed.timestamp()) / 86_400


# --- speech-act detectors: the exact whole-message tests in entailment_grounding.guard_failure --------------

def detectors(eg, claim, text: str) -> set[str]:
    """Which speech-act guards fire on `text`, by guard_failure's own tests (same constants, same order-free)."""
    folded = eg._fold(text)
    words_list = eg.tokens(folded)
    words = set(words_list)
    fired = set()
    if "?" in folded or any(mark in folded for mark in eg._QUOTES) or re.search(r"(?:^|\s)'\S", folded):
        fired.add("question_or_quote")
    if eg.NEGATIONS & words or any(w.endswith("n't") or (w.endswith("nt") and w[:-2] + "n't" in eg._NT)
                                   for w in words):
        fired.add("negated")
    if eg.HEDGES & words or any(eg._has_sequence(words_list, p) for p in eg.HEDGE_PHRASES):
        fired.add("hedged")
    raw_words = eg._TOKEN.findall(folded)
    if (eg.SARCASM & words or any(eg._has_sequence(words_list, p) for p in eg.SARCASM_PHRASES)
            or any(sign in text for sign in eg.SARCASM_SIGNS) or eg.SHOUTED & set(raw_words)
            or re.search(r"([^\W\d_])\1{3,}", folded.casefold()) or "!!" in folded):
        fired.add("sarcasm")
    anchor_words = set(eg.tokens(claim.anchor))
    anchor_stems = {eg.stem(w) for w in anchor_words}
    ended = {w for w in eg.ENDED & words if w not in anchor_words and eg.stem(w) not in anchor_stems}
    if claim.relation != "prefers" and "over" in words - anchor_words:
        ended.add("over")
    if claim.relation not in eg.PAST_PREDICATES and (ended or any(
            eg._has_sequence(eg._without_used_to_idiom(words_list), p) for p in eg.ENDED_PHRASES)):
        fired.add("ended")
    if claim.kind == "fact" and claim.relation not in eg.PAST_PREDICATES and (eg.FUTURE - anchor_words) & words:
        fired.add("not_yet")
    return fired


def scoped_passes(eg, claim, content: str, *, boundary, waive: frozenset) -> dict:
    """For each variant (none, each SCOPABLE guard, all), does the claim pass? Off-limits and special stay whole."""
    waive = frozenset(waive) & eg.OWNER_WAIVABLE
    base = eg.guard_failure(claim, content, author_is_owner=True, subject_attested=True, boundary=boundary,
                            waive=waive, env=OD45_ON)
    out = {"node_rule": base is None, "code": base}
    sentence = eg._anchor_sentence(claim, content)
    if sentence is None:
        out.update({f"scope:{g}": False for g in ("none", *SCOPABLE, "all")})
        return out
    # Every non-speech-act guard is already sentence-level (anchor, subject, third party, names, cues, specifics)
    # or must stay whole (length, shape, author, Off-limits, special, OD-45's own reported test). The sentence
    # passing guard_failure on its own, plus the whole-entry checks that must stay whole, is the scoped verdict.
    sentence_code = eg.guard_failure(claim, sentence, author_is_owner=True, subject_attested=True,
                                     boundary=boundary, waive=waive, env=OD45_ON)
    sentence_ok = sentence_code is None
    out["sentence_code"] = sentence_code
    folded, words = eg._fold(content), None
    words = eg.tokens(folded)
    whole_ok = not (len(content) > eg.MAX_MESSAGE_CHARS and "entailment_too_long" not in waive)
    try:
        whole_ok = whole_ok and not boundary.mentions_protected(claim.text, claim.anchor, content)
    except Exception:  # noqa: BLE001 -- unavailable boundary withholds
        whole_ok = False
    whole_ok = whole_ok and not (eg.SPECIAL & (set(words) | set(eg.tokens(claim.text)))
                                 or eg.SPECIAL & {eg.stem(w) for w in words + eg.tokens(claim.text)})
    fired = detectors(eg, claim, content)
    if "entailment_question_or_quote" in waive:
        fired.discard("question_or_quote")
    for g in ("none", *SCOPABLE):
        rest = fired - ({g} if g != "none" else set())
        out[f"scope:{g}"] = bool(sentence_ok and whole_ok and not rest)
    out["scope:all"] = bool(sentence_ok and whole_ok)
    out["fired"] = fired
    # What-if: diary ellipsis. A sentence with no pronoun and no person word at all ("Worked on X") is read as
    # the owner speaking: "I " is prefixed and the whole check re-run, every other guard unchanged. A sentence
    # naming anyone (a third-party word or pronoun) never gains a subject.
    sentence_words = set(eg.tokens(sentence))
    if (out["sentence_code"] == "entailment_not_first_person" and not eg.FIRST_PERSON_TOKENS & sentence_words
            and not eg.THIRD_PARTY & sentence_words):
        elided = content.replace(sentence.strip(), "I " + sentence.strip()[:1].lower() + sentence.strip()[1:], 1)
        out["elided_subject:node_rule"] = eg.guard_failure(claim, elided, author_is_owner=True, subject_attested=True,
                                                          boundary=boundary, waive=waive, env=OD45_ON) is None
        new_sentence = eg._anchor_sentence(claim, elided)
        out["elided_subject:scope_all"] = bool(whole_ok and new_sentence is not None and eg.guard_failure(
            claim, new_sentence, author_is_owner=True, subject_attested=True, boundary=boundary, waive=waive,
            env=OD45_ON) is None)
    return out


class Protected:
    """OD-38's `mentions_protected`, from the entity boundary's own term match (as od46_model_yield does)."""

    def __init__(self, boundary):
        self.boundary = boundary

    def mentions_protected(self, *texts):
        return self.boundary.active and any(self.boundary._hits({"content": t}) for t in texts if isinstance(t, str))


def measure(copy_root: Path) -> dict:
    from topos.disclosure.content_policy import is_record_nsfw
    from topos.permissions_v2 import entailment_grounding as eg
    from topos.permissions_v2.canonical import PolicyError
    from topos.permissions_v2.entity_boundary import EntityBoundary
    from topos.permissions_v2.evidence import SHAREABLE_DISCLOSURES
    from topos.permissions_v2.identity import attested_self
    from topos.permissions_v2 import journal_goal_field as jgf
    from topos.permissions_v2.knowledge_projections import PREDICATE_TEXT, _goal_stated
    from topos.permissions_v2.native_claim_grounding import explicitly_states_claim
    from topos.permissions_v2.predicate_classes import CLASSES, scalar

    manifest = json.loads((copy_root / "census-copy-manifest.json").read_text())
    if not manifest["consistency"]["consistent"]:
        raise cs.CensusRefused("copy_not_consistent")
    now_s = int(manifest["copied_at"])
    conn = cs.ro(copy_root / "database.db", immutable=True)
    conn.row_factory = sqlite3.Row
    boundary = EntityBoundary(conn)
    protected = Protected(boundary)
    owner_self = attested_self(conn)
    people = jgf.known_people(conn)
    journal = {row["entry_id"]: dict(row) for row in conn.execute("SELECT * FROM journal_entries")}

    def field_rule(text, row):
        """The engine's goal-field rule as if its flag were on; authorship and sensitivity assumed (no review)."""
        return jgf.refusal(text, row, boundary=protected, author_is_owner=True, subject_attested=owner_self is not None,
                           sensitivity="personal", people=people, env={jgf.FLAG: "true"})

    def gates(row) -> tuple[dict, dict]:
        age = stated_day_age(now_s, row.get("entry_at"))
        inside = {name: age is not None and 0 <= age <= days for name, days in WINDOWS.items()}
        try:
            offlimits = boundary.active and bool(boundary._hits(row) or boundary._linked(
                row.get("entry_id"), "journal_entries", row.get("source_id")))
        except PolicyError:
            offlimits = True
        owner_only = conn.execute("SELECT 1 FROM owner_only_records WHERE canonical_table='journal_entries' "
                                  "AND record_id=?", (row.get("entry_id"),)).fetchone() is not None
        content = row.get("content")
        return inside, {"not_nsfw": not is_record_nsfw(row), "offlimits_clear": not offlimits,
                        "not_owner_only": not owner_only,
                        "le_8000": isinstance(content, str) and len(content) <= 8000}

    def sentence_fullmatch(content, test):
        return isinstance(content, str) and any(test(s.strip()) for s in re.split(r"(?<=[.!?])\s+|\n+", content)
                                                if s.strip())

    windows = {family: {name: collections.Counter() for name in WINDOWS} for family in ("fact", "goal")}
    codes = {family: {rule: collections.Counter() for rule in ("od38_45", "owner_confirm")}
             for family in ("fact", "goal")}
    fired_outside = {family: collections.Counter() for family in ("fact", "goal")}
    sentence_codes = {family: collections.Counter() for family in ("fact", "goal")}
    parity = collections.Counter()

    def tally(family, inside, gate, *, releasable, whole_fm, sent_fm, verbatim, rules, field=False):
        support = all(gate.values())
        for name, ok in inside.items():
            if not ok:
                continue
            c = windows[family][name]
            c["cited_in_window"] += 1
            for key, value in gate.items():
                c["gate:" + key] += value
            c["support_ok"] += support
            ready = support and releasable
            c["releasable_class_and_support"] += ready
            c["(a) fullmatch_whole_entry"] += ready and whole_fm
            c["(a') fullmatch_one_sentence"] += ready and (whole_fm or sent_fm)
            c["value_verbatim_in_entry"] += ready and verbatim
            c["(d) goal_field_rule"] += ready and field
            for rule, result in rules.items():
                c[f"(b) od38_45:{rule}"] += ready and (result["guards"]["node_rule"] or whole_fm)
                c[f"(c) owner_confirm:{rule}"] += ready and (result["owner"]["node_rule"] or whole_fm)
                for variant in ("elided_subject:node_rule", "elided_subject:scope_all"):
                    c[f"what_if:{variant}:od38_45"] += ready and (result["guards"].get(variant, False)
                                                                  or result["guards"]["node_rule"] or whole_fm)
                    c[f"what_if:{variant}:owner_confirm"] += ready and (result["owner"].get(variant, False)
                                                                        or result["owner"]["node_rule"] or whole_fm)
                for variant in ("none", *SCOPABLE, "all"):
                    c[f"what_if:{variant}:od38_45"] += ready and (result["guards"][f"scope:{variant}"] or whole_fm)
                    c[f"what_if:{variant}:owner_confirm"] += ready and (result["owner"][f"scope:{variant}"] or whole_fm)

    def judge(family, claim, content):
        guards = scoped_passes(eg, claim, content, boundary=protected, waive=frozenset())
        owner = scoped_passes(eg, claim, content, boundary=protected, waive=eg.OWNER_WAIVABLE)
        for rule, result in (("od38_45", guards), ("owner_confirm", owner)):
            codes[family][rule][str(result["code"] or "pass")] += 1
            parity["checked"] += 1
            parity["agree"] += result["node_rule"] == result["scope:none"]
        sentence_codes[family][str(owner.get("sentence_code", "no_anchor_sentence") or "pass")] += 1
        sentence = eg._anchor_sentence(claim, content)
        if sentence is not None:
            outside = guards.get("fired", set()) - detectors(eg, claim, sentence)
            for g in outside:
                fired_outside[family][g] += 1
        return {"all": {"guards": guards, "owner": owner}}

    # ---- facts ----------------------------------------------------------------------------------------------
    facts = collections.Counter()
    for object_id, payload_json, refs_json in conn.execute(
            "SELECT object_id, payload_json, source_refs_json FROM signal_objects "
            "WHERE object_type='fact' AND valid_to IS NULL"):
        try:
            payload, refs = json.loads(payload_json or "{}"), json.loads(refs_json or "[]")
        except ValueError:
            continue
        cited = [journal[str(r.get("record_id"))] for r in refs
                 if isinstance(r, dict) and str(r.get("record_id")) in journal]
        if not cited:
            continue
        facts["cites_journal"] += 1
        predicate = payload.get("predicate")
        value = scalar(predicate, payload) if predicate in CLASSES else payload.get("object_value")
        klass = CLASSES.get(predicate)
        releasable = (klass is not None and payload.get("disclosure") in SHAREABLE_DISCLOSURES
                      and isinstance(value, str) and payload.get("subject_entity_id") == owner_self
                      and owner_self is not None)
        facts["releasable_class"] += klass is not None
        facts["subject_is_attested_self"] += owner_self is not None and payload.get("subject_entity_id") == owner_self
        facts["releasable_class_value_subject"] += releasable
        claim = eg.fact_claim(predicate, value) if predicate in PREDICATE_TEXT and isinstance(value, str) else None
        facts["has_od38_claim"] += claim is not None and releasable
        for row in cited:
            inside, gate = gates(row)
            content = row.get("content") if isinstance(row.get("content"), str) else ""
            whole = isinstance(value, str) and explicitly_states_claim(content, predicate, value)
            sent = isinstance(value, str) and sentence_fullmatch(content, lambda s: explicitly_states_claim(s, predicate, value))
            verbatim = isinstance(value, str) and value.casefold() in content.casefold()
            rules = (judge("fact", claim, content) if claim is not None and releasable else
                     {"all": {k: {"node_rule": False, "code": "no_claim",
                                  **{f"scope:{v}": False for v in ("none", *SCOPABLE, "all")}}
                              for k in ("guards", "owner")}})
            tally("fact", inside, gate, releasable=releasable, whole_fm=whole, sent_fm=sent, verbatim=verbatim,
                  rules=rules)
    # ---- goals ----------------------------------------------------------------------------------------------
    goals = collections.Counter()
    for goal_id, record_id, text in conn.execute("SELECT goal_id, record_id, goal_text FROM user_goals"):
        row = journal.get(record_id)
        if row is None:
            continue
        goals["cites_journal"] += 1
        content = row.get("content") if isinstance(row.get("content"), str) else ""
        releasable = owner_self is not None and isinstance(text, str)
        claim = eg.goal_claim(text) if isinstance(text, str) else None
        inside, gate = gates(row)
        whole = isinstance(text, str) and _goal_stated(content, text)
        sent = isinstance(text, str) and sentence_fullmatch(content, lambda s: _goal_stated(s, text))
        verbatim = isinstance(text, str) and text.strip().rstrip(".!").casefold() in content.casefold()
        rules = (judge("goal", claim, content) if claim is not None and releasable else
                 {"all": {k: {"node_rule": False, "code": "no_claim",
                              **{f"scope:{v}": False for v in ("none", *SCOPABLE, "all")}}
                          for k in ("guards", "owner")}})
        tally("goal", inside, gate, releasable=releasable and claim is not None, whole_fm=whole, sent_fm=sent,
              verbatim=verbatim, rules=rules, field=isinstance(text, str) and field_rule(text, row) is None)
    structured = structured_goals(conn, journal, gates, eg, protected, owner_self, field_rule)
    conn.close()
    return {"structured_goal_field": structured,
            "sentence_level_first_failing_guard_owner_confirm": {f: dict(c) for f, c in sentence_codes.items()},"copy": {"run_id": manifest["run_id"], "copied_at_utc": manifest["copied_at_utc"]},
            "attested_self_resolves": owner_self is not None, "journal_entries": len(journal),
            "facts": dict(facts), "goals": dict(goals),
            "by_window": {family: {name: dict(c) for name, c in per.items()} for family, per in windows.items()},
            "first_failing_guard": {family: {rule: dict(c) for rule, c in per.items()} for family, per in codes.items()},
            "speech_acts_firing_only_outside_the_value_sentence": {f: dict(c) for f, c in fired_outside.items()},
            "detector_parity": dict(parity)}


GOAL_LINE = re.compile(r"\AGoal: (.+?)(?:\n\n|\Z)", re.S)


def structured_goals(conn, journal, gates, eg, protected, owner_self, field_rule) -> dict:
    """The what-if of a journal-family grounding form: the entry's structured goal field (the time-log app's
    `goal`, rendered by build_time_log_content as the entry's first paragraph "Goal: <text>") is the owner's own
    stated goal, verbatim. Special categories and Off-limits stay mandatory; speech-act guards on the goal text
    itself are counted both ways. `releasable:engine_rule` is the engine's own rule (`field_rule`) with these gates:
    the entries the lane's derivation would store a goal for; `engine_rule_codes` is why the rest withhold (365 d)."""
    from topos.permissions_v2.journal_goal_field import structured_field
    from topos.permissions_v2.permitted_derivation import Spec, refusal
    existing = {(rid, (text or "").strip().casefold()) for rid, text in conn.execute(
        "SELECT record_id, goal_text FROM user_goals")}
    per = {name: collections.Counter() for name in WINDOWS}
    codes = collections.Counter()
    for row in journal.values():
        content = row.get("content") if isinstance(row.get("content"), str) else ""
        match = GOAL_LINE.match(content)
        try:
            field = json.loads(row.get("metadata_json") or "{}").get("goal")
        except (ValueError, AttributeError):
            field = None
        if not match and not field:
            continue
        goal = (match.group(1).strip() if match else str(field).strip())
        inside, gate = gates(row)
        support = all(gate.values())
        claim = eg.goal_claim(goal)
        shape_ok = claim is not None and refusal(Spec("goal", "goal", goal), None) is None
        words = eg.tokens(eg._fold(goal))
        special_clear = not (eg.SPECIAL & set(words) or eg.SPECIAL & {eg.stem(w) for w in words})
        offlimits_clear = not protected.mentions_protected(goal)
        speech_clear = claim is not None and not (detectors(eg, claim, goal) - {"not_yet"})
        base = support and shape_ok and special_clear and offlimits_clear and owner_self is not None
        rule_code = field_rule(structured_field(row), row)
        if inside.get("365d"):
            codes[("gate_failed" if not support else rule_code or "pass")] += 1
        for name, ok in inside.items():
            if not ok:
                continue
            c = per[name]
            c["entries_with_goal_field"] += 1
            c["field_equals_goal_line"] += bool(match and field and match.group(1).strip() == str(field).strip())
            c["support_ok"] += support
            c["shape_ok"] += support and shape_ok
            c["special_clear"] += support and shape_ok and special_clear
            c["offlimits_clear"] += support and shape_ok and special_clear and offlimits_clear
            c["releasable:field_rule"] += base
            c["releasable:field_rule_and_speech_acts_on_goal_text"] += base and speech_clear
            c["already_a_stored_goal_verbatim"] += (row.get("entry_id"), goal.casefold()) in existing
            c["releasable:engine_rule"] += support and rule_code is None
    return {**{name: dict(c) for name, c in per.items()}, "engine_rule_codes": dict(codes)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--copy", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    cs.require_scratch_environment()
    started = time.monotonic()
    result = measure(cs.refuse_live(args.copy.expanduser().absolute()))
    result["seconds"] = round(time.monotonic() - started, 1)
    text = json.dumps(result, sort_keys=True, indent=1)
    if args.out is not None:
        out = cs.refuse_live(args.out.expanduser().absolute())
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except cs.CensusRefused as exc:
        print(json.dumps({"refused": str(exc)}), file=sys.stderr)
        sys.exit(2)
