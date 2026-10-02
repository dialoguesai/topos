"""RD3: what lifting the 32-per-build vector cap in the p2c index build would cost. Synthetic only.

`SearchIndexService._members` embeds at most 32 members per build with the local passage
embedder, each with its own call; the rest of R(g) stays lexical-only, and nothing persists
a vector between builds, so every rebuild embeds the same 32 again. Two costs decide whether
the cap can go:

1. Build-time embedding: the per-member encode on the node's own device and thread bound
   (the build runs on an ungated read snapshot, so this is wall time, not gate time).
2. Gate-hold growth: the publish step writes every vector into the index file under the node
   write gate, so its cost with and without vectors is measured through the real `_publish`.

Passages are generated from a fixed vocabulary and seed; no database, node or model server is
touched. The embedder is loaded from the local model cache only, never downloaded. Run from
the engine root with a scratch app environment:
    TOPOS_DATABASE_PATH=<scratch>/throwaway.db TOPOS_ENV_FILE=<scratch>/topos.env \\
    .venv/bin/python3 scripts/permissions_v2/p2c_vector_cost.py --out <scratch>/rd3.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

WORDS = ("meeting project plan week trip review draft call schedule notes update weekend dinner run "
         "train code release demo budget design ship test sync lunch flight hotel bike book class "
         "morning evening today tomorrow team client office garden coffee music game read write").split()


def passage(rng: random.Random, words: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(words)).capitalize() + "."


def quantile(values, q):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def embed_cost(model_name: str, lengths, per_length: int, seed: int) -> dict:
    from topos.engine.torch_runtime import configure, device_for
    configure()
    device = device_for("embeddings")
    from huggingface_hub import snapshot_download
    from sentence_transformers import SentenceTransformer
    from topos.engine.backends.huggingface import apply_embedding_prefix
    started = time.perf_counter()
    path = snapshot_download(repo_id=model_name, local_files_only=True)
    model = SentenceTransformer(path, device=device, local_files_only=True, trust_remote_code=False)
    load_ms = (time.perf_counter() - started) * 1000
    rng = random.Random(seed)

    def encode(text):
        passages = apply_embedding_prefix([text], model_name=model_name, input_role="passage")
        return model.encode(passages, convert_to_numpy=True, normalize_embeddings=True, show_progress_bar=False)[0]
    for _ in range(5):
        encode(passage(rng, 12))
    by_length = {}
    for words in lengths:
        samples = []
        for _ in range(per_length):
            text = passage(rng, words)
            began = time.perf_counter()
            vector = encode(text)
            samples.append((time.perf_counter() - began) * 1000)
        by_length[str(words)] = {"n": per_length, "p50_ms": round(statistics.median(samples), 3),
                                 "p95_ms": round(quantile(samples, 0.95), 3), "max_ms": round(max(samples), 3)}
    import torch
    return {"model": model_name, "device": device, "torch_threads": torch.get_num_threads(), "dims": len(vector),
            "load_ms": round(load_ms, 1), "single_passage_by_words": by_length}


def publish_cost(member_counts, dims: int, repeats: int, seed: int) -> dict:
    """The real `_publish` on synthetic members, with no vectors and with one vector each."""
    from topos.permissions_v2.search_index import Member, SearchIndexService
    rng = random.Random(seed)
    out = {}
    with tempfile.TemporaryDirectory(prefix="p2c_vector_cost_") as scratch:
        root = Path(scratch) / "message-search"
        root.mkdir(mode=0o700)
        import threading
        owner = type("Publisher", (), {"root": root, "_published": set(), "_published_lock": threading.Lock()})()
        for count in member_counts:
            members = []
            for index in range(count):
                opaque = "r." + f"{index:064x}"
                terms = {rng.choice(WORDS): 1 for _ in range(6)}
                members.append((Member(opaque, 1_700_000_000_000_000 + index, 6, terms, os.urandom(300)), opaque, None,
                                [[rng.uniform(-0.1, 0.1) for _ in range(dims)]]))
            row = {}
            for label, built in (("no_vectors", [(m, o, i, []) for m, o, i, _ in members]), ("one_vector_each", members)):
                samples = []
                for _ in range(repeats):
                    began = time.perf_counter()
                    SearchIndexService._publish(owner, "grant-synthetic", {"format": "synthetic"}, "ready",
                                                "synthetic" if label != "no_vectors" else None,
                                                dims if label != "no_vectors" else None, built)
                    samples.append((time.perf_counter() - began) * 1000)
                size = next(root.glob("grant-*.db")).stat().st_size
                row[label] = {"p50_ms": round(statistics.median(samples), 2), "max_ms": round(max(samples), 2),
                              "file_bytes": size}
            out[str(count)] = row
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="sentence-transformers/all-MiniLM-L6-v2")
    parser.add_argument("--per-length", type=int, default=60)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    embedding = embed_cost(args.model, (5, 15, 40, 80, 200), args.per_length, args.seed)
    publish = publish_cost((32, 59, 200, 1000, 5000), embedding["dims"], args.repeats, args.seed)
    p50 = embedding["single_passage_by_words"]["15"]["p50_ms"]
    report = {"schema": "p2c-vector-cost/v1", "embedding": embedding, "publish_under_gate": publish,
              "build_embedding_ms_at_p50_15_words": {str(n): round(n * p50, 1) for n in (32, 59, 200, 1000, 5000)}}
    text = json.dumps(report, indent=1, sort_keys=True)
    if args.out:
        args.out.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
