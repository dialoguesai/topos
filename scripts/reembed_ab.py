"""Re-embed the vector store with a chosen embedding model — the A/B substrate for the
embedder upgrade (plan C2, PLAN_NODE_UPGRADE_AND_EVAL_EXPANSION.md).

KNOWN LIMITATION: this updates signal_embeddings.vector_blob (the brute-force path) but
NOT the sqlite-vec ANN shadow table (vec0), because sqlite-vec isn't loaded in a plain
sqlite3 connection — sync_vec_row silently no-ops. So A/B benches must force brute-force
(TOPOS_VECTOR_ANN=brute_force) to read the fresh vectors; the ANN path would serve stale
vectors. A real production swap must rebuild vec0 through the engine's vector migration.

Operates on a COPY of the DB by default (never mutates the live index unless --in-place).
Re-embeds every signal_embeddings row's stored search_text with the target model, rewrites
vector_blob (+ the sqlite-vec shadow table), and stamps the model column, so the eval can run
against the new index and be compared to the MiniLM baseline. arctic-embed-s is 384d — same
vec table width as MiniLM, so no schema migration; a 768d model requires the dim to match the
vec0 table (guarded).

Usage:
  .venv/bin/python scripts/reembed_ab.py --model Snowflake/snowflake-arctic-embed-s \
      --src ~/.topos/database.db --out /tmp/topos_arctic_s.db
Then point the eval at --db /tmp/topos_arctic_s.db and compare composites.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path


def _vec_dims(conn: sqlite3.Connection) -> int:
    try:
        from topos.storage.db.migrations.vector_storage_v4 import declared_vec_dims
        return declared_vec_dims(conn)
    except Exception:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-embed the vector store with a target model")
    ap.add_argument("--model", required=True, help="HF model id (must be in EMBEDDING_MODEL_PROFILES)")
    ap.add_argument("--src", default=str(Path.home() / ".topos/database.db"))
    ap.add_argument("--out", default="", help="output DB path (default: <src>.<model>.db copy)")
    ap.add_argument("--in-place", action="store_true", help="mutate --src directly (DANGEROUS)")
    ap.add_argument("--batch", type=int, default=64)
    ap.add_argument("--limit", type=int, default=0, help="cap rows (0 = all) for a smoke run")
    args = ap.parse_args()

    from topos.engine.backends.huggingface import (
        EMBEDDING_MODEL_PROFILES,
        embedding_model_profile,
    )
    from topos.storage.adapters.sqlite.vector_search import sync_vec_row
    from topos.features.signal.vector_codec import encode_f32  # vector_blob encoder

    if args.model not in EMBEDDING_MODEL_PROFILES:
        print(f"unknown model {args.model!r}; add it to EMBEDDING_MODEL_PROFILES first", file=sys.stderr)
        return 2
    profile = embedding_model_profile(args.model)
    passage_prefix = profile.get("passage_prefix", "")
    target_dims = int(profile.get("dims", 0))

    src = Path(args.src)
    if args.in_place:
        db_path = src
    else:
        out = Path(args.out) if args.out else src.with_suffix(f".{args.model.split('/')[-1]}.db")
        print(f"copying {src} → {out} ...")
        shutil.copy2(src, out)
        db_path = out

    conn = sqlite3.connect(str(db_path))
    vec_dims = _vec_dims(conn)
    if vec_dims and target_dims and vec_dims != target_dims:
        print(
            f"REFUSING: vec0 table is {vec_dims}d but {args.model} is {target_dims}d — a "
            f"dimension change needs a new vec0 table (rebuild migration), not a re-embed.",
            file=sys.stderr,
        )
        return 3

    from sentence_transformers import SentenceTransformer
    print(f"loading {args.model} ...")
    model = SentenceTransformer(args.model)

    rows = conn.execute(
        "SELECT embedding_id, search_text FROM signal_embeddings WHERE search_text IS NOT NULL"
        + (f" LIMIT {args.limit}" if args.limit else "")
    ).fetchall()
    total = len(rows)
    print(f"re-embedding {total} rows with {args.model} (passage_prefix={passage_prefix!r}) ...")

    t0 = time.perf_counter()
    done = 0
    for i in range(0, total, args.batch):
        chunk = rows[i : i + args.batch]
        texts = [passage_prefix + str(t or "") for _, t in chunk]
        vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        for (embedding_id, _), vec in zip(chunk, vecs):
            v = [float(x) for x in vec]
            conn.execute(
                "UPDATE signal_embeddings SET vector_blob=?, vector_format='f32', model=? WHERE embedding_id=?",
                (encode_f32(v), args.model, embedding_id),
            )
            sync_vec_row(conn, embedding_id=embedding_id, vector=v)
        done += len(chunk)
        if done % (args.batch * 10) == 0 or done == total:
            rate = done / max(1e-6, time.perf_counter() - t0)
            print(f"  {done}/{total} ({rate:.0f} rows/s)")
    conn.commit()
    conn.close()
    print(f"done in {time.perf_counter()-t0:.1f}s → {db_path}")
    print(f"next: run the eval with --db {db_path} and compare composite vs MiniLM baseline")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
