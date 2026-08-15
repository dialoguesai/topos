"""Shadow-mode scope classification — the flywheel, with zero behavioural change.

PLAN_SCOPE_CLASSIFIER.md open decision #1 and §6.5g. Every number in §9A–§9F is
synthetic-vs-synthetic, because the only test set that predicts production — real traffic
— has never existed. This is what produces it.

**The trick is that the caller already knows the answer.** ``QueryPipeline.execute``
receives ``query_text`` *and* ``scope_id``: something upstream has already decided which
scope this question needs. So running the classifier alongside it and comparing yields
**labelled real traffic for free** — no annotation, no user prompt, no behavioural change.

Shadow mode is the only responsible first wiring. §9F measured the trained head at 0.369
against an untrained prototype's 0.387, and §7's gate needs per-scope recall ≥ 0.60 on all
fourteen; nothing here is fit to *decide* anything. Observing costs nothing and is
reversible; deciding is neither.

Three hard rules, each a test:

* **Off by default.** ``TOPOS_SCOPE_SHADOW=1`` opts in. An unset env var must leave the
  query path byte-identical.
* **Never raises, and never blocks.** Any failure is swallowed, and a cold prototype
  cache is skipped rather than loaded — the first observation measured 10.5s inline
  before that guard, which would have made "changes nothing" false in the way users
  actually notice.
* **Same two-serializer split as M1.** ``as_local_row`` keeps the text; ``as_telemetry``
  carries counts and closed-set enums only. The comparison to truth is the valuable part
  and it is a *label*, not content.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

ENV_FLAG = "TOPOS_SCOPE_SHADOW"

#: Agreement between what the classifier predicted and the scope the caller supplied.
VERDICT_HIT = "hit"           # predicted exactly the supplied scope
VERDICT_OVER = "over"         # predicted it, plus scopes nobody asked for
VERDICT_MISS = "miss"         # predicted some other scope entirely
VERDICT_ABSTAIN = "abstain"   # predicted nothing
VERDICT_ESCALATE = "escalate"  # sat in the uncertain band


def enabled() -> bool:
    return os.environ.get(ENV_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


def default_log_path() -> Path:
    return Path.home() / ".topos" / "scope_shadow.jsonl"


@dataclass(frozen=True)
class ShadowRecord:
    """One prediction measured against the scope the caller actually used."""

    verdict: str
    true_scope: str
    predicted: Tuple[str, ...]
    confidence: float
    latency_ms: float
    text: str = ""
    ts: float = 0.0

    def as_local_row(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "verdict": self.verdict,
            "true_scope": self.true_scope,
            "predicted": list(self.predicted),
            "confidence": round(self.confidence, 4),
            "latency_ms": round(self.latency_ms, 2),
            "text": self.text,
        }

    def as_telemetry(self) -> Dict[str, Any]:
        """Counts and scope ids only. Adding a field here is a privacy decision."""
        return {
            "verdict": self.verdict,
            "true_scope": self.true_scope,
            "predicted": list(self.predicted),
            "n_predicted": len(self.predicted),
        }


def compare(predicted: Sequence[str], true_scope: str, *, escalated: bool) -> str:
    if escalated:
        return VERDICT_ESCALATE
    pred = set(predicted)
    if not pred:
        return VERDICT_ABSTAIN
    if pred == {true_scope}:
        return VERDICT_HIT
    if true_scope in pred:
        return VERDICT_OVER
    return VERDICT_MISS


class ShadowLog:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else default_log_path()

    def append(self, record: ShadowRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.as_local_row(), ensure_ascii=False) + "\n")

    def read(self) -> List[Dict[str, Any]]:
        if not self.path.is_file():
            return []
        out: List[Dict[str, Any]] = []
        for line in self.path.read_text("utf-8").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out


def observe(
    query_text: str,
    true_scope: str,
    *,
    log: Optional[ShadowLog] = None,
    classify_fn: Optional[Callable[..., Any]] = None,
    force: bool = False,
) -> Optional[ShadowRecord]:
    """Predict, compare against the scope the caller supplied, log. Never raises.

    Returns the record when shadowing ran, ``None`` when it was off or failed. Callers
    ignore the return value — it exists for tests.
    """
    if not (force or enabled()):
        return None
    try:
        from .scope_classifier import _prototypes_cached, classify

        if not force and _prototypes_cached.cache_info().currsize == 0:
            # Cold cache means this call would load the embedding model INLINE in the
            # request path — measured at 10.5s on a first observation. "Shadow mode
            # changes nothing" has to mean latency too, so skip until something else
            # (retrieval, normally) has warmed the slot.
            return None

        started = time.perf_counter()
        verdict_obj = (classify_fn or classify)(query_text)
        elapsed = (time.perf_counter() - started) * 1000.0
        record = ShadowRecord(
            verdict=compare(
                verdict_obj.labels, true_scope, escalated=verdict_obj.escalated
            ),
            true_scope=true_scope,
            predicted=tuple(verdict_obj.labels),
            confidence=float(verdict_obj.confidence),
            latency_ms=elapsed,
            text=query_text,
            ts=time.time(),
        )
        (log or ShadowLog()).append(record)
        return record
    except Exception:  # noqa: BLE001 — shadowing must never affect the query path
        logger.debug("scope shadow observation failed", exc_info=True)
        return None


@dataclass
class ShadowReport:
    total: int = 0
    by_verdict: Dict[str, int] = field(default_factory=dict)
    by_scope: Dict[str, Dict[str, int]] = field(default_factory=dict)
    confusion: Dict[str, int] = field(default_factory=dict)

    def accuracy(self) -> float:
        return self.by_verdict.get(VERDICT_HIT, 0) / self.total if self.total else float("nan")

    def as_telemetry(self) -> Dict[str, Any]:
        """The aggregate §6.5g wants: which pairs the classifier cannot separate, as counts."""
        return {
            "total": self.total,
            "by_verdict": dict(self.by_verdict),
            "hit_rate": round(self.accuracy(), 4) if self.total else None,
            "confusion": dict(
                sorted(self.confusion.items(), key=lambda kv: -kv[1])[:15]
            ),
        }


def summarize(rows: Sequence[Dict[str, Any]]) -> ShadowReport:
    """Roll the node-local log into the text-free shape that may leave the node."""
    report = ShadowReport()
    for row in rows:
        report.total += 1
        verdict = str(row.get("verdict") or "?")
        report.by_verdict[verdict] = report.by_verdict.get(verdict, 0) + 1
        true_scope = str(row.get("true_scope") or "?")
        bucket = report.by_scope.setdefault(true_scope, {})
        bucket[verdict] = bucket.get(verdict, 0) + 1
        if verdict == VERDICT_MISS:
            for predicted in row.get("predicted") or []:
                pair = f"{true_scope} -> {predicted}"
                report.confusion[pair] = report.confusion.get(pair, 0) + 1
    return report
