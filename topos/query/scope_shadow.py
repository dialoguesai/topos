"""Shadow-mode scope classification — real traffic, observed, with zero behavioural change.

PLAN_SCOPE_CLASSIFIER.md open decision #1 and §6.5g. Every number in §9A–§9F is
synthetic-vs-synthetic, because the only test set that predicts production — real traffic
— has never existed. This is what produces it.

**What this log is not: ground truth.** An earlier version of this docstring claimed the
caller "already knows the answer", so comparing against ``scope_id`` yielded *labelled*
traffic for free. That was wrong, and the field was called ``true_scope`` for a while on
the strength of it. ``scope_id`` is whichever scope the **incumbent router** picked, and
the incumbent is a heuristic that is sometimes wrong in ways worth catching. Observed
2026-08-17: for *"what is a good prompt we could ask of our work, schedule…"* — a
meta-question about prompt writing that should have retrieved nothing — the router chose
``schedule:read`` and ``work_context:read``, and this log would have recorded both as
truth. Train on that and the head learns to reproduce the router, mistakes included.
So the field is ``router_scope``, agreement is ``agreement_rate``, and turning any of it
into labels needs adjudication that happens elsewhere.

**Two record kinds, because the interesting failures make no query at all.** Observation
used to live only inside ``QueryPipeline.execute``, which runs *after* something decided a
scope — so a turn that queried nothing was invisible. In the session above, one turn of
four queried; the three that named data sources explicitly and then retrieved nothing were
absent from this log entirely. So:

* ``kind="turn"`` — logged when the user's text arrives (tool retrieval, once per turn),
  before any routing. No ``router_scope``: nothing has decided yet.
* ``kind="route"`` — logged per scope the router actually queried, as before.

Join them on ``text``: a ``turn`` row with no ``route`` rows *is* the no-query case, and
``turn_coverage()`` counts them. That population is the reason this exists.

Three hard rules, each a test:

* **Off by default, and off on request.** ``TOPOS_SCOPE_SHADOW=1`` opts in and an unset
  env var must leave the query path byte-identical — but ``TOPOS_SCOPE_SHADOW=0`` must
  also opt *out*, beating the flag file. For a while it could not: the env could only
  turn shadow on, so on an operator machine holding the file, any subprocess that ran
  the query path observed and appended to their real log with no way to decline.
* **Never raises, and never blocks.** Any failure is swallowed, and a cold prototype
  cache is skipped rather than loaded — the first observation measured 10.5s inline
  before that guard, which would have made "changes nothing" false in the way users
  actually notice.
* **Same two-serializer split as M1.** ``as_local_row`` keeps the text; ``as_telemetry``
  carries counts and closed-set enums only. The comparison to the router is the valuable
  part and it is a *label*, not content.
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

#: Redirects the log. See ``default_log_path()`` for why a harness needs this.
ENV_LOG_PATH = "TOPOS_SCOPE_SHADOW_LOG"

#: The closed sets ``ENV_FLAG`` is read against. Anything else is not a vote either way
#: and falls through to ``FLAG_FILE``, exactly as an unset var does.
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})

#: File-based twin of the env flag. The node under the macOS app shell inherits no
#: shell environment and nothing loads ~/.topos/.env into os.environ, so an env-only
#: opt-in is unreachable exactly where real traffic happens. Touching this file is the
#: operator gesture; deleting it turns shadow off at the next check. It lives in
#: ~/.topos so it is discoverable next to the log it produces. It is the *weaker* of the
#: two switches: an explicit ``TOPOS_SCOPE_SHADOW=0`` overrides it, because a subprocess
#: has no other way to decline a file it inherited. See ``enabled()``.
FLAG_FILE = Path.home() / ".topos" / "scope_shadow.on"

#: **Agreement with the incumbent router** — not correctness. See the module docstring:
#: the router is a heuristic and a "miss" may well be the head being right. These strings
#: are values in an existing on-disk log, so they stay stable even though "hit"/"miss"
#: read more like grades than they should.
VERDICT_HIT = "hit"           # predicted exactly the scope the router used
VERDICT_OVER = "over"         # predicted it, plus scopes the router did not use
VERDICT_MISS = "miss"         # predicted some other scope entirely
VERDICT_ABSTAIN = "abstain"   # predicted nothing
VERDICT_ESCALATE = "escalate"  # sat in the uncertain band

#: Turn-kind records have no router scope to agree or disagree with, so they carry the
#: head's own branch instead: this, ABSTAIN, or ESCALATE.
VERDICT_ACT = "act"           # would have routed to >=1 scope, unprompted

#: Record kinds. See the module docstring — ``TURN`` is the population that was missing.
KIND_TURN = "turn"
KIND_ROUTE = "route"


# 8 MiB of JSONL is on the order of 40k observations — far more than the
# evaluation needs, and small enough that two generations stay unremarkable.
_DEFAULT_MAX_LOG_BYTES = 8 * 1024 * 1024


def enabled() -> bool:
    """Env first, in BOTH directions; the file only when the env says nothing.

    The asymmetry this replaced was reachable in one place and it was the worst one: a
    subprocess harness (release smoke, the upgrade matrix, a demo script) inherits the
    operator's home directory, so ``FLAG_FILE`` is armed for it whether or not anyone
    meant the harness to observe, and the environment is the only channel it has to say
    otherwise. Under the old check ``TOPOS_SCOPE_SHADOW=0`` fell through to the file and
    the file won, which is how synthetic traffic ends up interleaved in an operator's
    real ``~/.topos/scope_shadow.jsonl``. Tests are covered by a fixture; a fixture does
    not cross a process boundary.
    """
    raw = os.environ.get(ENV_FLAG, "").strip().lower()
    if raw in _TRUTHY:
        return True
    if raw in _FALSY:
        return False
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
    """Where observations land; ``TOPOS_SCOPE_SHADOW_LOG`` redirects them.

    Same reason as the falsy env value in ``enabled()``, one notch finer: that one is
    for a harness that wants no observation at all, this one is for a harness that wants
    observation and wants it somewhere disposable, rather than appended to the log a
    real person's query history lives in. Spelled like ``TOPOS_LOG_FILE`` in
    ``core/logging.py`` — stripped, ``~`` expanded, blank means unset.
    """
    raw = (os.environ.get(ENV_LOG_PATH) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".topos" / "scope_shadow.jsonl"


@dataclass(frozen=True)
class ShadowRecord:
    """One prediction, either standalone (``kind="turn"``) or beside a routed scope."""

    verdict: str
    #: The scope the **incumbent router** used — emphatically not a label. Empty for
    #: ``kind="turn"`` records, where nothing has decided a scope yet.
    router_scope: str
    predicted: Tuple[str, ...]
    confidence: float
    latency_ms: float
    text: str = ""
    ts: float = 0.0
    #: Which ladder branch fired (REBUILD §B0a): "" | "ambiguity" | "ignorance" |
    #: "confident-none". Distinguishes "decided nothing" from "no idea" in the log —
    #: the whole point of the four-branch ladder, so the shadow must not flatten it.
    reason: str = ""
    kind: str = KIND_ROUTE

    def as_local_row(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "kind": self.kind,
            "verdict": self.verdict,
            "router_scope": self.router_scope,
            "predicted": list(self.predicted),
            "confidence": round(self.confidence, 4),
            "latency_ms": round(self.latency_ms, 2),
            "reason": self.reason,
            "text": self.text,
        }

    def as_telemetry(self) -> Dict[str, Any]:
        """Counts and scope ids only. Adding a field here is a privacy decision."""
        return {
            "kind": self.kind,
            "verdict": self.verdict,
            "router_scope": self.router_scope,
            "predicted": list(self.predicted),
            "n_predicted": len(self.predicted),
        }


def row_router_scope(row: Dict[str, Any]) -> str:
    """Read the router scope from a log row, old field name included.

    Rows written before 2026-08-17 spell it ``true_scope``. They are the same data under a
    name that overclaimed, so they stay readable rather than being migrated or dropped.
    """
    value = row.get("router_scope")
    if value is None:
        value = row.get("true_scope")
    return str(value or "")


def compare(predicted: Sequence[str], router_scope: str, *, escalated: bool) -> str:
    """Agreement with the router. For ``kind="turn"`` pass ``router_scope=""``."""
    if escalated:
        return VERDICT_ESCALATE
    pred = set(predicted)
    if not pred:
        return VERDICT_ABSTAIN
    if not router_scope:
        return VERDICT_ACT
    if pred == {router_scope}:
        return VERDICT_HIT
    if router_scope in pred:
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


def observe_turn(
    query_text: str,
    *,
    log: Optional[ShadowLog] = None,
    classify_fn: Optional[Callable[..., Any]] = None,
    force: bool = False,
) -> Optional[ShadowRecord]:
    """Observe the user's text as it arrives, before anything has decided a scope.

    This is the only hook that sees turns which go on to query **nothing** — the
    population the route-time hook structurally cannot see, and where the failures we
    care about live (module docstring). No comparison is made because there is nothing
    yet to compare to; the verdict is the head's own branch.
    """
    return observe(query_text, "", log=log, classify_fn=classify_fn, force=force,
                   kind=KIND_TURN)


def observe(
    query_text: str,
    router_scope: str,
    *,
    log: Optional[ShadowLog] = None,
    classify_fn: Optional[Callable[..., Any]] = None,
    force: bool = False,
    kind: str = KIND_ROUTE,
) -> Optional[ShadowRecord]:
    """Predict, compare against the scope the router used, log. Never raises.

    ``router_scope`` is the incumbent's choice, not ground truth — see the module
    docstring before treating any of this as labels.

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
                verdict_obj.labels, router_scope, escalated=verdict_obj.escalated
            ),
            router_scope=router_scope,
            predicted=tuple(verdict_obj.labels),
            confidence=float(verdict_obj.confidence),
            latency_ms=elapsed,
            text=query_text,
            ts=time.time(),
            reason=str(getattr(verdict_obj, "reason", "") or ""),
            kind=kind,
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

    def agreement_rate(self) -> float:
        """Share of routed observations that matched the router exactly.

        Named for what it measures. It was ``accuracy()``, which invited reading router
        agreement as correctness — the misreading this module now exists to prevent.
        """
        return self.by_verdict.get(VERDICT_HIT, 0) / self.total if self.total else float("nan")

    def as_telemetry(self) -> Dict[str, Any]:
        """The aggregate §6.5g wants: which pairs the classifier cannot separate, as counts."""
        return {
            "total": self.total,
            "by_verdict": dict(self.by_verdict),
            "agreement_rate": round(self.agreement_rate(), 4) if self.total else None,
            "divergence": dict(
                sorted(self.confusion.items(), key=lambda kv: -kv[1])[:15]
            ),
        }


def summarize(rows: Sequence[Dict[str, Any]]) -> ShadowReport:
    """Roll the node-local log into the text-free shape that may leave the node.

    Route-kind rows only: turn-kind rows have no router scope to agree with, and folding
    them in would silently deflate every rate here. They get ``turn_coverage()``.
    """
    report = ShadowReport()
    for row in rows:
        if str(row.get("kind") or KIND_ROUTE) != KIND_ROUTE:
            continue
        report.total += 1
        verdict = str(row.get("verdict") or "?")
        report.by_verdict[verdict] = report.by_verdict.get(verdict, 0) + 1
        router_scope = row_router_scope(row) or "?"
        bucket = report.by_scope.setdefault(router_scope, {})
        bucket[verdict] = bucket.get(verdict, 0) + 1
        if verdict == VERDICT_MISS:
            for predicted in row.get("predicted") or []:
                pair = f"{router_scope} -> {predicted}"
                report.confusion[pair] = report.confusion.get(pair, 0) + 1
    return report


def _routed_turns(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse per-scope ``route`` rows back into the turns that produced them.

    ``observe()`` is called once per scope the router queried, so a two-scope turn
    writes two rows and each is compared against ONE ``router_scope``. That is right
    for :func:`summarize`, which asks "did the head agree about *this scope*", and
    wrong for "would the head have routed *this turn*": a head that correctly named
    ``{availability, schedule}`` scores one ``hit`` and one ``over`` there, and a head
    that named neither scores two ``abstain``. Routing is a decision about a SET, so
    the set has to be reassembled before it can be judged as one.

    Grouped by ``text`` because no turn id crosses the control-plane boundary — the
    same coarseness, and the same caveat, as :func:`turn_coverage`.
    """
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        if str(row.get("kind") or KIND_ROUTE) != KIND_ROUTE:
            continue
        text = str(row.get("text") or "")
        entry = grouped.get(text)
        if entry is None:
            entry = {
                "router": set(),
                # The head saw the same text for every row of the turn, so its
                # prediction is identical across them; the first row carries it.
                "predicted": {str(x) for x in (row.get("predicted") or [])},
                "escalated": str(row.get("verdict") or "") == VERDICT_ESCALATE,
                "reason": str(row.get("reason") or ""),
            }
            grouped[text] = entry
        scope = row_router_scope(row)
        if scope:
            entry["router"].add(scope)
    return list(grouped.values())


#: Verdicts for a whole-turn routing comparison. Deliberately NOT the per-row
#: ``hit``/``miss`` vocabulary — these are set relations over a different population,
#: and sharing the words would invite averaging the two together.
ROUTE_EXACT = "exact"          # head named exactly the rules' scope set
ROUTE_SUBSET = "subset"        # head named some of them and nothing else (under-routes)
ROUTE_SUPERSET = "superset"    # head named all of them and more (over-routes)
ROUTE_OVERLAP = "overlap"      # head named some of them plus others
ROUTE_DISJOINT = "disjoint"    # head acted and shares NOTHING with the rules
ROUTE_DEAD = "dead"            # head named nothing where the rules routed
ROUTE_ESCALATE = "escalate"    # head handed off; something else decides


def _rate(numerator: int, denominator: int) -> Optional[float]:
    """A rate, or ``None`` when it is unmeasurable. Never ``0.0`` for "no data".

    ``0.0`` and "never observed" print identically and read identically, and a gate
    that cannot tell them apart passes on an empty log — which is exactly the state
    a promotion gate is most likely to be evaluated in.
    """
    return round(numerator / denominator, 4) if denominator else None


def routing_comparison(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Turn-level "would the head have routed this the way the rules did?".

    Reported in the **model card's own vocabulary**: ``exact``, ``dead_rate``,
    ``disjoint_rate`` and the per-scope recall behind ``scopes_below_floor`` mean here
    exactly what ``scripts/train_scope_head.py:evaluate`` means by them. That is the
    point — the card's held-out numbers and these real-traffic numbers can then be read
    side by side, and the synthetic-to-real gap becomes visible instead of assumed.

    **These are agreement rates, not accuracy.** ``router_scope`` is the incumbent
    keyword router's choice and the incumbent is a heuristic (module docstring); a
    ``disjoint`` turn may well be the head being right, and an ``exact`` turn may be
    both being wrong together. What the numbers do support is the *promotion*
    question, which is directional: a head that goes ``dead`` on a turn the rules
    routed removes data from an answer, and that is a regression whether or not the
    rules were ideal.

    Text-free by construction — counts and scope ids only, the same rule
    ``as_telemetry`` follows. Safe to paste into a note or a model card.
    """
    turns = _routed_turns(rows)
    by_verdict: Dict[str, int] = {}
    per_scope: Dict[str, List[int]] = {}
    divergence: Dict[str, int] = {}
    acted = 0
    disjoint = 0

    for turn in turns:
        router = set(turn["router"])
        predicted = set(turn["predicted"])
        if turn["escalated"]:
            verdict = ROUTE_ESCALATE
        elif not predicted:
            verdict = ROUTE_DEAD
        elif predicted == router:
            verdict = ROUTE_EXACT
        elif predicted < router:
            verdict = ROUTE_SUBSET
        elif predicted > router:
            verdict = ROUTE_SUPERSET
        elif predicted & router:
            verdict = ROUTE_OVERLAP
        else:
            verdict = ROUTE_DISJOINT
        by_verdict[verdict] = by_verdict.get(verdict, 0) + 1

        if predicted and not turn["escalated"]:
            acted += 1
            if router and not (predicted & router):
                disjoint += 1
                for scope in sorted(predicted):
                    for source in sorted(router):
                        pair = f"{source} -> {scope}"
                        divergence[pair] = divergence.get(pair, 0) + 1
        for scope in router:
            bucket = per_scope.setdefault(scope, [0, 0])
            bucket[1] += 1
            bucket[0] += int(scope in predicted)

    total = len(turns)
    return {
        "turns": total,
        "by_verdict": dict(sorted(by_verdict.items())),
        # Named `exact` to match the card. `ShadowReport.agreement_rate` is the
        # per-ROW twin over a different population; they are not interchangeable.
        "exact": _rate(by_verdict.get(ROUTE_EXACT, 0), total),
        "dead_rate": _rate(by_verdict.get(ROUTE_DEAD, 0), total),
        "escalation_rate": _rate(by_verdict.get(ROUTE_ESCALATE, 0), total),
        "disjoint_rate": _rate(disjoint, acted),
        "per_scope_recall": {
            scope: round(hit / seen, 4)
            for scope, (hit, seen) in sorted(per_scope.items())
            if seen
        },
        "scope_turns": {scope: seen for scope, (_hit, seen) in sorted(per_scope.items())},
        "divergence": dict(sorted(divergence.items(), key=lambda kv: -kv[1])[:15]),
        "multi_scope_turns": sum(1 for t in turns if len(t["router"]) > 1),
        "multi_scope_predicted": sum(1 for t in turns if len(t["predicted"]) > 1),
    }


def turn_coverage(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """How many observed turns retrieved **no data at all**, and what the head thought.

    The metric this module was re-wired to produce. Turns are joined to routes on text
    because no turn id crosses the control-plane boundary; that is coarse — two identical
    questions in a session merge — and still enough to size the population.

    ``would_have_routed`` is the interesting cell: turns where the router queried nothing
    and the head would have named a scope. Those are candidate recoveries, not proven
    ones — the head could be wrong too, which is what adjudication is for.
    """
    turns = [r for r in rows if str(r.get("kind") or "") == KIND_TURN]
    routed_texts = {
        str(r.get("text") or "")
        for r in rows
        if str(r.get("kind") or KIND_ROUTE) == KIND_ROUTE
    }
    no_query = [r for r in turns if str(r.get("text") or "") not in routed_texts]
    return {
        "turns_observed": len(turns),
        "turns_with_no_query": len(no_query),
        "no_query_rate": round(len(no_query) / len(turns), 4) if turns else None,
        "would_have_routed": sum(1 for r in no_query if r.get("predicted")),
        "head_also_silent": sum(1 for r in no_query if not r.get("predicted")),
        "by_reason": {
            reason: sum(1 for r in no_query if str(r.get("reason") or "") == reason)
            for reason in sorted({str(r.get("reason") or "") for r in no_query})
        },
    }
