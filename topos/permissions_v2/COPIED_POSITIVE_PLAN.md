# Copied positive and direct-prose bridge, design v1

Status: next implementation plan, not an enabled capability. This document is
based on the signed P2b implementation at `65b09a1` and exclusion hardening at
`18ef2af`. The structural/membership split (`fact_eligibility.py`) and the
offline owner-shadow bridge (`experiments/fact_bridge.py`, see
`docs/testing/PERMISSIONS_V2_EXPERIMENTS.md`) are now implemented; coverage,
copy enrollment, a genuine copied positive and any active B capability are not. It creates no coverage record, source mapping, grant, classifier input
or release exception. The current owner-only, record/entity Off-limits and
intelligence-exclusion floors remain intact. The first target is one existing
scoped, current, explicitly owner-authored `prefers` fact and its exact reviewed
scalar. Finding such a fact in the copy is an outcome to measure, not an assumption.

## What the current evidence establishes

The offline quarantine preserved 144 original tables. Its facts diagnostic found
310 facts: 278 owner-only and 32 scoped; 124 have a non-null `valid_to`. These are
marginal counts, not the intersection eligible for P2b. The negative campaign
made 620 resolver decisions and all withheld at the existing entity floor before
resolver `_load`. Integrity fingerprinting privately read and hashed retained
rows; zero resolver loads does not mean the entire diagnostic read no content.
No copied candidate has reached a release or language evaluator.

The inert posture exporter covered 38 contexts and 109,420 message rows. All 38
received deny caps: four conversation contexts had unverified owner rows, four
datasetless AI contexts lacked proven original node/resource identity, and 30
configured sources had no bound first-family instance. The completed aggregate
cannot distinguish missing owner values from contradictory ones. It proves no
mapped positive posture. The ambient-deactivation safeguard was demonstrated on
synthetic fixtures. The export is inert and `runtime_usable: false`; the copy is
still `safe_to_boot: false`.

These reports are separate evidence. None establishes intact lineage, exact UTC
event time, ordinary subject matter, complete entity coverage, an authenticated
owner approval, or eligibility of any particular copied fact. The three existing
entity blackholes and three intelligence exclusions remain present. Owner-only
facts must not be changed to scoped to manufacture a positive. A new synthetic
canary in the copy must still be reported as synthetic.

The implementation already has a narrow non-raw P2b path: exact self/`prefers`
scalar, independent evidence/output review, explicit tables, whole-rule source
correlation, contributor-event window, signed current authority and node/CP
send-start gates. Summary or Raw can select this expressly consented view;
Inference cannot. This is not general facts, generated summary, finance, work,
availability or the complete reading profile.

## Next slice: resolve one candidate without changing its history

Implement the following four packages in order. Start with read-only diagnostics
and pure validators; keep serving and model processing disabled until their
separate gates pass. A zero-candidate result is valid and must not trigger a
weaker fallback.

### 1. Count-only provenance and eligibility matrix

Extend the existing offline diagnostic with named aggregate dimensions over the
pinned baseline/copy, using explicit read-only SQLite connections and before/after
hash checks. Record schema/code/semantics hashes and retain no cell IDs or values
in the public report. A private opaque candidate reference may be retained only
in the later authenticated owner-review workflow.

Measure the intersection, not just individual totals:

- Already scoped, active fact; exact registered predicate/scalar grammar; exact
  canonical owner subject; explicit role authored or legacy null plus native
  author proof. No inferred or non-authored row becomes authored through review.
- Complete, finite recursive references with full table/source/dataset identity;
  target existence; current native owner/role proof at every leaf; no unsupported
  family, cycles, ambiguity or independent-copy dependency.
- Per-artifact exact UTC `valid_from`, null `valid_to`; per-leaf exact UTC
  `event_at`. Report unknown, future, outside-window and eligible separately.
  Never fill missing timestamps from ingestion time or replace them with now.
- For each source/dataset context, owner columns equal the canonical owner,
  null/missing, contradictory, or internally mixed. For datasetless AI, report
  whether authoritative original node/resource evidence exists separately from
  current execution identity. No source labels or conversation names establish it.
- Existing record/fact tombstone hits and unknown exclusion state. Entity floors
  remain dominating; this diagnostic cannot declare an unrelated entity match
  absent or silently invoke the recipient resolver with its floor disabled.

Structural inspection of candidate rows is a new private read, unlike the prior
resolver-load-zero campaign. Report that distinction explicitly. This pass runs
no model, creates no reviews, edits no lineage/identity and does not boot a copy.

### 2. Authenticated copy enrollment and inert posture reader

Add a private closed enrollment model and pure reader before using the copy in a
runtime. Proposed `topos-copy-evidence-enrollment/v1` fields are:

| Required field group | Proof and interpretation |
| --- | --- |
| Enrollment identity | Owner-authenticated operation ID, monotonic generation, status, time and exact enrollment hash |
| Execution binding | Full beta environment/node/resource/owner, canonical-file incarnation and working-copy/schema hashes |
| Origin binding | Original owner and exact dataset or original node/resource identity; authoritative proof reference/hash for each mapping |
| Snapshot binding | Baseline digest, quarantine manifest hash, inert sidecar hash, exporter and native-role/posture semantic hashes, bundled-default hashes |
| Exact entries | Full original source context, original-to-copy mapping, source-input hashes, resolved cap and resolution state |
| Lifecycle | Durable current generation and floor revision; revocation and rollback high-water mark |

Hashes prove consistency only. Enrollment must load original evidence and verify
an owner-authenticated mapping; it cannot accept recipient JSON or a sidecar hash
as identity authority. Keep original provenance identities separate from the new
execution namespace. Do not rewrite original node/resource/dataset values to the
lab resource. A privately known owner ID alone proves neither original node nor
resource. If native metadata cannot establish an identity, an authenticated
external historical record is required; a newly typed label is insufficient.

The existing all-deny sidecar cannot be made permissive by enrollment or an owner
semantic review. A resolved entry requires a new versioned export whose exact
provenance is independently supported. Missing, contradictory, ambiguous,
malformed or unavailable proof stays deny. The 30 configured-only contexts do
not become evidence until a supported canonical instance actually exists.

In copy mode the reader requires exact identity coverage and computes the meet
of the imported original attribution cap and current restrictive posture/role.
Disabling an executable installation must not increase attribution. The original
source definition is never reactivated, executed or copied into runtime config.
Current `authored` attribution does not override original `observed` or deny.
Changes to either cap, mapping, input hash, semantic version, copy incarnation or
enrollment generation invalidate evidence and reviews. Lost enrollment or clock
state fails closed; changed content cannot retain its old proof. Use explicit
readers under the same canonical snapshot, with no default-database lookup.

### 3. Bounded owner-attested entity coverage

Implement the separate [entity coverage design](ENTITY_COVERAGE_DESIGN.md) first
as an owner-only preview/store and a pure validator. The initial registered
surface must cover the entire small canonical fact closure, every terminal row,
all context that the surface depends on, and the exact scalar output. The owner
must see all those surfaces without truncation. Unsupported context, unresolved
references or excess size yields no completed coverage record.

The current `subject_entity_ids=[self]` review describes aboutness and cannot be
migrated into coverage. Human review may attest complete semantic inventory; it
is not a machine guarantee of absence. A model/NER result or empty mention join
cannot approve completion. Owner preview may inspect protected content only on
the existing verified owner channel. A non-owner evaluator must not see it to
answer whether it is protected.

The coverage store binds every original/execution identity, row/context hash,
lineage edge, native attribution and posture revision, evidence review, exact
output/review hash, copy enrollment, surface version, identity universe, current
restrictions and its own durable review generation. It must be loaded by the
service, never accepted from a recipient. An opaque handle is a locator, not a
bearer permit.

Extend the earlier coverage proposal's restriction binding to clock v2:
`owner_only_records`, entity blackholes, and all intelligence exclusions. Every
record tombstone and semantic fact tombstone remains a direct veto over the
complete closure, including the pre-purge interval. Any covered reference that
matches an entity exclusion, protected entity, alias or preemptive protected name
withholds. Known positive observations override contradictory negative reviews.
Purging entity observations never certifies absence. Uncertain selector matching
withholds; coverage cannot create an exception to the owner's selector.

Add a separately versioned durable identity-universe clock for the concrete
entity/alias/merge/remint/mention tables and normalization semantics actually
read by the validator. Validate exact trigger/schema coverage before enrollment.
It must detect add/remove ABA, missing state, replacement and rollback. Read all
restriction, identity and coverage state in the same snapshot; after slow work
requalify and recheck all current stores before dispatch. No protected input can
influence a released value, count, ordering, reason or citation.

Only a subsequent, explicitly consented capability may replace the global entity
floor for this certified surface. The new capability must pin its coverage and
copy-enrollment semantics and require a new grant identity; existing P2a/P2b
policies retain their current behavior. No `skip_entity_floor` switch, disguised
owner principal, migrated self-only review, raw fallback or partial-row redaction
is acceptable. This document does not register that capability.

### 4. One genuine positive, then client delivery

With a complete eligible candidate, enroll authenticated owner evidence, coverage
and exact output reviews. Evaluate a whole explicit hard-rule tuple with exact
evidence tables/source universe and event window. First run a standalone offline
release test into a private capture, with body access and model access separately
counted; then use a paired isolated node/CP and their current signed final-send
checks. Pin the original/copy/enrollment/review/contract hashes in private proof.

The positive must come from the unchanged copied record lineage, preserve every
owner-only selection and tombstone, and release only the exact six-field scalar.
Match it with a protected/unknown/stale negative at each boundary. Report the
specific registered form, not all copied facts or the four-recipient campaign.
If no fact survives, stop that positive claim and use independently identified
synthetic tests while preparing a future supported family; do not fabricate
original identity, missing provenance, event time or consent.

## Direct prose shares this boundary, not raw export authority

The current experiment package is an unmounted synthetic grammar. Its P2a
`AuthorityBinding`, `experiment.synthetic_fact.v1`, simplified leaf identities,
trusted qualification flags and capsule review hash are insufficient for copied
P2b inputs. Casting a `QualifiedEvidence` into those fields would lose proof.
Create a new closed offline bridge, provisionally
`topos-offline-qualified-fact-experiment/v1`, rather than widening the old parser.

The trusted bridge must obtain current evidence, coverage, copy enrollment and
exact output from the service. Its private immutable bundle contains the complete
artifact/leaf/context graph and original identities, actual inspected surfaces,
exact reviewed scalar, all review and protection/identity revisions, signed
request issuance anchor, full authority/assignment tuple, evaluator/model/prompt
versions, source/table/time bounds, and per-stage eligible clause IDs. A snapshot
hash alone does not authenticate any of these. The public result retains bounded
decision metadata and `execution_enabled: false`.

Factor P2b evaluation into common mandatory preflight and replaceable membership
stages under parity tests. Common checks retain native provenance, owner-only and
exclusions, complete coverage/lineage, current authority, explicit source/table
universe, event time, processor, purpose, exact output/ceiling and whole-clause
correlation. Domain/sensitivity membership is evaluated by arm A over the same
owner-reviewed evidence/output labels and by arm B against direct approved prose,
exclusions and examples. Do not prefilter B's candidate pool by A's membership
verdict; that would conceal disagreements and bias the comparison. Mandatory
structural failures must stop both before a model call.

Both stages remain required. Evidence use evaluates the entire contributing
closure, and output release evaluates the exact already reviewed scalar under a
common inclusion that survived evidence use. A deny or unknown relevant exclusion
dominates; neither clauses nor arms may be unioned to rescue a denial. B cannot
manufacture clause IDs, output text, identity, authority, review state or a new
projection. Original approved prose is the tested policy, not a compiler's rewrite.
Empty source/table/form selections remain empty in both arms.

Processing permission is separate from recipient raw disclosure. The owner must
approve the exact local classifier processor and policy capsule before copied
surfaces enter it. This does not authorize raw messages in a recipient result.
The bridge starts in owner-shadow mode with no serving adapter. Keep hard-bounded
local transport, no tools, no arbitrary URLs/subprocesses, pinned installed model
and prompt, cancellation, exact JSON schema, bounded context/output and no hidden
labels in model input. Over-budget context withholds instead of truncating.

Capture under the gates, release the gates for the model call, then requalify all
revisions and authority before retaining a usable observation. Bind caches to
both review stages, copy/coverage/identity state, full source/artifact graph,
request anchor/current validity, arm assignment, policy and model/prompt. No
stale positive reuse, B-to-A fallback, shadow output forwarding or denied sample
logging. A future active B capability and CP assignment/revocation path need a
separate review and quality gate; the offline bridge cannot authorize disclosure.

The existing eight public synthetic examples reached a real local model with no
design-prohibited permit, but they are not held-out owner truth or an estimated
leak rate. The positive required approximately eleven seconds across two model
calls. Candidate bounding and revision-aware caching need measurement; neither
fake-transport tests nor that pilot establish classifier accuracy or injection
resistance. Pre-register owner-adjudicated holdouts, split by underlying document/
conversation family and policy variant, and report false permits, abstention,
useful recall, latency and sample uncertainty without tuning on the test set.

## Concrete regression gates

| Boundary | Positive control | Required failure or race |
| --- | --- | --- |
| Origin enrollment | Exact verified source/dataset mapping with immutable retained identity | Lab-ID substitution, owner null/conflict, unknown original node/resource, caller-supplied mapping, rollback |
| Inert posture | Original/current authored cap plus native owner proof | Ambient install disabled, more permissive current override, missing entry, stale sidecar, wrong bundled version |
| Closure/time | Existing scoped atomic preference with complete current UTC evidence | Owner-only, non-authored, missing child, copied dependency, future/ambiguous event, closed fact, partial-rule stitching |
| Entity coverage | Unrelated fully reviewed surface while a separate selector exists | Alias, indirect reference, quote/context, preemptive name, positive mention contradiction, truncated preview, fabricated completeness |
| Tombstones/clocks | Unrelated known record/fact exclusion after fresh review | Record before purge, exact/subject-wide fact alias, unknown kind/schema, entity purge, all relevant add/remove ABA, restart rollback |
| Review provenance | Current authenticated owner store loaded by service | JSON/model-created review, self-only migration, wrong owner/copy, stale output, revoked evidence/coverage/output review |
| Evaluator parity | Same complete eligible closure and exact scalar in both arms | Empty bounds, two partial clauses, excluded evidence or output, manufactured IDs, malformed/late model response, cache cross-arm reuse |
| Final release | Signed exact scalar through current node and CP authority | Cancellation after issuance, signer removal, actor revocation, protection/coverage change during model or send, replay, raw/MCP/profile substitution |

First prove these with synthetic scratch fixtures and the live-DB tripwire. A
copied positive adds provenance evidence, not a substitute for adversarial tests.
Owner diagnostics stay private; recipient failures stay indistinguishable and
contain no selectors, source counts, candidate text or policy-internal reasons.

## Inputs and work ownership

| Can be engineered or verified now | Requires real owner/external evidence |
| --- | --- |
| Count-only intersection diagnostic; schema/lineage/time checks; strict inert reader/enrollment/clock; coverage preview/store; offline bridge; synthetic races | Semantic completeness of the actual displayed surfaces and exact output; authentic review of intended prose/exclusions/examples and held-out classifications |
| Read already available authoritative owner/catalog/installation metadata, with counts/hash reports | Missing historical dataset owner or original node/resource proof if no authoritative record survives; it cannot be invented from the desired mapping |
| Prepare isolated HTTPS routing, audience/client configuration, exact resource and draft grants; automated transport preflight | Actual Claude Desktop and ChatGPT account connection/authentication availability, Simulations user context, and Clark's own acceptance/login |
| Preserve default-off rollout, failure metrics and scoped cancellation controls | Approval of the concrete review/processor/candidate disclosure when presented; no review form can override owner-only or unresolved native provenance |

Do useful implementation and recoverable metadata checks before requesting any
missing evidence. Present a bounded owner preview or exact missing provenance
record, not a broad request to approve the design. Existing task authorization
covers preparation; no blanket repeat permission request is needed. Public-client
setup should proceed from concrete beta resources. A synthetic SDK connection is
preflight, never proof of those actual clients. No one recipient substitutes for
any uncompleted profile/arm cell.

Related implementation references: [fact policy](FACT_POLICY_DESIGN.md),
[entity coverage](ENTITY_COVERAGE_DESIGN.md),
[exclusion upgrade](EXCLUSION_CLOCK_UPGRADE.md), and
[offline experiment](../../docs/testing/PERMISSIONS_V2_EXPERIMENTS.md).
Quarantine, posture and negative-corpus results are documented separately under
the CP repository's `scripts/permissions_beta/` directory; this plan performs no
new corpus reads and changes none of their artifacts.
