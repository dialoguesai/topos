"""§F.5 — three-arm negotiation A/B harness.

Measures the payoff of the firewall's negotiation + minimizer against an open API, on the
same corpus and tasks:

  Arm A — open baseline: direct/owner-level access (what a normal API integration exposes).
  Arm B — firewall, no negotiation: grantee pipeline, disclosure-filtered, bare (broad intent
          proceeds and returns everything in scope, redacted).
  Arm C — firewall + negotiation + minimizer: a broad intent is met with a counter-offer; a
          simulated requester agent sharpens its question, then the minimizer keeps only what
          the intent needs.

The requester agent is *simulated* (deterministic) rather than a live LLM so the numbers are
reproducible in CI: it starts broad and, on a narrow_request, adopts the task's specific
refinement — modelling exactly the "AI sharpens its question" behaviour the protocol is meant
to induce.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List

from topos.query.negotiation import DEFAULT_MAX_ROUNDS, qualify_intent
from topos.query.manifest import ScopeResolutionManifest
from topos.query.pipeline import QueryPipelineOrchestrator
from topos.storage.adapters.factory import AdapterBundle
from topos.storage.adapters.fakes import (
    InMemoryAuditLogStore,
    InMemoryCanonicalStore,
    InMemoryGraphEdgeStore,
    InMemoryQuerySessionStore,
    InMemorySignalFeatureStore,
    InMemoryVectorIndex,
)

from tests.evals.privacy.common.disclosure_facts import disclosure_profile, extract_disclosed_facts

SCOPE = "messages:read"
_SENSITIVE_EMAIL = "bob-canary@example-priv.net"


@dataclass
class Task:
    task_id: str
    broad_intent: str
    refined_intent: str
    necessary_token: str
    sensitive_markers: List[str]


@dataclass
class ArmResult:
    arm: str
    task_id: str
    total_facts: int
    sensitive_facts: int
    task_success: bool
    rounds: int
    intents: List[str] = field(default_factory=list)
    turn_outcome: str = ""
    facts: List[str] = field(default_factory=list)


DEFAULT_TASK = Task(
    task_id="prep_launch_meeting",
    broad_intent="give me everything you have",
    refined_intent="launch logistics meeting with Alex on Tuesday",
    necessary_token="logistics",
    sensitive_markers=[_SENSITIVE_EMAIL, "bob-canary"],
)


def build_ab_corpus() -> AdapterBundle:
    """Rows share the phrase 'atlas' so a broad query returns them all; the refined query
    ('launch logistics … Alex … Tuesday') retrieves only the necessary row."""
    canonical = InMemoryCanonicalStore()
    rows = [
        ("nec", f"atlas launch logistics meeting with Alex on Tuesday 2pm",
         f"atlas launch logistics meeting with Alex on Tuesday 2pm"),
        ("sens", f"atlas contact: reach Bob at {_SENSITIVE_EMAIL} about billing",
         "atlas contact: reach Bob at [REDACTED_EMAIL] about billing"),
        ("irr1", "atlas sourdough recipe notes for the weekend", "atlas sourdough recipe notes for the weekend"),
        ("irr2", "atlas gym schedule reminder", "atlas gym schedule reminder"),
    ]
    for rid, content, disclosure in rows:
        canonical.upsert("conversation_messages", {"record_id": rid, "content": content, "content_disclosure": disclosure})
    return AdapterBundle(
        canonical=canonical,
        signal=InMemorySignalFeatureStore(),
        vector=InMemoryVectorIndex(),
        graph=InMemoryGraphEdgeStore(),
        audit=InMemoryAuditLogStore(),
        query_session=InMemoryQuerySessionStore(),
        backend="memory",
    )


def _manifest() -> ScopeResolutionManifest:
    return ScopeResolutionManifest(
        scope_id=SCOPE, primary_dimensions=["Relationships"],
        canonical_tables=["conversation_messages"], access_mode_ceiling="raw",
    )


@contextlib.contextmanager
def _env(**kw: str):
    old = {k: os.environ.get(k) for k in kw}
    try:
        for k, v in kw.items():
            os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _execute(orch, *, intent, session_id, grantee, mode="raw"):
    kwargs = dict(
        query_text=intent, scope_id=SCOPE, access_mode=mode, manifest=_manifest(),
        query_session_id=session_id,
    )
    if grantee:
        kwargs.update(requester_id="grantee-x", owner_id="owner-9", is_grantee_request=True)
    else:
        kwargs.update(requester_id="owner", owner_id="owner", is_grantee_request=False)
    return asyncio.run(orch.execute(**kwargs))


def _profile_and_success(resp, task: Task) -> tuple[int, int, bool, List[str]]:
    pr = resp.get("public_result")
    prof = disclosure_profile(pr, sensitive_markers=task.sensitive_markers)
    facts = extract_disclosed_facts(pr)
    success = any(task.necessary_token.lower() in f.lower() for f in facts)
    return prof["total_facts"], prof["sensitive_facts"], success, facts


def run_arm_open(task: Task = DEFAULT_TASK) -> ArmResult:
    """Arm A: open/owner-level access — broad intent returns everything in scope, raw."""
    orch = QueryPipelineOrchestrator(adapters=build_ab_corpus())
    with _env(TOPOS_NEGOTIATION="0", TOPOS_DISCLOSURE_MINIMIZER="0"):
        resp = _execute(orch, intent=task.broad_intent, session_id=f"A-{uuid.uuid4().hex[:8]}", grantee=False)
    total, sensitive, success, facts = _profile_and_success(resp, task)
    return ArmResult("open", task.task_id, total, sensitive, success, rounds=1,
                     intents=[task.broad_intent], turn_outcome=resp.get("turn_outcome", ""), facts=facts)


def run_arm_firewall(task: Task = DEFAULT_TASK) -> ArmResult:
    """Arm B: firewall, no negotiation — grantee, broad intent proceeds, disclosure-filtered."""
    orch = QueryPipelineOrchestrator(adapters=build_ab_corpus())
    with _env(TOPOS_NEGOTIATION="0", TOPOS_DISCLOSURE_MINIMIZER="0"):
        resp = _execute(orch, intent=task.broad_intent, session_id=f"B-{uuid.uuid4().hex[:8]}", grantee=True)
    total, sensitive, success, facts = _profile_and_success(resp, task)
    return ArmResult("firewall", task.task_id, total, sensitive, success, rounds=1,
                     intents=[task.broad_intent], turn_outcome=resp.get("turn_outcome", ""), facts=facts)


def run_arm_negotiated(task: Task = DEFAULT_TASK) -> ArmResult:
    """Arm C: firewall + negotiation + minimizer — simulated requester sharpens on counter-offer."""
    orch = QueryPipelineOrchestrator(adapters=build_ab_corpus())
    session_id = f"C-{uuid.uuid4().hex[:8]}"
    intents: List[str] = []
    intent = task.broad_intent
    resp: Dict[str, Any] = {}
    with _env(TOPOS_NEGOTIATION="1", TOPOS_DISCLOSURE_MINIMIZER="1"):
        for _ in range(DEFAULT_MAX_ROUNDS + 2):
            intents.append(intent)
            resp = _execute(orch, intent=intent, session_id=session_id, grantee=True)
            if resp.get("turn_outcome") == "narrow_request":
                intent = task.refined_intent  # the agent adopts a specific, bounded question
                continue
            break
    total, sensitive, success, facts = _profile_and_success(resp, task)
    return ArmResult("negotiated", task.task_id, total, sensitive, success, rounds=len(intents),
                     intents=intents, turn_outcome=resp.get("turn_outcome", ""), facts=facts)


def specificity_score(intent: str, *, grant_ceiling: str = "raw") -> int:
    """0–4 rubric: 4 = fully specific/proportional; each unmet requirement subtracts one."""
    out = qualify_intent(scope_id=SCOPE, access_mode="raw", query_text=intent, grant_ceiling=grant_ceiling)
    if out.ok:
        return 4
    return max(0, 4 - len(out.offer.requires))


def run_ab(task: Task = DEFAULT_TASK) -> Dict[str, ArmResult]:
    return {
        "open": run_arm_open(task),
        "firewall": run_arm_firewall(task),
        "negotiated": run_arm_negotiated(task),
    }


def build_scorecard(task: Task = DEFAULT_TASK) -> Dict[str, Any]:
    """Slide-ready A/B numbers: task success per arm, facts disclosed per arm, the headline
    facts-reduction ratio (open ÷ negotiated), sensitive facts per arm, and the intent-
    specificity delta (broad → accepted)."""
    results = run_ab(task)
    a, b, c = results["open"], results["firewall"], results["negotiated"]
    facts_ratio = (a.total_facts / c.total_facts) if c.total_facts else float("inf")
    return {
        "task_id": task.task_id,
        "task_success": {"open": a.task_success, "firewall": b.task_success, "negotiated": c.task_success},
        "facts_disclosed": {"open": a.total_facts, "firewall": b.total_facts, "negotiated": c.total_facts},
        "sensitive_facts": {"open": a.sensitive_facts, "firewall": b.sensitive_facts, "negotiated": c.sensitive_facts},
        "facts_reduction_ratio_open_over_negotiated": round(facts_ratio, 2),
        "rounds_to_resolution": {"negotiated": c.rounds},
        "specificity_delta": specificity_score(task.refined_intent) - specificity_score(task.broad_intent),
        "arm_results": {k: vars(v) for k, v in results.items()},
    }
