# Owner-attested live iMessage sync (proposed, reviewed, not built)

Status, 2026-09-16: **proposed and reviewed; not built.** This is the next step
after step 4, not part of it. It implements the "live-sync binding" that
[INGEST_OWNER_PROVENANCE.md](INGEST_OWNER_PROVENANCE.md) left as proposed, on the
pattern of the [snapshot lane](INGEST_SNAPSHOT_DESIGN.md).

An earlier draft was put through an adversarial design review before any code
was written. The review confirmed blockers in authority, durability, locator
binding, collision handling, extraction ordering and control-plane
feasibility. Step 4 therefore shipped the snapshot-first scope instead: the
owner snapshot lane now produces owner facts with complete references, inside
its own transaction, and a p2b-v4 grant can be activated through the control
plane. This document is the draft rewritten with every confirmed blocker folded
in, so the live lane can be built from it. Nothing below is implemented unless
it says so.

Default off, beta environment only, synthetic data only until a separately
reviewed flag says otherwise.

## What was wrong, and what has changed since

Read from the code and reproduced in scratch probes on synthetic databases;
nothing came from a live node.

| finding | status |
|---|---|
| Ingestion doors (`POST /sources/{id}/sync`, Signal upload, reprocess, backfill, relay `source_sync`) were authenticated, never authorized | fixed in `99ed4de`: the six relay types are owner-only; the HTTP routes refuse every non-owner principal once the node has minted `TOPOS_OWNER_KEY`, and stay byte-identical until then |
| `pipeline_jobs.payload_json` kept credentials (a Signal SQLCipher key, the engine key) for good, readable by non-owners | fixed in `6fa00ee` |
| A caller-chosen `job_id` could rewrite a different job's payload | fixed in `2688d57` |
| The legacy content heal crossed datasets (`imessage:<ROWID>` carries none) and rewrote rows the snapshot lane had proved | fixed in `498aa14`: skips across a dataset or source, and over any linked row |
| A correspondent handle literally `Self`, the Signal self-number substring match, declared field maps, quoted and tapback metadata, `llm_extract._infer_table`, and owner corrections could each turn someone else's words into the owner's | fixed in step 4 (section 9) |
| iMessage rows are written with `owner_user_id` NULL; Signal rows get `owner_user_id = dataset_id` | unchanged: legacy rows stay unproven |
| Worker tasks inherit the principal and transport contextvars of the request that started the worker | unchanged in the legacy lane; the live lane must not |
| No producer path reached a release: shared extractors write references without `dataset_id` | closed only for the snapshot lane (step 4). The shared extractors stay unchanged **on purpose** (section 6) |
| AI chat: evidence requires `sender_type == 'user'`, the ChatGPT parser writes `human` | unchanged: a separate evidence decision |

## The shape

A separate owner-attested **live** lane beside the snapshot lane. It does not
repair the legacy lane and does not backfill. Legacy rows stay unproven, and
evidence treats them exactly as it does today.

```text
owner (UDS, or CP relay stamped as the pinned owner and frontend client)
  └─ signed command  describe_live · enroll_live · enqueue_live · run_live · status_live · revoke_live
        └─ runtime.ingestion_live()  (own flags, fixed locator, own command table)
              └─ ingest_live_* tables (owner permission state)  +  private external marker
                    └─ claim (random token, lease CAS) → windowed native read of a private backup
                          └─ one transaction per batch: rows · links · batch record · cursor · facts · running totals
                                └─ evidence: enrollment current · link identity · committed batch
```

### 1. Authority, gates and the locator

**Commands.** The lane reuses `execute_signed_ingest`'s authority unchanged: an
`OWNER_APP` principal on `uds`, or on `cp_relay` with `acting_user` equal to the
pinned owner and `client_id` equal to the configured frontend client, **and** a
CP-signed command bound to the exact node identity, at most 120 s old, verified
again under the write gate and burned durably before dispatch.

**Dispatch.** Live operations are routed by a table, never through the snapshot
service:

| operation | accessor | burned in | installs the store |
|---|---|---|---|
| `describe_live`, `status_live` | `runtime.ingestion_live()` | not burned | no |
| `enroll_live` | `runtime.ingestion_live()` | `ingest_live_commands` | yes |
| `enqueue_live`, `run_live`, `revoke_live` | `runtime.ingestion_live()` | `ingest_live_commands` | no |

Snapshot operations keep today's exact code path. A test pins that a live
command leaves the snapshot store's authority digest and marker unchanged.

**Flags.** Engine: `TOPOS_PERMISSIONS_V2_ENABLED` and a new
`TOPOS_PERMISSIONS_V2_INGEST_LIVE_ENABLED`, independent of the snapshot flag.
Control plane: `PERMISSIONS_BETA_V2_INGEST_LIVE_ENABLED` gating new
`/ingestion/*_live` routes. Both are checked before any burn and again in the
worker before every batch. A disabled lane maps to 404 through both doors and
burns nothing. The live service constructor checks the `permissions-beta-`
environment prefix, as the snapshot service does.

**Locator.** A fixed path with no fallback, mirroring the snapshot root:
`TOPOS_PERMISSIONS_V2_INGEST_LIVE_LOCATOR` must *equal*
`<canonical>.parent/permissions-v2/ingest-live/chat.db`. Unset or different fails
closed with `ingest_live_locator_not_configured`. The lane never calls
`get_chat_db_path()`, never reads `IMESSAGE_CHAT_DB` or `DEFAULT_CHAT_DB_PATH`
(bound at import, so monkeypatching `Path.home` proves nothing), and does not
import `imessage_reader`. No request field carries a path. A path check alone is
not enough: a symlink in the pinned directory could point at the owner's real
Messages database. So at claim and before every backup:

- lstat every component and open with `O_NOFOLLOW`, as `_snapshot` does;
- both directories are private (no group or other bits) and owned by the engine
  user;
- the file and any `-wal`/`-shm` siblings are regular, single-link files owned by
  the engine user. Mode `0400` is not required, because a live database stays
  writable by its producer;
- device and inode are compared only within one read, never stored.

Reading `~/Library/Messages/chat.db` on a real node is out of scope. It needs its
own design: Full Disk Access, and Apple ID changes that no database digest can
detect. The attestation sentence therefore says:

> I attest that the Messages database enrolled on this node is my iMessage account and that its native sent-by-me messages are mine.

`describe_live` returns only the source kind, a keyed digest of the locator
(not a reversible hash of a home path) and whether the database opens. It never
returns content, handles, per-conversation counts or paths.

### 2. Durable state

**A sibling store, owner permission state.** The prefix is `ingest_live_`, which
does not match the snapshot store's `ingest_provenance_*` GLOB, so the enrolled
snapshot store's schema digest is unaffected. The same commit that creates the
tables adds `ingest_live_` to `PERMISSION_STATE_TABLE_PREFIXES`
(`data_explorer_tables.py`), so Data Explorer never lists, clears or drops them. A
test builds the names from the store's own schema constant, not a hand-written
list. `scrub_attributed_rows` and `plan_attributed_rows` skip permission-state
tables. **No live table has a `source_id` or `source_system` column**; the source
kind is fixed by the attestation, or stored as `source_kind`, so an iMessage
uninstall or scrub cannot delete an enrollment and lock the store.

```text
ingest_live_state        singleton: store_id, binding, file_revision, generation
ingest_live_enrollments  enrollment_id, source_kind='imessage', dataset_id UNIQUE,
                         locator_digest, revision, state, generation,
                         start_rowid, cursor_rowid, attestation, authorized_at, channel
ingest_live_jobs         job_id, enrollment_id, enrollment_revision, status, claim_token,
                         lease_until, result_json      (one queued or running job per enrollment)
ingest_live_batches      batch_id, job_id, claim_token, first_rowid, last_rowid, committed_at
ingest_live_records      message_id, enrollment_id, enrollment_revision, batch_id,
                         row_identity, native_guid_digest
ingest_live_commands     command_id, command_hash
ingest_live_buckets      table_name, bucket, row_count, digest
```

**Sharing the snapshot mechanisms without moving them.** A base class holds
the marker, pending/active publication, rollback detection and explicit
connection binding. It takes the authority function and marker schema per store.
Before the refactor, a golden test pins everything an enrolled node depends on:
the name-to-SQL map with all four source tables present, `_authority_digest` on a
fixed populated fixture, the marker key set, the snapshot origin-hint bytes and
key set, and `_record_identity` of a fixed row. A marker and database written by
today's code must still pass `_check` afterwards. After deploy, the owner's
`status` on the lab's existing snapshot job through the control plane must still
return done.

**A bounded authority digest.** The snapshot store digests every authority row
through `canonical_bytes`, which caps at 1 MiB. A live store would reach that at
about 5,300 links or 9,400 command burns. The batch that crossed it would roll
back, and after that every burned command would be refused, `revoke_live`
included, with no recovery. The live store instead assigns each row of its
growing tables to one of K = 1024 buckets by `H(table, primary key)`. It updates
the touched bucket rows in the same transaction, and pins
`root = digest(all bucket rows)` plus per-table counts in the marker. `_check`
costs O(K). Every keyed decision (command replay in consume, the job row in
claim, `assert_current`, origin validation, the link or its absence for a
message id) recomputes that key's bucket, O(N/K), which still detects an
in-place edit of any row a decision depends on. The snapshot store's digest and
marker formats do not change; its ceiling is recorded as a known limit with a
monitored count.

**Recovering a pending marker** is built into the base class and runs under
`with_db_write` when the service opens, never from evidence's read-only
connection. The pending marker embeds the whole prior active body (revision,
generation, authority digest) and the next digest. If the database matches the
next digest, the marker is promoted to active. If it matches the prior digest,
worker transactions, enroll and enqueue are reverted, while revoke and command
burns are rolled forward, because a plain revert would let an in-place restore
during the window undo a committed revocation. Any other digest is a rollback,
and the store stays closed. Recovery always republishes at a higher revision. A
snapshot pending marker in the old format, which has no prior field, stays
closed as it does today.

**Source clock.** The snapshot store's triggers fire on every write to
`user_ingestion_sources`, so each legacy sync receipt stales every snapshot
enrollment. That hazard is recorded here, not fixed. The live store's own
trigger bumps its generation only on INSERT or DELETE of an `imessage` source row
and on UPDATE OF `enabled` or `posture`, never on receipt columns. The enrolled
source row must exist with `enabled = 1` at enroll and on every batch. Any
generation change stales the enrollment, and only a new signed `run_live`
resumes it. A removed source is revocation, so a reinstall needs a new
enrollment. The lane reads `user_ingestion_sources` only through read-only
`sqlite_master`/PRAGMA checks and never writes the snapshot lane's source-clock
tables.

### 3. Execution

The lane never uses `pipeline_jobs`.

`run_live` claims the job with a new random token and a lease, and returns the
job metadata immediately, so the control plane's 20 s deadline never bounds a
long sync. The worker starts with a fresh `contextvars.Context()` (or a dedicated
thread started with one) and is kept in a runtime-owned registry keyed by
enrollment. A second in-process worker for that enrollment is refused whatever
the lease says. Inside the worker, `current_principal()` is `None`, the transport
var is at its default, `_defer_commit` is off, and every owner-guarded service
method raises; a test asserts all four.

Lease renewal is `WHERE claim_token=? AND status='running' AND lease_until > now`,
so an expired lease is never revived. It also renews during the native copy,
by stepwise backup with a CAS between steps. The worker captures
`runtime_shutdown.current_generation()` at start and polls `stop_checker` between
batches. Each run is capped by batch count and wall time. An interrupted job
resumes only through a new signed `run_live`, which mints a new token. A stale
task's writes and failure receipts are refused.

### 4. Native reader

The database is opened read-only by URI through the checked descriptor and
copied with SQLite's backup API into a private `0700` directory, so committed
WAL frames are included (the legacy reader copies only the main file). The copy
is never opened with `immutable=1` on the source. It is queried with windowed
statements (`WHERE ROWID > ? ORDER BY ROWID LIMIT n`, joins restricted to the
window) under the snapshot reader's authorizer and progress budget.

A row is written only if it meets the snapshot reader's form rules: native
`is_from_me` literally 0 or 1; exactly one chat join; plain text with no
attachment, attributed body, subject, reaction, reply thread, deleted or system
form; a correspondent handle that is present and does not casefold to `self`; a
date in the fixed modern nanosecond unit, microsecond-representable and not in
the future. A failing row is **withheld, not rejected**: it is counted by reason,
never written by this lane, and the cursor moves past it. A withheld row may
still be written, unproven, by the legacy sync.

Before the live canary, measure withheld-reason counts, content-free, on a
realistic synthetic generator. If the reused time rule withholds most rows, the
lane gets its own reader contract id and documents truncation to microseconds.
The snapshot contract stays as it is.

**A replaced database.** At claim, on the backup copy, the lane fails closed with
`native_locator_changed` when `sqlite_sequence` for `message` is below
`cursor_rowid`, or when the highest surviving linked ROWID's `message.guid` digest
differs from the stored one. Deleted rows are fine as long as some linked row
survives. If none survive, a new signed enrollment is required.

### 5. Canonical write and collisions

The trusted batch writer takes the origin version from the context's exact
type (`owner-attested-live-sync/v1`), not from a new field. It includes
`event_time_json` only when the column exists. The snapshot context keeps
today's rules and `_record_identity` byte for byte.

**Collisions, live context only.** Any node that has run legacy iMessage sync
already holds rows at the same `imessage:<ROWID>` ids, so aborting the batch
would make the lane unusable. In the check pass, before any write:

- withhold per row, count it, and advance the cursor when the stored row is
  unlinked with matching identity (`legacy_row_present`), unlinked with a
  different dataset, source, conversation, `source_record_id` or owner
  (`canonical_identity_collision`), or linked to another enrollment, a revoked
  enrollment or the snapshot lane (`row_owned_elsewhere`). A conversation
  parent with a different source withholds that conversation's rows;
- still abort the whole batch when this enrollment's own linked row changed
  identity or lost its row or parent, or when two copies of one message in the
  batch conflict.

The existing-record check reads both lanes' link tables, which are keyed by
message id alone, and never raises the way the snapshot lane's does.

**Cursor.** `enroll_live` records the native `max(ROWID)` from the same backup it
describes as `start_rowid`, and `cursor_rowid` starts there. History is out of
scope, and a re-enrollment never re-reads the lane's own earlier rows.
`describe_live` never returns it. Revocation is permanent for rows already
ingested: they keep the old enrollment's origin and link, so a later enrollment
only withholds them.

**Legacy sync.** While a live enrollment for the same locator is active, each
legacy iMessage read is capped at `ROWID <= cursor_rowid`, so the legacy
checkpoint trails the live lane rather than racing it. Rows the live lane
withheld sit below the cursor, so legacy sync still writes them, unproven. The
cap sits in `run_imessage_sync`, because requeued jobs never pass through enqueue.
A revoked enrollment removes the cap. This is a deliberate change to legacy
behaviour, and it applies only on nodes where the live tables exist. There is
at most one active enrollment per locator, and `dataset_id` is unique across both
lanes (live enroll reads `ingest_provenance_enrollments` read-only).

### 6. Facts: the snapshot lane's floor, unchanged

The lane reuses what step 4 built for the snapshot lane
(`ingest_snapshot_facts.py`): `extract.extract_rules_facts` inside the batch
transaction under `write_gate.joined_transaction`; the subject is the unique
`is_self` entity the owner actively attests, or nothing is extracted
(`owner_subject_unattested`); non-atomic labels are refused; the evidence time is
the linked row's native clock; and the store gets a trust that validates links.
It never calls `extract_facts_from_batch`, which starts the LLM pass whenever
`facts_llm_enabled` is true, and never creates a self entity from a background
worker.

The shared `extract._source_ref` and every loader stay unchanged. Emitting
`dataset_id` from them would let a forged Signal upload, a reprocess or a
backfill write complete references to rows no lane proved. Making
legacy-produced references complete is its own evidence decision, which must
say how it keeps p2b-v1 to v4 behaviour.

Each batch is all or nothing, extraction included: rows, links, the batch
record, the cursor, facts and the job's running totals (`messages_created`,
withheld counts by reason, `facts_written`, `last_rowid`) commit in one
transaction. An extraction error rolls all of it back, and the job fails through
the token-checked receipt with a reason code. A resumed `run_live` re-reads the
same rows, so no batch can commit unextracted. The job becomes `done` in its own
token-checked transaction once the read reaches the end, never inside a batch.

Known availability limit (shared with the snapshot lane): a later legacy
extraction over the lane's rows merges a dataset-less reference into the lane
fact, which then withholds as `lineage_identity_incomplete`. Each lab run also
supersedes the previous run's single-valued `works_at`, so canary text uses a
per-run employer label.

### 7. Evidence

A live row qualifies when its enrollment is active at the same revision, its
link matches `row_identity` and `native_guid_digest`, and the link's batch record
exists (a link is only ever written in a committed batch whose claim was
current). Job status is not consulted, so the rows of a job that later fails
still qualify. Evidence constructs the live service directly, as it does the
snapshot service, without requiring the live flag, so an enrolled node whose
flag is later turned off withholds rather than skips. When the store is closed,
a row carrying a live hint, or a hint-less row in an enrolled
`(source_id, dataset_id)` pair listed in the append-only marker, withholds.
Otherwise a stripped hint on a linked row would get through. Rows outside
enrolled pairs keep today's checks, so a snapshot-lane release and the lab's
hint-less fixture release still qualify while the live store is pending.
Attested live rows bind `event_time_json` into a v2 row identity, and a
`native_source_clock` label on any row without a validated origin is treated as
`unverified_producer`.

### 8. Protocol, control plane and lab

- Live operation literals, with a discriminated union of live result models
  (not `SnapshotJobMetadata`) and ack target checks: `enroll_live` echoes
  `dataset_id` and `locator_digest`, `revoke_live` requires `state == 'revoked'`,
  and the others echo `enrollment_id` or `job_id`. Both schema fixtures, engine
  and control plane, are regenerated together while golden-v1 still verifies, and
  the snapshot ack bytes are pinned by a golden test.
- The ack carries only status and coarse buckets. Exact per-reason counts and
  timing are node-local, readable over UDS, because they reveal messaging volume.
- The control plane gets explicit error-code mappings and `CampaignDiagnostics`
  paths for the new codes.
- Lab: `lab.py` allow-lists and a paired-flag check pinning the locator to
  `/root/.topos/permissions-v2/ingest-live/chat.db`; a `configure_ingest_live.py`
  that provisions a synthetic database under engine state; and
  `recover_durable_identity.py` archive and verify extended to the live store and
  its marker.

### 9. Authorship separation outside the lane (built in step 4)

These let a correspondent, a declaration or a model become the owner's authored
speech. Each was fixed in step 4 with a test that fails on the old code:

- the Signal export parser matched self numbers by substring with a precedence
  bug; it now requires exact equality of normalized numbers;
- a legacy iMessage correspondent handle spelled `Self` became
  `sender_id='self'`; the reader now namespaces such a handle (`handle:<id>`),
  and the sync stores native `is_from_me` instead of re-deriving the owner from
  the sender id;
- evidence's quoted-metadata denylist now covers the Signal reader's quote keys,
  `storyReplyContext`, and a non-zero iMessage reaction (`associated_message_*`;
  every ordinary iMessage row carries type 0). This only restricts, and it can
  withhold a fact under an existing grant;
- both extractors skip a row carrying iMessage reaction metadata, whose text
  quotes the message it reacts to (`features/facts/reactions.py`);
- `llm_extract._infer_table` adopts the rule extractor's condition, so an
  unstamped messenger row whose `human` sender is a correspondent is no longer
  read as the owner's AI-chat turn;
- declared field maps may not declare `is_from_self`, `from_self`, `role`,
  `actor_role`, `owner_user_id`, `sender_type`, `_table` or `canonical_table` on
  any table: the mapper drops them with a receipt and rewrites a declared `self`
  sender id, install and PATCH refuse them (other install paths, and boot-time
  rehydration, rely on the mapper), and `ai_chat_messages` cannot be a declared
  target. Every row the demo and declared lanes emit is stamped with its table,
  so a declared column named like a journal or profile column cannot retype a
  row into one written by the owner;
- the legacy writer and the role gate count only a typed owner flag (`True` or
  the integer 1), never text such as `"1"`;
- `verdicts.edit_fact` and `surfaces.revise_fact` keep the corrected fact's
  attribution unless the owner changes it.

One rule was deliberately **not** tightened. The role gate still counts
`sender_id == 'self'` as the owner beside an explicit `is_from_self` of 0. Rows
written before that column existed, and every row the conversations lane
re-stages from raw (`canonical_pipeline.build_staging_record` drops the flag),
store the owner's own messages as `(0, 'self')`, so letting the 0 decide would
silently demote real owner messages the moment it shipped, and no repair exists
for rows already stored. The paths by which a correspondent could be stored as
`self` are closed where rows are written instead (the reader and the mapper
above), and permission evidence requires `is_from_self == 1` regardless. What
stays open: a demo-file or runtime-parser row whose correspondent is literally
spelled `self`. Carrying a typed flag through `build_staging_record` for
bundled parsers, then a stated repair of stored `(0, 'self')` rows, is what
would let the gate trust the flag alone; that is a separate change.

Declared `journal_entries` and `profile_records` rows still count as owner-written
by construction, so authorship there is only the registerer's say-so. Neither is
an evidence leaf, so neither can release under p2b.

## Done means (for the live step)

1. **Authority.** Live provenance exists only through a signed owner command
   over UDS or the owner-stamped relay. A legacy-key call, an unstamped or
   third-party relay call, a tampered payload, a replayed command, an old or
   foreign job and a disabled flag are each refused through the real doors, and
   burn nothing. With the flag on and no locator, and separately with
   `IMESSAGE_CHAT_DB` set, `describe_live` and `run_live` refuse without opening
   any file (under patched `sqlite3.connect` and `open`).
2. **Durability.** Enrollment, jobs, batches, cursor and links are durable,
   claim-fenced and rollback-detected. A restart, SIGKILL between pending and active
   marker writes, a commit exception, ENOSPC, revocation, owner change, disabled or
   removed source, a changed or swapped database and a stale worker each land on
   the right side or fail closed. A store with 100k records, 10k jobs and 20k
   commands keeps committing, `_check` and per-leaf evidence stay within a budget
   that does not grow with size, and a signed `revoke_live` succeeds. In-place
   tampering with a job status, a command id or a link is detected.
3. **Snapshot lane untouched.** Its golden pins pass, the lab's existing snapshot
   job still reports done through the control plane, and live commands leave its
   marker, digest and source-clock generation unchanged.
4. **Canary, hermetic.** A WAL-mode synthetic `chat.db` with uncommitted-checkpoint
   frames and more than 1,000 messages, plus one each of thread reply,
   attributedBody-only, sub-microsecond, future and `Self`-handle rows after the
   cursor. Through the real signed doors: identity attestation, enroll, enqueue,
   run; withheld rows counted and never linked; exactly one owner `works_at` fact
   with complete references and no SQL edits; evidence review; projection review
   through the owner handler; a signed p2b-v4 grant; the exact recipient scalar.
   It runs three sequences: legacy sync under another dataset before `run_live`
   (completes with withheld counts, not stuck); legacy sync under the enrolled
   dataset; and revoke, re-enroll under a new dataset, then run (proves only new
   rows). Negative controls: the correspondent's identical pattern yields no
   releasable fact; revocation withholds; a legacy sync over the same ROWIDs cannot
   rewrite proved rows, and qualification and the scalar are unchanged afterwards;
   a crash between rows and commit leaves the fact exactly once and the cursor
   unskipped; an extractor error in batch 2 leaves batch 1 qualifying.
5. **Canary, live.** The same path runs against the lab through the control
   plane, labelled synthetic, with ROWIDs derived from a per-run suffix and appended
   after enrollment. Cleanup withdraws the grant, projection and evidence reviews,
   the identity attestation and the enrollment. What intentionally persists is
   listed and tolerated by every other lane's preflight: the store, marker, linked
   rows, one active `works_at` fact and consumed commands. The blast radius is
   recorded: a torn or pending live marker withholds conversation evidence in
   enrolled datasets.

## Explicitly not in this step

- Signal. No SQLCipher fixture exists and `pysqlcipher3` is not installed; the
  Signal reader also drops rows that share a timestamp across a batch boundary.
- Messages whose text exists only in `attributedBody`: withheld and counted.
- The owner's real `~/Library/Messages` database.
- Historical repair of any existing row.
- The snapshot store's receipt-driven source clock and its 1 MiB authority ceiling
  (recorded above).
- The AI-chat `sender_type` evidence decision.
