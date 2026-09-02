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
  :func:`_member`, which slugs it and then requires the result to be a declared member
  of :data:`STAGES`, :data:`ACTIONS` or :data:`REASONS` — anything else becomes
  :data:`UNRECOGNIZED`. Content that must be kept goes in ``detail``, which never
  leaves the node: enums by construction, not by care.

  Until 2026-08-19 that last clause was a promise this module did not keep. ``_slug``
  is a character filter — it sanitizes, it does not constrain membership — so
  ``record("scope_routing", "dropped", "Sarah Chen divorce lawyer meeting")`` arrived
  in ``as_public`` as ``sarah_chen_divorce_lawyer_meeting``: a name and a legal matter,
  fully legible, off the node. Every call site in the tree passed a constant, so there
  was no leak; the guarantee rested entirely on every *future* call site, which is
  exactly what this paragraph claimed it did not.

**The vocabulary is shared, and this module owns it.** Three codebases append to one
ledger that merges on the response, so their sets have to agree or the merged ledger is
not comparable. ``topos/protocol/narrowing_vocabulary.json`` publishes these sets for
the control plane and the front end, which cannot import Python from here;
``tests/query/test_narrowing_vocabulary.py`` fails when the file and this module drift.
A member is added here first, and only then emitted anywhere.
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
STAGE_COMPOSE = "compose"                # (CP/FE) building the request; the query-text cap
STAGE_RECOVERY = "recovery"              # (CP/FE) a retry after a thin or limited answer
STAGE_PART_ROUTING = "part_routing"      # (FE) which section of a multi-part ask got covered

#: Every stage name the merged ledger may carry. A stage outside this set is not a
#: stage — :func:`_member` turns it into :data:`UNRECOGNIZED` rather than letting it
#: through, because "whatever the caller passed" is precisely how the owner's words got
#: into a public field.
STAGES = frozenset(
    {
        STAGE_SOURCE_PIN,
        STAGE_SCOPE_ROUTING,
        STAGE_BOOTSTRAP,
        STAGE_TRANSPORT,
        STAGE_GRANT,
        STAGE_PLANNER,
        STAGE_RETRIEVAL,
        STAGE_RARE_GATE,
        STAGE_DISCLOSURE,
        STAGE_COMPOSE,
        STAGE_RECOVERY,
        STAGE_PART_ROUTING,
        # The sentinel is a member of every set, so coercion is idempotent: an entry
        # merged in from another codebase that already collapsed stays collapsed rather
        # than being re-flagged on each hop.
        "unrecognized",
    }
)

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

# --- the sentinel -------------------------------------------------------------------
#: What a non-member becomes. Dropping the entry instead would hide the fact that a
#: stage narrowed at all, which is the opacity the ledger exists to end; raising would
#: let telemetry cost a turn. So the entry survives and says, truthfully, that its own
#: vocabulary is unknown here — a diagnostic that points at the call site rather than at
#: the data.
UNRECOGNIZED = "unrecognized"

# --- actions ------------------------------------------------------------------------
#: What a stage did to the search, enumerated from the call sites that exist. This is a
#: description of the tree, not a design: a new verb is added here in the same commit as
#: the call site that emits it, and until it is, that call site records
#: :data:`UNRECOGNIZED` and the drift test in the emitting repo goes red.
ACTIONS = frozenset(
    {
        # engine — retrieval.py, disclosure.py, exclusion.py, pipeline.py
        "capped",
        "contributed",
        "dropped_items",
        "emptied",
        "excluded",
        "not_applied",
        "rewrote",
        "scoped",
        "windowed",
        # control plane — mcp_query.py, routines_executor.py, scope_query_router.py
        "declined_routes",
        "defaulted",
        "dropped_field",
        "dropped_sources",
        "pinned",
        "replaced_candidates",
        "requeried",
        "skipped",
        "truncated",
        # front end — scopeQueryRouter.ts, scopeRoutingRules.ts, homeChatAgent.ts
        "dropped",
        "released",
        "segmented",
        "uncovered",
        UNRECOGNIZED,
    }
)

# --- reasons ------------------------------------------------------------------------
#: Why the stage did it. The longest of the three sets and the one that actually leaked:
#: ``reason`` is where a call site is most tempted to be helpful, and being helpful with
#: the owner's own words is the failure mode. Grouped by owning repo; the engine's own
#: producers are re-asserted against this set by ``tests/query/test_narrowing_vocabulary.py``
#: so a rename in ``exclusion.py`` or ``negotiation.py`` cannot silently start emitting
#: :data:`UNRECOGNIZED`.
REASONS = frozenset(
    {
        # --- engine: retrieval lanes, gates and caps
        "blackhole_policy",
        "embedded_subject_not_instruction",
        "entity_thread_is_self",
        "entity_thread_lane",
        "entity_thread_selector_not_accessible",
        "entity_thread_self_check_unavailable",
        "entity_thread_unresolved",
        "multi_window_comparison",
        "no_evidence_lane_returned_rows",
        "no_rare_token_evidenced",
        "no_row_matched_the_request",
        "prefer_time_window",
        "rare_gate_partial_veto",
        "rare_token_unevidenced",
        "scope_stores_hold_no_rows",
        "selector_suppressed",
        "skip_retrieval_requested",
        "summary_item_cap",
        "time_range_parsed",
        # --- engine: commitment tracking (Q1) — evidence retrieved PER STATED GOAL,
        # keyed to that goal's own record and entity ids. The first four are the
        # per-goal empty-cause vocabulary: "no evidence found" is a first-class
        # result here, and the whole point of the mode is that it says WHICH kind of
        # nothing rather than letting synthesis assert progress it cannot see.
        "commitment_evidence_lane",
        "commitment_evidence_not_in_answer",
        "commitment_evidence_withheld",
        "commitment_goal_undated",
        "commitment_goal_unresolved",
        "commitment_no_evidence_matched",
        "commitment_scope_no_evidence_store",
        # --- engine: the topic thread (Q7) — one ordered thread over the message
        # stores the grant names, plus its participants and its decision points.
        "topic_thread_lane",
        "topic_thread_no_decision",
        "topic_thread_no_message_rows",
        "topic_thread_not_message_scope",
        "topic_thread_participants_withheld",
        "topic_thread_single_store",
        # --- engine: entity-anchored windows (Q3) — the window derived from the
        # SUBJECT's own mention density rather than parsed out of the sentence. The
        # derived RANGE is content and rides `detail` / the packet's `time_window`;
        # only the method and the refusals are declared here. Four of the five are
        # refusals, which is the honest ratio: a sparse node cannot anchor a period,
        # and saying which kind of "cannot" is the deliverable.
        "entity_window_density_too_thin",
        "entity_window_density_uniform",
        "entity_window_derived",
        "entity_window_no_mentions",
        "entity_window_no_triage_in_window",
        "entity_window_span_too_broad",
        "entity_window_triage_lane",
        "entity_window_unresolved",
        # --- engine: supply states (`_scope_supply_state`)
        "connected_never_delivered",
        "delivered_then_emptied",
        "no_source_connected",
        # --- engine: the grantee content filter, one per scrubbed artifact list
        "grantee_scrub_facts",
        "grantee_scrub_scores",
        "grantee_scrub_semantic_hits",
        "grantee_scrub_summaries",
        # --- engine: exclusion.py's REASON_* constants
        "exclusion_aggregate_unfilterable",
        "exclusion_category",
        "exclusion_enforced",
        "exclusion_entity",
        "exclusion_tier",
        "exclusion_uncompilable",
        # --- engine: denial codes (manifest_validation, pipeline, negotiation)
        "approval_required",
        "ceiling_escalation",
        "denied",
        "empty_query",
        "intent_scope_change",
        "intent_too_broad",
        "legacy_scope",
        "missing_scope",
        "mode_above_ceiling",
        "mode_ceiling_exceeded",
        "negotiation_exhausted",
        "purpose_missing",
        "scope_mismatch",
        "session_requester_mismatch",
        "time_window_required",
        "unknown_scope",
        # --- engine: turn outcomes that ended the turn without retrieving
        "expand_boundary",
        "narrow_request",
        "requalify",
        # --- the empty causes, which double as reasons when `empty()` gets no other
        *_CAUSE_PRECEDENCE,
        # --- control plane: the payload allow-list, routing and recovery
        "bootstrap_failed",
        "exclusive_composite_recipe",
        "expand_boundary_downgrade",
        "first_source_ref_only",
        "max_scope_routes",
        "no_scope_rule_matched",
        "now",
        "query_text_capped",
        "retrieval_parts",
        "row_cap_reached",
        "retrieval_text",
        "single_call_bootstrap",
        "source_has_live_tools",
        "source_ref",
        "summary_enrichment",
        "world_knowledge_no_retrieval",
        # --- control plane: refusals that never reach the node. The human-readable
        # sentence stays on the response's own `deny_reason`; the ledger carries the code.
        "access_mode_above_ceiling",
        "access_mode_invalid",
        "engine_error",
        "engine_response_invalid",
        "engine_unreachable",
        "scope_id_invalid",
        "scope_not_granted",
        # --- front end: chips, routes, recovery profiles and part coverage
        "message_typography",
        "multi_domain_question",
        "resolved_self",
        "scope_not_selected",
        "shared_grant_pinned",
        "shared_resource_discovered",
        "shared_target_resolved",
        "source_chip",
        "source_chips",
        "stale_empty_cache",
        "unmatched_route",
        UNRECOGNIZED,
    }
)

_SLUG_RE = re.compile(r"[^a-z0-9_.:-]+")
_SLUG_MAX = 64


def _slug(value: Any) -> str:
    """Lower-case, closed-character token. The *first* half of ``as_public``'s guarantee.

    Anything that is not ``[a-z0-9_.:-]`` collapses to ``_`` and the result is cut at 64
    characters. On its own that is a sanitizer and nothing more — ``"Sarah Chen divorce
    lawyer meeting"`` survives it intact as ``sarah_chen_divorce_lawyer_meeting``, which
    is the whole reason :func:`_member` exists. Kept as a separate step because the
    membership check needs a normalised token to compare, and because ``empty()``'s
    cause check has always used it that way.
    """
    try:
        text = str(value or "").strip().lower()
    except Exception:  # noqa: BLE001 — a __str__ that raises must not break a turn
        return ""
    return _SLUG_RE.sub("_", text)[:_SLUG_MAX].strip("_")


def _member(value: Any, allowed: frozenset) -> str:
    """Slug, then require membership. The *second* half, and the one that closes the set.

    A value outside ``allowed`` becomes :data:`UNRECOGNIZED`. That is a coercion rather
    than a drop or a raise: the entry still testifies that a stage narrowed the search,
    ``record`` still cannot cost a turn, and the owner's sentence still cannot reach
    ``as_public`` — the three properties this module has to hold at once.
    """
    token = _slug(value)
    return token if token in allowed else UNRECOGNIZED


def _duration(value: Any) -> Optional[int]:
    """Coerce a duration to whole non-negative milliseconds, or ``None``.

    Applied in ``__post_init__`` for the same reason ``_member`` is: ``NarrowingEntry``
    is a public constructor and ``as_public`` serializes whatever the object holds. A
    float, a string or a ``timedelta``-shaped mistake must not reach the wire as a
    non-integer, and a negative reading — only ever a clock bug — clamps to zero rather
    than travelling as a nonsense measurement.
    """
    if value is None:
        return None
    try:
        out = int(value)
    except (TypeError, ValueError):
        return None
    return out if out >= 0 else 0


@dataclass
class NarrowingEntry:
    """One stage's admission that it made the search smaller.

    The three public fields are narrowed to their declared sets in ``__post_init__``
    rather than only in :meth:`NarrowingLedger.record`, because ``as_public`` serializes
    whatever this object holds and ``NarrowingEntry(...)`` is a public constructor. A
    guarantee that only holds when the entry was built the usual way is the same
    convention-not-mechanism this module was caught relying on.
    """

    stage: str
    action: str
    reason: str
    dropped: Optional[int] = None
    #: Wall clock this stage spent, in whole milliseconds. Optional by design: a stage
    #: that does not time itself omits it and its entry is byte-identical to before.
    #: An integer carries no text, so it rides ``as_public`` without widening the
    #: privacy guarantee — which is the only reason a duration is allowed here at all.
    elapsed_ms: Optional[int] = None
    #: On-node only: token counts, lane names, raw window strings. Never serialized
    #: by ``as_public``; ``as_local`` keeps it for debug logging.
    detail: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        self.stage = _member(self.stage, STAGES)
        self.action = _member(self.action, ACTIONS)
        self.reason = _member(self.reason, REASONS)
        self.elapsed_ms = _duration(self.elapsed_ms)

    def as_public(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "stage": self.stage,
            "action": self.action,
            "reason": self.reason,
        }
        if self.dropped is not None:
            out["dropped"] = self.dropped
        if self.elapsed_ms is not None:
            out["elapsed_ms"] = self.elapsed_ms
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
        elapsed_ms: Optional[int] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Append one entry. Never raises — a failed record costs a debug line.

        ``stage``, ``action`` and ``reason`` are narrowed to their declared sets here,
        at record time rather than at serialization time, so an entry that never reaches
        ``as_public`` — one read only by an on-node log — is held to the same vocabulary.
        A non-member is logged at debug with the call site's value, which is where the
        fix belongs; the value itself does not survive into the entry.
        """
        try:
            count: Optional[int]
            try:
                count = int(dropped) if dropped is not None else None
            except (TypeError, ValueError):
                count = None
            entry = NarrowingEntry(
                stage=stage,
                action=action,
                reason=reason,
                dropped=count,
                elapsed_ms=elapsed_ms,
                detail=dict(detail) if isinstance(detail, dict) else None,
            )
            if UNRECOGNIZED in (entry.stage, entry.action, entry.reason):
                logger.debug(
                    "narrowing ledger: unrecognized vocabulary (stage=%r action=%r reason=%r)",
                    stage,
                    action,
                    reason,
                )
            self.entries.append(entry)
        except Exception as exc:  # noqa: BLE001
            logger.debug("narrowing ledger record skipped: %s", exc)

    def empty(
        self,
        cause: str,
        *,
        stage: Optional[str] = None,
        reason: Optional[str] = None,
        dropped: Optional[int] = None,
        elapsed_ms: Optional[int] = None,
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
                    elapsed_ms=elapsed_ms,
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
                    elapsed_ms=raw.get("elapsed_ms"),
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
        """Counts and enums only — adding a field here is a privacy decision.

        ``elapsed_ms`` sums per stage rather than listing per entry: the question it
        answers is "where did the turn go", and a per-entry list would grow with the
        number of narrowings rather than with the fixed stage vocabulary.

        The key is OMITTED when no stage timed itself, which is today's normal case.
        That is not tidiness — an unconditional ``"elapsed_ms": {}`` would change the
        telemetry record for every existing caller and consumer on the day this shipped,
        for a map that says nothing. Absent means unmeasured; present means measured.
        """
        stages: Dict[str, int] = {}
        elapsed: Dict[str, int] = {}
        for entry in self.entries:
            stages[entry.stage] = stages.get(entry.stage, 0) + 1
            if entry.elapsed_ms is not None:
                elapsed[entry.stage] = elapsed.get(entry.stage, 0) + entry.elapsed_ms
        out: Dict[str, Any] = {
            "n_entries": len(self.entries),
            "stages": stages,
            "empty_cause": self._cause,
        }
        if elapsed:
            out["elapsed_ms"] = elapsed
        return out


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
