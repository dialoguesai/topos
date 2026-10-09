# Safe share-index refresh (BL-155, planned for 1.5.2)

## Product problem

The node continuously ingests and assesses evidence. Catch-up normally visits
changed conversations; upgrades, changed rules/proofs and scheduled full-window
passes can require broader reassessment. The share index itself is currently
rebuilt as a complete snapshot, even for ordinary new assessments. In 1.5.1 its
node-wide review digest also acts as a serving invalidation: an unrelated
assessment can delete a valid share index before its replacement is ready.
Continuous assessment therefore creates repeated answering gaps beyond upgrades.

Sharing needs two guarantees: existing permitted evidence remains available
during routine growth; restrictions take effect before another record or answer
is released. Freshness can lag by a scheduling interval, but permission and
evidence validity cannot lag with it.

## Decision and invariants

For p2c-v3, retain a published snapshot only when all its indexed dependencies
remain valid under current signed authority. Build the complete replacement
beside it, validate at publication, and atomically replace it using the existing
publication path. Recipient requests never build, assess or expand a share.

1. **Safety:** releases still check live consent, policy, expiry, protection,
   provenance, source rows, classification contexts and projection dependencies.
2. **Availability:** reviews outside the snapshot's dependencies move freshness
   without removing safe evidence. A failed build alone is not a restriction.
3. **Completeness:** builds qualify the complete frozen candidate set and apply
   the signed cap. An `over_cap` publication replaces the previous ready snapshot;
   no partial-set fallback evades the cap.
4. **Local control:** sealed bindings stay on the node. There is no new hosted
   service, database daemon, model call, wire contract or package dependency.

## Serving basis and freshness signal

The whole review-store digest stays in the index basis. Enrollment and rollback
verification remain mandatory. Its movement signals refresh; it no longer proves
by itself that every member became unsafe.

Each new member contains `review_bindings` inside its existing AES-GCM envelope.
For its own evidence and every projection source, bind both the machine-review
key and the owner-correction key to exact current revisions. Absence is a binding
too: introducing a correction cannot pass because none existed at build time.
Fact projections also bind their own explicit fact review, including its absence.
Revisions include review identity and time, so even an identical reassessment of
indexed evidence invalidates this first implementation. It does not attempt
semantic review equivalence.

The nonidentifying `review_guard_version` and `opt_out_revision` are metadata.
Any opt-out set change remains a conservative global invalidation, including
undo. The guard checks coverage and values; missing, duplicate, unexpected or
unreadable bindings fail closed. Review reads are indexed point lookups, cached
within the check. Whole-store integrity verification retains its existing cost.

When the global digest differs, serving proves the exact review bindings, then
runs the existing canonical member checks. This relaxes only unrelated review
movement. Protection clock, boundary, policy, model/rubric and family revisions
remain hard boundaries. Scrub/removal, proof changes and projection dependency
changes retain their existing refusal and deletion paths.

| Change | Published snapshot | Index work |
| --- | --- | --- |
| Independent new assessment | Continue live-checked serving | Coalesced refresh |
| Assessment/correction of indexed evidence | Refuse and discard | Rebuild |
| Any opt-out change | Refuse and discard | Rebuild |
| Policy, protection, boundary or model/rubric change | Existing invalidation | Rebuild; no fallback |
| Canonical row/context/projection dependency change | Existing guard refuses | Discard and rebuild |
| Build fails while old snapshot is proven safe | Continue serving | Bounded retry |
| Complete replacement exceeds signed cap | Refuse | Publish `over_cap`; replace old ready snapshot |
| Revocation or expiry | Refuse | Remove index; existing key lifecycle |

## Builder and scheduler

The existing builder freezes reviews, canonical floor and clock, qualifies and
ranks outside the write gate, then checks and publishes under the gate. A review
arriving during a build can pass publication only if the built members' review
bindings and global opt-out set are unchanged. Existing authority and canonical
dependency rechecks remain. An over-cap build retains the stricter whole-digest
publication check. The cap applies to the complete frozen set, rather than newly
arriving evidence before its next snapshot is built.

The daemon sweep reports a safe older snapshot as `refresh_needed` without
deleting it. Observations bind the target digest to the observed file identity,
so replaced files cannot queue stale work. The refresh loop uses its existing
queue, BUILD_SLOT, debounce, rate limit, most-read ordering, owner queue
coordination and receipts. `review_added` refreshes may run during a progressing
assessment pass: independent arrivals no longer cancel them. Other invalidations
retain the assessment deferral rules.

Repeated observations preserve debounce and backoff. A new target during a build
owes one further build. Exponential retries remain bounded by `max_attempts`;
exhausted target digests persist in the existing private refresh-state file so
sweeps and restarts cannot reset the budget indefinitely. A different target or
owner publication permits a fresh try. After exhaustion, safe evidence remains
available but freshness requires a new change or owner action. The next sweep
rediscovers dirty snapshots after restart; no durable change log is needed yet.

Failed attempts and worker exceptions retain old files only after the full
current guard proves them safe. Unknown guards and failed checks remove them.
A request discovering a stale file removes only that observed file identity,
so it cannot accidentally shred a concurrent replacement.

## Answers and observation

An answer records the ordered opaque evidence IDs selected before generation.
After generation and at fetch, it revalidates those IDs against the current index
and live release checks. Independent additions can change ranking without
cancelling a supported answer. Missing evidence, changed output, restrictions,
authority changes and rolling-window expiry still withhold it. The IDs neither
pin an old database nor bypass the current member guard.

Recipients retain uniform `no_answer`. Owner-local receipts distinguish
`index_unavailable` from `nothing_matched` and unsupported answers. Refresh
receipts identify `review_added` with counts and codes, without record text or
review bindings. Census-copy validation understands the new basis but still
requires the latest whole-store digest: a safe older serving snapshot must not
be mistaken for a fully refreshed evaluation corpus.

## Compatibility and release scope

This branch makes no capability, schema or package version bump. Merge with
other planned changes when preparing 1.5.2. A p2c-v3 index without the new guard
retains strict whole-digest behavior until its next ordinary rebuild writes the
bindings. Unknown guard versions fail closed. Retired v1/v2 profiles retain
their prior behavior. Rolling back to 1.5.1 refuses the new basis and needs an
ordinary rebuild; it cannot accidentally serve under the relaxed basis.

This release fixes safe snapshot availability. It does **not** implement per-row
incremental indexing or guarantee availability through restrictions, indexed
reassessment, first build, boundary-changing upgrades or cap overflow.

## Next architecture step

Keep one authoritative snapshot per grant and one serialized builder. After
measuring refresh cost and freshness lag, introduce a durable monotonic change
cursor and dependency-to-member map. Explicit change classes can replace/remove
affected members, re-qualifying them through the same release decision. Recompute
complete-set membership/caps before atomic publication; an append is never
permission. Full builds remain the recovery path for lost cursors, format/model/
rubric changes and unknown mutations. This needs separate performance and privacy
evidence before replacing the simpler snapshot builder.

## Verification

Synthetic production-schema tests exercise signed search and answers, independent
reviews before/during builds, corrections, opt-outs, content and owner-only
changes, cap overflow, failures, restart recovery, bounded retries and answers
generated while the index changes. Permission, journal, projection, interest,
census and refresh suites remain regression gates. Live quality and availability
measurements belong to the release acceptance run after merge.
