#!/usr/bin/env python
"""Apple Silicon embedder / reranker latency micro-harness.

Plan item C2/E2 (PLAN_NODE_UPGRADE_AND_EVAL_EXPANSION.md): no published
M-series latency numbers exist for these models, so this harness's output is
the citable source for tier-threshold decisions (in-process
sentence-transformers serving path).

What it measures, per model x device (cpu, and mps when available):

  bi-encoders   - singleton query embed latency (median/p95, 50 runs after
                  5-run warmup), batch throughput (texts/sec, batch=32, whole
                  ~200-text corpus), load time, param count, embedding dim,
                  RSS delta at load (psutil, skipped if absent).
  cross-encoders- latency to score 20 and 50 (query, text) pairs
                  (median/p95, 20 runs after 5-run warmup), load time,
                  param count, RSS delta.

The test corpus is ~200 deterministic short informal personal texts
(SMS-like fragments through journal-entry paragraphs, ~5-200 tokens),
generated in-script from a fixed seed - MTEB-style prose does not transfer
to terse personal memory, so we measure on the text shapes the engine
actually sees.

Output: eval_reports/bench/embedders-<timestamp>.json plus a markdown table
on stdout. Model failures (download, load, OOM) are recorded per entry and
the run continues.

Usage:
  .venv/bin/python scripts/bench_embedders.py                # full run
  .venv/bin/python scripts/bench_embedders.py --devices cpu  # cpu only
  .venv/bin/python scripts/bench_embedders.py --quick        # smoke (fewer runs)
"""

from __future__ import annotations

import argparse
import gc
import json
import platform
import random
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Model matrix (plan C2)
# ---------------------------------------------------------------------------

BI_ENCODERS = [
    "sentence-transformers/all-MiniLM-L6-v2",   # baseline (engine today)
    "Snowflake/snowflake-arctic-embed-s",        # phase-1 default candidate
    "Snowflake/snowflake-arctic-embed-m-v1.5",   # phase-2 / performance tier
    "BAAI/bge-small-en-v1.5",                    # MIT fallback
]

CROSS_ENCODERS = [
    "cross-encoder/ms-marco-TinyBERT-L2-v2",     # low-latency fallback
    "cross-encoder/ms-marco-MiniLM-L6-v2",       # quality reranker
]

CE_PAIR_COUNTS = [20, 50]

# Per-(model,device) wall-clock cap for a single measurement loop. If the
# warmup estimate says the requested runs would blow past this, the run count
# is reduced (never below MIN_RUNS) and the entry is flagged `capped`.
STAGE_BUDGET_S = 60.0
MIN_RUNS = 10

# ---------------------------------------------------------------------------
# Deterministic corpus: ~200 short informal personal texts, 5-200 tokens
# ---------------------------------------------------------------------------

SHORT_SMS = [
    "dentist tmrw 3pm",
    "mom bday gift ideas",
    "call landlord about the leak",
    "pick up eggs milk bread",
    "gym at 6 with dee",
    "flight lands 9:40pm SFO",
    "vet appt for biscuit thursday",
    "renew passport before june",
    "rent due friday dont forget",
    "coffee w sarah tues 10am",
    "cancel hulu trial",
    "dr chen said retest in 3 months",
    "wifi guy coming between 12 and 4 ugh",
    "book haircut before the wedding",
    "return amazon package by mon",
    "prescription ready at cvs",
    "dads flight AA1123 gate b7",
    "spanish class moved to 7pm",
    "car reg expires end of month",
    "leftover thai in fridge",
    "meet raj at the north entrance",
    "pw for router is on the sticker",
    "9am standup moved to 930",
    "kids soccer sat 8am field 3",
    "grandma wants us over sunday",
    "tax docs to accountant by wed",
    "swap library books",
    "amex bill autopay set",
    "check tire pressure b4 trip",
    "buy stamps",
    "physio said ice 2x daily",
    "lunch w/ marco next thu?",
    "pool closed for cleaning",
    "gift card in junk drawer",
    "team dinner at that ramen place 7",
    "school pickup early wed 1:45",
    "plumber quoted 380 seems high",
    "meds refill 2 left",
    "movers confirmed for the 14th",
    "usb-c cable at office desk",
    "ellies recital friday bring flowers",
    "dry cleaning ready after 5",
    "dog food almost out",
    "electric bill 92 this month yikes",
    "parking spot 4b this week",
    "therapy moved to 11",
    "check in for flight opens 8pm",
    "borrowed toms drill return it",
    "smoothie place closes at 3 sundays",
    "annas address 44 elm ct apt 2",
    "hoa meeting zoom link in email",
    "farmer market sat morning strawberries",
    "battery in smoke alarm chirping again",
    "interview #2 with cortex labs monday 1pm",
    "ask jo about the cabin weekend",
    "car wash punch card 1 more for free",
    "storage unit code 4482",
    "half marathon training week 5 starts mon",
    "book club picked the sea of tranquility",
    "reimbursement form due eom",
    "dads knee surgery went ok call him",
    "switch to the 5:15 train",
    "leo allergic to peanuts NOT tree nuts",
    "yoga pass expires in 2 weeks",
    "bring charger and hdmi for demo",
    "split utilities w/ kay 63 each",
    "orthodontist consult for maya aug 12",
    "fantasy draft thurs 8pm",
    "donate old winter coats",
    "gate code changed to 7719",
    "tues trash AND recycling",
    "mri results portal says viewed",
    "roses need repotting this weekend",
    "brunch spot on 5th has no reservations",
    "keys under the blue pot",
    "visa photo 2x2 walgreens does them",
    "moms recipe uses buttermilk not regular",
    "office badge renewal desk 3",
    "carpool my turn wed fri",
    "pilates wed 7am dont snooze",
]

SENTENCE_POOL = [
    "Woke up early and actually went for the run this time, three miles along the river before work.",
    "Mom called about Thanksgiving plans and somehow we ended up talking for two hours.",
    "Work was chaos again, the release slipped and everyone is pretending that was the plan.",
    "I keep thinking about whether we should move closer to my sister next year.",
    "Biscuit threw up on the rug again so another vet visit is probably coming.",
    "Dinner was just leftovers but honestly the curry tastes better on day two.",
    "Sarah finally texted back about the cabin trip and it looks like it's happening in October.",
    "The dentist says I grind my teeth at night and wants me fitted for a guard.",
    "Budget-wise this month is tight because of the car repair, so no eating out for a while.",
    "The kids were up past ten again and mornings are becoming a negotiation.",
    "I told Raj I'd review his proposal by Friday and I still haven't opened it.",
    "There's a weird noise in the car when it turns left, hoping it's nothing expensive.",
    "Therapy today was heavy, we talked about dad for most of the hour.",
    "The tomatoes in the garden finally ripened and we have way too many now.",
    "My knee felt fine on the hike which is the first time in months.",
    "The landlord agreed to fix the water heater but wouldn't commit to a date.",
    "I signed up for the pottery class on Tuesdays even though evenings are already packed.",
    "Grandma's stories about the old house get a little different every time she tells them.",
    "The interview went okay I think, though I blanked on the systems design question.",
    "We watched the documentary about the octopus and now Ellie wants an aquarium.",
    "Payday hit and half of it disappeared into bills within the hour.",
    "The neighbor's renovation starts at seven sharp every single morning.",
    "I finally organized the garage and found dad's old toolbox behind the bikes.",
    "Marco brought pastries to the meeting which improved the mood considerably.",
    "The flight got delayed twice and we landed after midnight, completely wrecked.",
    "I've been sleeping badly all week and I think it's the coffee after 3pm.",
    "The book club discussion went off the rails but in the best way.",
    "Maya lost her first tooth and insisted on keeping it in a jar on the shelf.",
    "The doctor wants to recheck my iron levels in three months, nothing urgent she said.",
    "We argued about money again and both apologized before bed, progress maybe.",
    "The new manager seems decent, asks real questions and actually listens to answers.",
    "It rained all weekend so the beach plan turned into a puzzle and soup situation.",
    "I keep a list of gift ideas for people and it saves me every December.",
    "Jo mentioned her mom's diagnosis and I didn't know what to say, still don't.",
    "The half marathon plan says four easy miles today but my legs disagree.",
    "Someone at work microwaved fish again and the whole floor suffered.",
    "The kids' school added a second pickup lane and it changed nothing.",
    "I moved some savings into the emergency fund after reading about layoffs at Corex.",
    "Dad's knee is healing slower than he admits, I can hear it on the phone.",
    "We tried the new ramen place and it lived up to the line outside.",
    "The garden beds need mulch before it gets cold, adding it to the weekend list.",
    "My phone storage is full of duplicate photos of the dog sleeping.",
    "The insurance paperwork took the whole lunch break and I'm still not sure it's right.",
    "Ellie's recital piece is coming together, she practiced without being asked twice this week.",
    "I cancelled two subscriptions I forgot I had, small wins.",
    "The camping gear needs sorting before the June trip, the tent poles are somewhere.",
    "Tom fixed the fence gate in twenty minutes with the drill I'd been avoiding buying.",
    "The commute was strangely quiet, made it in by 8:15 for once.",
    "I journaled every day this week which hasn't happened since January.",
    "The quarterly review is Thursday and my notes are still bullet-point chaos.",
]

QUERY_POOL = [
    "when is the dentist appointment",
    "mom birthday gift ideas",
    "what did dr chen say about the retest",
    "cabin trip with sarah plans",
    "biscuit vet visit",
    "how much was the plumber quote",
    "maya orthodontist consult date",
    "notes about dads knee surgery",
]


def gen_corpus(seed: int) -> list[str]:
    """~200 deterministic texts: 90 SMS-like, 60 medium notes, 50 journal paragraphs."""
    rng = random.Random(seed)
    corpus: list[str] = []

    shorts = list(SHORT_SMS)
    rng.shuffle(shorts)
    corpus.extend(shorts)  # 80
    for _ in range(10):  # +10 composite shorts -> 90
        a, b = rng.sample(SHORT_SMS, 2)
        corpus.append(f"{a} + {b}")

    for _ in range(60):  # medium notes: 2-4 sentences (~25-90 tokens)
        n = rng.randint(2, 4)
        corpus.append(" ".join(rng.sample(SENTENCE_POOL, n)))

    for _ in range(50):  # journal paragraphs: 7-12 sentences (~110-210 tokens)
        n = rng.randint(7, 12)
        corpus.append(" ".join(rng.sample(SENTENCE_POOL, n)))

    return corpus


# ---------------------------------------------------------------------------
# Measurement helpers
# ---------------------------------------------------------------------------


def _rss_mb() -> float | None:
    try:
        import psutil

        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:
        return None


def _sync(device: str) -> None:
    if device == "mps":
        import torch

        torch.mps.synchronize()


def _pctl(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    idx = min(len(sorted_vals) - 1, max(0, round(q * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def _summarize_ms(samples_s: list[float]) -> dict:
    ms = sorted(x * 1000.0 for x in samples_s)
    return {
        "median_ms": round(statistics.median(ms), 3),
        "p95_ms": round(_pctl(ms, 0.95), 3),
        "min_ms": round(ms[0], 3),
        "max_ms": round(ms[-1], 3),
        "runs": len(ms),
    }


def _budgeted_runs(requested: int, est_per_run_s: float, label: str) -> tuple[int, bool]:
    if est_per_run_s <= 0 or requested * est_per_run_s <= STAGE_BUDGET_S:
        return requested, False
    runs = max(MIN_RUNS, int(STAGE_BUDGET_S / est_per_run_s))
    runs = min(runs, requested)
    print(
        f"    [cap] {label}: est {est_per_run_s * 1000:.0f} ms/run -> "
        f"{runs} runs instead of {requested} (budget {STAGE_BUDGET_S:.0f}s)",
        file=sys.stderr,
    )
    return runs, True


def _timed_load(loader, model_name: str, device: str) -> tuple[object, dict]:
    """Prefetch weights (download timed separately), then time the load itself."""
    from huggingface_hub import snapshot_download

    t0 = time.perf_counter()
    snapshot_download(model_name)
    download_s = time.perf_counter() - t0

    rss_before = _rss_mb()
    t0 = time.perf_counter()
    model = loader()
    load_s = time.perf_counter() - t0
    rss_after = _rss_mb()

    info = {
        "download_or_cache_check_s": round(download_s, 2),
        "load_s": round(load_s, 2),
        "load_rss_delta_mb": (
            round(rss_after - rss_before, 1)
            if rss_before is not None and rss_after is not None
            else None
        ),
        "device": device,
    }
    return model, info


def bench_bi_encoder(
    model_name: str, device: str, corpus: list[str], args
) -> dict:
    from sentence_transformers import SentenceTransformer

    result: dict = {"model": model_name, "device": device, "error": None}
    try:
        model, info = _timed_load(
            lambda: SentenceTransformer(model_name, device=device),
            model_name,
            device,
        )
        result.update(info)
        result["params_m"] = round(
            sum(p.numel() for p in model.parameters()) / 1e6, 1
        )
        get_dim = getattr(
            model, "get_embedding_dimension", None
        ) or model.get_sentence_embedding_dimension
        result["embedding_dim"] = get_dim()
        result["max_seq_length"] = int(model.max_seq_length)

        queries = QUERY_POOL

        # -- singleton query latency ------------------------------------
        for i in range(args.warmup):
            model.encode(queries[i % len(queries)], convert_to_numpy=True)
        _sync(device)

        t0 = time.perf_counter()
        model.encode(queries[0], convert_to_numpy=True)
        _sync(device)
        est = time.perf_counter() - t0
        runs, capped = _budgeted_runs(args.runs, est, f"{model_name}/{device} singleton")

        samples = []
        for i in range(runs):
            q = queries[i % len(queries)]
            t0 = time.perf_counter()
            model.encode(q, convert_to_numpy=True)
            _sync(device)
            samples.append(time.perf_counter() - t0)
        result["singleton"] = _summarize_ms(samples)
        result["singleton"]["capped"] = capped

        # -- batch throughput --------------------------------------------
        model.encode(corpus[:64], batch_size=args.batch_size, convert_to_numpy=True)
        _sync(device)
        t0 = time.perf_counter()
        model.encode(corpus, batch_size=args.batch_size, convert_to_numpy=True)
        _sync(device)
        est = time.perf_counter() - t0
        bruns, bcapped = _budgeted_runs(
            args.batch_runs, est, f"{model_name}/{device} batch"
        )
        totals = [est]  # first timed pass counts as a sample
        for _ in range(max(0, bruns - 1)):
            t0 = time.perf_counter()
            model.encode(corpus, batch_size=args.batch_size, convert_to_numpy=True)
            _sync(device)
            totals.append(time.perf_counter() - t0)
        med_total = statistics.median(totals)
        result["batch"] = {
            "batch_size": args.batch_size,
            "corpus_size": len(corpus),
            "median_total_s": round(med_total, 3),
            "texts_per_s": round(len(corpus) / med_total, 1),
            "runs": len(totals),
            "capped": bcapped,
        }

        del model
    except Exception as exc:  # noqa: BLE001 - record and continue
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        _cleanup(device)
    return result


def bench_cross_encoder(
    model_name: str, device: str, corpus: list[str], seed: int, args
) -> dict:
    from sentence_transformers import CrossEncoder

    result: dict = {"model": model_name, "device": device, "error": None}
    try:
        model, info = _timed_load(
            lambda: CrossEncoder(model_name, device=device),
            model_name,
            device,
        )
        result.update(info)
        result["params_m"] = round(
            sum(p.numel() for p in model.model.parameters()) / 1e6, 1
        )

        rng = random.Random(seed + 7)
        query = "when is the dentist appointment for mom"
        docs = rng.sample(corpus, max(CE_PAIR_COUNTS))

        for n_pairs in CE_PAIR_COUNTS:
            pairs = [(query, d) for d in docs[:n_pairs]]

            for _ in range(args.ce_warmup):
                model.predict(pairs, batch_size=args.batch_size)
            _sync(device)

            t0 = time.perf_counter()
            model.predict(pairs, batch_size=args.batch_size)
            _sync(device)
            est = time.perf_counter() - t0
            runs, capped = _budgeted_runs(
                args.ce_runs, est, f"{model_name}/{device} {n_pairs}p"
            )

            samples = []
            for _ in range(runs):
                t0 = time.perf_counter()
                model.predict(pairs, batch_size=args.batch_size)
                _sync(device)
                samples.append(time.perf_counter() - t0)
            summary = _summarize_ms(samples)
            summary["capped"] = capped
            summary["ms_per_pair_median"] = round(
                summary["median_ms"] / n_pairs, 2
            )
            result[f"pairs_{n_pairs}"] = summary

        del model
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        _cleanup(device)
    return result


def _cleanup(device: str) -> None:
    gc.collect()
    if device == "mps":
        try:
            import torch

            torch.mps.empty_cache()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Environment fingerprint (this output is citable -> record the instrument)
# ---------------------------------------------------------------------------


def _sysctl(key: str) -> str | None:
    try:
        return subprocess.run(
            ["sysctl", "-n", key], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except Exception:
        return None


def env_fingerprint(seed: int, corpus: list[str]) -> dict:
    import torch
    import sentence_transformers

    wc = [len(t.split()) for t in corpus]
    meta = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "torch": torch.__version__,
        "sentence_transformers": sentence_transformers.__version__,
        "torch_num_threads": torch.get_num_threads(),
        "mps_available": torch.backends.mps.is_available(),
        "seed": seed,
        "corpus": {
            "n_texts": len(corpus),
            "words_min": min(wc),
            "words_median": int(statistics.median(wc)),
            "words_max": max(wc),
        },
    }
    if sys.platform == "darwin":
        meta["chip"] = _sysctl("machdep.cpu.brand_string")
        mem = _sysctl("hw.memsize")
        if mem and mem.isdigit():
            meta["ram_gb"] = round(int(mem) / 2**30, 1)
        meta["perf_cores"] = _sysctl("hw.perflevel0.physicalcpu")
        meta["eff_cores"] = _sysctl("hw.perflevel1.physicalcpu")
    return meta


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------


def _fmt(v, nd=1):
    return "-" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))


def print_markdown(report: dict) -> None:
    m = report["meta"]
    print()
    print(
        f"## Embedder/reranker latency - {m.get('chip', m['machine'])}, "
        f"{m.get('ram_gb', '?')} GB, torch {m['torch']}, "
        f"st {m['sentence_transformers']}, threads {m['torch_num_threads']}"
    )
    print(
        f"Corpus: {m['corpus']['n_texts']} texts, "
        f"{m['corpus']['words_min']}-{m['corpus']['words_max']} words "
        f"(median {m['corpus']['words_median']}), seed {m['seed']}. "
        f"{m['timestamp_utc']}"
    )

    print("\n### Bi-encoders\n")
    print(
        "| Model | Device | Params (M) | Dim | Load (s) | Singleton med (ms) "
        "| Singleton p95 (ms) | Batch tput (texts/s) | Load RSS d (MB) |"
    )
    print("|---|---|---:|---:|---:|---:|---:|---:|---:|")
    for r in report["bi_encoders"]:
        name = r["model"].split("/")[-1]
        if r.get("error"):
            print(f"| {name} | {r['device']} | ERROR: {r['error']} | | | | | | |")
            continue
        s, b = r["singleton"], r["batch"]
        flag = " *" if s.get("capped") or b.get("capped") else ""
        print(
            f"| {name} | {r['device']} | {_fmt(r['params_m'])} "
            f"| {r['embedding_dim']} | {_fmt(r['load_s'], 2)} "
            f"| {_fmt(s['median_ms'])}{flag} | {_fmt(s['p95_ms'])} "
            f"| {_fmt(b['texts_per_s'])} | {_fmt(r['load_rss_delta_mb'])} |"
        )

    print("\n### Cross-encoders (rerank top-k fused candidates)\n")
    print(
        "| Model | Device | Params (M) | Load (s) | 20 pairs med/p95 (ms) "
        "| 50 pairs med/p95 (ms) | ms/pair @50 |"
    )
    print("|---|---|---:|---:|---:|---:|---:|")
    for r in report["cross_encoders"]:
        name = r["model"].split("/")[-1]
        if r.get("error"):
            print(f"| {name} | {r['device']} | ERROR: {r['error']} | | | | |")
            continue
        p20, p50 = r["pairs_20"], r["pairs_50"]
        print(
            f"| {name} | {r['device']} | {_fmt(r['params_m'])} "
            f"| {_fmt(r['load_s'], 2)} "
            f"| {_fmt(p20['median_ms'], 0)} / {_fmt(p20['p95_ms'], 0)} "
            f"| {_fmt(p50['median_ms'], 0)} / {_fmt(p50['p95_ms'], 0)} "
            f"| {_fmt(p50['ms_per_pair_median'], 2)} |"
        )
    print(
        "\n`*` = run count reduced to fit the per-stage time budget. "
        "RSS delta is process-level at load (rough; sequential in-process loads)."
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Apple Silicon embedder/reranker latency micro-harness (plan C2/E2)."
    )
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument(
        "--devices",
        nargs="+",
        default=None,
        help="Devices to bench (default: cpu, plus mps if available).",
    )
    ap.add_argument("--runs", type=int, default=50, help="Singleton timed runs.")
    ap.add_argument("--warmup", type=int, default=5, help="Singleton warmup runs.")
    ap.add_argument("--batch-runs", type=int, default=3, help="Batch throughput passes.")
    ap.add_argument("--batch-size", type=int, default=32)
    ap.add_argument("--ce-runs", type=int, default=20, help="Cross-encoder timed runs.")
    ap.add_argument("--ce-warmup", type=int, default=5)
    ap.add_argument(
        "--models",
        nargs="+",
        default=None,
        help="Substring filter on model names (applies to both classes).",
    )
    ap.add_argument(
        "--out-dir",
        default=str(Path(__file__).resolve().parent.parent / "eval_reports" / "bench"),
    )
    ap.add_argument(
        "--quick", action="store_true", help="Smoke mode: 10/2 singleton, 5/2 CE runs."
    )
    args = ap.parse_args()

    if args.quick:
        args.runs, args.warmup = 10, 2
        args.ce_runs, args.ce_warmup = 5, 2
        args.batch_runs = 1

    import torch

    devices = args.devices or (
        ["cpu", "mps"] if torch.backends.mps.is_available() else ["cpu"]
    )

    def keep(name: str) -> bool:
        return not args.models or any(f.lower() in name.lower() for f in args.models)

    corpus = gen_corpus(args.seed)
    meta = env_fingerprint(args.seed, corpus)
    meta["args"] = {
        k: v for k, v in vars(args).items() if k not in ("out_dir",)
    }
    report: dict = {"meta": meta, "bi_encoders": [], "cross_encoders": []}

    for device in devices:
        for name in BI_ENCODERS:
            if not keep(name):
                continue
            print(f"[bi]  {name} on {device} ...", file=sys.stderr)
            t0 = time.perf_counter()
            report["bi_encoders"].append(bench_bi_encoder(name, device, corpus, args))
            print(f"      done in {time.perf_counter() - t0:.1f}s", file=sys.stderr)
        for name in CROSS_ENCODERS:
            if not keep(name):
                continue
            print(f"[ce]  {name} on {device} ...", file=sys.stderr)
            t0 = time.perf_counter()
            report["cross_encoders"].append(
                bench_cross_encoder(name, device, corpus, args.seed, args)
            )
            print(f"      done in {time.perf_counter() - t0:.1f}s", file=sys.stderr)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = out_dir / f"embedders-{stamp}.json"
    out_path.write_text(json.dumps(report, indent=2))

    print_markdown(report)
    print(f"\nJSON: {out_path}")

    errors = [
        r for r in report["bi_encoders"] + report["cross_encoders"] if r.get("error")
    ]
    if errors:
        print(f"\n{len(errors)} model/device combos failed (see JSON).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
