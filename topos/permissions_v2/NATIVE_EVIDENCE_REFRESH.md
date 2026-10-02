# Native iMessage evidence refresh (owner-run)

Status, 2026-09-29: built on `codex/p2c-readiness`, not released. It is an owner maintenance
operation beside [the recovery](NATIVE_EVIDENCE_RECOVERY.md). The recovery route is unchanged.

2026-10-01 (`codex/imessage-provenance-forms`, not released): recovery and refresh capture under
reader v3, which also proves the owner's inline replies and Messages' chained rows
([IMESSAGE_RECONCILIATION_DESIGN.md](IMESSAGE_RECONCILIATION_DESIGN.md)). The first refresh of a
v2 enrollment moves it to v3. The 32-day ceiling and the automatic refresh are explained, and
proposed, at the end of this file.

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
  rejected) or a larger capture bound. See "The 32-day ceiling" below.
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
   - On a node with the since-last fix, a sync into any other dataset is refused while the
     enrollment is active (`dataset_not_enrolled`), and "since last" reads only messages newer
     than the last sync. If it answers with a plan to confirm instead, there was no checkpoint
     to resume from: read the plan's counts before confirming. The owner's automatic sync
     (`sync_schedule`) runs the same since-last sync into the same row.
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

## The 32-day ceiling

**What it is.** Under refreshes, an enrollment proves at most the last 32 days of messages:
- A refresh window may start no more than 31 days before the later of now and the
  enrollment's last authorization (`REFRESH_CAPTURE_REACH_SECONDS`,
  `reconciliation_provenance.py`; the door checks it before reading `chat.db`).
- One capture spans at most 31 days (`native_imessage_probe.window`).
- A refresh deletes every link it does not re-prove whose message is older than 32 days
  (`REFRESH_DELETE_AFTER_SECONDS`). It silently retires one 30 to 32 days old
  (`REFRESH_MINIMUM_COVERAGE_SECONDS`, sized for the 30-day grant of the time).

Without a refresh nothing expires: links stay valid until the enrollment goes stale (a source
clock move) or is revoked. With refreshes, each one trades the proofs older than 32 days for
the new capture. Under a 90-day grant that is a loss: on 1 Oct the owner's refresh dry run
would have deleted 15 links and retired 5, with no new link.

**Where it comes from.** It comes from implementation choices, not from what the proof can
show:
- the per-run capture bounds (31 days, 1,000 sent rows, 1 MiB of text, 4 MiB of archives,
  10 s), which keep one native read short;
- a bounded ledger (one capture plus 32 days of retired links);
- the rule that a deleted link may never come back without its ceiling. A deletion is safe
  only past the farthest point any later capture can reach.

The proof itself is a sent-by-me row in the owner's Messages database, an exact body, time and
identity match, and the owner's attestation. That proof is as strong for a 90-day-old message
as for yesterday's, as long as `chat.db` still holds the message. Messages' own "Keep messages"
setting is the one real limit.

**Lifting it (proposed, not built).** Give the reach a value R: the longest active grant window,
or a fixed 90 or 365 days. Then:
- one refresh captures R in slices of at most 31 days, each slice within today's per-run
  bounds;
- links are deleted only past R plus one day;
- the silent-retire age becomes the longest active grant window.

The ceiling argument is unchanged with R in place of 31 days. The ledger is bounded by R days
of sent rows (about 1,500 at 90 days on the owner's node; the streamed authority digest has no
size cap). Size: M in the engine, plus an independent review. Decision: the value of R.

## Automatic refresh after a scheduled sync (proposed, not built)

**What exists.** The owner's scheduled since-last sync (`local_sync_schedule.run_schedule_tick`)
enqueues through the same door as "Sync now". When a later tick finds the job finished,
`_settle_running` records the outcome. That settlement is the natural hook: after an `imported`
outcome for an enrolled dataset, run this refresh over the last 30 days.

**What a refresh needs that a scheduled sync does not have.**
1. *Reading `chat.db`*: nothing new. The node process already reads it for the sync, under the
   Full Disk Access granted to the app that launches it.
2. *The owner's attestation on every run*: the route requires the owner socket and the
   sentence, and `refresh_existing` requires the owner principal. A scheduled refresh would
   prove captures nobody attested one by one: a standing attestation. That is a new kind of
   trust. The risk it carries is a different Apple ID in Messages on the same Mac, whose
   sent-by-me rows would then read as the owner's. No database digest detects that
   ([INGEST_LIVE_SYNC_DESIGN.md](INGEST_LIVE_SYNC_DESIGN.md)).
3. *The owner's grant Sync after every protection-clock move*: every refresh advances the clock.
   The control plane refuses a grant's searches (`authority_binding`) until the owner presses
   Sync or edits the grant. An automatic refresh without an automatic control-plane re-sync
   would darken every grant after every scheduled sync.

**Options.**

| Option | What it is | Trust | Size |
|---|---|---|---|
| A. Standing attestation, bound to the account (recommended) | A one-time owner consent (the recovery, or a setting) arms automatic refreshes. It records a keyed digest of the account identifiers Messages stores on the attested capture's sent rows (on current macOS, `message.account` and `account_guid`; to be confirmed against the schema before building). Each automatic refresh dry-runs first and applies only with no refusal. It never sets `accept_*`. It refuses, and tells the owner, when a captured row's account is outside the attested set. It runs under a node-internal owner principal on its own channel, and the owner can disarm it at any time. It also enrolls any other iMessage dataset that holds the owner's rows. | New: standing, but scoped to one account set and revocable | engine M, app S |
| B. Proof at ingest (the live lane) | The sync proves the rows it writes, in its own transaction ([INGEST_LIVE_SYNC_DESIGN.md](INGEST_LIVE_SYNC_DESIGN.md)) | The same standing attestation | engine L to XL |
| C. Control-plane re-sync after a node protection change | The node sends its signed protection state, and the control plane re-signs each active grant whose policy did not change | Decides whether the owner's Sync click is consent or a mechanical step | engine S, CP M |
| D. Owner-run, one click | An app button after each sync carries the attestation sentence to this route | None new | app S |

A or B needs C, or every automatic refresh darkens the grants until the owner's Sync. D needs
no decision but stays manual. Owner decisions: (1) a standing attestation and its scope;
(2) whether the control plane may re-sign a grant after a protection change; (3) the reach R
above.

## Not in this change

- **A node-scheduled refresh.** See the section above: it needs a standing attestation and an
  automatic control-plane re-sync, both owner decisions.
- **Control-plane re-sync after a node protection change.** Today the owner's Sync is the
  only way a grant recovers from any protection-clock move.
- **Reader coverage.** Reader v3 reads inline replies, Messages' chained rows and attachment
  captions. Attachments without a caption, subjects, reactions, forwards and quotes, and
  unsupported archives stay unproven. The census above sizes each form, in the counts of any capture, dry run or
  owner-only `/imessage/preflight` call.
