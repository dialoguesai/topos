#!/usr/bin/env python3
"""Three-arm bake-off: prototype (M0) vs trained head (rung 2) vs LLM.

PLAN_SCOPE_CLASSIFIER.md §7's promotion gate is written as "macro-F1 >= the LLM's own
macro-F1 on the same held-out set". M0 reported only the left-hand side, which makes the
gate unevaluable. This script computes both sides plus the rung in between, on **one
identical test split**, so the numbers are comparable rather than merely adjacent.

The split is grouped by ``provenance.template_id`` (falling back to case id). A random
split would put one rendering of a phrasing in train and its sibling in test, and every
arm's score would read as memorisation — the same defect found in the difficulty ratchet
at G3.

Arms::

    prototype   cosine against embedded registry descriptions. No training. M0 as shipped.
    head        logistic regression on MiniLM embeddings, fit on the train split. Rung 2.
    llm         an instruction-following model over the closed label set, via ollama.

Usage (from topos/)::

    .venv/bin/python scripts/compare_scope_classifiers.py --arms prototype,head
    .venv/bin/python scripts/compare_scope_classifiers.py --arms llm --llm-model mistral:7b
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_CATALOG = (
    ROOT.parent / "topos-eval" / "src" / "topos_eval" / "catalog" / "data" / "role_classify_8.json"
)
TEST_BUCKETS = {0, 1}  # 2 of 8 -> ~25% test
N_BUCKETS = 8


def group_of(case: dict) -> str:
    prov = case.get("provenance") or {}
    return str(prov.get("template_id") or case["id"])


def in_test(group: str) -> bool:
    digest = hashlib.sha1(f"bakeoff:{group}".encode("utf-8")).hexdigest()[:8]
    return int(digest, 16) % N_BUCKETS in TEST_BUCKETS


def load_corpus(path: Path):
    """External training corpus (M2). Rows are {text, labels, ...} jsonl."""
    out = []
    for line in path.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        text = str(row.get("text") or "").strip()
        if text:
            out.append((text, {str(x) for x in (row.get("labels") or [])}, "corpus"))
    return out


def load_split(path: Path):
    blob = json.loads(path.read_text("utf-8"))
    train, test = [], []
    for case in blob["cases"]:
        if case.get("grading_tier") != "label":
            continue
        gold = {g for g in case["gold_labels"]} - {"none"}
        notes = (case.get("notes") or "").split()
        band = notes[0].rstrip(":") if notes else "?"
        row = (case["turns"][0]["user_text"], gold, band)
        (test if in_test(group_of(case)) else train).append(row)
    return train, test


def macro_f1(pairs) -> float:
    labels = {x for gold, _ in pairs for x in gold} | {x for _, pred in pairs for x in pred}
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


def score(pairs, bands, latencies) -> dict:
    negatives = [(g, p) for g, p in pairs if not g]
    per_scope = collections.defaultdict(lambda: [0, 0])
    for gold, pred in pairs:
        for label in gold:
            per_scope[label][1] += 1
            per_scope[label][0] += int(label in pred)
    per_band = collections.defaultdict(lambda: [0, 0])
    for (gold, pred), band in zip(pairs, bands):
        per_band[band][1] += 1
        per_band[band][0] += int(pred == gold)
    recalls = {k: v[0] / v[1] for k, v in per_scope.items() if v[1]}
    latencies = sorted(latencies)
    return {
        "n": len(pairs),
        "macro_f1": macro_f1(pairs),
        "exact": sum(1 for g, p in pairs if g == p) / len(pairs) if pairs else float("nan"),
        "negatives_abstained": (
            sum(1 for _, p in negatives if not p) / len(negatives) if negatives else float("nan")
        ),
        "scopes_clearing_060": sum(1 for r in recalls.values() if r >= 0.60),
        "scopes_measured": len(recalls),
        "worst_scope": min(recalls.items(), key=lambda kv: kv[1]) if recalls else None,
        "p50_ms": latencies[len(latencies) // 2] if latencies else float("nan"),
        "per_band": {k: v[0] / v[1] for k, v in sorted(per_band.items()) if v[1] >= 10},
        "per_scope_recall": dict(sorted(recalls.items(), key=lambda kv: -kv[1])),
    }


# --- arms -------------------------------------------------------------------


def run_prototype(train, test):
    from topos.query.scope_classifier import classify

    pairs, bands, lat = [], [], []
    for text, gold, band in test:
        t0 = time.perf_counter()
        verdict = classify(text)
        lat.append((time.perf_counter() - t0) * 1000)
        pairs.append((gold, set(verdict.labels)))
        bands.append(band)
    return score(pairs, bands, lat)


def _embed(texts):
    from topos.engine.backends.huggingface import HuggingFaceAdapter, active_embedding_model

    out = HuggingFaceAdapter().run_inference(
        {"texts": list(texts)},
        {"subtype": "embedding", "model": active_embedding_model(), "batch_size": 64},
    )
    return [[float(x) for x in v] for v in (out.get("vectors") or [])]


def run_head(train, test, *, threshold: float):
    """Rung 2: one-vs-rest logistic regression over MiniLM embeddings."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.multiclass import OneVsRestClassifier
    from sklearn.preprocessing import MultiLabelBinarizer

    x_train = _embed([t for t, _, _ in train])
    x_test = _embed([t for t, _, _ in test])
    binarizer = MultiLabelBinarizer()
    y_train = binarizer.fit_transform([sorted(g) for _, g, _ in train])

    # class_weight="balanced" is load-bearing, not a tweak. Each one-vs-rest problem is
    # ~2-4% positive in a real corpus (96 health:read rows against 3,906 others), and an
    # unweighted fit answers "never" to every one of them. The first bake-off hid this
    # because a benchmark split is ~35% positive; a real corpus is not.
    model = OneVsRestClassifier(
        LogisticRegression(max_iter=3000, C=4.0, class_weight="balanced")
    )
    model.fit(x_train, y_train)

    t0 = time.perf_counter()
    probs = model.predict_proba(x_test)
    per_case_ms = (time.perf_counter() - t0) * 1000 / max(len(x_test), 1)

    pairs, bands = [], []
    for row, (_, gold, band) in zip(probs, test):
        pred = {
            binarizer.classes_[i] for i, p in enumerate(row) if p >= threshold
        }
        pairs.append((gold, pred))
        bands.append(band)
    return score(pairs, bands, [per_case_ms] * len(test))


def run_llm(train, test, *, model: str, url: str):
    import urllib.request

    from topos.query.scope_classifier import live_scope_ids

    labels = list(live_scope_ids())
    prompt_head = (
        "You assign Topos personal-data scope labels. Do not call tools.\n"
        "Allowed labels (exact strings, one or more):\n"
        + "\n".join(labels + ["none"])
        + '\n\nReply with EXACTLY one JSON object and nothing else:\n'
        '{"labels": ["<label>"]}\n'
        "If the request is not about the OWNER's own personal data, use [\"none\"].\n\n"
        "Owner request:\n"
    )
    valid = set(labels)
    pairs, bands, lat = [], [], []
    for i, (text, gold, band) in enumerate(test, 1):
        body = json.dumps(
            {
                "model": model,
                "prompt": prompt_head + text + "\n",
                "stream": False,
                "options": {"temperature": 0},
            }
        ).encode()
        req = urllib.request.Request(
            f"{url}/api/generate", data=body, headers={"Content-Type": "application/json"}
        )
        t0 = time.perf_counter()
        pred: set[str] = set()
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = json.loads(resp.read()).get("response", "")
            start, end = raw.find("{"), raw.rfind("}")
            if start >= 0 and end > start:
                obj = json.loads(raw[start : end + 1])
                got = obj.get("labels") or obj.get("label") or obj.get("message") or []
                if isinstance(got, str):
                    got = [g.strip() for g in got.split(",")]
                pred = {str(g).strip() for g in got if str(g).strip() in valid}
        except Exception as exc:  # noqa: BLE001
            print(f"    case {i}: {type(exc).__name__}", file=sys.stderr)
        lat.append((time.perf_counter() - t0) * 1000)
        pairs.append((gold, pred))
        bands.append(band)
        if i % 25 == 0:
            print(f"    {i}/{len(test)} ...", flush=True)
    return score(pairs, bands, lat)


def report(name: str, r: dict) -> None:
    worst = r["worst_scope"]
    print(f"\n=== {name}  (n={r['n']})")
    print(f"  macro-F1             {r['macro_f1']:.3f}")
    print(f"  exact-set            {r['exact']:.3f}")
    print(f"  negatives abstained  {r['negatives_abstained']:.3f}")
    print(f"  scopes >= 0.60 recall {r['scopes_clearing_060']}/{r['scopes_measured']}")
    if worst:
        print(f"  worst scope          {worst[0]} {worst[1]:.3f}")
    print(f"  p50 latency          {r['p50_ms']:.1f} ms")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    ap.add_argument("--arms", default="prototype,head")
    ap.add_argument("--head-threshold", type=float, default=0.5)
    ap.add_argument("--llm-model", default="mistral:7b")
    ap.add_argument("--llm-url", default="http://localhost:11434")
    ap.add_argument("--limit-test", type=int, default=0)
    ap.add_argument(
        "--train-corpus", type=Path, default=None,
        help="M2 corpus jsonl. Without it the head trains on a split of the BENCHMARK, "
             "which measures the wrong thing once a real corpus exists.",
    )
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    bench_train, test = load_split(args.catalog)
    if args.train_corpus:
        train = load_corpus(args.train_corpus)
        bench_texts = {t.strip().lower() for t, _, _ in test}
        leaked = [t for t, _, _ in train if t.strip().lower() in bench_texts]
        if leaked:
            print(f"ABORT: {len(leaked)} training rows appear in the test split; "
                  f"the comparison would be contaminated. e.g. {leaked[:2]}", file=sys.stderr)
            return 2
        print(f"  head trains on {args.train_corpus.name} ({len(train)} rows), "
              f"disjointness verified")
    else:
        train = bench_train
        print("  WARNING: no --train-corpus; head trains on a split of the benchmark")
    if args.limit_test:
        test = test[: args.limit_test]
    print(f"catalog {args.catalog.name}: {len(train)} train / {len(test)} test "
          f"(grouped by template, {N_BUCKETS - len(TEST_BUCKETS)}:{len(TEST_BUCKETS)})")

    results: dict[str, dict] = {}
    for arm in [a.strip() for a in args.arms.split(",") if a.strip()]:
        if arm == "prototype":
            results[arm] = run_prototype(train, test)
        elif arm == "head":
            results[arm] = run_head(train, test, threshold=args.head_threshold)
        elif arm == "llm":
            print(f"  running {args.llm_model} over {len(test)} cases ...", flush=True)
            results[arm] = run_llm(train, test, model=args.llm_model, url=args.llm_url)
        else:
            print(f"unknown arm {arm!r}", file=sys.stderr)
            return 2
        report(arm, results[arm])

    if args.out:
        args.out.write_text(json.dumps(results, indent=2, default=str) + "\n")
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
