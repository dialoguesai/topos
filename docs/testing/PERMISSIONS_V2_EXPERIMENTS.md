# Offline swappable evaluator experiment

`topos/permissions_v2/experiments` is an **unmounted research harness**. It does
not change signed Policy v2 grammar, advertise natural-language support, activate
grants, fetch a corpus, or return candidate content. Every result says
`execution_enabled: false`. A permit is an experimental classification result,
not permission to disclose data. This is a foundation for P3, not completion of
P3 or any of the four recipient campaign cells.

## Two arms, one boundary

An owner-side caller supplies an approved experiment capsule, a fixed arm
assignment, a trusted snapshot provider and a clock. Both arms inspect the same
prequalified evidence bundle and proposed output projection:

| Arm | Membership evaluation |
| --- | --- |
| `rules_v2` | Typed three-valued predicates over reviewed attributes |
| `semantic_v1` | Direct approved original prose, inclusions, exclusions and illustrative examples |

The shared boundary verifies the complete authority binding, policy validity,
current protection revision, owner-only state, qualified lineage, source
universe and exact experimental form before either evaluator runs. Each eligible
inclusion must retain one complete rule's source/form tuple covering **every**
contributing leaf. Empty source/form lists deny; different rules cannot each
cover part of a derivation and be stitched together. These structural limits do
not disappear when selecting the language arm.

At `evidence_use`, the arm must permit the contributing evidence together under
at least one common inclusion. At `output_release`, it evaluates the actual
proposed projection using only inclusion IDs that permitted the evidence.
Applicable exclusions dominate positive matches; an uncertain rule exclusion
withholds even if another rule permits. A language response cannot invent clause
IDs, override an exclusion with a positive example, or request an arbitrary
projection. The language prompt retains all approved exclusions conservatively;
it does not turn source-specific deterministic exceptions into silent prose
exceptions. Mixed outputs are withheld rather than automatically redacted.

An evidence permit does not imply output permission. An active arm's denial is
not replaced with a shadow permit. No semantic error falls back to rules.
Timeouts, wrong model identity, malformed decisions, missing context and
unsupported projections are measured as indeterminate results.

## Trust and the P2b handshake

The experimental capsule is a separate versioned grammar. Its review hash pins
the exact capsule bytes but does not authenticate an owner. The snapshot's
qualification and lineage fields are trusted-provider observations, not
recipient-supplied flags or an unforgeable authorization token. The current CLI
uses explicit synthetic assumptions; there is no real-corpus adapter.

A future trusted adapter must call P2b `EvidenceResolver.qualify` with the
node-owned review store, require `Qualification.verdict == qualified`, and
preserve its binding, candidate/lineage/protection revisions and review revision.
Each contributing leaf retains its source/table/dataset identity and reviewed
classification. `QualifiedEvidence` supplies no candidate text and authorizes no
output form. A separate signed P2b scalar release now performs typed projection, native/
disclosure checks and final release validation. The experiment package is not
wired into it; its copied-evidence/coverage bridge remains separate work. The harness's `experiment.synthetic_fact.v1` is not that adapter
and is not added to the capability registry.

The provider is called again around each evaluation and every cache hit. A
changed authority, epoch, protection, candidate bytes or qualification/lineage
snapshot invalidates the observation. A frozen clock is useful only for offline
fixtures; a serving adapter must use current time at each validity boundary and
recheck current CP cancellation as well as node state at actual release.

## Injected model transport

There is no default model, network client, subprocess, environment discovery or
tool executor. An operator may explicitly inject an async local transport:

```python
async def complete(request: ModelRequest) -> ModelResponse:
    ...
```

`ModelRequest` pins arm, model ID, model artifact revision, prompt revision,
stage, output-token budget and temperature zero. Its `system` field carries the
fixed instruction template plus approved policy JSON. `candidate_data` contains
only the separate untrusted inspected units. Reviewed attributes, expected
verdicts, authority and protection flags never enter the model prompt. Arm B
interprets prose directly rather than compiling it to predicates.

The trusted transport must establish that processing is local, enforce model
identity, avoid truncation and tool execution, and honor cancellation. Merely
echoing a model digest is not verification. The harness checks response identity
against its pins and requires one bounded JSON decision with exactly:

```
verdict, matched_allow_clause_ids, matched_deny_clause_ids,
required_projection_id, missing_context_codes
```

The prompt is rejected when over budget; exclusions are never truncated to fit.
The response parser rejects unknown fields, duplicate keys, invalid enums,
invented IDs and unsupported projections. There is no free-text explanation or
replacement output channel. Candidate instructions remain data in a separate
message; this separation and parser validation do **not** prove model resistance
to prompt injection or semantic classification accuracy.

## Cache and retained results

The bounded in-memory cache stores only decision metadata. Its key includes the
entire capsule, evaluator/model/prompt configuration, arm/session, full request
and authority, candidate bytes and revisions, protection and lineage revisions,
stage, eligible clause IDs, and exact evaluation time. Operational failures are
not cached. Changing a request, source selection, policy, model, session, review,
lineage, protection or time cannot reuse an earlier permit. A cached result still
passes the current boundary check.

Provider/model failures expose bounded codes and suppress candidate-bearing
exception chains. Detailed observations remain node-side; this package adds no
logging or persistence of prompts, candidate bodies or denied samples.

## Owner-shadow fact bridge over the real P2b services

`experiments/fact_bridge.py` is the closed offline bridge the copied-positive
plan asked for, grammar `topos-offline-qualified-fact-experiment/v1`. It is
still unmounted: no serving module imports the experiments package (a test
asserts this), it has no ledger issuance, no send path and no recipient arm
selector. Its inputs are the real `ProjectionReviewService` (resolver, owner
evidence-review store, owner output-review store), a verified `Binding`, a
clock and an operator-injected local transport.

The capsule pairs the signed-grammar P2b policy with its prose twin: inclusion
identifiers are exactly the policy's permit rule identifiers and exclusions its
deny rule identifiers, so both arms answer over one clause universe. The
processor pin names the exact local model, model revision, prompt revision and
byte/token budgets the owner approved. The capsule digest detects edits; it
does not authenticate the owner.

One closure is captured under the live gates through `with_reviewed`, where
`prepare_fact_eligibility` supplies the mandatory structural floor and the
inspected surfaces are read: each fact artifact as its subject/predicate/value
and each terminal message as its content, at most 16 units of 16,000
characters; over budget withholds instead of truncating. Arm A is the exact
serving evaluator over that capture. Arm B stops before any model call on a
terminal reason, unknown fact validity, an inclusion with unknown or
out-of-window leaf time, or no structurally eligible inclusion; it then runs
evidence_use over the whole closure and output_release over the exact scalar,
offering only eligible inclusions and only exclusions whose own event window is
not already false. Reviewed labels, owner-only flags, record identifiers,
revisions and authority never enter the prompt. A matched exclusion dominates;
invented clause identifiers, missing projection identifiers and malformed or
oversized answers withhold with bounded reason codes and no rule fallback.

After a model call the closure, both review revisions, protection state,
policy time and structure are captured again; an unchanged capture is a
retained observation (`requalified`), any change or withholding discards it
(`not_retained`) and evicts its cache entries. Results carry decision metadata
only and always say `execution_enabled: false` and `serving_adapter: null`.
`tests/permissions_v2/test_fact_bridge.py` covers both arms, prompt hygiene,
every structural stop, exclusion precedence, output-stage clause narrowing,
window masks, five mid-call changes, cache isolation, withheld evidence, capsule
closure and the import boundary with fake transports (36 cases). An exclusion
with unknown event time cannot coexist with an eligible inclusion, because an
inclusion needs every leaf known and in window; that branch is defensive only.
The host-side measurement (`scripts/permissions_beta/run_fact_bridge.py` in
the control-plane repository) runs both arms on scratch corpora against the
pinned local model; its report is orchestration evidence, not accuracy.

## Run and verify

With the scratch/offline environment and live DB tripwire from
`PERMISSIONS_BETA_FOUNDATION.md`, run `tests/permissions_v2/test_experiments.py`.
The synthetic demo accepts no candidate file or model endpoint:

```sh
python -m topos.permissions_v2.experiments --synthetic
python -m topos.permissions_v2.experiments --synthetic --deterministic-test-transport
```

The default reports an unconfigured semantic arm. The optional deterministic
transport returns a fixed response solely to exercise orchestration. It is not a
classifier. The focused gate passed **217 tests** (87 experiment cases plus 130
existing contract/ledger cases). Restoring the earlier unfiltered clause list
reproduced both A/B structural regression failures before the shared guard fix.
Tests cover both stages, correlated tuples, empty sets, exclusion
precedence, unknown predicates, strict response identity/schema/budgets,
timeouts/errors, injection-shaped data separation, cache isolation and changes
during evaluation. They establish orchestration behavior, **not classifier
accuracy, real lineage certification, data disclosure safety or a winning arm**.

A separate actual local-model pilot exercised eight public synthetic examples;
its audit record is `NL_SYNTHETIC_EXPERIMENT.md`. That pilot establishes neither
held-out classifier quality nor copied-corpus eligibility. Owner adjudication,
relevance/utility/false-permit metrics, the real qualified-input bridge and all
eight actual client/arm cells remain outstanding. See the versioned
[copied-positive and prose-bridge plan](../../topos/permissions_v2/COPIED_POSITIVE_PLAN.md)
for the next bounded slice; current signed P2b serving uses hard rules only.
