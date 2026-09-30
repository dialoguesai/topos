"""OD-38 leak set and judge quality: labelled synthetic cases through the node's own entailment check.

Every case is synthetic (``tests/permissions_v2/entailment_cases/*.jsonl``); nothing here reads a node, a
database or an owner file. Output is counts and synthetic case ids only, never case text.

    python scripts/permissions_v2/entailment_eval.py tests/permissions_v2/entailment_cases/dev.jsonl --judge always
    python scripts/permissions_v2/entailment_eval.py <cases.jsonl> --judge local     # the pinned loopback model

``--judge always`` is the worst-case judge (it says ``entailed`` to everything): the false releases it
leaves are the ones the deterministic guards alone do not stop. ``--judge local`` asks the pinned
``qwen3.5:9b-mlx`` at its reviewed digest (``LocalEntailmentJudge``) and reports, separately:

- the judge alone against the ``entailed`` label (precision, recall: is the model a good entailment judge);
- the full check (guards + judge) against the ``release`` label, and every false release by case id.

A claim cited by two messages is released if either message ALONE passes, which is the node's rule.

``--od45`` scores the rule with OD-45's sentence-scoped reported speech on. ``--owner-waivers`` scores what the
owner's list would offer (the owner-waivable guards waived) with a judge that confirms everything: every false
release there is a must-withhold case the owner alone would have to catch.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from topos.permissions_v2 import entailment_grounding as eg  # noqa: E402
from topos.permissions_v2.entity_boundary import normalized, skeleton  # noqa: E402


class TermBoundary:
    """Off-limits terms matched the way ``EntityBoundary._hits`` matches them, without a database."""

    def __init__(self, terms):
        self.terms = {skeleton(term) for term in terms if skeleton(term)}

    def mentions_protected(self, *texts) -> bool:
        long_terms = [t for t in self.terms if len(t) >= 4]
        short_terms = self.terms.difference(long_terms)
        for text in texts:
            plain = normalized(text)
            compact = "".join(ch for ch in plain if ch.isalnum())
            words = {skeleton(t) for t in re.split(r"[\s@:/<>]+", plain)} | {skeleton(t) for t in re.findall(r"[^\W_]+", plain)}
            if short_terms & words or any(t in compact for t in long_terms):
                return True
        return False


class Always:
    def verify(self):
        return None

    def judge(self, claim_text, message):
        return "entailed"


def load(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def claim_of(case):
    return eg.fact_claim(case["predicate"], case["value"]) if case["kind"] == "fact" else eg.goal_claim(case["goal_text"])


def evaluate(cases, judge, *, env=None, waive=frozenset()):
    judge.verify()
    rows = []
    for case in cases:
        claim = claim_of(case)
        boundary = TermBoundary(case.get("offlimits_terms") or [])
        guards, judged, released = [], [], False
        for message in case["messages"]:
            code = eg.guard_failure(claim, message, author_is_owner=case["author"] == "owner", subject_attested=True,
                                    boundary=boundary, waive=waive, env=env)
            guards.append(code)
            verdict = None
            if claim is not None:
                try:
                    verdict = judge.judge(claim.text, message)
                except eg.JudgeUnavailable:
                    verdict = None
            judged.append(verdict)
            # The node: guards first, then a stored verdict; a message-less or unavailable verdict withholds.
            released = released or (code is None and verdict == "entailed")
        rows.append({"id": case["id"], "category": case["category"], "entailed": case["entailed"],
                     "release": case["release"], "guards": guards, "judged": judged, "released": released,
                     # judge alone: the model's own answer on the cited message(s), author known to it only as
                     # "the writer"; an "other" author cannot entail a first-person owner claim.
                     "judge_entailed": case["author"] == "owner" and "entailed" in judged})
    return rows


def _pr(pairs):
    tp = sum(1 for predicted, truth in pairs if predicted and truth)
    fp = sum(1 for predicted, truth in pairs if predicted and not truth)
    fn = sum(1 for predicted, truth in pairs if not predicted and truth)
    tn = sum(1 for predicted, truth in pairs if not predicted and not truth)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(tp / (tp + fp), 4) if tp + fp else None,
            "recall": round(tp / (tp + fn), 4) if tp + fn else None}


def summarize(rows, judge_name):
    per_category = collections.defaultdict(lambda: collections.Counter())
    guard_codes = collections.Counter()
    for row in rows:
        c = per_category[row["category"]]
        c["cases"] += 1
        c["released"] += row["released"]
        c["false_release"] += row["released"] and not row["release"]
        c["missed_release"] += row["release"] and not row["released"]
        for code in row["guards"]:
            guard_codes[code or "pass"] += 1
    out = {"judge": judge_name, "cases": len(rows),
           "false_releases": sorted(r["id"] for r in rows if r["released"] and not r["release"]),
           "full_check_vs_release": _pr([(r["released"], r["release"]) for r in rows]),
           "guards_vs_release": _pr([(any(g is None for g in r["guards"]), r["release"]) for r in rows]),
           "per_category": {k: dict(v) for k, v in sorted(per_category.items())},
           "guard_codes": dict(sorted(guard_codes.items()))}
    if judge_name != "always":
        out["judge_alone_vs_entailed"] = _pr([(r["judge_entailed"], r["entailed"]) for r in rows])
        out["judge_unavailable"] = sum(1 for r in rows for v in r["judged"] if v is None)
        out["missed_releases"] = sorted(r["id"] for r in rows if r["release"] and not r["released"])
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("cases", nargs="+")
    parser.add_argument("--judge", choices=("always", "local"), default="always")
    parser.add_argument("--out", help="write the JSON summary here as well")
    parser.add_argument("--od45", action="store_true", help="OD-45: reported speech vetoes in the value's sentence only")
    parser.add_argument("--owner-waivers", action="store_true",
                        help="the owner's list: owner-waivable guards waived (use with --judge always)")
    args = parser.parse_args(argv)
    cases = [case for path in args.cases for case in load(path)]
    judge = Always() if args.judge == "always" else eg.LocalEntailmentJudge()
    try:
        env = {eg.SENTENCE_REPORTING_FLAG: "true"} if args.od45 else {}
        waive = eg.OWNER_WAIVABLE if args.owner_waivers else frozenset()
        summary = summarize(evaluate(cases, judge, env=env, waive=waive), args.judge)
        summary.update(od45=args.od45, owner_waivers=args.owner_waivers)
    finally:
        getattr(judge, "close", lambda: None)()
    text = json.dumps(summary, indent=1, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)
    return 1 if summary["false_releases"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
