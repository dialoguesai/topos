#!/usr/bin/env python3
"""Fine-tune a DistilBERT scope classifier — rung 3.

PLAN_SCOPE_CLASSIFIER.md §4 rung 3, §7 (promotion gate), §6.4 (auditable manifest).

**Why an encoder and not another head.** §9F measured a linear probe on frozen MiniLM at
0.369 macro-F1 and an MLP on the same embeddings at 0.305 — *worse*. Extra classifier
capacity does not help, which locates the ceiling in the frozen representation rather
than the head. Fine-tuning is the remaining lever that moves it.

**What this costs, so it is a decision and not a surprise.** DistilBERT is ~265 MB
resident against MiniLM's already-loaded 90 MB, and it claims a new `ModelSlot`. The plan
carries a `bad-neighbor` headroom exclusion for exactly this. `--report-only` prints the
budget without training.

**What it refuses to do.** The artifact is validated *before* the fine-tune starts, not
after: a dirty corpus manifest, a drifted label set or a share-alike licence stops the run
in seconds rather than after twenty minutes of GPU time. The written head then has to pass
the same gates again at load.

Usage (from topos/)::

    .venv/bin/python scripts/train_scope_head.py --corpus <dir> --report-only
    .venv/bin/python scripts/train_scope_head.py --corpus <dir> --out ~/.topos/models/scope_head
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_BASE = "distilbert-base-uncased"
DEFAULT_CATALOG = (
    ROOT.parent / "topos-eval" / "src" / "topos_eval" / "catalog" / "data" / "role_classify_8.json"
)

#: §7 plus REBUILD §5. A head ships only if it clears every one of these, not their average.
GATE_MACRO_F1_MARGIN = 0.0     # must at least match the incumbent
GATE_NEG_ABSTAIN = 0.85
GATE_MIN_SCOPE_RECALL = 0.60
#: REBUILD §5: multi-gold recall within 0.05 of single-gold, or the head is single-label
#: in everything but configuration — exactly the defect that motivated the rebuild.
GATE_MULTI_GAP = 0.05
#: REBUILD §5: "dead" = no opinion at all (every label incl. `none` under threshold) on a
#: positive case. Q3 measured 54.6%; abstaining by ignorance is not abstaining by decision.
GATE_DEAD_RATE = 0.20
#: REBUILD §5: acting on a scope set sharing NOTHING with gold is the permission-boundary
#: error. Baseline 10.2%; over-broad and incomplete sets are quality bugs, this is a
#: safety bug, and the gate treats them differently on purpose.
GATE_DISJOINT_RATE = 0.03


def load_corpus(path: Path) -> List[Tuple[str, List[str], str]]:
    """Rows as (text, labels, group). ``group`` is the template a generated row came from."""
    rows: List[Tuple[str, List[str], str]] = []
    for i, line in enumerate((path / "train.jsonl").read_text("utf-8").splitlines()):
        if not line.strip():
            continue
        row = json.loads(line)
        text = str(row.get("text") or "").strip()
        if text:
            rows.append((
                text,
                [str(x) for x in (row.get("labels") or [])],
                str(row.get("template_id") or f"row:{i}"),
            ))
    return rows


def load_manifest(path: Path) -> Dict[str, Any]:
    manifest_file = path / "manifest.json"
    if not manifest_file.is_file():
        raise SystemExit(
            f"{path} has no manifest.json — a head without provenance cannot be audited "
            f"and will be refused at load (PLAN §6.4 rule 3). Rebuild the corpus with "
            f"topos-eval/scripts/build_training_corpus.py."
        )
    return json.loads(manifest_file.read_text("utf-8"))


def load_eval(path: Path) -> List[Tuple[str, set]]:
    blob = json.loads(path.read_text("utf-8"))
    out: List[Tuple[str, set]] = []
    for case in blob["cases"]:
        if case.get("grading_tier") != "label":
            continue
        out.append((case["turns"][0]["user_text"], {g for g in case["gold_labels"]} - {"none"}))
    return out


def macro_f1(pairs: Sequence[Tuple[set, set]]) -> float:
    labels = {x for g, _ in pairs for x in g} | {x for _, p in pairs for x in p}
    scores = []
    for label in sorted(labels):
        tp = sum(1 for g, p in pairs if label in g and label in p)
        fp = sum(1 for g, p in pairs if label not in g and label in p)
        fn = sum(1 for g, p in pairs if label in g and label not in p)
        if tp + fn == 0:
            continue
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        scores.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    return sum(scores) / len(scores) if scores else float("nan")


def evaluate(predict, cases, *, threshold: float) -> Dict[str, Any]:
    from topos.query.scope_head import NONE_LABEL

    pairs: List[Tuple[set, set]] = []
    per_scope: Dict[str, List[int]] = {}
    # REBUILD §5 decompositions. A single headline number hid the two defects that forced
    # the rebuild, so the gate reads all of these, not their average.
    dead = 0                       # positive case, NO opinion anywhere (none included)
    disjoint = 0                   # acted on a set sharing nothing with gold
    acted = 0                      # cases where the prediction set is non-empty
    by_card: Dict[str, List[int]] = {"single": [0, 0], "multi": [0, 0]}
    for text, gold in cases:
        scored = predict([text])[0]
        # `none` is the abstain sentinel, never a scope prediction (REBUILD §B0a).
        pred = {k for k, v in scored.items() if v >= threshold and k != NONE_LABEL}
        pairs.append((gold, pred))
        if pred:
            acted += 1
            if gold and not (pred & gold):
                disjoint += 1
        elif gold and max(scored.values()) < threshold:
            dead += 1
        card = "multi" if len(gold) > 1 else "single"
        for label in gold:
            bucket = per_scope.setdefault(label, [0, 0])
            bucket[1] += 1
            bucket[0] += int(label in pred)
            by_card[card][1] += 1
            by_card[card][0] += int(label in pred)
    negatives = [(g, p) for g, p in pairs if not g]
    positives_n = sum(1 for g, _ in pairs if g)
    recalls = {k: v[0] / v[1] for k, v in per_scope.items() if v[1]}
    rec_single = by_card["single"][0] / by_card["single"][1] if by_card["single"][1] else float("nan")
    rec_multi = by_card["multi"][0] / by_card["multi"][1] if by_card["multi"][1] else float("nan")
    return {
        "n": len(pairs),
        "macro_f1": macro_f1(pairs),
        "exact": sum(1 for g, p in pairs if g == p) / len(pairs) if pairs else float("nan"),
        "negatives_abstained": (
            sum(1 for _, p in negatives if not p) / len(negatives) if negatives else float("nan")
        ),
        "per_scope_recall": recalls,
        "scopes_below_floor": sorted(k for k, v in recalls.items() if v < GATE_MIN_SCOPE_RECALL),
        "recall_single_gold": rec_single,
        "recall_multi_gold": rec_multi,
        "multi_gap": (rec_single - rec_multi) if rec_multi == rec_multi else float("nan"),
        "dead_rate": dead / positives_n if positives_n else float("nan"),
        "disjoint_rate": disjoint / acted if acted else float("nan"),
    }


def check_gate(metrics: Dict[str, Any], incumbent_macro_f1: float) -> List[str]:
    """§7 is a conjunction. Beating the incumbent on average is not the bar."""
    failures: List[str] = []
    if metrics["macro_f1"] < incumbent_macro_f1 + GATE_MACRO_F1_MARGIN:
        failures.append(
            f"macro-F1 {metrics['macro_f1']:.3f} does not beat the incumbent "
            f"{incumbent_macro_f1:.3f}"
        )
    if metrics["negatives_abstained"] < GATE_NEG_ABSTAIN:
        failures.append(
            f"negatives abstained {metrics['negatives_abstained']:.3f} < {GATE_NEG_ABSTAIN}"
        )
    if metrics["scopes_below_floor"]:
        failures.append(
            f"{len(metrics['scopes_below_floor'])} scopes below {GATE_MIN_SCOPE_RECALL} "
            f"recall: {metrics['scopes_below_floor']}"
        )
    # REBUILD §5 clauses. NaN (no multi-gold cases / no positives) fails open with a
    # message rather than passing silently — an eval set that cannot measure a clause
    # is a problem with the eval set, not a green light.
    multi_gap = metrics.get("multi_gap", float("nan"))
    if multi_gap != multi_gap:
        failures.append("multi_gap unmeasurable: eval set has no multi-gold cases")
    elif multi_gap > GATE_MULTI_GAP:
        failures.append(
            f"multi-gold recall trails single-gold by {multi_gap:.3f} > {GATE_MULTI_GAP} "
            f"(single {metrics['recall_single_gold']:.3f}, multi {metrics['recall_multi_gold']:.3f})"
        )
    if metrics.get("dead_rate", 1.0) >= GATE_DEAD_RATE:
        failures.append(
            f"dead-prediction rate {metrics['dead_rate']:.3f} >= {GATE_DEAD_RATE} — the head "
            f"has no opinion (none included) on that share of positive cases"
        )
    if metrics.get("disjoint_rate", 1.0) > GATE_DISJOINT_RATE:
        failures.append(
            f"disjoint-set rate {metrics['disjoint_rate']:.3f} > {GATE_DISJOINT_RATE} — "
            f"acting on a scope set sharing nothing with gold is the permission-boundary error"
        )
    return failures


def pick_device(requested: str = "auto") -> str:
    """Training device by capability: CUDA, then MPS, then CPU.

    CUDA first because it is the faster and better-tested training path where both
    exist; the previous order asked for MPS first, which on a CUDA box would have
    picked the weaker accelerator. Shares `scope_head.resolve_device` so runtime and
    training cannot drift apart — but note the DEFAULTS differ on purpose: training
    wants the accelerator (`auto`), inference defaults to CPU because a 17 ms head must
    not contend with the owner's LLM for VRAM (see `resolve_device`'s docstring).

    ``--device cpu`` forces the safe path if a run ever misbehaves; this project pins
    ``torch<2.13`` after 2.13 segfaulted on MPS.
    """
    from topos.query.scope_head import resolve_device

    return resolve_device(requested if requested != "auto" else "auto")


def report_budget(base: str) -> None:
    known = {"distilbert-base-uncased": 265, "prajjwal1/bert-tiny": 17,
             "sentence-transformers/all-MiniLM-L6-v2": 90}
    mb = known.get(base)
    print(f"  base model      {base}")
    print(f"  resident RSS    ~{mb} MB" if mb else "  resident RSS    unknown — measure before shipping")
    print("  new ModelSlot   SCOPE_HEAD (MiniLM's EMBEDDING slot stays as it is)")
    print("  NOTE: PLAN §3.4 carries a `bad-neighbor` headroom exclusion. Budget the RSS")
    print("        on the smallest supported node before promoting this.")


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", type=Path, required=True, help="build_training_corpus.py output dir")
    ap.add_argument("--out", type=Path, default=None, help="where to write the head")
    ap.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    ap.add_argument("--base", default=DEFAULT_BASE)
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--lr", type=float, default=5e-5)
    ap.add_argument("--max-length", type=int, default=64)
    ap.add_argument("--threshold", type=float, default=0.5)
    ap.add_argument("--incumbent-macro-f1", type=float, default=0.387,
                    help="the prototype's score on the same catalog (PLAN §9F)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--device", default="auto", help="auto | mps | cuda | cpu")
    ap.add_argument("--report-only", action="store_true",
                    help="validate the corpus and print the budget without training")
    ap.add_argument("--force-write", action="store_true",
                    help="write the head even if it fails the §7 gate (for analysis ONLY)")
    args = ap.parse_args(argv)

    from topos.query.scope_classifier import live_scope_ids
    from topos.query.scope_head import NONE_LABEL, _check_labels, _check_manifest

    # REBUILD §B0a: `none` is a trained output, not an absence. 77.6% of the corpus is
    # negatives; without a named none-class they train an all-zero vector that is
    # indistinguishable from ignorance, and the router's four-branch ladder cannot exist.
    labels = list(live_scope_ids()) + [NONE_LABEL]
    manifest = load_manifest(args.corpus)

    # Validate BEFORE the fine-tune. Discovering a dirty manifest after twenty minutes of
    # training is the wrong order, and save_encoder_head would refuse it anyway.
    _check_labels(labels)
    _check_manifest(manifest)
    print(f"corpus {args.corpus}")
    for entry in manifest.get("corpora", []):
        print(f"  ok  {entry['source']:34s} {entry['licence']:14s} {entry['rows']:>6} rows")

    rows = load_corpus(args.corpus)
    cases = load_eval(args.catalog)
    positives = sum(1 for _t, ls, _g in rows if ls)
    print(f"\n  train rows      {len(rows)} ({positives} positive, {positives/len(rows):.1%})")
    print(f"  eval cases      {len(cases)} from {args.catalog.name}")
    print(f"  labels          {len(labels)}")
    report_budget(args.base)

    if args.report_only:
        print("\n(report only — nothing trained, nothing written)")
        return 0

    # --- the fine-tune ------------------------------------------------------
    import numpy as np
    import torch
    from torch.utils.data import DataLoader, TensorDataset
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    device = pick_device(args.device)
    print(f"\n  device          {device}")
    tokenizer = AutoTokenizer.from_pretrained(args.base)
    model = AutoModelForSequenceClassification.from_pretrained(
        args.base, num_labels=len(labels), problem_type="multi_label_classification"
    )
    model.to(device)
    index = {label: i for i, label in enumerate(labels)}

    # --- validation holdout, selected BEFORE training -------------------------
    # This used to happen AFTER the fine-tune, which meant the "held-out" slice had
    # already been trained on and memorised — threshold selection was reading training
    # data, and `train_rows` was computed and never used. The same leak class as the
    # catalog splits, difficulty ratchet, and row-level holdout: fourth appearance.
    import random as _random

    bench_pos = sum(1 for _t, g in cases if g)
    target_rate = bench_pos / len(cases) if cases else 0.5
    rng = _random.Random(args.seed)

    by_group: Dict[str, List[Tuple[str, List[str], str]]] = {}
    for row in rows:
        by_group.setdefault(row[2], []).append(row)
    # Compound rows (REBUILD §B1) carry BOTH parents as `cmp::{a}::{b}`. They never enter
    # holdout candidacy — a compound in validation whose parent phrasings are in training
    # is the same leak. Their exclusion from training when a parent is held out happens
    # after selection below.
    pos_groups = [g for g, rs in by_group.items()
                  if any(r[1] for r in rs) and not g.startswith("cmp::")]
    neg_groups = [g for g, rs in by_group.items() if not any(r[1] for r in rs)]
    rng.shuffle(pos_groups)
    rng.shuffle(neg_groups)

    want = max(len(rows) // 10, 200)
    want_pos = int(want * target_rate)
    holdout_groups: set = set()
    taken = 0
    for group in pos_groups:
        if taken >= want_pos:
            break
        holdout_groups.add(group)
        # Count POSITIVE rows, not group size — a twin shares its source's template_id,
        # so a "positive group" carries its negatives along.
        taken += sum(1 for r in by_group[group] if r[1])
    taken_neg = 0
    for group in neg_groups:
        if taken_neg >= want - want_pos:
            break
        holdout_groups.add(group)
        taken_neg += len(by_group[group])

    def _parents(group: str) -> set:
        return set(group.split("::")[1:]) if group.startswith("cmp::") else {group}

    holdout = [(t, set(l)) for t, l, g in rows if g in holdout_groups]
    # A compound whose EITHER parent template is held out leaks that parent's phrasings
    # into training — drop it entirely rather than let it straddle the split.
    train_rows = [r for r in rows
                  if r[2] not in holdout_groups and not (_parents(r[2]) & holdout_groups)]
    holdout_positives = sum(1 for _t, ls in holdout if ls)
    print(f"    benchmark is {target_rate:.0%} positive; stratifying validation to match")
    print(f"    holdout {len(holdout)} rows / {len(holdout_groups)} templates, "
          f"{holdout_positives} positive ({holdout_positives / max(len(holdout), 1):.0%})")
    print(f"    training on {len(train_rows)} rows "
          f"({len(rows) - len(train_rows) - len(holdout)} compounds dropped for held-out parents)")

    encoded = tokenizer(
        [t for t, _l, _g in train_rows], padding="max_length", truncation=True,
        max_length=args.max_length, return_tensors="pt",
    )
    targets = torch.zeros((len(train_rows), len(labels)), dtype=torch.float)
    for i, (_text, gold, _group) in enumerate(train_rows):
        scoped = False
        for label in gold:
            if label in index and label != NONE_LABEL:
                targets[i, index[label]] = 1.0
                scoped = True
        if not scoped:
            # A negative row is a POSITIVE example of `none` — "confidently no scope",
            # trained, not implied by silence (REBUILD §B0a).
            targets[i, index[NONE_LABEL]] = 1.0

    loader = DataLoader(
        TensorDataset(encoded["input_ids"], encoded["attention_mask"], targets),
        batch_size=args.batch_size, shuffle=True,
    )
    # pos_weight is load-bearing, not a tweak. Each label is 0.5-4% positive
    # (relationship_context:read is 41 rows in 8,309), so unweighted BCE minimises by
    # predicting all-zeros: the first run drove training loss to 0.006 and scored 0.280
    # macro-F1 with 0.968 abstention — a model that had learned to say nothing. This is
    # the same collapse class_weight="balanced" fixed for the linear head in §9E.
    # `none` is the MAJORITY label, so its weight lands below 1 — correct, not a bug.
    positives = targets.sum(dim=0).clamp(min=1.0)
    pos_weight = ((len(train_rows) - positives) / positives).to(device)
    print(f"  pos_weight      min {pos_weight.min():.0f}  max {pos_weight.max():.0f}")
    loss_fn = torch.nn.BCEWithLogitsLoss(pos_weight=pos_weight)

    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    model.train()
    for epoch in range(args.epochs):
        total = 0.0
        for step, (input_ids, mask, target) in enumerate(loader, 1):
            input_ids, mask, target = (
                input_ids.to(device), mask.to(device), target.to(device)
            )
            optimizer.zero_grad()
            logits = model(input_ids=input_ids, attention_mask=mask).logits
            loss = loss_fn(logits, target)
            loss.backward()
            optimizer.step()
            total += loss.detach().item()
            if step % 50 == 0:
                print(f"    epoch {epoch + 1} step {step}/{len(loader)} "
                      f"loss {total / step:.4f}", flush=True)
        print(f"  epoch {epoch + 1}/{args.epochs}  loss {total / max(len(loader), 1):.4f}", flush=True)
    model.eval()

    def predict(texts: Sequence[str]) -> List[Dict[str, float]]:
        batch = tokenizer(list(texts), padding=True, truncation=True,
                          max_length=args.max_length, return_tensors="pt")
        batch = {k: v.to(device) for k, v in batch.items()}
        with torch.no_grad():
            probs = torch.sigmoid(model(**batch).logits).cpu().numpy()
        return [{labels[i]: float(v) for i, v in enumerate(row)} for row in probs]

    # Pick the threshold on the validation holdout selected BEFORE training, never on the
    # eval catalog: sweeping thresholds against the benchmark and then reporting the best
    # is fitting the test set. The holdout is grouped by template AND stratified to the
    # benchmark's positive rate (a uniform slice of this corpus is 22% positive while
    # classify-8 is 76%; an unstratified sweep scored 0.985 flat across a 7x threshold
    # range and picked by tie-break).
    print("  choosing threshold on the pre-training holdout ...", flush=True)
    val_cases = holdout
    best_threshold, best_val = args.threshold, -1.0
    curve = []
    for candidate in (0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70):
        val = evaluate(predict, val_cases, threshold=candidate)
        curve.append((candidate, val["macro_f1"], val["negatives_abstained"],
                      val["disjoint_rate"]))
        # The search obeys EVERY gate it will be judged by, not just abstention. The
        # first B4 run optimised macro-F1 under the abstention floor alone, picked 0.10,
        # and bought its F1 with a 0.301 disjoint rate — the one error class the router
        # must not make. A selection rule blind to a gate will spend it.
        def _ok(value: float, floor_ok: bool) -> bool:
            return floor_ok or value != value  # NaN: unmeasurable on this slice
        eligible = (
            _ok(val["negatives_abstained"], val["negatives_abstained"] >= GATE_NEG_ABSTAIN)
            and _ok(val["disjoint_rate"], val["disjoint_rate"] <= GATE_DISJOINT_RATE)
        )
        if eligible and val["macro_f1"] > best_val:
            best_threshold, best_val = candidate, val["macro_f1"]
    if best_val < 0:
        # Nothing satisfies both constraints: prefer the safest point rather than the
        # default — the artifact is written for analysis, not promotion, in this state.
        best_threshold = min(curve, key=lambda c: (c[3] if c[3] == c[3] else 1.0))[0]
        print("    NO candidate satisfied all gates; choosing min-disjoint for analysis")
    print("    thr   val-macroF1  val-neg-abstain  val-disjoint")
    for candidate, f1, abstain, dis in curve:
        mark = "  <- chosen" if candidate == best_threshold else ""
        print(f"    {candidate:.2f}  {f1:11.3f}  {abstain:14.3f}  {dis:12.3f}{mark}")
    args.threshold = best_threshold

    # --- final fit on the FULL corpus -----------------------------------------
    # The holdout exists to choose the threshold honestly; the shipped artifact should
    # not pay a 3,000-row tax for it (the first B4 run lost 1,220 compounds and 1,040
    # positives to the carve-out). Standard select-then-refit: hyperparameters are
    # frozen above, so refitting on everything leaks nothing into the selection.
    print(f"  refitting on the full corpus ({len(rows)} rows) at threshold {args.threshold} ...",
          flush=True)
    model = AutoModelForSequenceClassification.from_pretrained(
        args.base, num_labels=len(labels), problem_type="multi_label_classification"
    )
    model.to(device)
    encoded = tokenizer(
        [t for t, _l, _g in rows], padding="max_length", truncation=True,
        max_length=args.max_length, return_tensors="pt",
    )
    targets = torch.zeros((len(rows), len(labels)), dtype=torch.float)
    for i, (_text, gold, _group) in enumerate(rows):
        scoped = False
        for label in gold:
            if label in index and label != NONE_LABEL:
                targets[i, index[label]] = 1.0
                scoped = True
        if not scoped:
            targets[i, index[NONE_LABEL]] = 1.0
    loader = DataLoader(
        TensorDataset(encoded["input_ids"], encoded["attention_mask"], targets),
        batch_size=args.batch_size, shuffle=True,
    )
    positives = targets.sum(dim=0).clamp(min=1.0)
    pos_weight = ((len(rows) - positives) / positives).to(device)
    loss_fn = torch.nn.BCEWithLogitsLoss(pos_weight=pos_weight)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    model.train()
    for epoch in range(args.epochs):
        total = 0.0
        for step, (input_ids, mask, target) in enumerate(loader, 1):
            input_ids, mask, target = (
                input_ids.to(device), mask.to(device), target.to(device)
            )
            optimizer.zero_grad()
            loss = loss_fn(model(input_ids=input_ids, attention_mask=mask).logits, target)
            loss.backward()
            optimizer.step()
            total += loss.detach().item()
        print(f"  refit epoch {epoch + 1}/{args.epochs}  loss {total / max(len(loader), 1):.4f}",
              flush=True)
    model.eval()

    print("  evaluating ...", flush=True)
    metrics = evaluate(predict, cases, threshold=args.threshold)
    metrics["threshold"] = args.threshold
    print(f"\n  threshold           {args.threshold}")
    print(f"  macro-F1            {metrics['macro_f1']:.3f}")
    print(f"  exact-set           {metrics['exact']:.3f}")
    print(f"  negatives abstained {metrics['negatives_abstained']:.3f}")
    print(f"  scopes below floor  {len(metrics['scopes_below_floor'])}/{len(labels) - 1}")
    print(f"  recall single/multi {metrics['recall_single_gold']:.3f} / "
          f"{metrics['recall_multi_gold']:.3f}  (gap {metrics['multi_gap']:.3f})")
    print(f"  dead rate           {metrics['dead_rate']:.3f}")
    print(f"  disjoint rate       {metrics['disjoint_rate']:.3f}")

    failures = check_gate(metrics, args.incumbent_macro_f1)
    if failures:
        print("\n§7 GATE FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        if not args.force_write:
            print("\nNot writing. A head below the incumbent would make routing worse, and "
                  "the gate is a conjunction by design. Re-run with --force-write only to "
                  "keep an artifact for analysis.")
            return 1

    if args.out is None:
        print("\n(no --out given; nothing written)")
        return 0

    from topos.query.scope_head import save_encoder_head

    model.to("cpu")  # the artifact must load on a node with no accelerator
    save_encoder_head(
        args.out, labels=labels, model=model, tokenizer=tokenizer, base_model=args.base,
        corpus_manifest=manifest,
        metrics={k: v for k, v in metrics.items() if k != "per_scope_recall"},
        tau_high=args.threshold, tau_low=max(args.threshold - 0.15, 0.05),
        max_length=args.max_length, trained_at=date.today().isoformat(),
    )
    print(f"\nwrote head -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
