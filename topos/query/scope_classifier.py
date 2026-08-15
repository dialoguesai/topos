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

#: Answer at or above this.
#:
#: Set to 0.30 by operator decision 2026-08-15, down from 0.42. The offline report over
#: 112 real questions on a live node measured median confidence at 0.346 — the classifier
#: is far less confident on real language than on generated validation data, and at 0.42
#: it answered only ~25% of real turns. TRADE-OFF, recorded so it is not rediscovered:
#: PLAN §9A measured negatives-abstained at 0.589 at this threshold against 0.877 at 0.42,
#: i.e. below the 0.85 gate. Nothing consumes this in production yet (the router is not
#: wired to a caller), so the exposure today is zero — but promoting the ladder to a real
#: caller at 0.30 would need that number re-measured first.
TAU_HIGH = 0.30

#: Below this, abstain rather than escalate: nothing in range looks like owner data.
#:
#: Set to 0.18 by operator decision 2026-08-15, down from 0.28, to reopen the escalation
#: band after TAU_HIGH moved to 0.30. At 0.28 the band was 0.02 wide and the ladder
#: degenerated: only 5.3% of real turns handed off, against 33% at the previous spacing.
#: The gap is load-bearing twice over — it is the safety valve AND the flywheel that
#: produces labelled hard cases (PLAN §6.5g), so a closed band stops both.
TAU_LOW = 0.18

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
    #: Why this branch was taken: "" (acted) | "ambiguity" | "ignorance" | "confident-none".
    #: Ambiguity and ignorance both escalate, but they are different facts — one says the
    #: model is torn, the other says it has never seen this — and only the label keeps
    #: them measurable apart (REBUILD §B0a).
    reason: str = ""

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


def resolve_scope_binding(conn: Any = None) -> Dict[str, Any]:
    """The pack's `scope` role binding, with the head as the engine default.

    Packs answer "when some code needs a model, which one?" — and scope routing is
    now a bound role rather than hardwired behaviour. Resolution order matches every
    other role: explicit pack binding, then the engine default (the installed head
    with LLM escalation — the hybrid REBUILD §2A measured above LLM-only on every
    axis except silent drops).

    Binding semantics:
      provider "scope-head"  → use the head at `model` (a path, or "" for the
                               default install location); escalate to the pack's
                               `classify` LLM on ambiguity/ignorance.
      provider "ollama" etc. → LLM-only for the scope step (the pre-head behaviour),
                               for owners who opt out of the head.
    Never raises: a broken pack cache falls back to the engine default, because a
    routing degradation must not become a query outage.
    """
    default = {"provider": "scope-head", "model": "", "source": "engine_default"}
    try:
        from ..config.model_packs import resolve_role_binding

        binding = resolve_role_binding(conn, "scope")
        if binding is not None and getattr(binding, "provider", ""):
            return {
                "provider": str(binding.provider),
                "model": str(binding.model or ""),
                "source": "pack",
            }
    except Exception:  # noqa: BLE001 — packs must never break scope routing
        logger.debug("scope role binding unresolvable; using engine default", exc_info=True)
    return default


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


#: Sanity cap on an acted-on scope set. The benchmark's widest gold is 3; a prediction
#: wider than this is treated as ambiguity, not as a very confident model.
MAX_ACTED_SCOPES = 3


def _classify_with_head(head: Any, query: str, *, top_k: int) -> ScopeVerdict:
    """The four-branch ladder (REBUILD §B0/§B0a): escalate on UNCERTAINTY, never on
    cardinality. A confident ``{availability, schedule}`` is the model doing its job;
    the LLM is the fallback for ambiguity and ignorance, not for sets.

    Heads without a trained ``none`` output (pre-B0a artifacts) keep the legacy
    two-threshold behaviour — they cannot distinguish ignorance from confident-none,
    so pretending they can would relabel every abstention as an escalation.
    """
    from .scope_head import NONE_LABEL

    scores = head.predict([query])[0]
    if NONE_LABEL not in scores:
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        best = ranked[0][1] if ranked else 0.0
        if best < head.tau_low:
            return ScopeVerdict((), best, SOURCE_HEAD, False, scores)
        if best < head.tau_high:
            return ScopeVerdict((), best, SOURCE_HEAD, True, scores, reason="ambiguity")
        labels = list(expand_routing([s for s, v in ranked[:top_k] if v >= head.tau_high]))
        _check_emittable(labels)
        return ScopeVerdict(tuple(labels), best, SOURCE_HEAD, False, scores)

    none_score = scores[NONE_LABEL]
    scope_scores = {s: v for s, v in scores.items() if s != NONE_LABEL}
    ranked = sorted(scope_scores.items(), key=lambda kv: -kv[1])
    best = ranked[0][1] if ranked else 0.0
    acted = [s for s, v in ranked if v >= head.tau_high]
    banded = any(head.tau_low <= v < head.tau_high for _s, v in ranked)
    none_high = none_score >= head.tau_high

    if acted and not banded and not none_high:
        if len(acted) > MAX_ACTED_SCOPES:
            return ScopeVerdict((), best, SOURCE_HEAD, True, scores, reason="ambiguity")
        labels = list(expand_routing(acted))
        _check_emittable(labels)
        return ScopeVerdict(tuple(labels), best, SOURCE_HEAD, False, scores)
    if none_high and not acted and not banded:
        # Confident abstain: the model DECIDED no scope is needed, it did not merely
        # fail to have an opinion.
        return ScopeVerdict((), none_score, SOURCE_HEAD, False, scores, reason="confident-none")
    if not acted and not banded and not none_high:
        # Ignorance: no signal anywhere, none included. Before B0a this was
        # representationally identical to confident-none and silently abstained —
        # 54.6% of all failures. Now it hands off to the LLM.
        return ScopeVerdict((), best, SOURCE_HEAD, True, scores, reason="ignorance")
    # Mixed signals — a banded scope, or none and a scope both confident: the model is
    # torn, and torn goes to the LLM.
    return ScopeVerdict((), best, SOURCE_HEAD, True, scores, reason="ambiguity")


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
