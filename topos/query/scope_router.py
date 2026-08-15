"""M1 — the escalation ladder and the logging boundary.

PLAN_SCOPE_CLASSIFIER.md §4 (escalation contract) and §6.3–6.4 (privacy architecture).

M0 built a classifier that *signals* escalation. M1 performs it, and — more importantly —
establishes the two-sided data boundary the whole plan rests on::

    node-local escalation log   full text, owner's own data, NEVER leaves the node
    telemetry                   counts only, no text, no embeddings, opt-in

That asymmetry is the point. The escalation log is what makes M3 possible (every hard
case is a labelled example) and what makes §6.5g's curriculum loop work: the node reports
*"availability:read ↔ schedule:read confusion at 14%"*, a number, and the synthetic
generator is aimed at that band centrally. **Real usage improves the model through error
signals, never through examples.**

``EscalationRecord`` therefore has two serializers and they are not interchangeable:

* ``as_local_row()`` — everything, for the node's own sqlite/jsonl.
* ``as_telemetry()`` — counts and enums only. ``test_scope_router.py`` asserts no field
  of it can carry free text, because "we promise not to send text" is a policy and this
  is a boundary.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .scope_classifier import (
    SOURCE_LLM,
    TAU_HIGH,
    TAU_LOW,
    ScopeVerdict,
    classify,
    live_scope_ids,
)

logger = logging.getLogger(__name__)

#: Why a turn was logged. ``HARD`` cases are the training seam; ``ABSTAIN`` cases say the
#: classifier saw nothing owner-shaped, which is also worth counting.
OUTCOME_ANSWERED = "answered"
OUTCOME_HARD = "hard"
OUTCOME_ABSTAINED = "abstained"

#: An LLM that maps free text to scope ids. Injected so the ladder is testable and so the
#: caller decides *which* LLM — a local ollama model and a cloud one have very different
#: privacy postures, and this module must not quietly pick one.
EscalationFn = Callable[[str], Sequence[str]]


def default_log_path() -> Path:
    return Path.home() / ".topos" / "scope_escalations.jsonl"


@dataclass(frozen=True)
class EscalationRecord:
    """One routing decision. Two serializers, deliberately asymmetric — see module doc."""

    outcome: str
    confidence: float
    source: str
    predicted: Tuple[str, ...]
    runner_up: Tuple[str, ...]
    latency_ms: float
    tau_high: float
    tau_low: float
    text: str = ""
    llm_labels: Tuple[str, ...] = ()
    ts: float = 0.0

    def as_local_row(self) -> Dict[str, Any]:
        """Full fidelity. Node-local only — this is the owner's own data."""
        return {
            "ts": self.ts,
            "outcome": self.outcome,
            "text": self.text,
            "confidence": round(self.confidence, 4),
            "source": self.source,
            "predicted": list(self.predicted),
            "runner_up": list(self.runner_up),
            "llm_labels": list(self.llm_labels),
            "latency_ms": round(self.latency_ms, 2),
            "tau_high": self.tau_high,
            "tau_low": self.tau_low,
        }

    def as_telemetry(self) -> Dict[str, Any]:
        """Counts and closed-set enums ONLY.

        Every value here is a float, a bool, or a scope id from the live registry. No
        free text, and no embedding — an embedding is invertible enough to count as
        content. Adding a field to this dict is a privacy decision, not a logging one.
        """
        return {
            "outcome": self.outcome,
            "source": self.source,
            "confidence_bucket": _bucket(self.confidence),
            "predicted": list(self.predicted),
            "runner_up": list(self.runner_up),
            "n_labels": len(self.predicted),
            "escalated": self.outcome == OUTCOME_HARD,
            "latency_bucket": _latency_bucket(self.latency_ms),
        }


def _bucket(value: float) -> str:
    """Confidence as a coarse band. A raw float is a fingerprint; a band is a statistic."""
    for edge, name in ((0.2, "very_low"), (0.35, "low"), (0.5, "mid"), (0.7, "high")):
        if value < edge:
            return name
    return "very_high"


def _latency_bucket(ms: float) -> str:
    for edge, name in ((5.0, "sub_5ms"), (25.0, "sub_25ms"), (100.0, "sub_100ms"), (1000.0, "sub_1s")):
        if ms < edge:
            return name
    return "slow"


class JsonlEscalationLog:
    """Append-only node-local log. Never read by anything that leaves the machine."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else default_log_path()

    def append(self, record: EscalationRecord) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record.as_local_row(), ensure_ascii=False) + "\n")
        except OSError:
            # Routing must never fail because logging did.
            logger.warning("could not append to the scope escalation log", exc_info=True)

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


@dataclass
class RouterStats:
    answered: int = 0
    hard: int = 0
    abstained: int = 0
    llm_failures: int = 0
    confusion: Dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.answered + self.hard + self.abstained

    def escalation_rate(self) -> float:
        return self.hard / self.total if self.total else float("nan")


class ScopeRouter:
    """classify -> escalate -> log. The rung behind it is swappable; this is not."""

    def __init__(
        self,
        *,
        escalate: Optional[EscalationFn] = None,
        log: Optional[JsonlEscalationLog] = None,
        tau_high: float = TAU_HIGH,
        tau_low: float = TAU_LOW,
        classify_fn: Optional[Callable[..., ScopeVerdict]] = None,
        enable_log: bool = True,
    ) -> None:
        self.escalate = escalate
        self.log = log or JsonlEscalationLog()
        self.tau_high = tau_high
        self.tau_low = tau_low
        self._classify = classify_fn or classify
        self.enable_log = enable_log
        self.stats = RouterStats()

    def route(self, text: str) -> ScopeVerdict:
        started = time.perf_counter()
        verdict = self._classify(text, tau_high=self.tau_high, tau_low=self.tau_low)
        runner_up = _runner_up(verdict.scores, verdict.labels)

        if verdict.escalated:
            verdict = self._do_escalate(text, verdict)
            outcome = OUTCOME_HARD
            self.stats.hard += 1
        elif verdict.labels:
            outcome = OUTCOME_ANSWERED
            self.stats.answered += 1
        else:
            outcome = OUTCOME_ABSTAINED
            self.stats.abstained += 1

        elapsed = (time.perf_counter() - started) * 1000.0
        self._record_confusion(verdict.labels, runner_up)
        record = EscalationRecord(
            outcome=outcome,
            confidence=verdict.confidence,
            source=verdict.source,
            predicted=verdict.labels,
            runner_up=runner_up,
            latency_ms=elapsed,
            tau_high=self.tau_high,
            tau_low=self.tau_low,
            text=text,
            llm_labels=verdict.labels if verdict.source == SOURCE_LLM else (),
            ts=time.time(),
        )
        if self.enable_log and outcome in (OUTCOME_HARD, OUTCOME_ABSTAINED):
            # Answered turns are the boring majority; only the seam is worth keeping.
            self.log.append(record)
        return verdict

    def _do_escalate(self, text: str, verdict: ScopeVerdict) -> ScopeVerdict:
        if self.escalate is None:
            # No LLM wired: hold the abstain rather than inventing a scope. Failing
            # open here would answer from owner records on a low-confidence guess.
            return ScopeVerdict((), verdict.confidence, verdict.source, True, verdict.scores)
        try:
            raw = self.escalate(text)
        except Exception:  # noqa: BLE001 — an LLM outage must not open a scope
            self.stats.llm_failures += 1
            logger.warning("scope escalation failed; holding abstain", exc_info=True)
            return ScopeVerdict((), verdict.confidence, verdict.source, True, verdict.scores)
        live = set(live_scope_ids())
        labels = tuple(str(x) for x in (raw or ()) if str(x) in live)
        return ScopeVerdict(labels, verdict.confidence, SOURCE_LLM, True, verdict.scores)

    def _record_confusion(self, labels: Sequence[str], runner_up: Sequence[str]) -> None:
        """The §6.5g curriculum signal: which pairs the classifier cannot separate."""
        if not labels or not runner_up:
            return
        pair = " ~ ".join(sorted((labels[0], runner_up[0])))
        if labels[0] != runner_up[0]:
            self.stats.confusion[pair] = self.stats.confusion.get(pair, 0) + 1

    def telemetry(self) -> Dict[str, Any]:
        """Aggregate, text-free. This is the only shape allowed off the node."""
        return {
            "total": self.stats.total,
            "answered": self.stats.answered,
            "hard": self.stats.hard,
            "abstained": self.stats.abstained,
            "llm_failures": self.stats.llm_failures,
            "escalation_rate": round(self.stats.escalation_rate(), 4)
            if self.stats.total
            else None,
            "tau_high": self.tau_high,
            "tau_low": self.tau_low,
            "confusion": dict(sorted(self.stats.confusion.items(), key=lambda kv: -kv[1])[:10]),
        }


def _runner_up(scores: Dict[str, float], labels: Sequence[str]) -> Tuple[str, ...]:
    if not scores:
        return ()
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    chosen = set(labels)
    for scope, _score in ranked:
        if scope not in chosen:
            return (scope,)
    return ()


def aggregate_telemetry(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Roll node-local rows into the text-free shape, for an offline reporter."""
    counts: Dict[str, int] = {}
    buckets: Dict[str, int] = {}
    total = 0
    for row in records:
        total += 1
        counts[str(row.get("outcome"))] = counts.get(str(row.get("outcome")), 0) + 1
        band = _bucket(float(row.get("confidence") or 0.0))
        buckets[band] = buckets.get(band, 0) + 1
    return {"total": total, "by_outcome": counts, "by_confidence": buckets}
