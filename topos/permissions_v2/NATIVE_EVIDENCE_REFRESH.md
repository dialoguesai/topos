# Native iMessage evidence refresh (owner-run)

Status, 2026-09-29: built on `codex/p2c-readiness`, not released. It is an owner maintenance
operation beside [the recovery](NATIVE_EVIDENCE_RECOVERY.md). The recovery route is unchanged.

## The limit it removes

The recovery makes one immutable enrollment per dataset. Under a rolling-window grant that
has two consequences.

- **The pool drains.** Proof covers only the rows in the recovery's capture. Every proven
  message leaves the grant's window at its own event time plus the window. Messages synced
  after the capture never gain proof. The permitted set therefore reaches zero one window
  after the capture.
- **One clock move darkens all of it.** Under source clock v2 these changes advance the
  ingest source clock:
  - an INSERT or DELETE of any `user_ingestion_sources` row, including the first sync of any
    new source or dataset;
  - an UPDATE of `dataset_id`, `source_id`, `enabled` or `posture`;
  - a runtime connector install, update, rollback or PATCH;
  - an owner change in `engine_config`.

  A clock move stales the enrollment, and with it every link. Nothing refreshes an
  enrollment's `source_generation`.

A second recovery is refused three times over:
- `api/permissions_native_probe.py` refuses an enrolled dataset
  (`native_recovery_already_enrolled`);
- `ingest_provenance_enrollments.dataset_id` is UNIQUE, revoked rows included, and `enroll`
  raises `ingest_dataset_already_enrolled`;
- `ingest_provenance_records.message_id` is the primary key.

"Use a fresh dataset" is not available for rows that already exist.
`compare_existing_message` binds the canonical row's own dataset. `imessage:<ROWID>` ids are
not scoped by dataset. A synthetic dataset key would lose the source posture binding, which
`_source_posture` reads by `(source_id, dataset_id)`.

## Options considered

| Option | Verdict |
|---|---|
| A fresh dataset per recovery | Rejected: not available for existing rows (above). |
| Append-only epochs (a v3 store allowing several immutable enrollments per dataset) | Rejected. It is an owner-run store upgrade with the node stopped. Every older wheel refuses a v3 store (`ingest_ledger_invalid`), and a refused store withholds all message evidence, so a wheel rollback after the upgrade takes the whole pool down. Links accumulate forever, and `_authority_digest` streams every ledger row on each evidence check. A stale epoch still needs a re-link path. |
| **In-place refresh (chosen)** | Same enrollment row, next revision, fresh capture, every link re-proven in one transaction. No schema change. |
| The live lane ([INGEST_LIVE_SYNC_DESIGN.md](INGEST_LIVE_SYNC_DESIGN.md)) | The long-term direction, not a stopgap. As written it reads a synthetic locator and puts the real `~/Library/Messages` database out of scope. It needs its own design for the real database: Full Disk Access, and Apple ID changes that no digest detects. |

## What a refresh does

**The door.** `POST /v1/permissions-beta/v2/imessage/refresh`:
- verified owner Unix socket only, with the recovery's exact attestation sentence;
- the recovery's bounds: a UTC interval of at most 31 days ending no later than now, and at
  most 1,000 native sent-by-me rows;
- the recovery's process lock;
- the dataset must already hold exactly one enrollment, of the recovery lane
  (`native_refresh_not_enrolled` otherwise).

**The capture.** The same native reader (`native_imessage_probe.capture_matching_snapshot`)
stages exact native/canonical matches into a new private 0400 capture, inside the Topos
process. It never copies `chat.db` or moves a sync cursor. A row another enrollment proves is
left out and counted (`excluded_row_owned_elsewhere`).

**One ledger transaction** (`reconciliation_provenance.refresh_existing`), under the store's
pending/active marker protocol:
1. The enrollment must be active. It may be stale; a revoked one is never refreshed. The
   previous job must be complete at the current revision.
   - The window may not start more than 31 days before the later of now and the enrollment's
     last authorization (`reconciliation_refresh_window_too_old`). The owner door checks this
     before it reads `chat.db`.
   - Every captured message must lie inside the window (`reconciliation_capture_outside_window`).
   - The authorization time a refresh records never moves backwards. So neither a past-dated
     window nor a clock set back can reach a message whose link was deleted.
2. The enrollment row keeps its id and dataset. It takes the new capture, the next revision,
   the store's current source generation, and the time and channel of this authorization.
3. The previous job is replaced by one for the new revision.
4. Every captured row is compared exactly again (`compare_existing_message`) and linked at
   the new revision. A re-proven message keeps any whole-message ceiling it ever had, even if
   its row changed.
   - The ceiling only ever raises a release bar. Dropping one could widen a release of text
     it was computed on.
   - Keeping one on changed text can only withhold more.
5. A link the new capture does not re-prove cannot move to the new revision: its evidence is
   not in the snapshot the enrollment now names, so moving it would relabel provenance. Its
   own native time decides what happens instead:
   - **Older than 32 days: deleted.** No later window may start more than 31 days before its
     refresh's reach, which never moves backwards (step 1), so no later capture can reach that
     message again. The extra day is margin for clock skew. A deleted link can therefore never
     come back without the ceiling it had.
   - **Younger: retired.** It stays at its old revision, where it proves nothing. A later
     refresh that captures the message again re-links it with its ceiling intact, so a
     mistaken refresh is undone by a correct one. Between 30 and 32 days old (past every
     30-day grant) it is retired silently; younger than 30 days, the refusals below
     apply.
6. Two losses are refused, rolled back whole, unless the owner acknowledges them in the
   request:
   - A current young link the window does not cover, because the window starts too late or
     ends too early: `reconciliation_refresh_window_uncovered`. Acknowledged with
     `accept_uncovered_links`.
   - A capture that fails to re-prove more than half of the links it could have re-proven,
     as a reader loss or a swapped native database would: `reconciliation_refresh_mass_unproven`.
     Acknowledged with `accept_unproven_links`. The share counts only young current links
     whose rows did not change, captured or not. Links retired earlier, links whose rows
     changed (re-proven or not), and links outside the window never count toward it.
7. The capture bytes are re-hashed, and the enrollment must read current, including its
   source's enable switch.
8. The job completes and the protection clock advances once, as it does for publication and
   revocation.

**Dry run.** `dry_run: true` runs all of the above, then rolls it back and returns the same
counts. Nothing is written, and the route deletes the capture it made.

**After the commit:**
- the previous capture is deleted if no enrollment names it;
- the node synchronizes its own signed protection state, as every recipient admission does;
- the route sweeps and rebuilds every search index.

The response is counts only:
- `counts`: the capture's native counts;
- `refresh`: `reproven`, `reproven_row_changed`, `relinked_retired`, `linked_new`,
  `ceiling_carried`, `retired_uncovered`, `retired_unmatched`, `retired_row_changed`,
  `retired_aged`, `still_retired`, `dropped_aged`, `previous_capture_removed`, and `dry_run`
  for a dry run;
- `search`: `protection_synced`, `grants`, `ready`. A dry run has no `search`.

**Refusals.** Each is an HTTP 503 whose detail is the code:
- `native_refresh_not_enrolled`, `reconciliation_refresh_unenrolled`;
- `reconciliation_lane_required`, `reconciliation_enrollment_revoked`;
- `ingest_source_disabled`, `reconciliation_refresh_unchanged`,
  `reconciliation_refresh_incomplete`, `reconciliation_refresh_conflict`;
- `reconciliation_window_invalid`, `reconciliation_refresh_window_too_old`,
  `reconciliation_capture_outside_window`, `reconciliation_refresh_window_uncovered`,
  `reconciliation_refresh_mass_unproven`;
- `reconciliation_row_owned_elsewhere`, `reconciliation_empty`;
- any `reconciliation_*` comparison code;
- the native bounds codes (`native_probe_*`).

## Effects

- **Dataset id: unchanged.** The same enrollment row, compared against the rows' own dataset.
- **Opaque record ids: unchanged.** An id is `HMAC(k_grant, grant_id, table, source_id,
  dataset_id, record_id)` (`opaque_ids.py`). A refresh writes none of these. Canonical rows
  are byte-identical before and after (test F2), so a recipient's citations keep their ids
  across refreshes.
- **Posture binding: unchanged.** Posture is resolved at read time from
  `user_ingestion_sources` and runtime installs for the row's `(source_id, dataset_id)`. A
  refresh writes neither (F2).
- **Ceilings never disappear.** A message keeps its whole-message ceiling for as long as it is
  linked or retired (F4, F10, F11).
- **Caps.** Per run, as in the recovery: at most 31 days, and in the window at most
  - 1,000 native sent-by-me rows of any form (reactions included),
  - 1 MiB of text,
  - 4 MiB of attributed-body archives,
  - 10 s of native reading.

  A window over a cap refuses whole (`native_probe_*`). The remedy is a shorter window
  acknowledged with `accept_uncovered_links`. The links it leaves uncovered are retired, not
  deleted, and a later refresh restores them once a covering window fits the caps again.
- **Protection clock: +1 per refresh.** Its consequences:
  - The sweep drops every index. The clock generation is in the index basis.
  - The node ledger's protection revision falls behind until something synchronizes it; the
    route does this.
  - Every control-plane envelope binds `protection_revision` and `node_epoch`, and the
    control plane refreshes its copy only through the owner's grant **Sync**. So every
    recipient search refuses from the refresh until the owner presses Sync on that grant.
    This is true today for every proof publication, revocation and Off-limits change.
- **Grant windows longer than 31 days.** One capture spans at most 31 days, and links older
  than 32 days are deleted. A grant whose window is longer keeps proof only for the capture's
  span. Such grants would need several enrollments per dataset (the epochs this design
  rejected) or a larger capture bound. Today's live grant is 30 days.
- **Staleness.** A refresh brings a stale enrollment current. It does not reopen a revoked one.
- **A clock set forward** during a refresh records a future authorization time. Until real
  time passes it, later refreshes refuse their windows as too old. That fails closed, and it
  is the price of a reach that never moves backwards.
- **Ledger size** is bounded by one capture plus the retired links of the last 32 days, so the
  per-check authority digest stays bounded. A retired link never validates:
  `validate_existing` requires the link's revision to be the enrollment's current one, and its
  job to be the current job, done. Every proof path goes through it. Every other reader of the
  link table reads it for something other than proof:
  - the owner review queue (`message_evidence.queue_messages`), to pick candidates; each one
    then passes `validate_existing` or is skipped;
  - the legacy writers' row guards (`canonical_store` content heal, `_attested_link`), to
    leave linked rows untouched;
  - this door's capture exclusion, to leave out rows another enrollment proves;
  - the snapshot lane's `existing_record` and `validate_record_origin`, which require an exact
    enrollment, revision, job and row identity;
  - the snapshot lane's fact reader, filtered by its own job and revision;
  - `ingest_snapshot_facts.existed_by`, which reads the enrollment's authorization time as an
    "existed by" bound. A refresh records a later time: a weaker but still true bound. It is
    reached only through the snapshot lane's `LinkedRowTrust`.

## Failure behaviour

- **Before the transaction** (attestation, lock, enrollment lookup, window, bounds,
  capture): nothing is written. A capture already written is deleted (F9).
- **Inside the transaction:** any refusal or mismatch rolls back the enrollment, job, links
  and clock together, including a late one after the aged links were deleted (F12). The
  marker stays active at its previous revision, and every previous link validates exactly as
  before (F5). The route deletes the unused capture. A capture any enrollment names is never
  deleted, and a deletion that cannot read the names deletes nothing (F7).
- **A mistaken refresh** (a wrong window, a reader fault) that the owner acknowledged is
  undone by a correct refresh, not by a restore. Young links were retired, not deleted, and
  come back with their ceilings.
- **A crash** between the pending marker and the commit, or between the commit and the active
  marker, leaves the store closed. Every message-evidence read then withholds. This is the
  existing hazard of every ingest-provenance transaction, and only a backup repairs it.
- **Restoring a backup.** `database.db` alone is not a backup. The store's marker, the node
  ledger and the captures live in `permissions-v2/`:
  - Restoring the database alone refuses with `ingest_ledger_rollback`, because the external
    marker has moved on.
  - Every recipient admission then refuses with `protection_clock_rollback`, because the
    ledger has seen a newer clock.
  - The previous capture is gone.

  So a backup is `database.db` (with any `-wal` and `-shm`) and the whole `permissions-v2/`
  directory, copied together with the node stopped, and restored together with the node
  stopped. Restore only a store the crash closed before the owner pressed Sync. After a Sync,
  the control plane refuses a node epoch that moves backwards (`ack_stale`).
- **After the commit**, a failed protection sync or rebuild leaves the refresh standing. The
  response reports `protection_synced: false` or `ready: 0`. The owner's grant Sync plus a
  rebuild (the owner rebuild hook, or the refresh loop's restore) recovers.

## Operating it

The owner runs every refresh. It is a request on the owner socket, not a UI button.

1. **The wheel.** The node must run a wheel containing this change; the route answers 404
   on one that does not. Never reinstall the package under a running node: stop, install,
   start.
2. **Back up.** Stop the node. Copy `database.db`, its `-wal` and `-shm` if present, and the
   whole `permissions-v2/` directory into one private backup directory. Start the node.
3. **Sync iMessage for the enrolled dataset.** New messages must be in the canonical store
   before anything can prove them. To confirm the sync reused the existing source row:
   ```bash
   sqlite3 "file:$HOME/.topos/database.db?mode=ro" "SELECT source_id, substr(last_sync_at,1,16) FROM user_ingestion_sources"
   ```
   - Before the sync, note the row count and the last-sync time of the enrolled dataset's row.
   - Afterwards, the count must be unchanged and only that row's time may have moved.
   - A new row means the sync used another dataset. That advanced the source clock, so the
     enrollment is stale. The refresh below brings it current, but rows in the new dataset
     are not covered by it.
4. **Dry-run, then refresh, the last 30 days.** Use 30 rather than 31, so two clock readings
   a second apart can never exceed the 31-day bound. The dataset id comes from the owner's
   own read:
   ```bash
   DATASET=$(sqlite3 "file:$HOME/.topos/database.db?mode=ro" "SELECT dataset_id FROM ingest_provenance_enrollments")
   ```
   Then send the refresh with `"dry_run":true` first:
   ```bash
   curl -sS --unix-socket ~/.topos/engine.sock -X POST http://localhost/v1/permissions-beta/v2/imessage/refresh \
     -H 'content-type: application/json' \
     -d "{\"dataset_id\":\"$DATASET\",\"starts_at\":\"$(date -u -v-30d +%Y-%m-%dT%H:%M:%S.000000+00:00)\",\"ends_at\":\"$(date -u +%Y-%m-%dT%H:%M:%S.000000+00:00)\",\"owner_attestation\":\"I attest that this snapshot contains my iMessage account and that native sent-by-me messages are mine.\",\"dry_run\":true}"
   ```
   Read `refresh` in the response:
   - `retired_unmatched` near zero;
   - no refusal;
   - `reproven` about the number of proven messages still inside the window;
   - `linked_new` the newly provable ones.

   Then send the same request without `"dry_run":true`. After the real run,
   `search.protection_synced` should be true and `search.ready` at least 1. Add
   `"accept_uncovered_links":true` or `"accept_unproven_links":true` only when you understand
   why the refusal happened. Both retire links rather than delete them.
5. **Press "Sync with node" on each active grant** in the owner permission lab
   (`/app/settings/permissions/lab`, which calls the control plane's assignment `sync`).
   Recipient searches refuse until you do.
6. **Assess the newly proven messages.** Start the owner's automatic assessment over the same
   window (`automatic_start`), or wait for the refresh loop's catch-up: a clock move triggers
   one within its interval. A newly proven message enters an index only once it is assessed.

**How long a grant is dark per refresh.** From the refresh's commit until the owner's Sync
on that grant. The index is rebuilt inside the route, typically seconds to a minute for a
grant of tens of members, so a Sync pressed right after the route returns ends it. Messages
proven for the first time appear once assessed. Harness runs spanning a refresh are void
until the Sync and the rebuild complete. A dry run darkens nothing.

**Cadence.** Weekly, after a sync, keeps the permitted set within a week of complete. A
missed week drains a week of the oldest messages, never more.

## Evidence

`tests/permissions_v2/test_reconciliation_refresh.py`, on synthetic native captures:
- F1: re-prove, link new rows, delete aged links.
- F2: rows, source row and opaque-id inputs unchanged; the clock moves once.
- F3: a stale enrollment is brought current; revoked and disabled refuse.
- F4: the ceiling survives every refresh, even when the row changed.
- F5: a mismatch leaves ledger, marker and proof as they were.
- F6: owner, attestation, unchanged, unenrolled and invalid windows refuse.
- F7: capture exclusion; only an unnamed capture is discarded; discarding never raises.
- F8: owner socket only, one refresh at a time, the paired owner only; the protection sync
  and rebuild report counts and never raise.
- F9: the owner door end to end: success; a mid-transaction failure leaves the ledger and
  the capture directory unchanged; a dry run; the body's window reaches the coverage guard.
- F10: a late start or an early end refuses until acknowledged, then retires. A later refresh
  restores the link with its ceiling. A link 30–32 days old is retired silently, relinked with
  its ceiling, and deleted only once no capture can reach it.
- F11: a mass-unproven capture refuses until acknowledged. Already-retired and changed rows do
  not count, and the retired links come back with their ceilings.
- F11: the floor is a share of the links the capture could have re-proven: four lost of six
  refuses, although only a third of twelve.
- F12: incomplete publication, a row owned by another enrollment, another lane, and a capture
  changed after the writes each refuse and roll back.
- F13: a dry run reports and writes nothing.
- F14: a deleted link never returns. A past-dated window refuses, an old message smuggled into
  a recent capture refuses, and a clock set back cannot move the reach back or the
  authorization time backwards.

`scripts/permissions_v2/p2c_refresh_mutants.py` applies 38 guard-breaking patches
to a scratch copy of the engine, one at a time, and runs these suites. It covers the service,
the door, the capture and the pool probe. All 38 are killed, with none left as equivalent.

## Not in this change

- **A node-scheduled refresh.** A standing attestation replaces the per-run one only with
  Apple ID change detection for the real Messages database. That is an owner decision and
  its own design.
- **Control-plane re-sync after a node protection change.** Today the owner's Sync is the
  only way a grant recovers from any protection-clock move.
- **Reader coverage.** Native forms the reader withholds (attachments, reactions, replies,
  unsupported archives) stay unproven. Counts per reason come from the owner-only
  `/imessage/preflight` route.
