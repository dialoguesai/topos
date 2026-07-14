# Timeline Projection and Deferred-Work Audit

Date: 2026-07-13  
Database audited: local owner node (`~/.topos/database.db`)  
Scope: canonical-to-timeline parity and restart safety of deferred engine work

## Outcome

The timeline projection gap is repaired and the live ingest path is restart-safe for
timeline rows.

- Browser repair dry-run found 1,817 canonical browser visits and 1,340 timeline
  rows: 477 missing.
- The browser-only repair inserted all 477 missing rows. A second run reported zero
  writes.
- The cross-source audit then found another 4,557 projection defects:
  - 4,507 missing iMessage `conversation_messages`.
  - 2 missing ChatGPT UI `ai_chat_messages`.
  - 4 missing `journal_entries`.
  - 23 missing `location_events`.
  - 21 location rows stored with the correct ID/time but the wrong
    `canonical_table` (`journal_entries` instead of `location_events`).
- All 4,557 were repaired with backup-first, missing-only/idempotent operations.
- Final parity: 6,933 eligible canonical records, 6,933 matching timeline
  projections, zero missing IDs, zero identity mismatches, and zero semantic
  timestamp mismatches.
- A browser visit arriving after restart was projected synchronously within one
  second. Browser parity at that check was 1,829 canonical rows and 1,829 timeline
  rows.

Backups and machine-readable reports were written under `~/.topos/` before each
live repair.

## Remaining data hygiene findings

Thirteen timeline rows have no matching canonical record:

- 11 `journal_entries` rows from `time_log`.
- 1 `ai_chat_messages` row from `chatgpt_ui_conversation`.
- 1 `activity_events` row from `browser_events`.

The sampled IDs look like test/reprocessing artifacts (`tl-job-time-log-1`,
`mapped-msg-1`, and an `example.com` browser event). They were not deleted because
the repair command is intentionally non-destructive. These rows should be reviewed
and removed through the existing scrub/lifecycle path if confirmed synthetic.

Historical journal timestamps used both naive UTC and explicit `+00:00` formats.
The audit now compares parsed timestamps, so formatting-only differences are not
reported as drift.

## Root cause and code correction

`app_ingest` acknowledged canonical writes before launching signal derivation with
an in-memory `asyncio.create_task`. Timeline projection was the last signal job, so
a restart or cancelled task permanently left canonical rows without timeline rows.

Corrections:

- Timeline projection is now a synchronous, lightweight consequence of
  canonicalization, before an ingest acknowledgement is returned.
- Local iMessage/Signal sync projects conversation rows before advancing through
  optional enrichment.
- `TimelineJob`, live ingestion, nightly backfill, and the repair command share one
  exclusion-aware, metadata-preserving projector.
- Canonical ID/timestamp support now includes journal `entry_id`, financial
  `transaction_id`, `posted_at`, and profile `start_date`.
- Missing-only repair detects and safely corrects a conflicting table/source
  identity without replacing richer timeline metadata.

## Recurrence audit

### Critical: uploaded file ingestion is non-durable

`topos/core/handlers/ingest.py` acknowledges `start_ingestion` and then runs the
entire file ingestion inside an untracked `asyncio.create_task`. A process restart
can lose the ingestion before canonical data is durable. Progress callbacks do not
provide local recovery.

Required follow-up:

- Persist the job request and input reference before acknowledgement.
- Execute through a durable worker/lease with retry and restart recovery.
- Persist terminal status and make the job idempotent by source record ID.
- Add a restart test that kills the worker after acknowledgement and proves the job
  resumes.

### High: app-ingest heavy derived intelligence remains non-durable

Timeline is now safe, but embeddings, topic clusters, entities, relationships,
statistics, facts, and URL classification still run in deferred in-memory tasks.
Canonical data remains intact, but a restart can leave uneven intelligence
coverage. `write_id` delivery dedupe is recorded before that deferred work
completes, so redelivery suppresses both re-ingestion and recovery of the lost
derivation.

Required follow-up:

- Queue post-canonical enrichment durably by source and sync batch.
- Track ingest delivery and derivation completion separately; a successful ingest
  dedupe hit must still enqueue incomplete derivation.
- Persist per-job completion checkpoints.
- Make coverage APIs compare configured jobs with durable completion, not only
  in-memory progress.
- Add an idempotent source/batch replay command and restart tests.

### High: WebSocket manual enrichment omits the signal lane

The `enrichment_process_source` WebSocket handler invokes only
`EnrichmentOrchestrator.run_canonical()`. Unlike the shared HTTP/core processing
path, it does not run signal derivation, so timeline and the other baseline signal
jobs are omitted even when the background task completes.

Required follow-up:

- Route WebSocket and HTTP enrichment through the same
  `_process_enrichment_core(..., include_signal=True)` implementation.
- Add parity tests asserting identical timeline and signal outputs through both
  entry points.

### Medium: manual enrichment jobs are non-durable

`topos/core/handlers/enrichment.py` returns a processing job ID and launches
`_process_in_background()` with an untracked task. Restart loses in-memory progress
and the operation must be manually retried. Canonical data is not at risk.

Required follow-up:

- Store job state and requested job names in SQLite.
- Resume queued/running jobs on startup.
- Mark abandoned leases retryable instead of leaving indefinite processing state.

### Medium: timeline coverage tooling was incomplete

The enrichment catalog declares timeline output, but timeline is absent from
`_COVERAGE_TABLES`. Generic `only_missing` backfills therefore cannot select
canonical records missing timeline rows, and coverage can report a configured job
without detecting the projection gap.

Required follow-up:

- Add a timeline-specific coverage anti-join keyed by canonical record identity.
- Use the same parity implementation as `repair_timeline.py` rather than treating
  timeline as a conventional one-output-table enrichment.
- Test coverage and `only_missing` backfill against an intentionally deleted
  timeline row.

### Medium: debounced graph refresh is also non-durable

Entity graph refresh uses an in-memory daemon timer. Restart during the debounce
window can leave materialized graph layers stale after canonical/enrichment writes.
This does not affect timeline parity, but it has the same lost-deferred-work shape.

Required follow-up:

- Persist a graph-dirty generation marker.
- Rebuild on startup whenever the materialized generation trails the canonical
  generation.
- Add a restart-during-debounce test.

### No equivalent data-loss risk

Control-plane inbound tasks are retained in `_inbound_tasks`, and long-running
connection/presence tasks are retained on application/client state. They reconnect
after restart and do not represent canonical-to-derived projection work.

The sync relay/oplog path is a separate projection boundary and does not call the
post-canonical signal pipeline in the audited code. Its projection manager requires
a dedicated parity test to prove that sync-originated canonical writes receive the
same mandatory timeline projection.

## Regression coverage

Tests now cover:

- Deferred browser ingest writes timeline before background work.
- Handler acknowledgement remains timeline-safe when the background task is
  immediately cancelled, simulating restart.
- Every canonical family routes IDs and timestamps correctly.
- Local sync projects timeline without requiring enrichment configuration.
- Exclusions, invalid rows, metadata preservation, dry-run, source/date filtering,
  idempotency, timestamp migration, and projection-identity repair.
- Orphan reporting and final residual-gap verification.

## Operational verification

The repair procedure was:

1. Stop the engine.
2. Run a read-only dry-run.
3. Create a SQLite backup using the backup API.
4. Apply only missing/identity-repair operations in one transaction.
5. Rerun the audit and require zero residual writes.
6. Restart the engine and verify `/healthcheck`.
7. Confirm a newly arriving browser visit appears immediately in both
   `activity_events` and `timeline`.

The final dry-run reports zero writes required across all canonical tables.
