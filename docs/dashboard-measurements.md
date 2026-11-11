# Owner dashboard measurements v1

The personal SQLite node records first observations at storage commit. The control plane exposes `GET /v1/dashboard/measurements?period_start=YYYY-MM-DD&period_end=YYYY-MM-DD` (exclusive end, at most 366 days; `&time_zone=` optional, see Day boundaries) to the verified first-party owner. The owner-only `get_dashboard_measurements` relay handler runs off the event loop. Shared hosted pools are unavailable: facts do not carry a reliable tenant partition.

## Day boundaries

`time_zone` (optional, an IANA name, at most 64 characters) sets where days end. Each bucket covers local midnight to
local midnight in that zone, so a DST day is 23 or 25 hours and half-hour offsets fall on the right day. The response
always carries `time_zone`: the zone the buckets were actually cut in. A missing, malformed or unloadable zone (the
node may lack tzdata) is `"UTC"`. A response without the field comes from an older node and its days are UTC. Instants
(`as_of`, `measured_since`, `calls[].at`) are UTC ISO timestamps in every zone. Request-activity `blocks` are four-hour
spans of the local day. Coverage days (the activation day and today are partial) are local days too.

## Definitions

- **Inbound records:** new canonical messages, activity events, calendar events, journal entries, profile records, financial transactions, location events, documents and transcript sessions. Counted by table and its stable primary key, grouped by source at first insertion. These are stored records, not raw export items or files. Parent conversations, contacts, transcript speakers and segments are excluded to avoid counting structural fan-out.
- **New facts:** first observation of an active `signal_objects` fact's logical `object_key`, grouped by signal dimension. Revisions, corroborations, conflicts returning an incumbent, and inserted closed historical revisions do not add new facts. Fact attribution by inbound source is not claimed.
- **Bytes:** unavailable. File-transfer size, local cursor offsets and canonical text length are different measures; none is presented as total inbound volume.

The identity ledger has no copied payloads. It intentionally keeps identity receipts after source-row deletion, so re-imports cannot inflate the counts. Counts describe arrivals, not current inventory. Identity-changing updates also reserve their new identity without an arrival. SQL triggers and receipts commit or roll back together, including replacement writes and concurrent retries. Keep the ledger as owner data: logical fact keys can themselves contain values.

## Coverage

Explicit source erasure removes its source-attributed receipts too, invalidating coverage for the affected family. Until instrumentation re-baselines the remaining identities, that metric is unavailable. Re-baselining starts a new observation window; erased history is never presented as measured zero. A later import of deliberately erased data belongs to that new window.

Explicit source or record erasure also removes value-bearing logical-fact receipts when no fact revision with that key survives. Shared evidence and surviving revisions retain their receipts. This invalidates fact coverage until re-baselining; ordinary pruning, replacements and revisions retain deduplication.

Migration 88 seeds existing identities with NULL observation times. It never reconstructs historical arrivals from mutable `ingested_at`, event time or fact validity time. Triggers record UTC observation time. Earlier dates are `count: null, coverage: unavailable`, the activation day and current day are partial, and only fully covered days may be measured zero. Lazy-created canonical tables get triggers before their writer runs. Aggregate coverage conservatively starts at the latest covered family start. A missing trigger makes its metric unavailable; repairing it baselines intervening rows and restarts coverage. Queries do not create schema.

## Rollout

Land/release the node migration with the node's normal backup and downgrade guard. Then advance the control-plane compatibility pin and deploy its owner relay endpoint; deploy the dashboard client. Old/offline nodes and missing endpoints remain unavailable, never fixture values or zeroes. This change does not migrate a running owner's database during development.

### Verification outcomes

The additive `verify_claim` block uses population `answered_verify_claim_calls`.
Each daily bucket contains `count`, `coverage`, and `outcomes` with integer counts
for `true`, `false`, `unanswerable`, and `unavailable`. The four counts sum to the
bucket total. Unmeasured days have null count/outcomes; measured empty days are zero.
Only answered app/owner verification-door calls are included, including labelled
development callers. Automatic inbound checks, signed-grant checks, refusals, and
failed requests are outside this population, matching the usage-log contract.

A nullable `verification_outcome` column is added by the request log's existing
idempotent write-time schema upgrade. The same usage row stores the outcome; there
is no second event or claim/evidence text. Historical rows are never guessed or
backfilled. Read-only dashboard requests do not migrate the schema.

Owner verdicts map `supported` → true, `contradicted` → false, and `no_evidence` →
unanswerable. `unable` (the deadline passed or the model failed) records no outcome:
the call still counts, under unavailable, because a timeout says nothing about the claim. The older lane response maps non-conflicting support or
contradiction to true/false; no evidence, explicit abstention, or conflicting lanes
is unanswerable. Unknown/malformed schemas remain unavailable. This reporting
adapter does not modify the answer, evidence selection, or verifier behavior.

The Essentials bar uses all recorded calls as its denominator and keeps unavailable
outcomes as a separate hatched portion. Its outcome shades follow the selected Essentials accent; labels and segment order retain their meaning.
Older nodes without this block can still show usage totals with unavailable outcomes.

### Provisional self-coherence

The additive `self_coherence` block is owner-only through the same dashboard door.
`method: literal-claim-consistency-v1` is a descriptive measure of **literal statement
agreement**, not truthfulness, hypocrisy, independent corroboration or a confidence
estimate. All scores remain provisional; merely collecting more of the same narrow
sample does not validate broader interpretations.

For each displayed day, consider the preceding 30 local calendar days (inclusive),
using the later of the retained check timestamp and source timestamp. Compare the
owner's surviving, undismissed claims whose check completed (`supported` or
`no_evidence`). Exclude plans, owner-separated groups, and claims currently labelled
false or partly true. A true label does not add a vote or prove a statement.

Group by category, complete normalized wording and period. Preserve numbers, word
order, names and internal punctuation; never use the existing lossy `claim_key` as
a semantic identity. A stored reading supplies a closed explicit date interval;
without one, only literal repeats on the same owner-local day qualify. Open periods
and malformed readings are excluded. Reported speech and non-owner subjects in a
stored reading are excluded. Legacy readings without structured subject metadata
remain a coverage limitation. Duplicate source identities and identical source
instants within one proposition do not create extra observations.

A narrow direct assertion of the form `I/the owner have/had/was [not] …`, classified
as `did_or_made` with the same closed explicit period, can produce a **potential
tension**. The remaining words must match exactly; extra negations, conditionals,
modal or sequencing words prevent that comparison. This is not confirmation of
a contradiction: an event may have happened between two statements even within
one date interval. V1 does not detect arbitrary paraphrases, numeric disagreements,
implicit negations, intentions versus actions, or semantic changes of mind.

Each group with at least two distinct source instants gets one vote. A group with
one polarity is aligned; one with both is potential tension. The score is:

`aligned_groups / (aligned_groups + tension_groups)`

No comparable groups means `score: null, status: insufficient`; absent/unsupported
tables, an oversized scan (over 50,000 retained owner claims), and future days are
`unavailable`. Never display missing measurements as zero or one. Counts include
`owner_claims`, `eligible_claims`, `compared_claims`, `aligned_groups` and
`tension_groups`. The client validates this denominator and displays coverage beside
the score. A perfect score from very few groups is explicitly provisional.

`history_basis: current_retained_checks` means a recomputed view, **not an immutable
record of what was known on each date**. Corrections, erasure, relabelling and rechecks
can revise earlier points. Source existence is checked inside the dashboard's read
snapshot. No copied claim payload, new schema, background job, model call, label
write or change to verification behavior is introduced. The API returns aggregates
only. Unsupported optional metric versions do not break the other client metrics.

Changing matching rules, exclusions, window or formula requires a new method
identifier and paired synthetic fixtures. Preserve method identifiers in exported
weekly snapshots; never join scores from different methods into one unlabeled trend.
The first release tests repetitions, dates/timezones, numeric/order preservation,
explicit negation, missing periods, failures, corrections, duplicate imports,
erasure, future checks, expiration, contract parsing and the home-screen connection.
