# Intelligence-exclusion floor and clock v2

This beta upgrade makes intelligence tombstones effective at the read boundary,
including the interval after `exclude_record` commits its tombstone and before
its derived purge finishes. Every contributing record/fact is checked; known
nonmatching record/fact/stat tombstones preserve availability. Fact matching
retains native predicate/value normalization and conservatively matches known
owner aliases. Unknown kinds, malformed tombstones, missing schema and read
errors withhold. No lifecycle tombstone is deleted or rewritten.

Any entity exclusion withholds this uncertified fact family, because entity
exclusion deliberately removes mention observations while preserving canonical
text. A missing mention after exclusion is not an absence certificate. Entity
Off-limits remains a separate dominating veto. No entity-coverage relaxation or
owner-only declassification is introduced. Owner preview remains owner-only.

Clock v2 adds a required `contract_version=2` column to
`permissions_v2_protection_state` and adds three canonical-transaction triggers
for `intelligence_exclusions`. Every insert/update/delete advances the same
clock's generation. The current protection revision includes the complete strict
exclusion fingerprint and version; remove-after-add ABA invalidates prior
reviews/envelopes even without an intervening reader. No raw values enter a
signed proof, receipt or public diagnostic.

## Explicit migration of an initialized beta node

Only upgrade an isolated node whose recorded prior artifact proves a complete v1
clock. Stop serving and all canonical writers first. Keep the stopped canonical
DB and its private ledger/review/enrollment stores together for backup. Do not
infer "v1" from missing fields on a deployment already recorded as v2; damaged
or restored v2 state requires recovery investigation.

The operator can invoke this standalone helper using the new engine environment,
without importing/starting the app or loading models:

```python
from pathlib import Path
from topos.permissions_v2.protection_clock import upgrade_protection_clock_v2
result = upgrade_protection_clock_v2(
    Path(EXPLICIT_STOPPED_BETA_DATABASE),
    owner_id=VERIFIED_PINNED_OWNER,
    expected_clock_id=RECORDED_V1_CLOCK_ID,
    expected_generation=RECORDED_V1_GENERATION,
)
```

The caller must supply the already verified pinned owner and clock values from
the stopped instance and its prior artifact manifest. The helper validates full
legacy clock columns, exact six old trigger definitions, owner binding, expected
clock/generation and existing exclusion schema/state. It preserves clock ID,
increments generation once, adds the version and three triggers in one SQLite
transaction, and verifies the new clock before committing. Repeating the same
upgrade against intact v2 is an idempotent read; partial triggers/metadata,
changed clock ID or stale generation are rejected. This is an explicit migration
operation, never a runtime read fallback or authorization from an arbitrary JSON
object. A privileged operator who replaces all canonical and durable provenance
cannot be detected by a database-only check.

Before/after assertions: original user content, row IDs, owner identities,
`owner_only_records`, `entity_blackholes` and `intelligence_exclusions` are byte/type
unchanged; the clock ID is identical; generation increases exactly once; the
only schema changes are the clock-version column and three named triggers. Record
the resulting schema/clock version with the tested engine artifact. Existing
lifecycle schema must already have run through the normal migration mechanism;
this helper never creates a lost exclusion table.

On restart the existing ledger opens with its recorded prior revision; actual
protection synchronization advances its epoch to the new revision before serving.
CP must accept a fresh authenticated node status. Prior evidence/output reviews
and issued requests remain stale; obtain new owner reviews before testing an
unaffected positive. Revoked grants stay revoked. No grant, owner review, key or
store is reset by this operation. Signed policy/ACK/result schemas do not change.

Rollback means disabling beta release. An older v1 engine rejects the v2 trigger
set and cannot serve against this upgraded state. Do not drop triggers, decrement
generation, strip the version column or reverse-migrate to make older code run.

## Clock v3: closure-scoped review binding

Clock v3 keeps the single monotonic generation and adds an event log that the
engine only ever appends to (v3 had no trigger refusing a direct delete or
update of its rows; v4 adds them), `permissions_v2_protection_events(sequence, generation, source,
artifact_key)`, written by the same nine triggers in the same canonical
transaction. Each insert, update or delete on `owner_only_records`,
`entity_blackholes` or `intelligence_exclusions` records the generation it
advanced to and the touched artifact (`canonical_table|record_id`, the
blackhole id, or `artifact_type|artifact_key`; an update records both the old
and the new key). No note, name or content is copied into the log.

Owner review snapshots bind `closure_protection_revision`: the protection and
tombstone state of their own records and fact keys plus the latest event that
touched them, with entity floors kept node-wide. Before v3 a review bound the
node-wide revision, so protecting one unrelated record staled every review on
the node; the event log lets a closure-scoped binding still detect a
protect-then-lift of its own record. Signed authority, envelopes and receipts
keep the node-wide `current_protection_revision`, which now carries
`contract_version: 3`.

### Explicit migration of an initialized v2 node

Stop serving and every canonical writer, keep the canonical DB with its
private stores together for backup, then run the standalone helper with the
new engine environment:

```python
from pathlib import Path
from topos.permissions_v2.protection_clock import upgrade_protection_clock_v3
result = upgrade_protection_clock_v3(
    Path(EXPLICIT_STOPPED_BETA_DATABASE),
    owner_id=VERIFIED_PINNED_OWNER,
    expected_clock_id=RECORDED_V2_CLOCK_ID,
    expected_generation=RECORDED_V2_GENERATION,
)
```

The helper validates the exact v2 columns, the nine v2 trigger definitions,
owner binding, expected clock identity and generation, existing exclusion
schema and the absence of an event table. It rebuilds the state table because
the v2 CHECK constraint pins the version, preserving the clock identity and
advancing the generation exactly once, creates the event log, replaces the nine
triggers and verifies the v3 clock before committing. Repeating it against
intact v3 is an idempotent read; partial triggers, a changed clock id, a stale
generation or a stray event table are rejected. Before/after: every other
table's rows and schema are unchanged; the only schema changes are the rebuilt
state table, the event log and the nine trigger bodies.

On restart the ledger reopens with its recorded revision and protection
synchronization advances its epoch once. Existing owner reviews are stale by
design because their snapshots bound the former node-wide revision; obtain new
reviews. Review stores need no re-enrollment: their durable identity and clock
high-water are unchanged and the generation only moved forward. An older v2
engine rejects the v3 trigger set and cannot serve this state; rollback means
disabling beta release, never dropping the event log or reversing the clock.


## Clock v4: owner identity, and a log that cannot be edited

v4 adds the storage and the detection that owner identity binding needs
(`OWNER_IDENTITY_BINDING.md`), and closes the v3 gap where the event log was
append-only by convention rather than by rule.

Three tables are now append-only by trigger: the event log,
`permissions_v2_identity_attestations` (the owner's consent ledger) and
`permissions_v2_identity_subjects` (the restriction registry). `BEFORE UPDATE`
and `BEFORE DELETE` raise on each. An event log that can be emptied cannot prove
that a protect-then-lift ever happened, and an attestation ledger that can be
edited is not a consent record at all.

Seventeen identity triggers are added, all conditional so that enrichment
writers fire nothing:

* `entities` insert, delete, and update of `entity_id`, `entity_type`,
  `is_self` and `contact_id`, scoped to self rows and tracked ids.
* `entity_merge_tombstones` insert, update and delete when either side is a
  tracked owner spelling.
* `entity_mentions` update of `entity_id`, deduplicated per generation.
* `signal_objects` update of `object_key` on a fact, logged as
  `fact_rekeyed|<object_id>`. The merge remap is the only writer of that column.
* the ledger's own insert, which advances the generation, registers the id and
  logs `identity|<id>`, plus two ordering guards: one live attestation per
  entity, and a revocation that must name the current entry.

`current_protection_revision` gains an identity fingerprint and the coverage
list, so an identity change stales signed authority uniformly for every
capability. `closure_protection_revision` becomes `closure-protection/v2`, with
a closure-scoped identity section and the raw fact-prefix list removed because
it churned. The state table pins `contract_version=4`.

### The event log admits generation zero

v3 stamped every event from a trigger that had just advanced the clock, so a
generation of zero was impossible and its table carries `CHECK(generation>0)`.
v4 also logs identity churn that must *not* advance the clock: a merge moves
many mentions and re-keys many facts, and the merge's own tombstone advances the
generation once. Such an event can land while the clock is still at its
installed zero, and refusing it would abort the node's own merge. v4 therefore
uses `CHECK(generation>=0)` and the upgrade rebuilds the table, preserving every
row and its sequence, so a hash chain folded over the log before the upgrade
folds to the same value after it. The table is rebuilt by `CREATE` and copy
rather than by `ALTER TABLE ... RENAME`, because SQLite stores a renamed table's
schema with the name quoted and the clock compares that text byte for byte.

### Coverage is recorded, not assumed

`entities`, `entity_mentions` and `signal_objects` belong to the engine's own
schema. Requiring all three at install would couple the permission floor to the
entity spine migration, so the clock watches whichever of them the node has and
records the list in the protection revision.

A table that **appears** later is a coverage change: the expected trigger set no
longer matches and every read fails closed until `resync_identity_coverage` runs
on a stopped node. That lane only adds and removes identity triggers; it never
creates an engine table, never touches the ledger, the registry or the event
log, never changes the contract version, and refuses a clock whose other
triggers were altered or lost. A table that **disappears** is also a coverage
change, and it moves the protection revision, so every authority signed while it
was watched goes stale.

### Explicit migration of an initialized v3 node

Stop serving and every canonical writer, keep the canonical DB with its private
stores together for backup, then run the standalone helper with the new engine
environment:

```python
from pathlib import Path
from topos.permissions_v2.protection_clock import upgrade_protection_clock_v4
result = upgrade_protection_clock_v4(
    Path(EXPLICIT_STOPPED_BETA_DATABASE),
    owner_id=VERIFIED_PINNED_OWNER,
    expected_clock_id=RECORDED_V3_CLOCK_ID,
    expected_generation=RECORDED_V3_GENERATION,
)
```

The helper validates the exact v3 columns, the twenty-six v3 trigger
definitions, owner binding, expected clock identity and generation, the v3 event
table text, and the absence of a ledger or registry. It rebuilds the state
table, rebuilds the event log under the v4 schema carrying every row, creates
the ledger, the registry and `entity_merge_tombstones` when the node has never
merged, seeds the registry from the node's current self rows and merge
tombstones, installs the v4 triggers for the coverage this node has, and
verifies the clock before committing.

Seeding matters: an owner who excluded `<entity>:prefers` before this upgrade
keeps that veto afterwards only because the registry starts from what the node
already holds. Starting empty would silently drop those vetoes until the next
identity event.

Before and after, every other table's rows and schema are unchanged, with the
single exception of `entity_merge_tombstones` when the clock had to create it,
which must arrive empty. Existing owner reviews are stale by design: the closure
revision changed. Review stores need no re-enrollment. An older v3 engine
rejects the v4 trigger set and cannot serve this state; rollback means disabling
beta release, never dropping the ledger or reversing the clock.

The lab runs both operations through
`scripts/permissions_beta/upgrade_protection_clock.py --contract 4` and
`--resync-coverage`, on a stopped synthetic engine, with the unchanged-elsewhere
proof above.


## Bookkeeping: the event-log indexes and the cached revision (17 Sep 2026)

Two changes from the scaling assessment that alter no contract and no value.

**The event log carries two indexes**, `permissions_v2_protection_events_artifact` on
`(artifact_key, generation)` and `permissions_v2_protection_events_source` on
`(source, artifact_key, generation)`. They answer the re-key and mention trigger
probes, the owner's identity lookups and the closure revision's event filters as
seeks instead of scans of the whole log (a 20,000-fact merge: 38.5 s to 0.77 s
against an empty log; three hours to 1.2 s against 500,000 events). They are not
part of the contract `clock_state` verifies, which compares the state row, the
trigger text and the table declarations and never an index, so dropping them or
adding a stray one changes no clock state and no revision, and an engine that
predates them serves a clock that carries them. They are created with the event
table at install, rebuilt with it by `upgrade_protection_clock_v4` and kept by
`resync_identity_coverage`, and added to an existing clock by
`ensure_protection_clock` at the next node start -- after the clock has verified,
inside the same transaction, so a clock the node refuses gets no DDL. Nothing
about the stopped-node lanes above changes: they need no extra step, and a lab
that runs `upgrade_protection_clock.py --contract 4` gets the indexes from the
rebuild. What guards the log's content is unchanged: the canonical floor's chain
folds the table by `sequence` and the append-only triggers refuse edits;
`PRAGMA integrity_check` is the operator-side check for an index that disagrees
with its table.

**`current_protection_revision` is remembered per canonical database file**, keyed
by the clock identity and generation, SQLite's `schema_version`, the identity
coverage and the restriction registry's row count, so the three whole-table
fingerprints it folds (Off-limits rows and black holes, exclusion tombstones, the
consent ledger with the registry) are computed once per owner mutation rather than
once per read. Every table it folds is watched by the clock, so any SQLite write to
one of them misses the cache through the generation; DDL misses it through
`schema_version`; a registry row written outside a trigger through the count; a
second database file at the same generation has its own entry. `_floor_schema`
and `clock_state` run on every call before the cache is read. The cache is
process-local and never written to disk. The exclusion fingerprint itself is now
streamed (`canonical.digest_stream`/`MappingRows`), so the floor no longer refuses
at about 5,000 tombstones; its value is byte-identical to the built digest.

