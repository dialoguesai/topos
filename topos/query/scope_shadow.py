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

#: File-based twin of the env flag. The node under the macOS app shell inherits no
#: shell environment and nothing loads ~/.topos/.env into os.environ, so an env-only
#: opt-in is unreachable exactly where real traffic happens. Touching this file is the
#: operator gesture; deleting it turns shadow off at the next check. It lives in
#: ~/.topos so it is discoverable next to the log it produces.
FLAG_FILE = Path.home() / ".topos" / "scope_shadow.on"

#: Agreement between what the classifier predicted and the scope the caller supplied.
VERDICT_HIT = "hit"           # predicted exactly the supplied scope
VERDICT_OVER = "over"         # predicted it, plus scopes nobody asked for
VERDICT_MISS = "miss"         # predicted some other scope entirely
VERDICT_ABSTAIN = "abstain"   # predicted nothing
VERDICT_ESCALATE = "escalate"  # sat in the uncertain band


# 8 MiB of JSONL is on the order of 40k observations — far more than the
# evaluation needs, and small enough that two generations stay unremarkable.
_DEFAULT_MAX_LOG_BYTES = 8 * 1024 * 1024


def enabled() -> bool:
    if os.environ.get(ENV_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}:
        return True
    try:
        return FLAG_FILE.is_file()
    except OSError:  # a broken home dir must not break the query path
        return False


#: Circuit breaker. Observation is telemetry — it may cost a millisecond, never a turn.
#: After this many slow-or-failed observations the shadow disables itself for the life of
#: the process and says so once. Restarting re-arms it; the flag file is untouched, so an
#: operator's intent survives a trip.
BREAKER_LIMIT = 3
#: An observation slower than this is not "shadow mode changes nothing" any more.
BREAKER_SLOW_MS = 1500.0
_breaker_faults = 0
_breaker_announced = False


def _breaker_tripped() -> bool:
    return _breaker_faults >= BREAKER_LIMIT


def _breaker_fault(why: str) -> None:
    global _breaker_faults, _breaker_announced
    _breaker_faults += 1
    if _breaker_tripped() and not _breaker_announced:
        _breaker_announced = True
        logger.warning(
            "scope shadow DISABLED for this process after %d faults (last: %s). "
            "Routing is unaffected; restart the node to re-arm.",
            _breaker_faults, why,
        )


def warm() -> bool:
    """Load the scorer ONCE, at startup, before the node serves traffic.

    This is the whole safety design. The head is 265 MB and loading it holds the GIL in
    C-level stretches; doing that mid-request, alongside the engine's MPS work, can trip
    a live torch deadlock (synchronous Metal dispatch whose block re-acquires the GIL) —
    observed wedging this node twelve times on 2026-08-15. Called from app startup, the
    load happens single-threaded, before any MPS model exists and before the first
    request, so there is nothing to contend with.

    Never raises and never blocks startup on a failure: shadow is telemetry, and a node
    that cannot load a head simply does not observe.
    """
    if not enabled():
        return False
    try:
        started = time.perf_counter()
        from .scope_classifier import _head_cached

        head = _head_cached()
        elapsed = (time.perf_counter() - started) * 1000.0
        if head is None:
            logger.info("scope shadow armed, but no head is installed — nothing to warm")
            return False
        logger.info("scope shadow armed: head warm in %.0f ms", elapsed)
        return True
    except Exception:  # noqa: BLE001 — startup must not fail because telemetry cannot
        logger.warning("scope shadow warm failed; observation stays off", exc_info=True)
        _breaker_fault("warm failed")
        return False


def max_log_bytes() -> int:
    """Size at which the shadow log rotates. 0 disables rotation."""
    raw = os.environ.get("TOPOS_SCOPE_SHADOW_MAX_BYTES", "").strip()
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_LOG_BYTES
    return value if value >= 0 else _DEFAULT_MAX_LOG_BYTES


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
    #: Which ladder branch fired (REBUILD §B0a): "" | "ambiguity" | "ignorance" |
    #: "confident-none". Distinguishes "decided nothing" from "no idea" in the log —
    #: the whole point of the four-branch ladder, so the shadow must not flatten it.
    reason: str = ""

    def as_local_row(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "verdict": self.verdict,
            "true_scope": self.true_scope,
            "predicted": list(self.predicted),
            "confidence": round(self.confidence, 4),
            "latency_ms": round(self.latency_ms, 2),
            "reason": self.reason,
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
        """Append one observation, rotating before the file can grow unbounded.

        The local row carries the raw query text — that is the point of shadow
        mode, and it never leaves the node (`as_telemetry` strips it). But
        "node-local" is not the same as "unbounded is fine": an append-only log
        of every query a person ever ran, with no cap, is the wrong default for
        a product whose security page says the data stays theirs. One rotation
        keeps a bounded window of recent traffic, which is all the evaluation
        in PLAN_SCOPE_CLASSIFIER.md §6.5 needs, and drops the rest.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_if_needed()
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.as_local_row(), ensure_ascii=False) + "\n")

    def _rotate_if_needed(self) -> None:
        """Roll to a single `.1` sibling once the live file passes the cap.

        One generation, not N: two files bound the raw text on disk at 2x the
        cap, and a rotation scheme that keeps ten of them just means ten times
        as much query history to reason about. Best effort — a log that cannot
        rotate must not take the query path down with it, which is the same
        rule `observe()` applies to everything else here.
        """
        cap = max_log_bytes()
        if cap <= 0:  # explicitly disabled; 0 must mean off, not "rotate always"
            return
        try:
            if not self.path.is_file() or self.path.stat().st_size < cap:
                return
            previous = self.path.with_suffix(self.path.suffix + ".1")
            previous.unlink(missing_ok=True)
            self.path.rename(previous)
        except OSError:
            return

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
    if _breaker_tripped() and not force:
        return None
    if not (force or enabled()):
        return None
    try:
        from .scope_classifier import _head_cached, _prototypes_cached, classify

        if not force:
            # Never load a model INLINE in the request path. Warming used to happen on a
            # daemon thread here; that put a 265 MB load in flight ALONGSIDE the engine's
            # MPS work, and this process carries a live torch/MPS deadlock (a synchronous
            # Metal dispatch whose block re-acquires the GIL) that wedged the node twelve
            # times on 2026-08-15. Warming now happens once at startup via `warm()`,
            # single-threaded, before traffic and before the MPS models load. If a scorer
            # is still cold here, skip the turn rather than load anything.
            if _head_cached.cache_info().currsize == 0 and _prototypes_cached.cache_info().currsize == 0:
                return None

        started = time.perf_counter()
        verdict_obj = (classify_fn or classify)(query_text)
        elapsed = (time.perf_counter() - started) * 1000.0
        if elapsed > BREAKER_SLOW_MS and not force:
            # Slow enough that the owner would feel it. Record the fault, still log the
            # observation — a slow verdict is data about the node, not a reason to lose it.
            _breaker_fault(f"observation took {elapsed:.0f} ms")
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
            reason=str(getattr(verdict_obj, "reason", "") or ""),
        )
        (log or ShadowLog()).append(record)
        return record
    except Exception:  # noqa: BLE001 — shadowing must never affect the query path
        logger.debug("scope shadow observation failed", exc_info=True)
        _breaker_fault("observation raised")
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
