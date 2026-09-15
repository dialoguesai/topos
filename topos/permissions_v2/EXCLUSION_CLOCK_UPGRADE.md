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
engine only ever appends to (no trigger yet refuses a direct delete or update of
its rows; see the open issues in the release checkpoint), `permissions_v2_protection_events(sequence, generation, source,
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
