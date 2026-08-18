"""The narrowing ledger — every stage that reduces the search says so.

A request travels through eight stages across three codebases, and six of them can
shrink it: a `/source` chip pins one connector, keyword routing picks 1–4 scopes out
of 14, the bootstrap declines, a hand-written payload allow-list drops a field, the
planner parses a window, the rare-token gate empties a lane. On 2026-08-17 ten
independent defects were found in one afternoon; every one produced a well-formed
result and none produced a warning. The single most useful diagnostic all day was
``stores_touched`` missing the string ``"signal"`` — a field that exists for
unrelated reasons.

Two things live here.

**The ledger.** Each narrowing stage appends ``{stage, action, reason, dropped}``.
Downstream — including the model that writes the final answer — can then tell
"searched one day instead of six" apart from "searched everything and found
nothing".

**The empty-cause taxonomy.** An empty lane had four indistinguishable causes: the
store is genuinely empty, the scope was never queried, the rare gate vetoed it, or
the scope was denied. The response shape was identical for all four, so the model
said "no data" — or worse, "your data may not be synced" — for every one. Two of
six sections in a real report told the owner exactly that about data that was
present and indexed. A fifth cause, ``no_match``, splits the first: rows existed,
none of them matched. Calling that ``store_empty`` is the false-absence bug in
miniature.

Three rules, each a test:

* **Additive and optional.** Every function that takes a ledger takes ``None``, and
  ``None`` must leave the path byte-identical. Nothing here changes what is
  retrieved — it only records what was already happening.
* **Never raises.** A ledger is telemetry. ``record`` swallows everything; a broken
  ledger may cost a line of debug output, never a turn.
* **Two serializers, same split as ``scope_shadow``.** ``as_public`` carries closed-set
  enums and integers and is what travels off the node. ``as_local`` keeps the
  ``detail`` payload for on-node logs. Every public string is passed through
  :func:`_slug`, so a caller that hands ``reason`` a fragment of the owner's
  question cannot leak it even by accident — enums by construction, not by care.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# --- stages -------------------------------------------------------------------------
# Named for the stage rail in the pipeline evaluation: compose → route → bootstrap →
# transport → plan → retrieve → gate → synthesize. Front-end and control-plane stages
# are declared here too, because the ledger they append to is this one — the shape has
# to be agreed across all three codebases or the merged ledger is not comparable.
STAGE_SOURCE_PIN = "source_pin"          # a /source chip pinned the whole request
STAGE_SCOPE_ROUTING = "scope_routing"    # keyword rules / composite recipes / route cap
STAGE_BOOTSTRAP = "bootstrap"            # the deterministic pre-fire declined
STAGE_TRANSPORT = "transport"            # a payload allow-list dropped a field
STAGE_GRANT = "grant"                    # scope / mode / selector denial
STAGE_PLANNER = "planner"                # the parsed time window
STAGE_RETRIEVAL = "retrieval"            # lane filters, fusion cap, soft window
STAGE_RARE_GATE = "rare_gate"            # the rare-token veto
STAGE_DISCLOSURE = "disclosure"          # tier / filter-manifest removal

# --- empty causes -------------------------------------------------------------------
#: The stores backing this scope hold nothing at all — no candidate ever existed.
#: "Connect a calendar", not "you had a quiet week".
CAUSE_STORE_EMPTY = "store_empty"
#: Candidates existed; none matched the question, the window, or the filters.
CAUSE_NO_MATCH = "no_match"
#: The rare-token gate emptied a non-empty candidate set: the ask named something
#: the corpus does not contain. Correct behaviour, invisible until now.
CAUSE_GATE_VETOED = "gate_vetoed"
#: No retrieval ran for this scope — it was never routed, the bootstrap declined,
#: or the caller asked for a skip. The commonest false "no data" in a compound ask.
CAUSE_NOT_QUERIED = "not_queried"
#: Permission stopped it: mode ceiling, classifier denial, selector suppression, or
#: a scope this client is not granted.
CAUSE_SCOPE_DENIED = "scope_denied"

#: Precedence when several stages have an opinion, most authoritative first. A denied
#: scope is denied whatever the stores hold; a gate veto outranks "nothing matched",
#: because the veto is *why* nothing matched.
_CAUSE_PRECEDENCE = (
    CAUSE_SCOPE_DENIED,
    CAUSE_NOT_QUERIED,
    CAUSE_GATE_VETOED,
    CAUSE_NO_MATCH,
    CAUSE_STORE_EMPTY,
)

EMPTY_CAUSES = frozenset(_CAUSE_PRECEDENCE)

_SLUG_RE = re.compile(r"[^a-z0-9_.:-]+")
_SLUG_MAX = 64


def _slug(value: Any) -> str:
    """Lower-case, closed-character token. The privacy guarantee of ``as_public``.

    Public ledger fields are enums. Rather than trusting every call site to pass one,
    anything that is not ``[a-z0-9_.:-]`` collapses to ``_`` and the result is cut at
    64 characters — so a caller that mistakenly passes a slice of the owner's question
    emits ``how_often_do_i_go_to_the_gym`` shaped noise at worst, and more usually
    nothing recognisable. Content that must be kept goes in ``detail``, which never
    leaves the node.
    """
    try:
        text = str(value or "").strip().lower()
    except Exception:  # noqa: BLE001 — a __str__ that raises must not break a turn
        return ""
    return _SLUG_RE.sub("_", text)[:_SLUG_MAX].strip("_")


@dataclass
class NarrowingEntry:
    """One stage's admission that it made the search smaller."""

    stage: str
    action: str
    reason: str
    dropped: Optional[int] = None
    #: On-node only: token counts, lane names, raw window strings. Never serialized
    #: by ``as_public``; ``as_local`` keeps it for debug logging.
    detail: Optional[Dict[str, Any]] = None

    def as_public(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "stage": self.stage,
            "action": self.action,
            "reason": self.reason,
        }
        if self.dropped is not None:
            out["dropped"] = self.dropped
        return out

    def as_local(self) -> Dict[str, Any]:
        out = self.as_public()
        if self.detail:
            out["detail"] = dict(self.detail)
        return out


@dataclass
class NarrowingLedger:
    """Append-only record of the narrowing done to one request.

    Mutated in place and threaded by reference, because the stages that narrow are
    spread across a call tree that already passes a dozen keyword arguments and
    returns plain lists. Threading a return value through all of them would have
    changed every signature on the path; an optional collector changes none of them
    for callers that pass nothing.
    """

    entries: List[NarrowingEntry] = field(default_factory=list)
    _cause: Optional[str] = None

    # -- writing ---------------------------------------------------------------------

    def record(
        self,
        stage: str,
        action: str,
        reason: str,
        *,
        dropped: Optional[int] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append one entry. Never raises — a failed record costs a debug line."""
        try:
            count: Optional[int]
            try:
                count = int(dropped) if dropped is not None else None
            except (TypeError, ValueError):
                count = None
            self.entries.append(
                NarrowingEntry(
                    stage=_slug(stage),
                    action=_slug(action),
                    reason=_slug(reason),
                    dropped=count,
                    detail=dict(detail) if isinstance(detail, dict) else None,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("narrowing ledger record skipped: %s", exc)

    def empty(
        self,
        cause: str,
        *,
        stage: Optional[str] = None,
        reason: Optional[str] = None,
        dropped: Optional[int] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Declare why a result came back empty, and optionally log the stage that did it.

        Later calls only win when they are *more* authoritative (see
        ``_CAUSE_PRECEDENCE``): retrieval declaring ``store_empty`` must not overwrite
        the rare gate's ``gate_vetoed``, because the gate is the answer to "why".
        """
        try:
            candidate = str(cause or "").strip().lower()
            if candidate not in EMPTY_CAUSES:
                logger.debug("narrowing ledger: unknown empty cause %r ignored", cause)
                return
            if self._cause is None or _CAUSE_PRECEDENCE.index(candidate) < _CAUSE_PRECEDENCE.index(
                self._cause
            ):
                self._cause = candidate
            if stage:
                self.record(
                    stage,
                    "emptied",
                    reason or candidate,
                    dropped=dropped,
                    detail=detail,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("narrowing ledger empty-cause skipped: %s", exc)

    def extend_public(self, entries: Iterable[Any]) -> None:
        """Absorb entries recorded by another codebase (front end, control plane).

        They arrive as plain dicts over the wire and are re-slugged on the way in, so
        an upstream that is careless with ``reason`` cannot widen this one's guarantee.
        """
        try:
            for raw in entries or []:
                if not isinstance(raw, dict):
                    continue
                self.record(
                    raw.get("stage") or "",
                    raw.get("action") or "",
                    raw.get("reason") or "",
                    dropped=raw.get("dropped"),
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("narrowing ledger merge skipped: %s", exc)

    # -- reading ---------------------------------------------------------------------

    @property
    def empty_cause(self) -> Optional[str]:
        return self._cause

    def __bool__(self) -> bool:
        return bool(self.entries) or self._cause is not None

    def __len__(self) -> int:
        return len(self.entries)

    def as_public(self) -> Dict[str, Any]:
        """Closed-set enums and integers. This is what travels off the node."""
        out: Dict[str, Any] = {"ledger": [e.as_public() for e in self.entries]}
        if self._cause is not None:
            out["empty_cause"] = self._cause
        return out

    def as_local(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"ledger": [e.as_local() for e in self.entries]}
        if self._cause is not None:
            out["empty_cause"] = self._cause
        return out

    def as_telemetry(self) -> Dict[str, Any]:
        """Counts and enums only — adding a field here is a privacy decision."""
        stages: Dict[str, int] = {}
        for entry in self.entries:
            stages[entry.stage] = stages.get(entry.stage, 0) + 1
        return {
            "n_entries": len(self.entries),
            "stages": stages,
            "empty_cause": self._cause,
        }


def result_is_empty(public_result: Optional[Dict[str, Any]]) -> bool:
    """True when a well-formed result carries no findings.

    All three access modes in one place, because "empty" is exactly the condition the
    model turns into "no data" and it must be decided identically for summaries, rows
    and scores. A denied turn has no ``public_result`` at all and is empty too.
    """
    if not isinstance(public_result, dict):
        return True
    for key in ("summaries", "rows", "scores", "items"):
        value = public_result.get(key)
        if isinstance(value, list) and value:
            return False
    # Inference answers are a verdict, not a list: a band or a yes is a finding.
    if str(public_result.get("answer") or "").strip().lower() in ("yes", "list", "band"):
        return False
    if public_result.get("availability_band"):
        return False
    return True
