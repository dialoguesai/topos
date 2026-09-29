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
   source's enable switch is checked. The previous job must be complete, with every link at
   the current revision.
2. The enrollment row keeps its id and dataset. It takes the new capture, the next revision,
   the store's current source generation, and the time and channel of this authorization.
3. The previous job is replaced by one for the new revision.
4. Every captured row is compared exactly again (`compare_existing_message`) and linked at
   the new revision. A link that already existed keeps its whole-message ceiling only if the
   row revision it was computed on is unchanged.
5. Links the new capture does not re-prove are removed. Their evidence is not in the
   snapshot the enrollment now names, so carrying them forward would relabel provenance.
6. The capture bytes are re-hashed and the enrollment must read current.
7. The job completes and the protection clock advances once, as it does for publication and
   revocation.

**After the commit:**
- the previous capture is deleted if no enrollment names it;
- the node synchronizes its own signed protection state, as every recipient admission does;
- the route sweeps and rebuilds every search index.

The response is counts only:
- `counts`: the capture's native counts;
- `refresh`: `reproven`, `reproven_row_changed`, `linked_new`, `dropped_before_window`,
  `dropped_unproven`, `ceiling_carried`, `previous_capture_removed`;
- `search`: `protection_synced`, `grants`, `ready`.

**Refusals.** Each is an HTTP 503 whose detail is the code:
- `native_refresh_not_enrolled`, `reconciliation_refresh_unenrolled`;
- `reconciliation_lane_required`, `reconciliation_enrollment_revoked`;
- `ingest_source_disabled`, `reconciliation_refresh_unchanged`,
  `reconciliation_refresh_incomplete`, `reconciliation_refresh_conflict`;
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
- **Caps.** Per run, as in the recovery: at most 31 days and at most 1,000 native sent-by-me
  rows, of any form, in the window. A window over the cap refuses whole
  (`native_probe_message_limit`); a shorter window is the remedy.
- **Protection clock: +1 per refresh.** Its consequences:
  - The sweep drops every index. The clock generation is in the index basis.
  - The node ledger's protection revision falls behind until something synchronizes it; the
    route does this.
  - Every control-plane envelope binds `protection_revision` and `node_epoch`, and the
    control plane refreshes its copy only through the owner's grant **Sync**. So every
    recipient search refuses from the refresh until the owner presses Sync on that grant.
    This is true today for every proof publication, revocation and Off-limits change.
- **Links removed:**
  - messages older than the new window, which aged out by design;
  - messages no longer an exact match (edited, deleted natively, changed canonically).
  A fact citing a removed link loses native proof.
- **Staleness.** A refresh brings a stale enrollment current. It does not reopen a revoked one.
- **Ledger size** is bounded by one capture of at most 1,000 links, so the per-check
  authority digest stays bounded.

## Failure behaviour

- **Before the transaction** (attestation, lock, enrollment lookup, bounds, capture): nothing
  is written. A capture already written is deleted.
- **Inside the transaction:** any refusal or mismatch rolls back the enrollment, job, links
  and clock together. The marker stays active at its previous revision, and every previous
  link validates exactly as before (F5). The route deletes the unused capture; a capture any
  enrollment names is never deleted (F7).
- **A crash** between the pending marker and the commit, or between the commit and the active
  marker, leaves the store closed. Every message-evidence read then withholds until the
  owner repairs it. This is the existing hazard of every ingest-provenance transaction. No
  automatic repair exists, so take a node backup before a refresh.
- **After the commit**, a failed protection sync or rebuild leaves the refresh standing. The
  response reports `protection_synced: false` or `ready: 0`. The owner's grant Sync plus a
  rebuild (the owner rebuild hook, or the refresh loop's restore) recovers.

## Operating it

The owner runs every refresh. It is a request on the owner socket, not a UI button.

1. **The wheel.** The node must run a wheel containing this change; the route answers 404
   on one that does not. Never reinstall the package under a running node: stop, install,
   start.
2. **Back up the node database first** (the owner's usual backup).
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
4. **Refresh the last 30 days.** 30 rather than 31, so two clock readings a second apart can never exceed the 31-day bound:
   ```bash
   DATASET=$(sqlite3 "file:$HOME/.topos/database.db?mode=ro" "SELECT dataset_id FROM ingest_provenance_enrollments")
   curl -sS --unix-socket ~/.topos/engine.sock -X POST http://localhost/v1/permissions-beta/v2/imessage/refresh \
     -H 'content-type: application/json' \
     -d "{\"dataset_id\":\"$DATASET\",\"starts_at\":\"$(date -u -v-30d +%Y-%m-%dT%H:%M:%S.000000+00:00)\",\"ends_at\":\"$(date -u +%Y-%m-%dT%H:%M:%S.000000+00:00)\",\"owner_attestation\":\"I attest that this snapshot contains my iMessage account and that native sent-by-me messages are mine.\"}"
   ```
   Check the response: `refresh.linked_new` and `refresh.reproven` should be as expected,
   `search.protection_synced` should be true, and `search.ready` should be at least 1.
5. **Press Sync on each active grant** in the owner permission settings. Recipient searches
   refuse until you do.
6. **Assess the newly proven messages.** Start the owner's automatic assessment over the same
   window (`automatic_start`), or wait for the refresh loop's nightly full pass. A newly
   proven message enters an index only once it is assessed.

**How long a grant is dark per refresh.** From the refresh's commit until the owner's Sync
on that grant. The index is rebuilt inside the route, typically seconds to a minute for a
grant of tens of members, so a Sync pressed right after the route returns ends it. Messages
proven for the first time appear once assessed. Harness runs spanning a refresh are void
until the Sync and the rebuild complete.

**Cadence.** Weekly, after a sync, keeps the permitted set within a week of complete. A
missed week drains a week of the oldest messages, never more.

## Evidence

`tests/permissions_v2/test_reconciliation_refresh.py`, 16 tests on synthetic native captures:
- F1: re-prove, link new rows, drop unproven.
- F2: rows, source row and opaque-id inputs unchanged; the clock moves once.
- F3: a stale enrollment is brought current; revoked and disabled refuse.
- F4: the ceiling is carried only for an unchanged row revision.
- F5: a mismatch leaves ledger, marker and proof as they were.
- F6: owner, attestation, unchanged and unenrolled captures refuse; drops are split by window.
- F7: capture exclusion; only an unnamed capture is discarded.
- F8: owner socket only; the protection sync and rebuild report counts and never raise.

Thirteen guard-breaking mutants were run (owner check, attestation, revoked refusal, source
switch, unchanged refusal, same-row ceiling, removal of unproven links, carrying unproven
links forward, clock advance, source-generation refresh, named-capture keep, owner-socket
door, capture exclusion). Twelve are killed. The survivor removes the early source-switch
check, which is equivalent: the final `_enrollment(active=True)` in the same transaction
refuses a disabled source.

## Not in this change

- **A node-scheduled refresh.** A standing attestation replaces the per-run one only with
  Apple ID change detection for the real Messages database; that is an owner decision and
  its own design.
- **Control-plane re-sync after a node protection change.** Today the owner's Sync is the
  only way a grant recovers from any protection-clock move.
- **Reader coverage.** Native forms the reader withholds (attachments, reactions, replies,
  unsupported archives) stay unproven. Counts per reason come from the owner-only
  `/imessage/preflight` route.
