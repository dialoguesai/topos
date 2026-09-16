# Owner identity binding

## The problem this exists to fix

A producer writes a fact's subject as an entity id. The first permissions v2
fact family could only release a fact whose subject was the literal string
`self`, because that was the only spelling the node could prove denoted the
owner. Measured against a real corpus, no production producer writes that
literal, so the family could describe a capability it could never exercise.

The obvious repair — "treat the `is_self` entity as the owner" — is wrong in
both directions:

* A node legitimately holds several `is_self` rows. Picking one is a guess, and
  a guess here releases one person's data under another person's name.
* Entity resolution merges. A merge re-keys the absorbed entity's facts onto the
  surviving entity. If the survivor is the owner, another person's fact silently
  becomes an owner-subject fact, carrying the owner's own messages as evidence.

So identity cannot be inferred. It has to be **attested**: the owner says, per
entity, "this is me", and the node detects when that statement stops being true.

## Two sets, never one

| | permit set `P` | restriction set `R` |
|---|---|---|
| answers | whom may a release be about | what does an owner veto match |
| contains | only what the owner attested | every spelling the node has seen |
| on ambiguity | refuses | never refuses |

`P ⊆ R` always, and it is property-tested across every identity state. This is
the load-bearing invariant: widening whom the owner may release about can never
narrow what an owner restriction covers.

* `P_legacy` = `{"self", E}` where `E` is the sole `is_self` row, or a refusal.
  This is a verbatim copy of the pre-binding rule, it never reads identity
  state, and every capability that existed before the binding still uses it.
* `P_attested` = `{"self"}` (unless shadowed) ∪ every active, valid attestation.
* `R` = `{"self"}` ∪ every current `is_self` row ∪ every registry id ∪ the
  merge-tombstone fixpoint over those. The old `{"self"}` fallback is deleted:
  it silently dropped every entity-keyed owner tombstone on exactly the
  multi-self nodes that needed it most.

An entity row whose id is literally `self` shadows the producer constant. The
two meanings are then indistinguishable, so the attested contract withholds the
literal rather than guessing which one a fact meant.

## Which rule applies

The contract comes from the signed capability, never from a caller:

| capability | subject contract |
|---|---|
| `permissions-beta/p2a-v1` | `legacy_single_self_v1` |
| `permissions-beta/p2b-v1` | `legacy_single_self_v1` |
| `permissions-beta/p2b-v2` | `legacy_single_self_v1` |
| `permissions-beta/p2b-v3` | `owner_attested_v1` |

`QualifiedEvidence.subject_contract` records which rule qualified the evidence,
and the policy preparation refuses a policy whose capability maps to a different
one. Every entry point defaults to the legacy rule, so a caller that forgets the
argument can only ever reproduce today's behaviour.

## Where consent lives

Protection clock contract **v4** adds two canonical tables:

* `permissions_v2_identity_attestations` — the append-only consent ledger. Ids,
  digests, a statement version. No names.
* `permissions_v2_identity_subjects` — the append-only restriction registry,
  with the basis on which each id was recorded.

Both are hidden from every database explorer surface, and both are append-only
by trigger: `BEFORE UPDATE` and `BEFORE DELETE` raise. The event log carries the
same guards, because an event log that can be emptied cannot prove that a
protect-then-lift ever happened.

Canonical storage rather than a private store, because one read transaction then
binds facts, entity rows, attestation state and protections together, and SQLite
triggers see an identity change inside the writer's own transaction. A read-time
pin misses a self row created and deleted between two reads.

## What the clock watches

Triggers, all conditional so that enrichment writers fire nothing:

* `entities` — insert, delete, and update of `entity_id`, `entity_type`,
  `is_self`, `contact_id`, scoped to self rows and tracked ids.
* `entity_merge_tombstones` — insert, update, delete when either side is an
  owner spelling.
* `entity_mentions` — update of `entity_id`, deduplicated per generation.
* `signal_objects` — update of `object_key` on facts, logged as a re-key. The
  merge remap is the only writer of that column.

Names, aliases, identifiers, mention counts and metadata are deliberately not
watched. They change on every enrichment batch, and binding them would decay
consent to noise. Merge membership is watched, because it does not change on its
own: it changes when this entity absorbs another or is absorbed, which is
exactly when the owner should look again.

### Coverage is recorded, not assumed

`entities`, `entity_mentions` and `signal_objects` belong to the engine's own
schema. Requiring all three at install would couple the permission floor to the
entity spine migration, so the clock instead watches whichever of them the node
has and records that list.

* A table that **appears** after install is a coverage change. The expected
  trigger set no longer matches, so every read fails closed until
  `resync_identity_coverage` installs the missing triggers on a stopped node.
* A table that **disappears** is also a coverage change, and it moves the
  node-wide protection revision, so every authority signed while it was watched
  goes stale.

`resync_identity_coverage` only adds and removes identity triggers. It never
creates an engine table, never touches the ledger, the registry or the event
log, never changes the contract version, and refuses a clock whose other
triggers were altered or lost.

### v4 admits generation zero in the event log

v3 stamped every event from a trigger that had just advanced the clock, so a
generation of zero was impossible. v4 also logs identity churn that must *not*
advance the clock: a merge moves many mentions and re-keys many facts, and the
merge's own tombstone advances the generation once. Such an event can land while
the clock is still at its installed zero. Refusing it would abort the node's own
merge, so the v4 event table admits zero and the v4 upgrade rebuilds the table
under the new schema, preserving every row and its sequence.

## Attestation validity

An entry is `active` only while every pinned identity column still matches, its
composition digest is unchanged, and no identity event landed after the
attestation's own generation. Anything else is `stale`: the owner attested
something that has since moved, and only the owner can say whether it still
denotes them. Quarantine is not forgetting — a stale entry leaves the permit set
and stays in the restriction set.

Revocation is terminal. Re-attesting mints a new entry at a higher generation.

There is no merge refusal in native code: owners legitimately merge duplicate
self rows, and a permission layer that aborts that write is a permission layer
people turn off.

## Taint

A fact with any `fact_rekeyed` event is permanently ineligible under the
attested contract. This is the overlay signature: another person's fact re-keyed
onto the owner's entity, carrying the owner's own messages as its evidence. The
owner can state the claim again, which writes a new fact with its own lineage.
Legacy capabilities are unaffected, because the rule is new and does not change
what an existing grant meant.

## Staleness

`EvidenceSnapshot`'s schema is unchanged. Its `protection_revision` becomes
`closure-protection/v2`: the previous fields, minus the raw prefix list that
would churn, plus a closure-scoped identity section. For each subject the
closure actually names, that section carries its registry state, its last
identity event, its event count, its entry id, generation and state, and the
closure's re-keyed facts. Attesting an unrelated entity, or a merge elsewhere on
the node, leaves the value unchanged.

The node-wide `current_protection_revision` gains an identity fingerprint and
the coverage list, so an identity change stales signed authority uniformly for
every capability. A recipient cannot tell an identity change from an Off-limits
change by which of their grants went stale.

## What never leaves the node

Entity ids are not labels. A review's `subject_entity_ids` stays `["self"]`
under the attested contract — the vocabulary does not change with the binding,
and `self` means "about me" while the contract resolves which entities that
covers. The permit set is derived beside the evidence in the same read
transaction and passed by value into the projection. It is never stored in a
candidate, a review, a receipt or an error, so no entity id reaches the control
plane, the frontend or a recipient. Output subject stays the literal `self`.

A refusal the owner sees can distinguish `owner_subject_unattested` from
`not_owner_self_statement`; a recipient sees one refusal either way.

## Residual risks

* Consent is asserted through the control plane; the owner holds no signing key.
* A consistent whole-node restore is not detected from inside the node.
* A hand-written merge with no mentions, no fact re-key and no tombstone leaves
  no trace to observe.
* Mis-attestation and review fatigue are human failure modes the design cannot
  remove, only keep reversible.
* Native writes are coupled to clock health: a damaged clock aborts the writes
  the triggers sit on.
* Binding alone still yields zero copied-corpus and zero production positives.
  The disclosure, predicate-family and lineage gates remain in front of it.
