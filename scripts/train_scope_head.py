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

#: §7. A head ships only if it clears every one of these, not their average.
GATE_MACRO_F1_MARGIN = 0.0     # must at least match the incumbent
GATE_NEG_ABSTAIN = 0.85
GATE_MIN_SCOPE_RECALL = 0.60


def load_corpus(path: Path) -> List[Tuple[str, List[str]]]:
    rows: List[Tuple[str, List[str]]] = []
    for line in (path / "train.jsonl").read_text("utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        text = str(row.get("text") or "").strip()
        if text:
            rows.append((text, [str(x) for x in (row.get("labels") or [])]))
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
    pairs: List[Tuple[set, set]] = []
    per_scope: Dict[str, List[int]] = {}
    for text, gold in cases:
        scored = predict([text])[0]
        pred = {k for k, v in scored.items() if v >= threshold}
        pairs.append((gold, pred))
        for label in gold:
            bucket = per_scope.setdefault(label, [0, 0])
            bucket[1] += 1
            bucket[0] += int(label in pred)
    negatives = [(g, p) for g, p in pairs if not g]
    recalls = {k: v[0] / v[1] for k, v in per_scope.items() if v[1]}
    return {
        "n": len(pairs),
        "macro_f1": macro_f1(pairs),
        "exact": sum(1 for g, p in pairs if g == p) / len(pairs) if pairs else float("nan"),
        "negatives_abstained": (
            sum(1 for _, p in negatives if not p) / len(negatives) if negatives else float("nan")
        ),
        "per_scope_recall": recalls,
        "scopes_below_floor": sorted(k for k, v in recalls.items() if v < GATE_MIN_SCOPE_RECALL),
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
    return failures


def pick_device(requested: str = "auto") -> str:
    """Prefer Apple Silicon, then CUDA, then CPU.

    This project pins ``torch<2.13`` because 2.13 segfaulted on MPS during a first query,
    so MPS is only reached on a version already known to work here. ``--device cpu``
    forces the safe path if a run ever misbehaves.
    """
    import torch

    if requested != "auto":
        return requested
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


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
    ap.add_argument("--device", default="auto", help="auto | mps | cuda | cpu")
    ap.add_argument("--report-only", action="store_true",
                    help="validate the corpus and print the budget without training")
    ap.add_argument("--force-write", action="store_true",
                    help="write the head even if it fails the §7 gate (for analysis ONLY)")
    args = ap.parse_args(argv)

    from topos.query.scope_classifier import live_scope_ids
    from topos.query.scope_head import _check_labels, _check_manifest

    labels = list(live_scope_ids())
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
    positives = sum(1 for _t, ls in rows if ls)
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

    encoded = tokenizer(
        [t for t, _ in rows], padding="max_length", truncation=True,
        max_length=args.max_length, return_tensors="pt",
    )
    targets = torch.zeros((len(rows), len(labels)), dtype=torch.float)
    for i, (_text, gold) in enumerate(rows):
        for label in gold:
            if label in index:
                targets[i, index[label]] = 1.0

    loader = DataLoader(
        TensorDataset(encoded["input_ids"], encoded["attention_mask"], targets),
        batch_size=args.batch_size, shuffle=True,
    )
    # pos_weight is load-bearing, not a tweak. Each label is 0.5-4% positive
    # (relationship_context:read is 41 rows in 8,309), so unweighted BCE minimises by
    # predicting all-zeros: the first run drove training loss to 0.006 and scored 0.280
    # macro-F1 with 0.968 abstention — a model that had learned to say nothing. This is
    # the same collapse class_weight="balanced" fixed for the linear head in §9E.
    positives = targets.sum(dim=0).clamp(min=1.0)
    pos_weight = ((len(rows) - positives) / positives).to(device)
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

    print("  evaluating ...", flush=True)
    metrics = evaluate(predict, cases, threshold=args.threshold)
    print(f"\n  macro-F1            {metrics['macro_f1']:.3f}")
    print(f"  exact-set           {metrics['exact']:.3f}")
    print(f"  negatives abstained {metrics['negatives_abstained']:.3f}")
    print(f"  scopes below floor  {len(metrics['scopes_below_floor'])}/{len(labels)}")

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
        max_length=args.max_length, trained_at=date.today().isoformat(),
    )
    print(f"\nwrote head -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
