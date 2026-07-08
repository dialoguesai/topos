"""Embedder A/B on the terse-text vector-probe lane (plan C2).

Runs raw-vector recall@10 + MRR over VECTOR_PROBES against a chosen DB/index+model. Point it
at the live (MiniLM) index and at an arctic-re-embedded copy (scripts/reembed_ab.py) with
TOPOS_EMBED_MODEL matching, and compare — this is the powered instrument the composition
catalog can't provide.

  # MiniLM baseline (live index):
  .venv/bin/python scripts/bench_embed_recall.py
  # arctic-s (after reembed_ab.py):
  TOPOS_EMBED_MODEL=Snowflake/snowflake-arctic-embed-s \
    .venv/bin/python scripts/bench_embed_recall.py --db /tmp/topos_arctic_s.db
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path.home() / ".topos/database.db"))
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--json", action="store_true", help="emit JSON only")
    args = ap.parse_args()

    os.environ["TOPOS_DATABASE_PATH"] = args.db
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/gap/qq/engine"))

    # Force the GLOBAL db connection (which get_signal_service reads) to the requested DB:
    # point settings at it and drop any connection already opened to another file, so the
    # query embeds AND the vectors searched come from the same index (else arctic queries
    # hit MiniLM vectors → guaranteed 0 recall — the bug this fixes).
    from topos.config.settings import settings
    settings.topos_database_path = args.db
    from topos.core import state as _state
    _state.db_conn = None
    _state._db_conn_path = None

    from topos.features.signal.service import get_signal_service
    from topos.engine.backends.huggingface import active_embedding_model
    from vector_probe_cases import vector_recall

    svc = get_signal_service()
    result = vector_recall(svc, k=args.k)
    result["embed_model"] = active_embedding_model()
    result["db"] = args.db

    if args.json:
        print(json.dumps(result, indent=1))
    else:
        print(f"embed_model : {result['embed_model']}")
        print(f"db          : {args.db}")
        print(f"recall@{args.k}   : {result['recall_hits']}/{result['n']} = {result['recall_at_k']}")
        print(f"MRR         : {result['mrr']}")
        print("per-probe ranks:")
        for p in result["per_probe"]:
            print(f"   rank={p['rank']:2d}  {p['query']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
