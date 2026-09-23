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
tool executor, and the engine ships no network transport. An operator may
explicitly inject an async transport:

```python
async def complete(request: ModelRequest) -> ModelResponse:
    ...
```

`ModelRequest` pins arm, processor, model ID, model revision, prompt revision,
stage, output-token budget and sampling. `processor` is the approved pin's
`owner-engine-local` or `synthetic-eval-hosted`; a request without one, or with
any other value, is refused. `sampling` is `temperature_zero` with
`reasoning_effort: null`, or `provider_reasoning_default` with a
`reasoning_effort` of `minimal`, `low`, `medium` or `high`; any other pairing
is refused, an `owner-engine-local` request must use `temperature_zero`, and
there is no `temperature` field, so a request never claims a temperature it did
not send. The transport must request exactly that sampling. This flat harness
always sends `owner-engine-local` at `temperature_zero`. Its `system` field carries the
fixed instruction template plus approved policy JSON. `candidate_data` contains
only the separate untrusted inspected units. Reviewed attributes, expected
verdicts, authority and protection flags never enter the model prompt. Arm B
interprets prose directly rather than compiling it to predicates.

For the local processor the trusted transport must establish that processing is
local. Every transport must enforce model identity, avoid truncation and tool
execution, and honor cancellation. Merely
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
still unmounted: no module under `topos/` outside the experiments package
imports it (a test parses every such module's absolute, relative and
`import_module` string imports), it has no ledger issuance, no send path and no
recipient arm selector. Its inputs are the real `ProjectionReviewService` (resolver, owner
evidence-review store, owner output-review store), a verified `Binding`, a
clock and an operator-injected local transport.

The capsule pairs the signed-grammar P2b policy (either the exact-instant v1
class or the separate stated-day `permissions-beta/p2b-v2` class; the rules
arm evaluates whichever the owner approved) with its prose twin: inclusion
identifiers are exactly the policy's permit rule identifiers and exclusions its
deny rule identifiers, so both arms start from one clause universe. The
processor pin names the processor, model, model revision, prompt revision,
sampling and byte/token budgets the owner approved: either the local owner
engine or, for synthetic evaluation only, a hosted model (see "Hosted processor"
below). The capsule digest detects edits; it does not authenticate the owner.

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
not already false. Each offered exclusion carries its rule's declared sources
and tables and `structural_scope_unit_ids`: at evidence_use the units that
rule's sources, tables and window can reach, at output_release only the output.
The prompt (`fact-bridge-prompt/v3`) says that list is scope, not a match. Under
v2 the field was `unit_ids`, and every unit sharing those sources was listed
under every exclusion; the one synthetic positive was then denied citing the
health exclusion, a likely misreading of that list as a finding. The owner's
original prose restates every clause, so it is sent only when every inclusion
is eligible and every exclusion offered; otherwise `original` is null and the
model sees only the offered clause texts and their examples. Reviewed labels,
owner-only flags, record and entity identifiers, revisions and authority never
enter the prompt. At evidence_use a derived fact unit shows only its predicate:
its subject is an entity id under the attested contract, and its value is the
scalar the output stage judges. The subject rule, output family, view and
reviewed projection all come from the capsule's capability through the maps the
release adapter reads, so arm A is the serving decision for p2b-v1 to v4
(`tests/permissions_v2/test_fact_bridge_parity.py` compares it with the decision
`FactProjectionRelease` checkpoints, 11 cases). A matched exclusion dominates. `semantic_deny` always names the
exclusions it matched; a deny naming no clause is recorded separately as
`no_semantic_match`, and a deny naming only an inclusion withholds as
`clause_binding`. Invented clause identifiers, missing projection identifiers
and malformed or oversized answers withhold with bounded reason codes and no
rule fallback.

A model call is counted whenever the transport was awaited, including a call
that times out, errors or answers with the wrong model identity. After every
such call, and before the next stage is sent, the closure, both review
revisions, protection state, policy time and structure are captured again. Any
change or withholding stops the run with `requalification_failed`
(`not_retained`), evicts the capture's cache entries and makes no further model
call; nothing is cached before it requalifies. A run whose last call
requalified is `requalified`; a run with no call (structural stop or cache hit)
stays `captured_under_gates`. Results carry decision metadata only and always
say `execution_enabled: false` and `serving_adapter: null`.
`tests/permissions_v2/test_fact_bridge.py` covers both arms, prompt hygiene,
every structural stop, exclusion precedence and scope, masked and narrowed
clauses keeping the original prose out, output-stage clause narrowing, window
masks, five mid-call changes during either model call (a change during the
first stops before the second call; one during the second evicts the cached
first-stage decision), post-call identity mismatch, cache isolation, withheld
evidence, capsule closure and the import boundary with fake transports, plus the
processor pins, sampling, the processor each request carries and the
hosted-processor refusal described below (115 cases). An exclusion with unknown
event time cannot coexist with an eligible inclusion, because an inclusion
needs every leaf known and in window; that branch is defensive only.

For synthetic runs only, `experiments/retention.py` keeps each model request
and raw response privately (a caller-chosen 0700 directory, 0600 files, no
symlinks), so a wrong verdict can be diagnosed; reports never carry bodies.
`experiments/scoring.py` scores both arms against an external case file whose
exact bytes are hashed into the report: permits, prohibited permits, recall with
a 95% Wilson interval, unresolved verdicts, p50/p95 latency, paraphrase
stability, and the B-minus-A recall gap with Newcombe's paired interval. Gold
labels are joined only after both arms ran, and evaluators never receive a gold
field (`test_experiment_scoring.py`, 24 cases; `test_experiment_retention.py`,
15).

The host-side measurement (`scripts/permissions_beta/run_fact_bridge.py` in
the control-plane repository) runs both arms over the cases in a `--cases` file
(`--emit-design-cases` writes the 11 synthetic design cases: the original eight,
five of them designed to deny, plus three restatements) against the pinned local
model. Each
arm captures under the gates itself; the runner refuses a record whose arms
report different bundle revisions. It refuses any uncommitted change under
the engine's `topos/` and `shared/` packages, the two engine trees the measured
path loads (storage migrations import `shared` through source definitions);
a runner test fails if it loads an engine module outside them. Its report is
orchestration evidence, not accuracy.

## Owner-shadow source bridge for P2a raw message release

`experiments/source_bridge.py` (grammar `topos-offline-source-message-experiment/v1`)
gives arm B a path for `permissions-beta/p2a-v1`, the whole-message release the
family cells C and G run on. It is unmounted like the fact bridge, reuses its
processor pin, transport, requalification, cache and retention, and its capsule
pairs a P2a policy (vocabulary `owner-review-vocabulary/v1` only) with prose
whose inclusion and exclusion identifiers are the policy's permit and deny rule
identifiers.

The closure is read exactly as `SourceMessageRelease` reads it:
`with_qualified(..., contract=LEGACY_CONTRACT, discloses_sources=True)`, so a
message that also backs an owner-only fact withholds both arms (`owner_only`)
before any model call. The capture also binds the node-wide protection revision
the signed authority would bind, and the policy's validity. Arm A is
`source_message_decision` over the capture, and after a permit the same
disclosure build and 256,000-byte budget the adapter applies;
`tests/permissions_v2/test_source_bridge.py` drives the real adapter and
compares arm A with the decision it checkpoints (permit, rule deny, and
indeterminate). The stored review vocabulary cannot yield an unknown label
today, so the indeterminate cases patch the one label map both paths read.

Arm B stops before any call when no permit rule's processor, raw ceiling,
sources and tables cover every message (`no_structural_match`), or when a unit
is over the 16-unit or 16,000-character surface budget. evidence_use shows every
closure unit (the locator fact as its predicate only, each message as its
content); output_release shows exactly the records the adapter would disclose.
A unit carries `unit_id`, `table`, `source_id` and `text`, and never a dataset,
record or entity identifier, a label, an owner-only flag, a revision or
authority. An exclusion is offered when its sources and tables reach a message.
Its `structural_scope_unit_ids` are the whole closure at evidence_use, as the
rules apply a reached exclusion to the whole derivation, and only the reached
records at output_release. The prompt `source-bridge-prompt/v1` is pinned by
hash and uses the fact bridge's "scope, not a match" wording.
Clause binding, projection, malformed, oversized, timeout and identity failures
withhold with the fact bridge's bounded codes and never fall back to rules.
Results carry decision metadata only, with `execution_enabled: false` and
`serving_adapter: null`. The test file (fake transports only) also covers
prompt hygiene, the legacy subject rule of the capture, inclusion coverage and
exclusion reach on a two-message closure, eight mid-call changes during either
call (among them a revoked review, a new owner-only sibling fact, a protection
change outside the closure, and an edit or relabel the owner reviewed again,
which still qualifies and is caught only by the revision comparison), cache
isolation, synthetic-only retention and the import boundary.

## Hosted processor: synthetic evaluation only

A bridge's `ProcessorPin.processor` is `owner-engine-local` or
`synthetic-eval-hosted`. A hosted processor sends candidate data to a hosted
model. It is for synthetic evaluation only and never for copied or real owner
data.

| Pin field | `owner-engine-local` | `synthetic-eval-hosted` |
| --- | --- | --- |
| `sampling` | `temperature_zero` only | `temperature_zero` or `provider_reasoning_default` |
| `reasoning_effort` | `null` | named exactly when `sampling` is `provider_reasoning_default` |
| `model_id` | any identifier | a dated snapshot (`-YYYY-MM-DD` or `-YYYYMMDD`, a real day) |
| `model_revision` | the local model's artifact revision | expected to be `hosted_model_revision(provider=..., model_id=...)`; any 64-hex value parses, so the transport checks it |
| `timeout_ms` | 1 to 30,000 | 1 to 120,000 |
| `max_output_tokens` | 32 to 1,024 | 32 to 4,096 (reasoning tokens count toward it) |
| `max_prompt_bytes`, `max_response_bytes` | 1,024 to 65,536; 128 to 8,192 | unchanged |

`hosted_model_revision` is the sha256 of the canonical
`{"provider", "model_id"}` identity of the snapshot. It is not a weight digest.
It names what the owner approved and cannot show what the provider serves under
that name. The pin carries no provider, so the operator's transport, not the
pin, must check that the revision matches the provider and model it calls.

`FactShadowBridge` and `SourceShadowBridge` take a keyword
`synthetic_evaluation` (default `False`). With a hosted pin, construction raises
`hosted_processor_requires_synthetic_evaluation` unless it is `True`, and a
non-boolean raises `synthetic_evaluation_invalid` for either processor. `run`
checks again before any capture, because the `capsule` attribute can be replaced
after construction. The bridge cannot verify that its rows are synthetic, so the
flag is the caller's assertion. A harness must set it only from the run's own
binding (the dataset is synthetic and no owner data is mounted), never from a
command-line default.

That refusal reads only the pin's processor label, and a pin labelled
`owner-engine-local` may name any model, including a hosted snapshot. Every
request therefore carries the approved pin's `processor`, and two requests that
differ only in that label are otherwise byte-identical. The transport is where
candidate data leaves the engine, so a transport that calls a hosted model must
refuse any request whose `processor` is not `synthetic-eval-hosted`, before any
network call. It must also check the run binding itself (a synthetic dataset
and `owner_data_mounted: false`) rather than trust the engine flag, which is
only the caller's assertion. Both bridge test files check that each request
carries its pin's processor and that a local label on the hosted snapshot is
still sent as `owner-engine-local`.

Arm B's structural floor still reads each signed rule's
processor as serving does (`owner-engine-local`, the only value the policy
grammar admits), so a hosted pin changes who judges the prose, not which rules
are eligible.

Arm B results from a hosted pin describe the approved policy language as that
hosted model judged it. They do not certify an in-node local evaluator, and a
report must say so.

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
relevance/utility/false-permit metrics, copied-corpus inputs to the offline
fact bridge above (it runs only on scratch corpora), any mounted shadow adapter
and all eight actual client/arm cells remain outstanding. See the versioned
[copied-positive and prose-bridge plan](../../topos/permissions_v2/COPIED_POSITIVE_PLAN.md)
for the next bounded slice; current signed P2b serving uses hard rules only.
