# P2b fact policy foundation, v1

The pure policy parser/evaluator and a separate signed node dispatch exist.
The fact delivery flag is disabled by default. This implementation performs no
grant migration or natural-language evaluation. Serving requires explicitly
paired CP authorization and current owner evidence/output reviews.

`fact_contract.py` is the portable closed schema. `fact_policy.py` evaluates
fresh resolver-owned evidence/rows and a separately reviewed exact scalar. The
existing `contract.PolicyV2` and raw P2a grammar remain unchanged and reject this
policy. `fact_projection` re-exports the same `FactScalarDisclosure` class name
and schema; extraction changes no output fields or review semantics.

## Explicit policy choices

The top-level `topos-policy/v2` shape retains complete binding, source universe,
validity and hard constraints. New policies explicitly select:

```text
versions.capability = permissions-beta/p2b-v1
versions.vocabulary = owner-review-vocabulary/v1
evaluator = {kind: hard_rules, version: hard-rules/p2b-v1}
natural_language = null
```

Each `evidence_use` retains source selection, predicate, processor selection and
new-record behavior, and adds required independent `tables` and `event_window`:

```json
{
  "tables": ["signal_objects", "conversation_messages", "ai_chat_messages"],
  "purpose": "owner-stated-fact-projection",
  "event_window": {
    "kind": "rolling",
    "anchor": "server_request_as_of",
    "max_age_seconds": 31536000,
    "event_time_semantics": "canonical_event_time_v1",
    "missing_or_ambiguous": "withhold",
    "future": "withhold"
  }
}
```

Tables are an exact closed set, including every contributing derived fact's
`signal_objects` table. An omitted selection is invalid; `[]` selects nothing.
Processors remain explicitly limited to `owner-engine-local`. The purpose is
registered, not an arbitrary ignored label. Source `all` binds the exact consented
universe/revision; no new source or table becomes eligible automatically.

`release.forms` selects only `{family: owner_stated_fact, operation: read,
view_id: owner_stated_fact.scalar.v1}` and has no `tables` field. An empty forms
list selects nothing. The six-field scalar output remains exact self/prefers
data, with its existing lexical constraints and mandatory output review.

Summary and Raw ceilings can select this explicitly consented view. Inference
provides no permit for it. This is a capability mapping, not a changed total
ordering or an automatic upgrade of existing grants. A deny clause cannot be
neutralized by choosing an Inference ceiling.

## Time semantics

The signed transport supplies `envelope.issued_at` as `request_as_of`.
The recipient cannot provide or override that anchor. The pure function keeps a
separate argument for deterministic tests. `now` must be a strict safe integer,
at or after issuance and no more than 120 seconds later; current policy lifetime
is checked independently. Actual signed expiry may be earlier and is enforced by
the admission/final-release boundary.

Both supported terminal tables use `event_at` only. Accepted text is explicit UTC
`YYYY-MM-DDTHH:MM:SS[.1-6 digits]Z` or the equivalent `+00:00` suffix. Naive text,
nonzero offsets, `-00:00`, epoch numbers/strings, malformed dates, leap-second text
and excess fractional precision are unknown. No sorting helper, local timezone,
`created_at`, ingestion timestamp or fact timestamp supplies a fallback.

Integer microseconds implement the inclusive interval
`[request_as_of - max_age_seconds, request_as_of]`. Future evidence is unknown and
withheld even if it becomes past by final dispatch. Every fact artifact separately
requires a valid explicit UTC `valid_from` at/before the request anchor. Any
non-null `valid_to` withholds, including a future close time; the current resolver
already rejects closed facts. Missing/ambiguous fact validity also withholds.

This represents source-event windows rather than merely grant expiry. It does
not implement the complete 180/365/30/14-day profiles: finance, project facts,
availability and summaries need additional closed forms and evidence adapters.

### Stated-day validity (P2b v2)

`permissions-beta/p2b-v2` is a separate capability with its own policy class
(`StatedDayFactPolicy`), evaluator version (`hard-rules/p2b-v2`) and decision
class. Its rules, reviews, lineage, contributor event window, precedence and
exact scalar output are the v1 rules; only the meaning of `valid_from` differs,
and a policy must select every part of that meaning explicitly in
`versions.fact_validity`: `stated_day_v1`, day precision, an unrecorded timezone
basis, currency from 12:00 UTC on the following day, explicit UTC instants kept
exact, and unknown or not-yet-elapsed values withheld.

A `valid_from` of the exact form `YYYY-MM-DD` is a stated calendar day whose
timezone basis the producers did not record. It counts as current only once it
has ended at every Earth offset, 36 hours after its own 00:00 UTC. A stated day
that has not elapsed is `fact_not_current`, like a future instant. Month, year,
naive, offset, calendar-invalid or padded text stays unknown and withholds as
`fact_validity`, exactly as under v1. Explicit UTC instants keep their v1
meaning, so for such rows a v2 decision equals the frozen v1 oracle except for
its evaluator version and policy hash. The payload's real-world period fields
are not evaluated by either contract.

The v1 class is byte-identical after this addition: existing signed v1 policies
keep their hashes, `FactPolicyV2` never parses a v2 document and vice versa, the
registry dispatches on the capability literal, and changing the capability of an
existing grant still requires a new grant. `valid_from` is on the review surface,
so re-stamping a fact's validity stales its evidence review, and a snapshot taken
before the change is rejected as `fact_policy_revision`. The copied-corpus
waterfall that motivated this contract is recorded in the control-plane lab
documentation; a temporal contract alone produced no copied positive there.

## A second output family (P2b v4)

`permissions-beta/p2b-v4` releases `owner_stated_work.scalar.v1`: one
organisation the owner said, in the first person, that they work for. It is a
second output family, not a second spelling of the first. It rides the v3
attested subject rule and either validity contract, by composition; the evidence
rules, event window, correlation, exclusions, review gates and the scalar shape
are v1 unchanged.

### Why a second family at all

The first family releases `prefers`, and **no producer in the engine can write a
`prefers` fact this contract accepts.** The contract requires a payload that is
both `disclosure = "scoped"` and `asserted_by = "owner"`. The only writer that
emits `prefers` is the LLM extractor, and at `features/facts/llm_extract.py` it
sets `disclosure = "owner_only" if asserted_by == "owner" else "scoped"` — the
two conditions are mutually exclusive there. Every LLM-extracted fact is
therefore either owner-asserted and owner-only, or scoped and asserted by
somebody else. Neither qualifies, for any predicate.

That is the real reason the first family has measured zero releasable facts, and
it is a property of the producers rather than of any particular node.

### The producer, and the semantics it actually has

Exactly one writer in the engine emits a fact that is `scoped`, `asserted_by =
"owner"`, and sourced from a table this contract accepts as a leaf: the
first-person message patterns in `features/facts/extract.py`. Two of them match
`works_at`:

    I (currently |now )?work (at|for) X
    I('m| am) (now )?working (at|for) X

They run only over a row that `provenance.roles.record_role` says the owner
authored, on `conversation_messages` or `ai_chat_messages`, and
`extract_facts_from_batch` asserts every rule-extracted fact as the owner with
`disclosure` defaulting to `scoped`. So the value reaching a v4 disclosure is a
proper-noun-shaped span the owner typed after "I work at", cleaned by
`_clean_object` — which splits at the first `.` or `,`, so the label can carry
letters, digits, spaces, `&`, `'` and `-` and essentially nothing else. That is
what `explicit_atomic_work_engagement` means, and the owner still has to attest
it in review; the contract never decides it.

### Why not `works_on`, which measures higher

The corpus waterfall (`scripts/permissions_beta/CORPUS_FACT_WATERFALL.md` in the
control plane) scores `works_on` at 7 current scoped facts and 3 owner-asserted,
the only family reaching the assertion stage at all, and scores `works_at` at 0.
The family was still chosen as `works_at`, because the count measures a copy and
the producer measures the future:

- The only writer that emits a `scoped`, owner-asserted `works_on` is
  `extract_journal_facts`, which matches a journal entry's **category** string
  against the `normalized_name` of a declared `org`/`topic` entity and emits that
  entity's canonical name. That is a filing label, not a statement — the owner
  never said it — and its lineage always terminates in `journal_entries`, which
  is not a supported leaf table. All 3 measured candidates die at
  `reference_not_supported`, and any future one would too.
- `works_at`'s producer is a first-person declaration in a supported message
  table. Zero on that copy; reachable on every node.

So the measurement chose the shape of the question and ruled out six predicates;
reading the producers chose between the two it left. A family picked on the count
alone would have shipped a capability that cannot close, named after a statement
nobody made.

### What still blocks a live `works_at` closure

`extract._source_ref` emits `{table, record_id}` plus `source_id` when present,
and never `dataset_id`. The lineage grammar in `fact_eligibility._reference`
requires exactly `{table, record_id, source_id, dataset_id}` for
`conversation_messages` and exactly `{table, record_id, source_id}` for
`ai_chat_messages`. A `works_at` fact sourced from a conversation message
therefore fails lineage today; one sourced from an AI chat message with a
`source_id` does not. This is a property of the producer, not of the contract,
and it is recorded rather than worked around: widening `_reference` would accept
a reference whose dataset is unknown.

### What the signed document records, and what it enforces

`OwnerStatedWorkFamily` writes the family's premise into the policy: its name,
view id, predicate, assertion, projection version, and `producer`, a literal
naming the writer whose semantics the family claims. None of it is enforceable —
nothing in the contract can inspect the engine — in the same sense that
`OwnerAttestedSubjectBinding` records which identity rule was selected without
granting anything. Pinning it means a reader of a signed v4 policy can see what
the owner was told they were releasing, and that every signed v4 policy stops
parsing the day the family is redefined.

### Freezing

The v1/v2/v3 classes are untouched and their schema exports are byte-identical;
`test_the_first_familys_exports_did_not_move` asserts that against the checked-in
fixtures rather than against themselves. The second family is parallel classes
throughout — `WorkScalarDisclosure`, `WorkFactOutputForm`, `WorkFactRule`,
`WorkFactPolicy`, `WorkFactDecision`, `WorkOutputClassification`,
`WorkFactProjectionCandidate`, `WorkFactProjectionReview` — never a widened
Literal. Widening `FactScalarDisclosure.predicate` would have been the cheap
edit and the worst one: `prepare_fact_projection` derives the output predicate
from the payload, so every already-signed v1/v2/v3 grant would silently have
begun producing work candidates it was never granted.

The one shared piece is the lexical grammar, extracted to
`fact_contract.atomic_label_syntax` and called by both disclosures. It answers
"is this one label or is it prose", which does not depend on what the label
names; `test_both_families_share_one_label_grammar` pins the accept/reject
battery for both so the extraction is provably behaviour-preserving.

Which family a request releases comes from `FAMILY_BY_CAPABILITY`, keyed by the
signed capability, and is never inferred from the row being disclosed.

## Correlation and exclusions

One permit clause must cover every artifact/leaf table, every terminal source,
every contributing input predicate and time, and the exact output predicate.
Two partial source/table/predicate grants cannot be stitched into permission.

A deny must select this output form. Within its source/table/time selection,
either a matching contributing input predicate or a matching output predicate
vetoes the entire scalar. Derived artifacts inherit the times/sources of their
actual descendants; unrelated sibling leaves do not make an artifact match.
There is no partial recomputation or source redaction. A potentially matching
unknown deny dominates all permits; a known deny dominates unknown context.

The evaluator verifies the full row/reference/revision sets, reconstructs and
hash-checks the bounded dependency graph, rejects unreachable/cyclic/incomplete
lineage, and rebinds the exact projection to the qualified snapshot and evidence
review. Output classification cannot lower the evidence sensitivity floor.
The decision revision binds the snapshot, evidence/output review revisions,
projection/version, request anchor and time-semantics version.

These checks establish internal consistency, not authenticity. The serving
adapter must obtain fresh qualification and load current authenticated owner
reviews itself. Caller-created model objects are not capabilities. Off-limits,
native source posture/role, complete lineage and current authority remain
conjunctive requirements outside positive policy matching.

Schemas are exported under `fixtures/permissions_v2/fact_policy/`. Focused tests
cover explicit opt-in, empty sets, both output ceilings and Inference withholding,
timestamp precision/unknowns, current validity, identity axes, request expiry,
correlated rules, descendant-aware exclusions and revision substitution.

## Signed beta integration (disabled by default)

The dedicated node transport now accepts `permissions_v2_fact_read` frames with
exact `{envelope, intent}` payloads, where intent is `{query: "fact:<id>"}`. Its
signed request type is `permissions.v2.fact.read`; `FactAuthorityBinding`,
`FactEnvelopeBody` and `SignedFactEnvelope` accept exactly `permissions-beta/p2b-v1`
or `permissions-beta/p2b-v2` (`FactCapability`). Concrete P2a parsers retain
their original closed contracts. An explicit registry dispatches the known P2a and
P2b capability literals and rejects unknown ones without fallback. The mutation/ACK and
node-result schemas carry the corresponding closed authority union; canonical
signing domains and old P2a golden signature bytes remain unchanged.

A grant cannot change capability, including after revocation. P2b requires a new
explicitly approved grant/assignment. Signed requests must fit both the current
policy lifetime and the 120-second envelope limit. The signed issuance timestamp
alone supplies the event-window anchor. `FactProjectionRelease` admits exact
current authority, loads fresh evidence plus both authenticated owner reviews,
evaluates one complete clause, checkpoints a one-shot private decision receipt,
and signs only the exact six-field `FactScalarDisclosure`. There is no raw-source,
generic-query, Inference, model, or prose fallback.

Actual WebSocket send runs while the process write gate, canonical read snapshot,
and both private review-store write transactions remain held. The task that
invokes `ws.send` rechecks expiry, the feature flag, runtime identity and current
CP signing key immediately before invocation. Cancellation drains that actual
send before releasing gates. Send-start is the linearization boundary: bytes
already accepted by the transport cannot be recalled. This guarantee assumes
canonical writers use the node write gate; it does not authorize external direct
SQLite writes against a serving node. Errors expose only `permission_denied` and
consume admitted requests when execution or delivery is uncertain.

`TOPOS_PERMISSIONS_V2_FACT_RELEASE_ENABLED` is a separate default-off flag. The
generic handler always denies, and the dedicated relay interception returns no
content to the generic outbox. Code and synthetic tests do not enable a public
service or certify a full client profile. The registry's default capability
metadata remains non-executing; deployment/profile advertisement and CP final
forwarding must be explicitly paired before any recipient can use the feature.

The signed fixture at `fixtures/permissions_v2/fact_policy/signed-golden-v1.json`
binds a synthetic policy, request, mutation/ACK and six-field node result with
fixed public verification keys. `signed-golden-v2.json` does the same for a
stated-day policy over a fact whose validity is a calendar day; it is rebuilt by
`python -m tests.permissions_v2.test_fact_stated_day --write-golden` under the
test environment and verified by both the engine and control-plane suites. Regression coverage includes signed activation,
status/restart/revocation, same-grant capability rejection, independently bounded
policy expiry, exact result typing, both review revisions, Off-limits/protection
ABA, source changes, unknown/future event time, denied/empty rule selections,
request replay, and mutable send-task races on both source and fact transports.
