"""Shared hard boundary and two-stage orchestration. Returns metadata only."""
from __future__ import annotations

from collections import OrderedDict
from typing import Callable

from ..canonical import MAX_INTEGER, PolicyError, canonical_bytes, digest
from .evaluators import LocalModelTransport, RulesEvaluator, SemanticEvaluator, withheld
from .models import Decision, EvaluatorConfig, ExperimentPolicy, ExperimentResult, FORM, Judgment, RequestContext, TrustedSnapshot


class DecisionCache:
    """Bounded process-local cache of metadata; no candidate bodies or prompts."""
    def __init__(self, max_entries: int = 128):
        if type(max_entries) is not int or not 1 <= max_entries <= 4096:
            raise ValueError("cache budget")
        self.max_entries = max_entries
        self._entries: OrderedDict[str, bytes] = OrderedDict()

    def get(self, key: str):
        value = self._entries.get(key)
        if value is not None:
            self._entries.move_to_end(key)
            return Decision.parse(value)
        return None

    def put(self, key: str, decision: Decision):
        self._entries[key] = canonical_bytes(decision.model_dump())
        self._entries.move_to_end(key)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)


class ExperimentHarness:
    """Arm assignment/policy/provider are trusted constructor inputs.

    There is no recipient arm selector. A future adapter must authenticate the
    assignment and obtain/refetch qualification itself. A serialized snapshot
    or an owner_approved_revision hash alone is not evidence of that authority.
    """
    def __init__(self, policy: ExperimentPolicy, config: EvaluatorConfig, *,
                 provider: Callable[[], TrustedSnapshot], clock: Callable[[], int],
                 transport: LocalModelTransport | None = None, cache: DecisionCache | None = None):
        self._policy = canonical_bytes(ExperimentPolicy.parse(policy.model_dump()).model_dump())
        self._config = canonical_bytes(EvaluatorConfig.parse(config.model_dump()).model_dump())
        self.provider, self.clock = provider, clock
        self.evaluator = RulesEvaluator() if config.arm == "rules_v2" else SemanticEvaluator(transport)
        self.cache = cache if cache is not None else DecisionCache()

    def _now(self):
        now = self.clock()
        if type(now) is not int or not 0 <= now <= MAX_INTEGER:
            raise PolicyError("clock_invalid")
        return now

    def _snapshot(self):
        try:
            value = self.provider()
            if not isinstance(value, TrustedSnapshot):
                raise PolicyError("snapshot_invalid")
            return TrustedSnapshot.parse(value.model_dump())
        except Exception:
            raise PolicyError("snapshot_unavailable") from None

    def _fence(self, policy, request, original, now):
        current = self._snapshot()
        if current.current_authority != policy.authority or request.authority != policy.authority or digest(current.model_dump()) != digest(original.model_dump()):
            return "stale_authority"
        candidate = current.candidate
        if candidate.protection_revision != policy.authority.protection_revision:
            return "stale_authority"
        if not policy.validity.starts_at <= now < policy.validity.expires_at:
            return "policy_time"
        if request.processor != policy.processor:
            return "processor_boundary"
        # Content is never sent to either evaluator until all these pass.
        if candidate.output.owner_only or any(unit.owner_only for unit in candidate.evidence):
            return "owner_only"
        if candidate.lineage_state != "qualified":
            return "unknown_lineage"
        if candidate.form != FORM:
            return "unsupported_form"
        if any(unit.source_id is None or unit.source_id not in policy.source_universe for unit in candidate.evidence):
            return "source_outside_boundary"
        return None

    @staticmethod
    def _decision(policy, config, request, snapshot, stage, judgment: Judgment, reason):
        if reason in {"owner_only", "source_outside_boundary", "no_structural_match"}:
            judgment = Judgment.parse({**judgment.model_dump(), "verdict": "deny"})
        return Decision(stage=stage, verdict=judgment.verdict, reason_code=reason,
            policy_hash=digest(policy.model_dump()), candidate_revision=digest(snapshot.candidate.model_dump()),
            evaluator_config_hash=digest(config.model_dump()), context_hash=digest(request.model_dump()),
            matched_allow_clause_ids=judgment.matched_allow_clause_ids,
            matched_deny_clause_ids=judgment.matched_deny_clause_ids,
            required_projection_id=judgment.required_projection_id,
            missing_context_codes=judgment.missing_context_codes)

    async def run(self, request: RequestContext) -> ExperimentResult:
        policy, config = ExperimentPolicy.parse(self._policy), EvaluatorConfig.parse(self._config)
        request = RequestContext.parse(request.model_dump())
        original = self._snapshot()
        decisions = []
        # Structural selections survive evaluator swapping. One complete rule
        # must cover all contributing sources and the exact proposed form;
        # neither a model-selected ID nor a union of partial rules can widen it.
        eligible = [rule.clause_id for rule in policy.rules if rule.effect == "permit"
                    and original.candidate.form in rule.forms
                    and all(unit.source_id in rule.sources for unit in original.candidate.evidence)]
        for stage in ("evidence_use", "output_release"):
            now = self._now()
            try:
                reason = self._fence(policy, request, original, now)
            except (PolicyError, OSError):
                reason = "stale_authority"
            if reason is None and not eligible:
                reason = "no_structural_match"
            if reason:
                judgment, reason = withheld(reason)
                decisions.append(self._decision(policy, config, request, original, stage, judgment, reason))
                break
            key = digest({"version": "experiment-decision-cache/v1", "policy": policy.model_dump(),
                "config": config.model_dump(), "request": request.model_dump(), "snapshot": original.model_dump(),
                "stage": stage, "eligible": eligible, "evaluation_time": now})
            decision = self.cache.get(key)
            if decision is None:
                judgment, reason = await self.evaluator.evaluate(policy, original.candidate, stage, eligible, config)
                decision = self._decision(policy, config, request, original, stage, judgment, reason)
            # Check even a cached result. Revocation/protection/expiry may have
            # changed while a local classifier was running.
            try:
                reason = self._fence(policy, request, original, self._now())
            except (PolicyError, OSError):
                reason = "stale_authority"
            if reason:
                judgment, reason = withheld(reason)
                decision = self._decision(policy, config, request, original, stage, judgment, reason)
            elif decision.reason_code in {"rule_permit", "rule_deny", "semantic_permit", "semantic_deny", "unknown_context"}:
                self.cache.put(key, decision)
            decisions.append(decision)
            if decision.verdict != "permit":
                break
            eligible = decision.matched_allow_clause_ids
        result = decisions[-1].verdict
        return ExperimentResult(experiment_id=policy.experiment_id, arm=config.arm,
            evidence=decisions[0], output=decisions[1] if len(decisions) == 2 else None,
            verdict=result, execution_enabled=False)
