"""VP-series: terse-text vector-recall probes (plan C2 / D-vector).

The composition catalog cannot distinguish embedders — it is dominated by canonical/stats/
facts surfaces; only vector-bound queries exercise the embedding model. These probes are
purpose-built to isolate the embedder: each query is a semantic PARAPHRASE that shares NO
content token with its target (so lexical/FTS/canonical-token retrieval cannot find it — only
dense retrieval can), yet uses common-enough words that the rare-token abstention gate does
not fire. Graded by RAW vector recall (bypassing fusion), so the score is the embedder's
alone.

Each probe: (query, needle_group). needle_group = topic-level alternatives — a hit is any
top-k vector item whose preview contains any alternative (terse personal messages are short,
so the "answer" is a small set of on-topic messages, not one exact string — deep-research R5:
short-text asymmetric retrieval). Report recall@10 and MRR; compare across embedders.

Validated against the live corpus 2026-07-08 (every query: zero content-token overlap with
its target message, all query tokens FTS df >= 3 so the gate stays open). MiniLM-L6 baseline:
recall@10 7/8, MRR 0.768 — real rank spread (0..6, one miss) gives headroom to separate models.
"""

from __future__ import annotations

from typing import List, Tuple

# (paraphrase query, needle_group). The query deliberately avoids the target's own words.
VECTOR_PROBES: List[Tuple[str, List[str]]] = [
    ("cheap late night japanese food nearby", ["sushi", "ramen", "japanese"]),
    ("peer to peer money transfer app on my phone", ["zelle", "venmo", "cash app", "peer to peer"]),
    ("watching fireworks by the water with a crowd", ["firework", "bushwick", "inlet"]),
    ("planning to work out at the gym", ["equinox", "gym", "workout", "working out", "lift"]),
    ("my friend is away traveling this week", ["out of town", "away", "trip", "vacation"]),
    ("grabbing dinner at a restaurant tonight", ["reserved", "dinner", "restaurant", "table"]),
    ("problems getting a rideshare", ["uber", "lyft", "ride"]),
    ("i redesigned my website", ["overhaul", "site", "website", "redesign"]),
]


def vector_recall(service, *, model: str | None = None, k: int = 10, mode: str = "vector") -> dict:
    """Raw-vector recall@k + MRR over VECTOR_PROBES for the given signal service/model.
    Embedder-only by design: mode='vector' forces PURE semantic search (no FTS), so the
    score is the embedding model's alone — a hybrid search would let FTS satisfy needle
    words lexically and mask the embedder (measured 2026-07-08: hybrid gave MiniLM and
    arctic-s byte-identical ranks because FTS carried the lexically-overlapping probes)."""
    hits = 0
    rr = 0.0
    per_probe = []
    for query, group in VECTOR_PROBES:
        kwargs = {"query": query, "limit": k, "mode": mode}
        if model:
            kwargs["model"] = model
        res = service.search_vectors(**kwargs)
        items = res.get("items") or []
        rank = -1
        for i, it in enumerate(items):
            blob = str(it.get("text_preview") or "").lower()
            if any(n in blob for n in group):
                rank = i
                break
        hit = rank >= 0
        hits += int(hit)
        rr += (1.0 / (rank + 1)) if hit else 0.0
        per_probe.append({"query": query, "rank": rank, "hit": hit})
    n = len(VECTOR_PROBES)
    return {
        "model": model or "active",
        "n": n,
        "recall_at_k": round(hits / n, 4),
        "recall_hits": hits,
        "mrr": round(rr / n, 4),
        "k": k,
        "per_probe": per_probe,
    }
