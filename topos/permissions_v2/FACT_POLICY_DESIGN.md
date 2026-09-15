# P2b fact policy foundation, v1

The pure policy parser and evaluator exist. This foundation alone enables no
recipient route, ledger output, grant migration or natural-language evaluation.
Serving requires explicit signed P2b dispatch and current owner output review.

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

The future signed transport supplies `envelope.issued_at` as `request_as_of`.
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
