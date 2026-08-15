#!/usr/bin/env python3
"""M0 viability check — score the rung-1 prototype classifier on a classify catalog.

PLAN_SCOPE_CLASSIFIER.md §7. This answers exactly one question: is a local classifier
worth pursuing at all, before any training data exists? It reports the metrics the
promotion gate will use later — macro-F1, per-scope recall, and the near-miss abstain
rate — so the M0 number is directly comparable to what M3/M4 will produce.

Nothing here trains. Prototypes are the registry's own descriptions and example
questions, so the score is a floor, not a result.

Usage (from topos/)::

    .venv/bin/python scripts/eval_scope_classifier.py
    .venv/bin/python scripts/eval_scope_classifier.py --tau-high 0.5 --sweep
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_CATALOG = (
    ROOT.parent / "topos-eval" / "src" / "topos_eval" / "catalog" / "data" / "role_classify_7.json"
)


def load_cases(path: Path, *, split: str | None) -> list[tuple[str, set[str], str]]:
    blob = json.loads(path.read_text("utf-8"))
    out: list[tuple[str, set[str], str]] = []
    for case in blob["cases"]:
        if case.get("grading_tier") != "label":
            continue
        prov = case.get("provenance") or {}
        if split and prov.get("split") and prov["split"] != split:
            continue
        if split and not prov and split != "all":
            # Hand-authored cases carry no split; they belong to neither side.
            continue
        gold = {g for g in case["gold_labels"]}
        band = (case.get("notes") or "").split()[0].rstrip(":") if case.get("notes") else "?"
        out.append((case["turns"][0]["user_text"], gold, band))
    return out


def macro_f1(pairs: list[tuple[set[str], set[str]]]) -> float:
    labels = {x for gold, _ in pairs for x in gold} | {x for _, pred in pairs for x in pred}
    labels.discard("none")
    scores = []
    for label in sorted(labels):
        tp = sum(1 for gold, pred in pairs if label in gold and label in pred)
        fp = sum(1 for gold, pred in pairs if label not in gold and label in pred)
        fn = sum(1 for gold, pred in pairs if label in gold and label not in pred)
        if tp + fn == 0:
            continue
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        scores.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    return sum(scores) / len(scores) if scores else float("nan")


def evaluate(cases, *, tau_high: float, tau_low: float) -> dict:
    from topos.query.scope_classifier import classify

    pairs: list[tuple[set[str], set[str]]] = []
    per_scope = collections.defaultdict(lambda: [0, 0])
    per_band = collections.defaultdict(lambda: [0, 0])
    escalated = abstained = exact = 0

    for text, gold, band in cases:
        verdict = classify(text, tau_high=tau_high, tau_low=tau_low)
        pred = set(verdict.labels)
        if verdict.escalated:
            escalated += 1
        if verdict.abstained:
            abstained += 1
        pairs.append((gold - {"none"}, pred))

        want_abstain = gold == {"none"}
        ok = (not pred) if want_abstain else (pred == gold - {"none"})
        exact += int(ok)
        per_band[band][1] += 1
        per_band[band][0] += int(ok)
        for label in gold - {"none"}:
            per_scope[label][1] += 1
            per_scope[label][0] += int(label in pred)

    negatives = [(g, p) for g, p in pairs if not g]
    return {
        "n": len(cases),
        "exact": exact / len(cases) if cases else float("nan"),
        "macro_f1": macro_f1(pairs),
        "escalation_rate": escalated / len(cases) if cases else float("nan"),
        "abstain_rate": abstained / len(cases) if cases else float("nan"),
        "negative_abstain": (
            sum(1 for _, p in negatives if not p) / len(negatives) if negatives else float("nan")
        ),
        "per_scope_recall": {k: v[0] / v[1] for k, v in sorted(per_scope.items()) if v[1]},
        "per_band_exact": {k: v[0] / v[1] for k, v in sorted(per_band.items()) if v[1] >= 10},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    ap.add_argument("--split", default="all", help="all | train | heldout")
    ap.add_argument("--tau-high", type=float, default=None)
    ap.add_argument("--tau-low", type=float, default=None)
    ap.add_argument("--sweep", action="store_true", help="sweep tau_high to find the knee")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args(argv)

    from topos.query.scope_classifier import TAU_HIGH, TAU_LOW

    tau_high = args.tau_high if args.tau_high is not None else TAU_HIGH
    tau_low = args.tau_low if args.tau_low is not None else TAU_LOW

    if not args.catalog.is_file():
        print(f"catalog not found: {args.catalog}", file=sys.stderr)
        return 2

    split = None if args.split == "all" else args.split
    cases = load_cases(args.catalog, split=split)
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print("no cases matched that split", file=sys.stderr)
        return 2

    print(f"catalog {args.catalog.name}  split={args.split}  n={len(cases)}")

    if args.sweep:
        print(f"\n{'tau_high':>9s} {'macroF1':>8s} {'exact':>7s} {'escal':>7s} {'neg-abst':>9s}")
        for th in (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60):
            r = evaluate(cases, tau_high=th, tau_low=tau_low)
            print(
                f"{th:9.2f} {r['macro_f1']:8.3f} {r['exact']:7.3f} "
                f"{r['escalation_rate']:7.3f} {r['negative_abstain']:9.3f}"
            )
        return 0

    r = evaluate(cases, tau_high=tau_high, tau_low=tau_low)
    print(f"  tau_high={tau_high}  tau_low={tau_low}")
    print(f"  macro-F1            {r['macro_f1']:.3f}")
    print(f"  exact-set accuracy  {r['exact']:.3f}")
    print(f"  escalation rate     {r['escalation_rate']:.3f}")
    print(f"  abstain rate        {r['abstain_rate']:.3f}")
    print(f"  negatives abstained {r['negative_abstain']:.3f}")
    print("\n  per-scope recall:")
    for scope, rec in sorted(r["per_scope_recall"].items(), key=lambda kv: -kv[1]):
        flag = "" if rec >= 0.60 else "   <-- below the 0.60 gate"
        print(f"    {scope:30s} {rec:.3f}{flag}")
    print("\n  per-band exact:")
    for band, acc in sorted(r["per_band_exact"].items(), key=lambda kv: -kv[1]):
        print(f"    {band:30s} {acc:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
