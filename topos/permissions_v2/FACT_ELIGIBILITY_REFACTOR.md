# Pure P2b structural preparation, v1

This refactor separates consistency and structural selection from the existing
hard-rule membership calculation. It changes no policy schema, registered
capability, endpoint, runtime flag, resolver, review store or final-send gate.
It contains no model call or experiment adapter.

`prepare_fact_eligibility` reparses the supplied inputs and checks binding,
complete correlated row/lineage revisions, the exact reviewed projection and
its sensitivity floor. It retains request/policy time, artifact validity,
source/table/processor/form selections, the unsupported Inference permit state,
and each clause's native-event window. Returned clause contexts are frozen
tuples of identifiers and temporal masks. Parsed model inputs are fresh copies;
their nested collections are not advertised as deeply immutable.

These checks establish consistency of supplied values, **not authenticated
provenance or current authorization**. The function does not read the durable
ingest enrollment, owner-review stores, exclusion/protection state, identity
coverage, copy enrollment or ledger. `FactProjectionRelease` and its resolver,
review and ledger services still establish those boundaries and final-send
freshness. A caller can construct a consistent pure input; neither that input
nor a structural context is a bearer permit or permission to send text to a model.

Hard-rule membership remains in `fact_projection_decision`. A permit covers one
whole closure and its output. A deny artifact uses only descendants from its own
selected sources; its output predicate receives the OR of those exact temporal
masks. False and unknown masks are preserved separately. Unknown fact validity
does not short-circuit a known matching deny, whose existing reason takes
precedence. Inference permits remain unsupported; an Inference ceiling on a
deny does not neutralize it. Empty selections stay empty. First-permit ordering,
all matched deny IDs, reason codes, missing-context ordering and hashes remain
unchanged.

The differential oracle is frozen from engine
`81c1e9c65bb59dcd18650ac316df36ed9e10c145`, original `fact_policy.py` SHA-256
`bee3dc870812a6e0ab6a05cea49488099973576169063a98eb1354c8d4e16fe1`.
Only its imports are rewritten for test loading. It is never a runtime fallback.
Tests compare complete canonical decision bytes and error types/codes across
independent source/table/window selections, a three-level graph, temporal
unknowns, exclusion masking, clause permutations and invalid/stale bundles.

The offline A/B bridge (`experiments/fact_bridge.py`) now consumes these
contexts: it captures its input from the real review services under their
gates, requires an owner-approved processor/policy capsule, bounds the inspected
surfaces, rejects mandatory unknowns before any model call and requalifies the
capture afterwards. The pure contexts remain consistency results, not that gate.
See [COPIED_POSITIVE_PLAN.md](COPIED_POSITIVE_PLAN.md#direct-prose-shares-this-boundary-not-raw-export-authority).
