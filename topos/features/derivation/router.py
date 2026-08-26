"""R0-M2 — semantic pack router (hybrid with the lexical prefilter).

The lexical prefilter is recall-tuned but blind to phrasing it has never seen;
the semantic router scores a record against each pack's ROUTING CENTROID (the
embedding of its descriptors + exemplars + gold texts). Hybrid = lexical OR
semantic-above-threshold, so nothing the lexical layer already routes is lost.

OPT-IN by env (TOPOS_DERIVATION_ROUTER=hybrid); default stays lexical until the
shadow evaluation picks the threshold from data — a router change silently
shifts which records cost LLM calls, so it ships measured or not at all.
"""

from __future__ import annotations

import math
import os
from typing import Dict, List, Optional

_CENTROIDS: Dict[str, List[float]] = {}


def router_mode() -> str:
    return os.environ.get("TOPOS_DERIVATION_ROUTER", "lexical").strip().lower()


def semantic_threshold() -> float:
    try:
        return float(os.environ.get("TOPOS_DERIVATION_ROUTER_TAU", "0.42"))
    except ValueError:
        return 0.42


def _embed(text: str) -> Optional[List[float]]:
    try:
        from ...engine.backends.huggingface import HuggingFaceAdapter
        from ...engine.backends.huggingface import active_embedding_model
        hf = HuggingFaceAdapter()
        out = hf.run_inference(
            {"text": text[:2000]},
            {"subtype": "embedding", "model": active_embedding_model(), "input_role": "query"},
        )
        vecs = out.get("vectors") or []
        return list(vecs[0]) if vecs else None
    except Exception:  # noqa: BLE001 — router must never break ingest
        return None


def _cos(a: List[float], b: List[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a)) or 1.0
    db = math.sqrt(sum(x * x for x in b)) or 1.0
    return num / (da * db)


def pack_centroid(pack) -> Optional[List[float]]:
    key = f"{pack.pack}@{pack.version}"
    if key in _CENTROIDS:
        return _CENTROIDS[key]
    r = pack.routing or {}
    parts = [str(d) for d in (r.get("descriptors") or [])]
    parts += [str(e) for e in (r.get("exemplars") or [])]
    for g in ((pack.raw or {}).get("eval") or {}).get("gold") or []:
        t = str(g.get("text") or "")
        if t:
            parts.append(t)
    vec = _embed("\n".join(parts)) if parts else None
    if vec is not None:
        _CENTROIDS[key] = vec
    return vec


def semantic_passes(pack, record_vec: Optional[List[float]]) -> bool:
    if record_vec is None:
        return False
    c = pack_centroid(pack)
    if c is None:
        return False
    return _cos(record_vec, c) >= semantic_threshold()


def embed_record(text: str) -> Optional[List[float]]:
    return _embed(text)
