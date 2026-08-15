"""M0 — local scope classifier, rung 1 (prototype similarity).

PLAN_SCOPE_CLASSIFIER.md §4. Given free text, name the UMA scope(s) it asks about, or
abstain. This is the only stage of the query path that runs **before** the permission
gate: everything downstream sees data the grant already released, but this sees the raw
question, and the question alone is sensitive. That is why it runs on-device.

Rung 1 needs **no training data and no extra model**. Each scope's ``description`` and
``example_questions`` from ``scope_registry.json`` are embedded into a prototype with
``all-MiniLM-L6-v2`` — already resident in ``ModelSlot.EMBEDDING`` for retrieval — and a
query is scored by cosine against those prototypes. Its job is to establish the
interface, the thresholds and the escalation contract so rungs 2 and 3 drop in behind a
stable API. Its accuracy is expected to be mediocre; that is the point of a cold start.

Two design commitments carried from the plan:

* **Escalate, never guess.** Above ``tau_high`` answer; between the thresholds escalate
  to an LLM; below ``tau_low`` abstain and open nothing. A miss degrades to today's
  behaviour rather than to a wrong answer, and every escalation is a labelled hard case.
* **Routing is not authorization.** ``ROUTING_TO_SCOPES`` sits between the model's output
  and the permission layer as an identity map today (§6A.4). Building the indirection now
  makes a later routing/permission split a config change instead of a rewrite, and stops
  the permission taxonomy from being quietly reshaped by model convenience.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Dict, List, Sequence, Tuple

from .scope_registry_loader import LEGACY_SCOPE_IDS, list_scopes

logger = logging.getLogger(__name__)

#: Answer at or above this. Start high — escalating is cheap, a wrong scope is not.
TAU_HIGH = 0.42

#: Below this, abstain rather than escalate: nothing in range looks like owner data.
TAU_LOW = 0.28

#: Routing label -> permission scopes. Identity today; the seam for §6A.4 option (B),
#: where a coarser routing taxonomy maps many-to-one onto finer permission scopes.
ROUTING_TO_SCOPES: Dict[str, Tuple[str, ...]] = {}

SOURCE_PROTOTYPE = "prototype"
SOURCE_HEAD = "head"
SOURCE_LLM = "llm"


@dataclass(frozen=True)
class ScopeVerdict:
    """The contract every rung returns, unchanged as rungs 2 and 3 land."""

    labels: Tuple[str, ...]
    confidence: float
    source: str
    escalated: bool
    scores: Dict[str, float] = field(default_factory=dict)

    @property
    def abstained(self) -> bool:
        return not self.labels and not self.escalated


def live_scope_ids() -> Tuple[str, ...]:
    """The scopes this classifier is allowed to emit, from the live registry."""
    return tuple(
        str(e["scope_id"])
        for e in list_scopes()
        if str(e.get("implementation_status", "")) == "live"
    )


def prototype_texts() -> Dict[str, List[str]]:
    """Per scope, the phrases that define it. Human-authored, zero training data."""
    out: Dict[str, List[str]] = {}
    for entry in list_scopes():
        scope_id = str(entry["scope_id"])
        if str(entry.get("implementation_status", "")) != "live":
            continue
        texts = [str(entry.get("description") or "").strip()]
        texts += [str(q).strip() for q in (entry.get("example_questions") or [])]
        out[scope_id] = [t for t in texts if t]
    return out


def _default_embed(texts: Sequence[str], input_role: str) -> List[List[float]]:
    """Embed through the engine adapter so the resident MiniLM slot is reused.

    Going around the adapter would load a second copy of the model, which defeats the
    rung's whole cost argument.
    """
    from ..engine.backends.huggingface import HuggingFaceAdapter, active_embedding_model

    result = HuggingFaceAdapter().run_inference(
        {"texts": list(texts)},
        {
            "subtype": "embedding",
            "model": active_embedding_model(),
            "input_role": input_role,
            "batch_size": 32,
        },
    )
    return [[float(x) for x in vec] for vec in (result.get("vectors") or [])]


def _mean(vectors: Sequence[Sequence[float]]) -> List[float]:
    if not vectors:
        return []
    dims = len(vectors[0])
    return [sum(v[i] for v in vectors) / len(vectors) for i in range(dims)]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


@lru_cache(maxsize=1)
def _head_cached() -> Any:
    """The installed head, or None. Errors are surfaced, not swallowed.

    A missing head is the shipping default and stays silent. A head that exists but is
    refused — drifted labels, an unauditable manifest — is logged loudly and we fall back
    to prototypes: degraded routing is recoverable, a mis-routed scope is not.
    """
    from .scope_head import ScopeHeadError, load_head

    try:
        return load_head()
    except ScopeHeadError:
        logger.error("installed scope head REFUSED; falling back to prototypes", exc_info=True)
        return None
    except Exception:  # noqa: BLE001 — never let a model file break routing
        logger.warning("could not load scope head; falling back to prototypes", exc_info=True)
        return None


@lru_cache(maxsize=1)
def _prototypes_cached() -> Tuple[Tuple[str, Tuple[float, ...]], ...]:
    return _build_prototypes(_default_embed)


def _build_prototypes(
    embed: Callable[[Sequence[str], str], List[List[float]]],
) -> Tuple[Tuple[str, Tuple[float, ...]], ...]:
    """One centroid per scope. Prototype text is a passage; the query is a query."""
    texts_by_scope = prototype_texts()
    flat: List[str] = []
    spans: List[Tuple[str, int, int]] = []
    for scope_id, texts in texts_by_scope.items():
        start = len(flat)
        flat.extend(texts)
        spans.append((scope_id, start, len(flat)))
    if not flat:
        return ()
    vectors = embed(flat, "passage")
    out: List[Tuple[str, Tuple[float, ...]]] = []
    for scope_id, start, end in spans:
        centroid = _mean(vectors[start:end])
        if centroid:
            out.append((scope_id, tuple(centroid)))
    return tuple(out)


def expand_routing(labels: Sequence[str]) -> Tuple[str, ...]:
    """Routing labels -> permission scopes. Identity unless ROUTING_TO_SCOPES says otherwise."""
    out: List[str] = []
    for label in labels:
        for scope in ROUTING_TO_SCOPES.get(label, (label,)):
            if scope not in out:
                out.append(scope)
    return tuple(out)


def _check_emittable(labels: Sequence[str]) -> None:
    """§6A.2 — never emit a legacy id, never emit one the live registry dropped."""
    live = set(live_scope_ids())
    for label in labels:
        if label in LEGACY_SCOPE_IDS:
            raise ValueError(f"scope classifier emitted legacy scope id {label!r}")
        if label not in live:
            raise ValueError(
                f"scope classifier emitted {label!r}, absent from the live registry "
                f"({len(live)} scopes) — regenerate prototypes after a taxonomy change"
            )


def _classify_with_head(head: Any, query: str, *, top_k: int) -> ScopeVerdict:
    """Same thresholds, same contract — only the scorer changes."""
    scores = head.predict([query])[0]
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best = ranked[0][1] if ranked else 0.0
    if best < head.tau_low:
        return ScopeVerdict((), best, SOURCE_HEAD, False, scores)
    if best < head.tau_high:
        return ScopeVerdict((), best, SOURCE_HEAD, True, scores)
    labels = list(expand_routing([s for s, v in ranked[:top_k] if v >= head.tau_high]))
    _check_emittable(labels)
    return ScopeVerdict(tuple(labels), best, SOURCE_HEAD, False, scores)


def classify(
    text: str,
    *,
    tau_high: float = TAU_HIGH,
    tau_low: float = TAU_LOW,
    top_k: int = 2,
    prototypes: Sequence[Tuple[str, Sequence[float]]] | None = None,
    embed: Callable[[Sequence[str], str], List[List[float]]] | None = None,
    head: Any = None,
    use_head: bool = True,
) -> ScopeVerdict:
    """Score one utterance against the scope prototypes.

    ``top_k`` allows a genuine multi-label answer ("am I free Friday according to my
    calendar" is availability AND schedule), admitted only when the runner-up also
    clears ``tau_high``.
    """
    query = (text or "").strip()
    if not query:
        return ScopeVerdict((), 0.0, SOURCE_PROTOTYPE, False, {})

    # Prefer a trained head when installed. Nothing else changes: same ScopeVerdict,
    # same escalation contract, same callers.
    if prototypes is None and embed is None and use_head:
        active = head if head is not None else _head_cached()
        if active is not None:
            return _classify_with_head(active, query, top_k=top_k)

    protos = list(prototypes) if prototypes is not None else list(_prototypes_cached())
    if not protos:
        return ScopeVerdict((), 0.0, SOURCE_PROTOTYPE, False, {})

    embed_fn = embed or _default_embed
    vector = (embed_fn([query], "query") or [[]])[0]
    scores = {scope_id: _cosine(vector, centroid) for scope_id, centroid in protos}
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    best_score = ranked[0][1] if ranked else 0.0

    if best_score < tau_low:
        return ScopeVerdict((), best_score, SOURCE_PROTOTYPE, False, scores)
    if best_score < tau_high:
        # In the band: the caller escalates to an LLM and logs a hard case (M1).
        return ScopeVerdict((), best_score, SOURCE_PROTOTYPE, True, scores)

    labels = [scope for scope, score in ranked[:top_k] if score >= tau_high]
    labels = list(expand_routing(labels))
    _check_emittable(labels)
    return ScopeVerdict(tuple(labels), best_score, SOURCE_PROTOTYPE, False, scores)


def reset_cache() -> None:
    """Drop memoized prototypes and head — call after a registry or model change."""
    _prototypes_cached.cache_clear()
    _head_cached.cache_clear()


def active_source() -> str:
    """Which rung is live right now, for a status surface."""
    return SOURCE_HEAD if _head_cached() is not None else SOURCE_PROTOTYPE
