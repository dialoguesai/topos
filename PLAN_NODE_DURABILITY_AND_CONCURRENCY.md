# Node durability & concurrency

**Status:** M0–M3 IMPLEMENTED 2026-07-30/31 (uncommitted; needs `just run` or a
tool reinstall — the running node is a frozen snapshot). See "Status" below.
**Trigger:** the 88-record `github_activity` ingest of 2026-07-30 17:31 lost its
`facts` output and took the control-plane websocket down with it.

## What actually happened

```
17:33:05.684 ERROR job facts failed: cannot commit - no transaction is active
17:33:07.695 DEBUG complete source_id=github_activity jobs_run=8 deferred=[]
17:34:17.544 WARN  connection_failed ... keepalive ping timeout ... attempt=198
15:43:59.741 WARN  graph refresh failed: cannot start a transaction within a transaction
15:42:16.752 DEBUG journal tuning skipped: Safety level may not be changed inside a transaction
```

Three defects, one root.

### D1 — One SQLite connection shared across threads

`core/state.py:280` creates a single process-wide connection with
`check_same_thread=False`, and `storage/adapters/factory.py:128` deliberately
hands that same object back out. `check_same_thread=False` silences Python's
guard; it does not make the connection safe. A `sqlite3.Connection` holds
exactly ONE transaction state, so concurrent users corrupt each other:

* thread B issues `BEGIN` inside thread A's transaction → *cannot start a
  transaction within a transaction*
* thread B commits out from under thread A → *cannot commit - no transaction is
  active*
* `PRAGMA` runs mid-transaction → *Safety level may not be changed*

The concurrency is structural, not incidental: `graph_refresh` runs on its own
`threading.Timer` thread (`features/entities/graph_refresh.py:91`), entirely
outside the pipeline's scheduling, on the same connection.

### D2 — A failed derivation job is silently discarded

`enrichment/orchestrator.py:366` catches the exception, appends to
`results["errors"]`, and continues. Nothing durable is written. The completion
log prints `jobs_run` and `deferred` but NOT `errors`, so the batch reads as
clean. The facts for those 88 commits are simply gone.

This is the priority defect: **anything meant to reach the database must reach
it, or be durably marked for retry.**

### D3 — The event loop can block on a threading lock

`storage/db/write_gate.py` serializes writes with `threading.RLock`. It is a
blocking OS lock, not an async one, and 89 files touch `get_db_connection()`.
Any coroutine that takes the gate stalls the whole event loop, which is why the
control-plane keepalive ping times out (`attempt=198` — chronic, not a blip).
The routines page reads through CP → node websocket, so a stalled loop reads to
the user as "the node is locked".

Not in scope: the `/v1/signal/* → 401` lines are the react-app failing auth
against the node API. Unrelated to any of this.

## Milestones

Ordered by the stated priority: never lose data first, then stop corrupting
transactions, then stop blocking, then make it fast.

### M0 — Durability: a failed job is recorded and retryable

Existing infrastructure to reuse — `pipeline/job_store.py` already has a durable,
leased queue (`pipeline_jobs`: status, lease_owner, lease_expires_at,
idempotency_key, `idx_pipeline_jobs_recover`) plus `enqueue_job`, `fail_job`,
`recover_stale_jobs`, `record_derivation_completion`. The signal-derive path
simply does not use it.

* **M0.1** On job failure, persist a durable retry record keyed
  `{batch}:{job}:signal_derive` (idempotent), carrying source_id,
  sync_batch_id, job_name, record ids and the error.
* **M0.2** A batch with failures reports `status="degraded"`, and the completion
  log names the failed jobs. No more clean-looking broken batches.
* **M0.3** A recovery entry point re-runs failed derivations for a batch,
  driven off those durable records.
* **M0.4** Repair the batch that already lost data
  (`1bcab09c-6d51-4baf-92eb-dd5d5d44f498`, facts for 88 github records).

Raw payloads are written *before* enrichment (`Stored raw payload` precedes
every derive), so the source data survives — only the derived layer was lost,
and it is recoverable by re-running the job.

### M1 — One connection per thread (root fix for D1)

* **M1.1** `get_db_connection()` returns a thread-local connection. The
  signature does not change, so all 457 call sites stay as they are — this is
  what makes the change tractable.
* **M1.2** Preserve the `:memory:` and explicit-path behaviour tests rely on.
* **M1.3** A regression test that reproduces the concurrent-`BEGIN` race and
  fails on the current code.

Risk: code that captures a connection in one thread and uses it in another
still shares. Audit `conn=` parameters that cross a `to_thread` boundary.

### M2 — The event loop never blocks on SQLite (D3)

* **M2.1** Instrument the write gate: if acquired on the event-loop thread,
  log loudly (behind a setting, so it can be made fatal in tests).
* **M2.2** Offload the remaining on-loop DB paths found by that instrument.

### M3 — Scheduling and efficiency

* **M3.1** `graph_refresh` becomes a queued `pipeline_jobs` entry instead of a
  free-running `threading.Timer`, so it runs after the batch it depends on
  rather than inside it.
* **M3.2** Hoist per-record DDL: the run created `DerivedTablesManager#121`
  through `#196` — 75 managers, each issuing `CREATE TABLE IF NOT EXISTS`
  inside the ingest loop, each taking a lock.
* **M3.3** Debounce/chunk global recompute. 88 records triggered a full
  topic-cluster recompute over all 27 sources (44s) plus a 77s graph refresh.
  A connector backfill must not synchronously trigger a global rebuild.

## Verification

* M0: a fault-injection test proves a failing job leaves a durable retry record
  and a `degraded` batch; recovery re-runs it and the records land.
* M1: the D1 regression test passes; full engine suite green.
* M2: instrument reports zero loop-thread gate acquisitions on a full ingest.
* M3: re-run an 88-record ingest and confirm the CP websocket survives it.

## Status — 2026-07-30

Implemented, uncommitted. **The node must be restarted to pick any of this up.**

| Item | State | Where |
|---|---|---|
| M0.1 durable failure record | done | `enrichment/derivation_recovery.py` |
| M0.2 degraded batch + honest log | done | `enrichment/orchestrator.py` |
| M0.3 recovery entry point + API | done | `derivation_recovery.retry_pending_derivations`, `GET/POST /v1/signal/derivation-debt` |
| M0.4 repair the 2026-07-30 loss | **blocked on node restart** | see below |
| M1 connection per thread | done | `core/state.py` |
| M2.1 write-gate loop/slow-hold warnings | done | `storage/db/write_gate.py` |
| M2.1b instrument `commit_connection`/`batched_writes` (the uninstrumented gate paths) + flag ungated write transactions (WAL lock-order inversion) | done 2026-08-08 | `storage/db/write_gate.py` |
| M2.2 rebuild endpoint off the loop | done 2026-08-08 | `api/signal.py` |
| M2.3 rebuild holds the gate only for write phases; edge fold runs in memory, swap is one DELETE + batched INSERT (smoke: worst concurrent-writer wait 0.115s vs full-rebuild wait before) | done 2026-08-08 | `features/entities/maintenance.py`, `features/entities/edges.py`, `features/entities/graph_refresh.py` |
| M3.1 graph refresh defers to batches | done | `enrichment/pipeline_activity.py`, `features/entities/graph_refresh.py` |
| M3.2 DDL once per connection | done | `enrichment/derived_tables.py` |
| M3.3 defer global recompute | done | `enrichment/jobs/canonical/topic_clusters_job.py`, `pipeline/job_runner.py` |

Tests: `tests/core/test_db_connection_threading.py` (12), including one that
reproduces the original transaction corruption on a shared connection.

Suite: 1,457 passing across core/enrichment/pipeline/api/ingestion/features/
query/topos. Seven failures were confirmed pre-existing by stashing this work
and re-running them against a clean tree — `test_enrichment_orchestrator`
(job-count drift, asserts 8 == 9), `test_manual_enrichment_trigger_flow`,
`test_start_ingestion_handler`, three `test_live_engine_pressure` cases, and
`test_en_qq_eval_queries` (missing `/tmp/sample.jsonl`).

### M0.4 — what was actually lost

Measured against the live database, not inferred:

* the 17:31 batch derived 88 records
* `signal_facts` holds 27 rows for `github_activity` stamped `2026-07-30 22:33`
  UTC — the same minute the facts job reported failure

So the job **wrote 27 facts and then failed on commit**: a partial, non-atomic
write, which is worse than a clean failure because nothing marks it incomplete.
Raw payloads survive (`activity_events` holds 199 github rows), so this is
recoverable by re-running the job.

Recovery, once the node is restarted onto this code:

```
POST /v1/signal/derivation-debt/retry?dry_run=true    # inspect first
POST /v1/signal/derivation-debt/retry
```

The pre-restart loss has no durable debt record — that mechanism did not exist
when it happened — so this specific batch needs a manual re-derive of
`facts` for `github_activity`. Every future failure records itself.

### Deployment — the running node is a frozen snapshot

`~/.local/bin/topos-node` resolves to a `uv tool install` copy under
`~/.local/share/uv/tools/topos-node/`, NOT the working tree. The justfile says
so explicitly ("that one is a frozen snapshot"). Restarting that binary will
therefore pick up nothing here.

Measured drift, installed copy vs working tree, `__pycache__` excluded:
13 differing source files and 3 new ones — this work plus the co-resident
model-packs changes. Everything committed is already in the snapshot.

To run this code:

```
just run          # working tree via uv run (recommended)
```

or reinstall the snapshot, then restart:

```
uv tool install --force /Users/dialogues/developer/topos-control-plane/topos
```

The live node (PID 21290 at time of writing) runs in a foreground terminal, so
the restart has to happen there.

### M3.3 — deferred global recompute

Consolidation no longer runs inline. `topic_clusters_job._defer_consolidation`
queues `kind="topic_consolidation"` on `pipeline_jobs` under a fixed
idempotency key, so any number of batches coalesce to one pending recompute,
and `job_runner._execute_topic_consolidation` runs it — re-queueing itself if a
derivation is still in flight. `TOPOS_DEFER_TOPIC_CONSOLIDATION=off` restores
the inline behaviour, and an unavailable queue falls back to inline rather than
skipping consolidation entirely.
