# Native iMessage evidence refresh (owner-run)

Status, 2026-09-29: built on `codex/p2c-readiness`, not released. It is an owner maintenance
operation beside [the recovery](NATIVE_EVIDENCE_RECOVERY.md). The recovery route is unchanged.

2026-10-01 (`codex/imessage-provenance-forms`, not released): recovery and refresh capture under
reader v3, which also proves the owner's inline replies and Messages' chained rows
([IMESSAGE_RECONCILIATION_DESIGN.md](IMESSAGE_RECONCILIATION_DESIGN.md)). The first refresh of a
v2 enrollment moves it to v3.

2026-10-01, owner decision 3 (`codex/imessage-provenance-forms`, not released): the reach follows
the longest active grant window, at most 365 days, read in slices of at most 31 days. That lifts
the 32-day ceiling; see "The reach" near the end of this file.

2026-10-01, owner decision 1 (same branch): with the owner's standing statement, made once in the app's
iMessage settings, the node enrolls every iMessage dataset that holds the owner's rows and runs this
refresh itself after each scheduled sync. See "The owner's standing attestation" near the end of this
file. The owner-run route below stays, for a Mac without the statement.

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
- the recovery's bounds: a UTC interval ending no later than now and starting no earlier than
  the reach (below), read from the native database in slices of at most 31 days and 1,000
  native sent-by-me rows each;
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
   - The window may not start more than the reach before the later of now and the enrollment's
     last authorization (`reconciliation_refresh_window_too_old`). The reach is the coverage C
     plus a day; C is the longest window any grant the node holds active can release, 30 to 365
     days (`proof_coverage_seconds`, `proof_bounds`). The owner door checks this before it reads
     `chat.db`.
   - Every captured message must lie inside the window (`reconciliation_capture_outside_window`)
    and carry its native time (`reconciliation_capture_time_missing`).
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
   - **Older than C + 2 days and without a ceiling: deleted.** No later window of this coverage
     may start further back than its reach, which never moves backwards (step 1). The extra day
     is margin for clock skew. A later, longer coverage may capture the message again and link it
     anew; it had no ceiling, so nothing is lost.
   - **With a ceiling: never deleted, only retired.** So a link can never come back without the
     ceiling it had, however the coverage moves. Only a recovery sets a ceiling, so these are at
     most one recovery capture's candidates per dataset.
   - **Younger: retired.** It stays at its old revision, where it proves nothing. A later
     refresh that captures the message again re-links it with its ceiling intact, so a
     mistaken refresh is undone by a correct one. Older than C (past every active grant's
     window) it is retired silently; younger than C, the refusals below apply.
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
  `reconciliation_capture_outside_window`, `reconciliation_capture_time_missing`,
  `reconciliation_refresh_window_uncovered`,
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
- **Caps.** Per native read, as in the recovery: at most 31 days, and in that slice at most
  - 1,000 native sent-by-me rows of any form (reactions included),
  - 1 MiB of text,
  - 4 MiB of attributed-body archives,
  - 10 s of native reading.

  A slice over one of these is read again as two halves, down to a day
  (`native_imessage_probe._read_slices`, counted as `native_capture_split`). One message past
  64 KiB of text is not read (`native_text_unsupported`), as a `text` column that long is not; it
  never refuses the read, which no split could help. Per capture: at most 48 reads in 120 s,
  12,000 captured rows and a 16 MiB file, its bodies and archives counted before anything is
  written (`native_probe_capture_limit`): twelve reads' worth, what the v3 reader accepts
  (`owner_snapshot.FORMS_SLICES`). A
  window over a cap refuses whole. The remedy is a shorter window acknowledged with
  `accept_uncovered_links`. The links it leaves uncovered are retired, not deleted, and a later
  refresh restores them once a covering window fits the caps again.
- **Protection clock: +1 per refresh.** Its consequences:
  - The sweep drops every index. The clock generation is in the index basis.
  - The node ledger's protection revision falls behind until something synchronizes it; the
    route does this.
  - Every control-plane envelope binds `protection_revision` and `node_epoch`. Until owner
    decision 2 (1 Oct 2026) the control plane refreshed its copy only through the owner's grant
    **Sync**, so every recipient search refused from the refresh until the owner pressed it.
    Now the node rings the control plane when its protection revision moves
    (`protection_doorbell.py`, within 10 s, an empty frame), and the control plane re-signs each
    active grant whose policy did not change: the status half of Sync, never a mutation, never a
    pending grant (control plane `permissions_v2/auto_resync.py`). The node answers that relay
    client a status only. This holds for every protection change: proof publication and refresh,
    revocation, Off-limits.
- **Grant windows longer than 31 days.** Since owner decision 3 the bounds follow the longest
  active grant window, up to 365 days. See "The reach" below.
- **Staleness.** A refresh brings a stale enrollment current. It does not reopen a revoked one.
- **A clock set forward** during a refresh records a future authorization time. Until real
  time passes it, later refreshes refuse their windows as too old. That fails closed, and it
  is the price of a reach that never moves backwards.
- **Ledger size** is bounded by one capture (at most 12,000 links) plus the retired links of
  the last C + 2 days, plus the retired links that carry a ceiling (only a recovery sets one, at
  most `MAX_CANDIDATES` = 96 per dataset), so the per-check authority digest stays bounded. See
  "The reach" below for the numbers. A retired link never validates:
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

With the owner's standing statement armed, the node runs every refresh itself (see "The owner's
standing attestation" below) and none of this is needed. Without it, the owner runs every refresh.
It is a request on the owner socket, not a UI button.

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
   - On a node with the since-last fix, a sync into any other dataset is refused while the
     enrollment is active (`dataset_not_enrolled`), and "since last" reads only messages newer
     than the last sync. If it answers with a plan to confirm instead, there was no checkpoint
     to resume from: read the plan's counts before confirming. The owner's automatic sync
     (`sync_schedule`) runs the same since-last sync into the same row.
4. **Dry-run, then refresh, the last 30 days** (or the longest grant window, C: the reach is a
   day longer, so two clock readings a second apart never exceed it). The dataset id comes from the owner's
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
5. **The grants re-sync on their own** (owner decision 2): the node rings the control plane and
   it re-signs each active grant whose policy did not change, within seconds. Press "Sync with
   node" in the owner permission lab (`/app/settings/permissions/lab`) only for a grant that
   stays refused: with the control plane's `PERMISSIONS_BETA_V2_AUTO_RESYNC_ENABLED` off, the
   node's `TOPOS_PERMISSIONS_V2_AUTO_RESYNC` off, or the node offline when it rang.
6. **Assess the newly proven messages.** Start the owner's automatic assessment over the same
   window (`automatic_start`), or wait for the refresh loop's catch-up: a clock move triggers
   one within its interval. A newly proven message enters an index only once it is assessed.

**How long a grant is dark per refresh.** From the refresh's commit until the control plane's
re-sync of that grant: the node's next doorbell check (at most 10 s) and one status round trip
per active grant. The index is rebuilt inside the route, typically seconds to a minute for a
grant of tens of members. Messages proven for the first time appear once assessed. Harness runs
spanning a refresh are void until the re-sync and the rebuild complete. A dry run darkens
nothing.

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

`scripts/permissions_v2/p2c_refresh_mutants.py` applies 60 guard-breaking patches
to a scratch copy of the engine, one at a time, and runs these suites. It covers the service,
the door, the capture, the pool probe and the reader coverage census. It first requires a clean
unmutated run, and it counts a mutant as killed only when a test fails. All 60 are killed, with
none left as equivalent. (It had 68 until the caption reader removed the deferred attachment
census those 8 mutants broke; `imessage_forms_mutants.py` covers the caption reader.)

## Reader coverage census

A capture, a dry run and the preflight route all return the probe's counts. Each sent-by-me row
the reader withholds as `native_message_form_unsupported` is also counted in exactly one
`native_form_*` bucket. The first failing field wins, in this order:

| Bucket | Native fields | Whose words |
|---|---|---|
| `native_form_deleted` | `is_deleted` | none left |
| `native_form_spam` | `is_spam` | not the owner's |
| `native_form_system` | `is_system_message`, `is_service_message`, `group_action_type`, `item_type` | none |
| `native_form_reaction` | `associated_message_type`, `associated_message_guid` | a tapback quotes the other party |
| `native_form_forward_or_quote` | `is_forward`, `is_forwarded`, `forwarded_from`, `quoted_message_guid` | carries someone else's |
| `native_form_thread_reply` | `thread_originator_guid`, `thread_originator_part` | the owner's, but the two fields are not a reply the v3 reader reads (a part with no originator, a malformed value) |
| `native_form_subject` | `subject` | the owner's, with a subject line |
| `native_form_attachment_only` | `cache_has_attachments` = 1 with no caption | none: only an attachment |
| `native_form_attachment_unmeasured` | `cache_has_attachments` neither 0 nor 1 | not read |

Since reader v3 ([IMESSAGE_RECONCILIATION_DESIGN.md](IMESSAGE_RECONCILIATION_DESIGN.md)) a
well-formed inline reply and Messages' `reply_to_guid` pointer are not failing fields: such rows
are compared like any other and counted under `native_observed_thread_reply` and
`native_observed_reply_pointer`, each with an `_exact_match` split. An attachment with a caption
is read too: its body is decoded inline, as a decision, and counts toward the archive limit like
any other. It is counted under `native_observed_attachment_caption`, with an `_exact_match` split.
`native_observed_caption_placeholder_stored` counts a stored caption that kept its attachment
placeholder, which never matches.

The order ranks value, not frequency. The first five buckets are rows with no words of the
owner's own, or with someone else's words in them. The thread and subject buckets are the
owner's own words in a form the reader does not accept yet. `native_form_other` would mean the
census and the form check disagree; the tests hold it at zero.

Until the caption reader, attachment bodies were measured after the last decision, within a
budget of their own. Captions are now decisions, so that deferred census is gone, and so are
the `native_form_attachment_with_text` bucket and its budget.

`native_observed_edited` and `native_observed_retracted` count rows whose native edit or
retraction time is set, whatever their outcome. `native_observed_edited_exact_match` and
`native_observed_edited_content_mismatch` split the edited rows by result. Both columns are
read in the same statement under an alias, and set aside before anything else sees the row.

**It decides nothing.** A bucket is counted after the row's decision is taken, and the edit
columns reach no comparison and no capture. (Until the caption reader, the census also read
attachment bodies after every decision; the replay below dates from then.)
`scripts/permissions_v2/p2c_probe_equivalence.py` loads the probe as it was at `8d64d5c1`
beside the current one and runs both over 48 synthetic cases:
- every form field on its own, the caption variants, edits, and rows carrying several forms;
- six refusals: the message, archive and text limits inside the row loop, and the window,
  binding and unavailable-database checks before it;
- one case that would refuse if a census body counted toward the archive limit.

Refusals, the rows handed to the capture and every earlier count are identical, and the
buckets sum to `native_message_form_unsupported`. Each case also carries the outcome it was
built for, a refusal code or a count of captured rows, and both versions must reach it. So
agreement cannot hide two wrong answers.

The replay is not a test run and not in the mutant run. So the probe's own tests pin what must
never regress:
- the order, pair by adjacent pair;
- both budgets;
- the archive and text limits;
- the census body kept out of the archive total.

**The counts are the owner's.** Edits, retractions, and deleted and spam rows describe the
owner's own messaging. They leave the node only in the answers of the three owner-socket routes,
and the node logs none of them. Nothing may forward them to the control plane or to telemetry.

## The reach (owner decision 3, 1 Oct 2026; built, not released)

**Before.** Under refreshes an enrollment proved at most the last 32 days of messages: a window
could start at most 31 days back, one capture spanned at most 31 days, a link 30 to 32 days old
was retired silently and an older one deleted. Those were implementation choices (the per-read
bounds, a bounded ledger, and the rule that a deleted link never returns without its ceiling),
not limits of the proof. The proof is a sent-by-me row in the owner's Messages database, an exact
body, time and identity match, and the owner's attestation; it is as strong for a 90-day-old
message as for yesterday's while `chat.db` still holds it. Messages' own "Keep messages" setting
is the one real limit. Under a 90-day grant the old bounds were a loss: on 1 Oct the owner's
refresh dry run would have deleted 15 links and retired 5, with no new link.

**Now.** A refresh is told its coverage C (`reconciliation_provenance`):
- C is the longest window any grant the node ledger holds active can release: the largest
  `max_age_seconds` anywhere in the policy of each grant `Ledger._authority` accepts at that
  moment, whatever its capability (`proof_coverage_seconds`). It is at least 30 days and at most
  365. A grant that is inactive, expired or not yet valid does not count. A ledger that cannot
  be read, or a grant whose policy cannot (`policy_unknown`, `policy_integrity`), refuses the
  refresh (`reconciliation_coverage_unavailable`) rather than shrink C and delete proofs.
- The reach is C + 1 day and the deletion bound C + 2 days (`proof_bounds`). At C = 30 days these
  are the 30/31/32 days of before, so a node with no grant longer than 30 days behaves as it did.
- A link that carries a ceiling is never deleted. C follows the grants, so it can grow: a later,
  longer reach could capture a message whose link a shorter one deleted, and link it anew. For a
  link without a ceiling that loses nothing. For one with a ceiling it would, so such a link is
  kept, retired, and a capture that reaches the message again relinks it with its ceiling.
- A v2 enrollment past revision 1 is not refreshed (`reconciliation_refresh_legacy_enrollment`).
  Only a wheel without v3, with the fixed 31/32-day bounds, can have made that revision, and its
  deletions, ceilings included, cannot be seen; a longer reach could link those messages anew
  without their ceilings. The owner decides what happens to such an enrollment. Before the first
  refresh under this change, check:
  `SELECT count(*) FROM ingest_provenance_enrollments WHERE json_extract(snapshot_json,'$.reader_contract')='imessage-existing-comparison/v2' AND revision>1`
  must be 0. On the 1 Oct copy it is 0 (one v2 enrollment, revision 1).
- The capture reads its window from `chat.db` in consecutive half-open slices of at most 31 days
  (`native_imessage_probe.capture_slices`), each within one read's bounds, and splits a slice
  that hits one of them (see "Caps" above). All of them go into one capture, which the v3 reader
  reads whole: up to twelve reads' worth (`owner_snapshot.FORMS_SLICES`: 12,000 messages and
  12 MiB of text in a 16 MiB file). Every statement of the v3 parser reads one row past that
  bound, so no row escapes a form check. The v1 ingest lane and v2 keep one read's bounds.
- Both owner doors compute C from the node ledger at the request: the refresh passes it to
  `refresh_existing`; the recovery bounds its window by the reach.

**What it changes in the ledger bounds.**

| Bound | Before | Now (C = 90 days, the owner's grants on 1 Oct) | At the cap (C = 365) |
|---|---|---|---|
| One capture | 31 days, 1,000 rows, 1 MiB text | 91 days in 3 to 6 reads; on the 1 Oct copy about 520 and 1,310 owner-sent rows in the two datasets | 366 days, at most 12,000 rows, 16 MiB |
| Links kept unproven (retired) | 30 to 32 days old | C to C + 2 days old | the same |
| Retired links with a ceiling | deleted past 32 days | never deleted (at most 96 per dataset) | the same |
| Authority digest per `_check` (streamed; 0.89 ms at 386 links, 11 ms at 5,000) | at most one 31-day capture | about 3 ms at 1,300 links | about 11 ms at 5,000 links (a year of the owner's rate) |
| Capture re-hash per search pass (`ExistingProvenancePass.finish`) | about 140 KiB on 1 Oct (204 rows) | about 0.9 MiB | at most 16 MiB |

On the 1 Oct copy, 1 of the 204 links carries a ceiling: only a recovery sets one, at most 96
candidates per dataset (`reconciliation_facts.MAX_CANDIDATES`). Nothing in the schema,
the marker or the digest changes, and no store is re-pinned.

**Review notes.**
- *Ceilings.* "A deleted link never comes back without its ceiling" no longer depends on any
  reach: a link with a ceiling is never deleted, and a link without one has none to lose.
  `authorized_at` still never moves backwards and the reach is still measured from the later of
  now and it, so a clock set back cannot reach further.
- *Wheel rollback.* A wheel without v3 (such as the installed `a15b7b50`) reads a v3 enrollment as
  unknown and withholds it, so it deletes nothing from one. A v2 enrollment that such a wheel
  refreshes (revision 2 or later) is refused by this one (above), because that wheel deleted
  ceiling links past 32 days. Do not run the installed wheel's refresh before this change is
  installed; the owner's node has not refreshed since its 27 Sep capture.
- *Who sets C.* Only the owner authors grants; a recipient cannot move C. A grant the owner
  revokes shrinks C at the next refresh: links between the new C and the old one are retired
  silently (they are past every active window) and deleted at the new C + 2 days unless they
  carry a ceiling. A grant made later relinks them from `chat.db` without loss.
- *Reads.* Splitting stops at a day; a slice whose read is refused for any other reason refuses
  the capture; the rows of a refused read are dropped; a row read twice refuses the capture.
- *Independent adversarial review (1 Oct 2026),* by a separate agent over the uncommitted diff,
  with the dispositions:
  1. A fixed-bound wheel's refresh of a v2 enrollment before this change could delete ceiling
     links that a longer reach then links anew without them. Fixed: ceiling links are never
     deleted, and a v2 enrollment past revision 1 is refused, with the pre-deploy check above.
  2. Every refusal of `Ledger._authority` read as "not active", so a corrupt policy row shrank C.
     Fixed: only `grant_inactive` and `policy_time` are skipped; anything else refuses.
  3. One message past 64 KiB refused the read as the splittable text bound, so splitting could
     never help and the standing lane would stay stuck for C days. Fixed: that row is not read.
  4. A capture could be written well past 16 MiB before being refused. Fixed: the reads count
     the bytes first.
  5. Performance at the cap (ledger digest about 26 ms at 12,000 links; the owner review queue's
     per-candidate `validate_existing` about 2 s a page at 90 days, against 0.3 s). Not changed
     here: routing the queue through `ExistingProvenancePass` is a follow-up.
  6. `coverage_seconds` defaulted to the floor, so a caller that forgot it shrank C. Fixed: every
     caller must name it.
  7. Test gaps (a row on a slice boundary, the real ledger, the legacy state, the per-message
     bound): each now has a test (R2, R5, R6, R7).
  It found sound: ceilings under the new bounds, the slice arithmetic, every v3 statement's
  bound, the v1 and v2 readers unchanged, who can move C, and the recovery door's new reach.

Tests: `tests/permissions_v2/test_imessage_proof_reach.py` (R1 to R9), with F10 and F14 in
`test_reconciliation_refresh.py` updated for the ceiling horizon. Mutants:
`scripts/permissions_v2/imessage_reach_mutants.py`.

## The owner's standing attestation and the automatic refresh (owner decision 1, 1 Oct 2026; built, not released)

The owner chose option A of the proposal below: a standing statement, bound to the Messages accounts
(`imessage_standing.py`). With it, nothing ages out and no owner command is needed after setup.

**The statement.** Made once, through the product surface the app's iMessage settings screen already uses
(`put_source_settings`, field `proof_standing`, and its HTTP twin `PUT /sources/imessage/settings`), never
through the owner socket's maintenance routes:
1. `{"action": "preview"}` reads the account columns (`message.account`, `message.account_guid`) of the
   owner's sent rows over the coverage window and answers counts only: how many accounts, how many sent
   messages each, how many sent rows carry no account, and a token. The record keeps keyed digests
   (HMAC-SHA256 under a random key of its own), never an identifier.
2. `{"action": "arm", "statement": STANDING_STATEMENT, "accounts_token": ...}` arms it, only if the
   accounts are still exactly those of the preview (`standing_accounts_changed` otherwise).
3. `{"action": "disarm"}` stops the automatic runs and revokes nothing. `get_source_settings` shows the
   state and the last run to the owner only.
All three need the owner's principal on the owner socket or the signed relay. The record is
`permissions-v2/imessage-standing-attestation.json` (0600, single link, no symlink), backed up with that
directory.

**What it trusts.** A message is proven exactly as before (an exact native/canonical match of a sent-by-me
row of the node user's Messages database). The statement stands in for the per-run attestation sentence
for rows whose every account identifier was on the owner's sent rows when they stated and saw the list.

**What it refuses.**
- A sent row in the window from an account the owner did not attest (a second Apple ID signed in to
  Messages on the same Mac, the owner's address under another account object, another address under
  the owner's): the whole run refuses (`standing_account_unattested`) and writes nothing. The owner
  sees it in the settings and can state again over the accounts listed then.
- A sent row with no account identifier: left out of the capture (`excluded_account_unknown`), never
  proven by this path. A Messages database with neither column cannot be attested
  (`standing_account_unavailable`): such a Mac keeps the owner-run recovery and refresh only.
- Any acknowledgement of a loss: the standing principal passes the owner check of a refresh only without
  `accept_uncovered` and `accept_unproven`, of a publication only without a derivation or ceilings, of an
  enrollment only on this lane (`evidence._owner(standing=True)`); every other owner operation refuses it.
  No request resolves to it: it is set in this process only.
- A record of another owner (`standing_owner_changed`), a statement other than the exact sentence, a
  statement without a preview, and a preview whose accounts changed before the statement.
It cannot detect someone else using the owner's own account on this Mac; and an account already signed in
when the owner states is attested if the owner accepts the list.

**What runs.** `imessage_standing.maintain`, under the owner's lock with the recovery and refresh doors
(one at a time), over the coverage window (decision 3), for every iMessage dataset that holds the owner's
sent rows and whose source is enabled:
- not enrolled: capture, check every captured row exactly on one read (the dry run), enroll, publish
  (no fact derivation, as a refresh links new rows without one);
- enrolled under the existing-row lane: capture, `refresh_existing` as a dry run, then the same capture for
  real. A refresh that would only re-prove, on an enrollment that is current and v3, is not made, so a sync
  that brought nothing of the owner's moves no clock;
- revoked, another lane, a disabled source: skipped and reported.
After anything committed: the node's protection state and every search index, as the owner door does. The
outcome (counts and codes) is recorded for the owner's screen.

**When.** After every settled scheduled iMessage sync that imported rows (`local_sync_schedule._settle_running`),
and on the scheduler's own tick when one is due (`imessage_standing.due`): never run since the statement, a
week since the last run, or an hour after one that could not read what it needed. A Mac whose scheduler is
off (`TOPOS_LOCAL_SYNC_SCHEDULER=off`) or whose permissions beta is off runs none of this.

**Fresh install.** iMessage setup: Full Disk Access, the first sync, then the owner screen below. After the
statement the node enrolls every dataset holding the owner's rows within a minute (the scheduler's tick)
and keeps them current after each scheduled sync. No owner socket, no command.

**An upgrading node.** Nothing happens until the owner makes the statement on that screen. Then the same
run enrolls the datasets no enrollment covers (on the 1 Oct copy, the second iMessage dataset that holds 594
of the 822 veto-free unproven owner rows) and refreshes the existing one. Each committed run moves the
protection clock; decision 2 (`control-plane re-sync`) re-signs the owner's unchanged grants on its own.

**The owner screen (app, not built here).** In the iMessage source settings, below the sync schedule:
- a "Prove my own messages automatically" section showing, from the preview: "N accounts send from this
  Mac", the sent-message count of each (no identifiers), and how many sent messages carry no account and
  will not be proven;
- the exact `STANDING_STATEMENT`, and one button that sends it with the preview's token;
- once armed: the state, when it was stated, the last run's outcome and counts, and a refusal in plain
  words with its remedy (`standing_account_unattested`: "Messages on this Mac is now signed in to an
  account you did not list. Review the accounts and state again.");
- a "Turn off" button (`disarm`).

## Automatic refresh: the options the owner chose between (1 Oct 2026)

**What exists.** The owner's scheduled since-last sync (`local_sync_schedule.run_schedule_tick`)
enqueues through the same door as "Sync now". When a later tick finds the job finished,
`_settle_running` records the outcome. That settlement is the hook the standing attestation uses.

**What a refresh needs that a scheduled sync does not have.**
1. *Reading `chat.db`*: nothing new. The node process already reads it for the sync, under the
   Full Disk Access granted to the app that launches it.
2. *The owner's attestation on every run*: a standing attestation, a new kind of trust, scoped
   above to the attested accounts.
3. *The owner's grant Sync after every protection-clock move*: every committed refresh advances the clock.
   Decision 2 built the automatic control-plane re-sync (see "Effects" above).

**Options.**

| Option | What it is | Trust | Size |
|---|---|---|---|
| A. Standing attestation, bound to the account (chosen) | Built above. | New: standing, but scoped to the attested account set and revocable | engine M, app S |
| B. Proof at ingest (the live lane) | The sync proves the rows it writes, in its own transaction ([INGEST_LIVE_SYNC_DESIGN.md](INGEST_LIVE_SYNC_DESIGN.md)) | The same standing attestation | engine L to XL |
| C. Control-plane re-sync after a node protection change (chosen, decision 2) | The node rings the control plane, which re-signs each active grant whose policy did not change | The owner's Sync click becomes a mechanical step for unchanged grants | engine S, CP M |
| D. Owner-run, one click | An app button after each sync carries the attestation sentence to this route | None new | app S |

## Not in this change

- **The owner screen.** The node and API side is built (above); the app screen is not.
- **Reader coverage.** Reader v3 reads inline replies, Messages' chained rows and attachment
  captions. Attachments without a caption, subjects, reactions, forwards and quotes, and
  unsupported archives stay unproven. The census above sizes each form, in the counts of any capture, dry run or
  owner-only `/imessage/preflight` call.
