# Owner-stated fact projection design, v1

Status: pure projection prototype. A separate explicit P2b policy foundation is
described in [the fact policy design](FACT_POLICY_DESIGN.md); it alone enables no
advertised capability, signed authority, or recipient route for this projection.
This is not completion of the reading profile or an executable permission.

## First exact output

The first closed value in `fact_projection.py` is:

```json
{
  "family": "owner_stated_fact",
  "operation": "read",
  "view_id": "owner_stated_fact.scalar.v1",
  "subject": "self",
  "predicate": "prefers",
  "value": "history books"
}
```

Only `prefers` is registered. `value` is the exact existing fact scalar, not an
LLM rewrite, substring, truncation or automatic redaction. It is a strict string
of 1–256 code points, already NFC, with normalized single ASCII spaces. The
lexical grammar allows Unicode letters/marks/numbers, spaces, hyphens,
apostrophes and ampersands. Quotation delimiters, sentence punctuation, newlines,
controls, bidi controls and zero-width format characters are unsupported.
Surrounding apostrophes are rejected. No input is silently normalized.

These syntactic limits reject multiline paragraphs and explicit quotations; they
do not prove that a label is a simple preference or non-sensitive. A short
sentence without punctuation can still fit the syntax. Exact human output review
and later policy evaluation are mandatory. More expressive titles, dates, amounts,
project statuses and other predicates need separately registered contracts.
No source/fact IDs, excerpts, reasons, confidence, dates or free-form metadata
appear in this output.

`prepare_fact_projection` requires a qualified evidence value and a canonical
fact row whose full revision exactly matches the snapshot root. It checks the
complete classification/reference set and consistent node/resource identities.
The prototype additionally requires exact self-only classification, native scoped
owner assertion, and the registered predicate/scalar. Missing, withheld, unknown,
non-authored, mixed-speech, or independent-copy evidence cannot be overridden by
an output review.

`FactProjectionReview` binds owner ID, full candidate/snapshot, evidence-review
revision, projection implementation version, exact candidate/output hashes,
review ID/time/status, and separate output domains/sensitivity. The initial exact
scalar view cannot lower the contributing evidence's sensitivity floor; no
semantic declassification function has been certified. `bind_output_review`
rejects stale, revoked or future-dated reviews. It always returns
`authorization_status: not_evaluated` and `execution_enabled: false`.

The models establish consistency, not authenticity. A caller can construct
self-consistent models; neither hashes nor a Python type makes them a permit.
A future adapter must obtain qualification itself under the resolver gates, load
the current review from an authenticated owner store, then perform signed policy
and actual-release checks. Do not expose these functions as accepting recipient
qualification or review objects.

## Minimal serving contract that still must be agreed

The pure P2b foundation now defines an explicit new capability version requiring
owner consent; its serving integration remains a separate gate. Existing P2a
policies/envelopes are closed to this form and must remain so. Keep the existing
raw source reader unchanged. Do not map a fact form onto its output `tables`:
that field currently doubles as evidence-table coverage in the raw adapter.

The next evidence-use schema needs independent `sources` and exact canonical
`tables`, plus an explicit event-time constraint. Proposed bounded first form:

```text
event_window = {
  kind: rolling,
  anchor: server_request_as_of,
  max_age_seconds: positive safe integer,
  event_time_semantics: canonical_event_time_v1,
  missing_or_ambiguous: withhold,
  future: withhold
}
```

An unrestricted interval, if ever offered, must be a separate explicit choice.
The server supplies and signs the request anchor. Every contributing leaf must
satisfy the interval using an approved table-specific, timezone-aware canonical
UTC event timestamp. Grant expiry stays a separate current-time check. Never use
fact creation/`valid_from` as a replacement for a missing source event timestamp.
Fact validity also needs a current, non-future assertion check. The planned
180/365/30/14-day profile windows are not represented by current P2a grant validity.

Output permission remains an exact family/operation/view tuple. A single whole
rule must cover every contributing source/table, processing purpose, time window,
evidence predicate and actual output predicate. Exclusions and unknowns dominate.
Summary/inference/raw labels remain UI presets selecting explicitly consented
views; changing their order must not migrate existing grants into this form.

Implementation seams are `EvidenceResolver.with_qualified`, an authenticated
output-review store, a new closed output registry/parser, ledger projection-shape
validation, and mirrored engine/CP result/output validation. The existing signed
result hash, replay ledger and actual node/CP transport gates can be reused only
after their capability/closed-schema contracts recognize this version. The model
must never select an arbitrary output parser, raw fallback or grant.

## What this enables, and what remains separate

The next useful profile slice is an explicitly stated ordinary reading interest.
It does not yet satisfy the full reading profile: date windows, output permission,
actual client delivery and policy evaluation remain outstanding. Finance needs
separate typed amount/currency/date or planning claims; work needs project-bound
facts and later endpoint-safe relationships. Availability is a distinct closed
inference family over certified calendar/free-busy lineage, not a preference fact.

The next summary should be a deterministic rendering of already authorized atomic
facts with a bounded closed schema, not general generated prose. One whole rule
must authorize the combined evidence where cross-rule derivation is denied.
Counts, ordering, omissions and citation fields need their own exclusion tests.
General inference stays unavailable; a short inferred diagnosis is not an
attenuation of an ordinary fact. The existing availability egress vocabulary is a
useful pattern, not certification of a new v2 calendar adapter.

Mixed private/allowed source records remain withheld. Dropping a private reason
from `object_value` does not prove independent support. Segment-level access needs
an explicit source-slice identity, parent revision, exact offsets and reviewed
independence/classification before it can safely relax whole-record withholding.
An Off-limits parent still protects every slice.

The separate [owner-attested entity coverage design](ENTITY_COVERAGE_DESIGN.md)
describes a possible future replacement for global entity withholding. It is
not implemented or a machine completeness certificate; existing self-only
reviews cannot be migrated into it.

## Shared A/B path without raw recipient disclosure

Both evaluators can consume the same fresh qualified evidence and exact proposed
scalar through a trusted adapter. The direct-prose arm reads bounded evidence
only inside the separately authorized local classification processor; it does
not receive recipient source-export authority. The output-release stage evaluates
the exact scalar, not a model-authored replacement. The active arm is server-owned,
exclusions dominate, and B never falls back to A on uncertainty or failure.

Do not hold the node writer gate across a slow model call. Capture a qualified,
revision-bound input under the gate, perform authorized classification locally,
then reacquire and requalify before release. Any evidence, output-review, posture,
protection, policy, identity, model/prompt, time or assignment change invalidates
the result. Cache keys must bind those same values. The existing experiment
harness is synthetic-only and non-executing; its data types are not trusted
qualification and cannot simply be mounted as this adapter. The eight-case local
pilot measured roughly eleven seconds for one two-stage positive and is not a
classifier-quality gate.

## Inert attribution snapshot for quarantined copies

Disabling `source_runtime_installs.is_active` must not erase an ambient posture.
Before quarantine, resolve attribution from the offline baseline and produce an
inert source-attribution snapshot independent of executable install state. It
should contain full source/dataset or datasetless node/resource identity, original
and explicitly approved copied bindings, effective posture/cap, resolution status,
input hashes, bundled-default version/hash, baseline digest and snapshot version.
Store no executable definition body, endpoint, credential or parser code there.

Pin its immutable digest and identity mapping in the private copy manifest and
node runtime. In copied-runtime mode, missing or ambiguous entries withhold;
never inherit mixed merely because a quarantined install became inactive. Apply
the imported restrictive cap conjunctively with current restrictions. Include
both snapshot and current-input revisions in `_p2b_source_revision`. An owner
change requires a new snapshot/revision and review invalidation, not activation
of source code. The initial snapshot must cover the full eligible source universe
and prove attribution after quarantine is no more permissive than before it.
Missing mappings are counted and held, not guessed. Current copied artifacts
remain non-bootable until this reader and its identity binding are implemented.

## Required next gates

- Exact source/scalar/review hashes; changed values, lineage, source posture,
  protection, qualification and owner-review revocation invalidate preparation.
- Public schema rejects unknown predicates/fields, raw source details, unsupported
  values and normalization changes. Forged models never become execution authority.
- Separate evidence/output exclusions, one-rule closure, explicit empty sets,
  unknown classification, native role caps and processor boundaries.
- Event-time cutoffs, missing timestamps, future facts, timezones and boundary
  changes between model evaluation and final send.
- Mutation/revocation during model execution and transport; current signer and
  exact output binding on both node and CP; no delayed-body or replay path.
- Differential canaries varying excluded content without changing allowed input;
  no influence on output, count, order or citations. Held-out human adjudication
  for B, including prompt injection, mixed context and policy paraphrases.
